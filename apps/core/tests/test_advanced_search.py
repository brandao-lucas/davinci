"""
Testes da feature Pesquisa Avançada Premium com MeSH.

Cobre:
1. Paridade preview↔ingestão — build_pubmed_query é a única fonte de verdade
2. Query-builder — fallback legado, modo AND/OR, major_only, qualifiers, sanitização
3. Validação de year_buckets (PanelFlagsSerializer)
4. Isolamento cross-user (404 no projeto alheio)
5. Read-only do search_preview (sem IngestionJob/Paper criados)

rust_engine é mockado em todas as chamadas de endpoint.
"""

import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from apps.core.models import DaVinciProject, IngestionJob, Paper
from apps.core.services.query_builder import (
    _escape_free_text_term,
    _escape_mesh_term,
    build_free_text_query,
    build_pubmed_query,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_project(user, *, title='Test', query_term='cancer', synonyms=None,
                  advanced=False, mesh=None, mesh_mode='and'):
    """Cria DaVinciProject com campos avançados opcionais."""
    return DaVinciProject.objects.create(
        user=user,
        title=title,
        slug=f'{title.lower()}-{user.username}-test',
        query_term=query_term,
        query_synonyms=synonyms or [],
        advanced_search_enabled=advanced,
        selected_mesh=mesh or [],
        mesh_default_mode=mesh_mode,
    )


def _mesh_entry(descriptor, *, ui='D000000', qualifiers=None, mode='and', major_only=False):
    """Atalho para construir um item de selected_mesh."""
    return {
        'descriptor': descriptor,
        'ui': ui,
        'qualifiers': qualifiers or [],
        'mode': mode,
        'major_only': major_only,
    }


def _fake_magnitude_preview(**kwargs):
    """Retorna um MagnitudePreview fake compatível com o que o Rust devolveria."""
    defaults = dict(
        free_text_count=100,
        mesh_count=80,
        combined_count=120,
        overlap=60,
        only_free_text=40,
        only_mesh=20,
        not_yet_indexed=5,
        reviews=10,
        systematic_reviews=3,
        by_year=[],
        by_pub_type=[],
        open_access=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _fake_mesh_suggestion(descriptor='Diabetes Mellitus', ui='D003920'):
    return SimpleNamespace(
        descriptor=descriptor,
        ui=ui,
        tree_numbers=['C18.452.394.750'],
        scope_note='A heterogeneous group of disorders...',
        allowable_qualifiers=['diagnosis', 'therapy'],
        pubmed_count=95000,
    )


# ─── Grupo 1: Query-builder — testes unitários puros ─────────────────────────

class TestQueryBuilderLegacyFallback(APITestCase):
    """Sem advanced_search_enabled → OR simples dos termos livres."""

    def setUp(self):
        self.user = User.objects.create_user(username='u_legacy', password='pw')

    def test_sem_sinonimos(self):
        p = _make_project(self.user, query_term='cancer', advanced=False)
        q = build_pubmed_query(p)
        self.assertEqual(q, 'cancer')

    def test_com_sinonimos(self):
        p = _make_project(self.user, query_term='cancer', synonyms=['neoplasm', 'tumor'],
                          advanced=False)
        q = build_pubmed_query(p)
        # OR simples: cancer OR neoplasm OR tumor
        self.assertIn('cancer', q)
        self.assertIn('neoplasm', q)
        self.assertIn('tumor', q)
        self.assertIn(' OR ', q)
        # Nenhuma tag de campo MeSH deve aparecer
        self.assertNotIn('[mh]', q)
        self.assertNotIn('[majr]', q)

    def test_advanced_enabled_mas_sem_mesh(self):
        """advanced_search_enabled=True mas selected_mesh=[] → fallback legado."""
        p = _make_project(self.user, query_term='cancer', advanced=True, mesh=[])
        q = build_pubmed_query(p)
        self.assertIn('cancer', q)
        self.assertNotIn('[mh]', q)
        self.assertNotIn('[majr]', q)


class TestQueryBuilderMeshAnd(APITestCase):
    """Modo AND (precisão): free_text AND bloco1 AND bloco2."""

    def setUp(self):
        self.user = User.objects.create_user(username='u_and', password='pw')

    def test_bloco_and_sem_qualifier(self):
        p = _make_project(
            self.user,
            query_term='cancer',
            advanced=True,
            mesh=[_mesh_entry('Diabetes Mellitus', mode='and', major_only=False)],
        )
        q = build_pubmed_query(p)
        self.assertIn('"Diabetes Mellitus"[mh]', q)
        self.assertIn(' AND ', q)

    def test_bloco_and_major_only(self):
        p = _make_project(
            self.user,
            query_term='cancer',
            advanced=True,
            mesh=[_mesh_entry('Diabetes Mellitus', mode='and', major_only=True)],
        )
        q = build_pubmed_query(p)
        self.assertIn('"Diabetes Mellitus"[majr]', q)
        self.assertNotIn('[mh]', q)

    def test_dois_blocos_and(self):
        p = _make_project(
            self.user,
            query_term='cancer',
            advanced=True,
            mesh=[
                _mesh_entry('Diabetes Mellitus', mode='and'),
                _mesh_entry('Insulin', mode='and'),
            ],
        )
        q = build_pubmed_query(p)
        self.assertIn('"Diabetes Mellitus"[mh]', q)
        self.assertIn('"Insulin"[mh]', q)
        # Ambos ligados por AND
        self.assertEqual(q.count(' AND '), 2)


class TestQueryBuilderMeshOr(APITestCase):
    """Modo OR (recall): free_text OR bloco."""

    def setUp(self):
        self.user = User.objects.create_user(username='u_or', password='pw')

    def test_bloco_or_sem_qualifier(self):
        p = _make_project(
            self.user,
            query_term='cancer',
            advanced=True,
            mesh=[_mesh_entry('Diabetes Mellitus', mode='or')],
        )
        q = build_pubmed_query(p)
        self.assertIn('"Diabetes Mellitus"[mh]', q)
        # OR conectando blocos
        self.assertIn(' OR ', q)

    def test_bloco_or_major_only(self):
        p = _make_project(
            self.user,
            query_term='cancer',
            advanced=True,
            mesh=[_mesh_entry('Insulin Resistance', mode='or', major_only=True)],
        )
        q = build_pubmed_query(p)
        self.assertIn('"Insulin Resistance"[majr]', q)


class TestQueryBuilderQualifiers(APITestCase):
    """Qualifier → Descriptor/Qualifier[tag] como OR dentro do bloco."""

    def setUp(self):
        self.user = User.objects.create_user(username='u_qual', password='pw')

    def test_com_um_qualifier(self):
        p = _make_project(
            self.user,
            query_term='diabetes',
            advanced=True,
            mesh=[_mesh_entry('Diabetes Mellitus', qualifiers=['diagnosis'], mode='and')],
        )
        q = build_pubmed_query(p)
        self.assertIn('"Diabetes Mellitus/diagnosis"[mh]', q)
        # Inclui também o termo base sem qualifier
        self.assertIn('"Diabetes Mellitus"[mh]', q)

    def test_com_multiplos_qualifiers(self):
        p = _make_project(
            self.user,
            query_term='diabetes',
            advanced=True,
            mesh=[_mesh_entry('Diabetes Mellitus',
                              qualifiers=['diagnosis', 'therapy', 'drug therapy'],
                              mode='and')],
        )
        q = build_pubmed_query(p)
        self.assertIn('"Diabetes Mellitus/diagnosis"[mh]', q)
        self.assertIn('"Diabetes Mellitus/therapy"[mh]', q)
        self.assertIn('"Diabetes Mellitus/drug therapy"[mh]', q)

    def test_qualifier_major_only(self):
        p = _make_project(
            self.user,
            query_term='diabetes',
            advanced=True,
            mesh=[_mesh_entry('Diabetes Mellitus', qualifiers=['diagnosis'],
                              mode='and', major_only=True)],
        )
        q = build_pubmed_query(p)
        self.assertIn('"Diabetes Mellitus/diagnosis"[majr]', q)
        self.assertNotIn('[mh]', q)

    def test_qualifier_composto_apenas_de_caracteres_especiais_ignorado(self):
        """
        Qualifier composto EXCLUSIVAMENTE de caracteres especiais (colchetes, aspas,
        parênteses) fica vazio após sanitização → usa apenas o base.
        '[()]"' → '' → qualifier ignorado.
        """
        p = _make_project(
            self.user,
            query_term='cancer',
            advanced=True,
            mesh=[_mesh_entry('Diabetes Mellitus', qualifiers=['[()]"'], mode='and')],
        )
        q = build_pubmed_query(p)
        # Qualifier ficou vazio → nenhum /qualifier no resultado
        self.assertNotIn('Mellitus/', q)
        # Mas o descritor base ainda está presente
        self.assertIn('"Diabetes Mellitus"[mh]', q)


class TestQueryBuilderSanitizacaoMesh(APITestCase):
    """Termos MeSH com caracteres especiais são escapados."""

    def setUp(self):
        self.user = User.objects.create_user(username='u_san', password='pw')

    def test_aspas_duplas_removidas(self):
        safe = _escape_mesh_term('Diabetes "Mellitus"')
        self.assertNotIn('"', safe)
        self.assertIn('Diabetes Mellitus', safe)

    def test_colchetes_removidos(self):
        safe = _escape_mesh_term('Cancer[uid] OR evil')
        self.assertNotIn('[', safe)
        self.assertNotIn(']', safe)

    def test_parenteses_removidos(self):
        safe = _escape_mesh_term('(Diabetes) OR (evil)')
        self.assertNotIn('(', safe)
        self.assertNotIn(')', safe)

    def test_injecao_tag_uid_bloqueada(self):
        """Atacante tenta injetar [uid] para recuperar IDs arbitrários."""
        malicious = 'Diabetes"[uid] OR "evil'
        safe = _escape_mesh_term(malicious)
        self.assertNotIn('[uid]', safe)
        self.assertNotIn('"', safe)

    def test_injecao_tag_ti_bloqueada(self):
        """Atacante tenta injetar [ti] para buscar por título."""
        malicious = 'Diabetes"[ti] OR "evil'
        safe = _escape_mesh_term(malicious)
        self.assertNotIn('[ti]', safe)

    def test_truncamento_255_chars(self):
        longo = 'A' * 300
        safe = _escape_mesh_term(longo)
        self.assertLessEqual(len(safe), 255)

    def test_descriptor_muito_longo_ignorado_na_query(self):
        """Descriptor com >255 chars é ignorado pelo build_mesh_block → fallback legado."""
        longo = 'X' * 260
        p = _make_project(
            self.user,
            query_term='cancer',
            advanced=True,
            mesh=[_mesh_entry(longo, mode='and')],
        )
        q = build_pubmed_query(p)
        # Nenhum bloco MeSH → fallback para query simples
        self.assertNotIn('[mh]', q)
        self.assertNotIn('[majr]', q)
        self.assertIn('cancer', q)

    def test_descriptor_vazio_ignorado(self):
        p = _make_project(
            self.user,
            query_term='cancer',
            advanced=True,
            mesh=[_mesh_entry('', mode='and')],
        )
        q = build_pubmed_query(p)
        self.assertNotIn('[mh]', q)


class TestQueryBuilderSanitizacaoFreeText(APITestCase):
    """query_term e query_synonyms com injeção são escapados."""

    def setUp(self):
        self.user = User.objects.create_user(username='u_ft', password='pw')

    def test_aspas_duplas_em_query_term_removidas(self):
        safe = _escape_free_text_term('cancer"[uid] OR evil')
        self.assertNotIn('"', safe)
        self.assertNotIn('[uid]', safe)

    def test_colchetes_em_query_term_removidos(self):
        safe = _escape_free_text_term('cancer[ti] AND bad')
        self.assertNotIn('[', safe)
        self.assertNotIn(']', safe)

    def test_parenteses_em_sinonimo_removidos(self):
        safe = _escape_free_text_term('(evil OR good)')
        self.assertNotIn('(', safe)
        self.assertNotIn(')', safe)

    def test_truncamento_500_chars(self):
        longo = 'B' * 600
        safe = _escape_free_text_term(longo)
        self.assertLessEqual(len(safe), 500)

    def test_query_term_malicioso_na_query_final_legado(self):
        """query_term malicioso é sanitizado antes de entrar na query final legado."""
        p = _make_project(
            self.user,
            query_term='cancer"[uid] OR evil',
            advanced=False,
        )
        q = build_pubmed_query(p)
        self.assertNotIn('"', q)
        self.assertNotIn('[uid]', q)
        # O termo 'cancer' ainda está presente
        self.assertIn('cancer', q)

    def test_sinonimo_malicioso_sanitizado(self):
        p = _make_project(
            self.user,
            query_term='cancer',
            synonyms=['(evil"[ti]']  ,
            advanced=False,
        )
        q = build_pubmed_query(p)
        self.assertNotIn('"', q)
        self.assertNotIn('[ti]', q)


# ─── Grupo 2: Paridade preview↔ingestão ──────────────────────────────────────

class TestParidadePreviewIngestao(APITestCase):
    """
    Princípio inegociável: build_pubmed_query(project) produz EXATAMENTE a mesma
    string que o preview e o dispatch_pubmed_search usam.

    Verificamos isso inspecionando o IngestionJob.parameters['query'] criado
    pelo SearchService e comparando com o resultado direto do builder.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='u_par', password='pw')

    @patch('apps.core.tasks.ingestion_tasks.run_pubmed_ingestion')
    def test_paridade_legado(self, mock_task):
        """Sem MeSH: query do job == build_pubmed_query."""
        mock_task.delay = MagicMock()
        p = _make_project(self.user, query_term='diabetes', synonyms=['DM2'], advanced=False)

        expected_query = build_pubmed_query(p)

        from apps.core.services.search_service import SearchService
        job = SearchService.dispatch_pubmed_search(p, user=self.user)

        self.assertEqual(job.parameters['query'], expected_query)

    @patch('apps.core.tasks.ingestion_tasks.run_pubmed_ingestion')
    def test_paridade_com_mesh_and(self, mock_task):
        """Com MeSH AND: query do job == build_pubmed_query."""
        mock_task.delay = MagicMock()
        mesh = [_mesh_entry('Diabetes Mellitus', mode='and', major_only=False)]
        p = _make_project(self.user, query_term='diabetes', advanced=True, mesh=mesh)

        expected_query = build_pubmed_query(p)

        from apps.core.services.search_service import SearchService
        job = SearchService.dispatch_pubmed_search(p, user=self.user)

        self.assertEqual(job.parameters['query'], expected_query)
        self.assertIn('"Diabetes Mellitus"[mh]', job.parameters['query'])

    @patch('apps.core.tasks.ingestion_tasks.run_pubmed_ingestion')
    def test_paridade_com_mesh_or(self, mock_task):
        """Com MeSH OR: query do job == build_pubmed_query."""
        mock_task.delay = MagicMock()
        mesh = [_mesh_entry('Insulin Resistance', mode='or', major_only=True)]
        p = _make_project(self.user, query_term='diabetes', advanced=True, mesh=mesh)

        expected_query = build_pubmed_query(p)

        from apps.core.services.search_service import SearchService
        job = SearchService.dispatch_pubmed_search(p, user=self.user)

        self.assertEqual(job.parameters['query'], expected_query)
        self.assertIn('"Insulin Resistance"[majr]', job.parameters['query'])

    @patch('rust_engine.pubmed_magnitude_preview')
    def test_paridade_preview_usa_mesmo_builder(self, mock_preview):
        """
        O endpoint search/preview aplica overrides e chama build_pubmed_query.
        O campo query_used na resposta deve ser idêntico ao que o builder produziria
        diretamente com os mesmos overrides aplicados no projeto.
        """
        mock_preview.return_value = _fake_magnitude_preview()

        client = APIClient()
        client.force_authenticate(user=self.user)
        project = _make_project(self.user, query_term='cancer', advanced=False)

        mesh_override = [_mesh_entry('Neoplasms', mode='and')]
        resp = client.post(
            f'/api/v1/projects/{project.id}/search/preview/',
            {
                'selected_mesh': mesh_override,
                'mesh_default_mode': 'and',
                'panel_flags': {},
            },
            format='json',
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        # Reconstruir o mesmo proxy que a view constrói.
        # Nota: variáveis de atributo de classe não fecham sobre variáveis locais
        # da função enclosing — usamos SimpleNamespace para evitar o NameError.
        proxy = SimpleNamespace(
            query_term=project.query_term,
            query_synonyms=project.query_synonyms,
            advanced_search_enabled=True,  # selected_mesh não-vazio → True
            selected_mesh=mesh_override,
            mesh_default_mode='and',
            date_from=project.date_from,
            date_to=project.date_to,
        )

        expected_query = build_pubmed_query(proxy)
        self.assertEqual(resp.data['query_used'], expected_query)


# ─── Grupo 3: Validação year_buckets (PanelFlagsSerializer) ──────────────────

class TestPanelFlagsYearBucketsValidacao(APITestCase):

    def setUp(self):
        self.user = User.objects.create_user(username='u_flags', password='pw')
        self.client.force_authenticate(user=self.user)
        self.project = _make_project(self.user, query_term='cancer')

    @patch('rust_engine.pubmed_magnitude_preview')
    def test_year_buckets_validos_aceitos(self, mock_preview):
        mock_preview.return_value = _fake_magnitude_preview()
        resp = self.client.post(
            f'/api/v1/projects/{self.project.id}/search/preview/',
            {'panel_flags': {'by_year': True, 'year_buckets': [2020, 2021, 2022]}},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_year_buckets_excede_20_elementos_rejeita_400(self):
        resp = self.client.post(
            f'/api/v1/projects/{self.project.id}/search/preview/',
            {'panel_flags': {'by_year': True, 'year_buckets': list(range(2000, 2022))}},
            format='json',
        )
        # 22 elementos > limite 20
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_year_buckets_exatamente_20_aceitos(self):
        """Limite é 20 (inclusivo). 20 elementos deve passar."""
        with patch('rust_engine.pubmed_magnitude_preview') as mock_preview:
            mock_preview.return_value = _fake_magnitude_preview()
            resp = self.client.post(
                f'/api/v1/projects/{self.project.id}/search/preview/',
                {'panel_flags': {'by_year': True, 'year_buckets': list(range(2003, 2023))}},
                format='json',
            )
            self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_year_buckets_ano_antes_1900_rejeita_400(self):
        resp = self.client.post(
            f'/api/v1/projects/{self.project.id}/search/preview/',
            {'panel_flags': {'by_year': True, 'year_buckets': [1899, 2020]}},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        body = resp.data
        self.assertIn('year_buckets', str(body).lower() + str(body.get('panel_flags', '')).lower())

    def test_year_buckets_ano_no_futuro_distante_rejeita_400(self):
        ano_futuro = datetime.datetime.now().year + 5
        resp = self.client.post(
            f'/api/v1/projects/{self.project.id}/search/preview/',
            {'panel_flags': {'by_year': True, 'year_buckets': [ano_futuro]}},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_year_buckets_ano_atual_mais_1_aceito(self):
        """Limite superior é ano_atual + 1."""
        ano_limite = datetime.datetime.now().year + 1
        with patch('rust_engine.pubmed_magnitude_preview') as mock_preview:
            mock_preview.return_value = _fake_magnitude_preview()
            resp = self.client.post(
                f'/api/v1/projects/{self.project.id}/search/preview/',
                {'panel_flags': {'by_year': True, 'year_buckets': [ano_limite]}},
                format='json',
            )
            self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_year_buckets_null_aceito(self):
        """year_buckets=null (default) é válido."""
        with patch('rust_engine.pubmed_magnitude_preview') as mock_preview:
            mock_preview.return_value = _fake_magnitude_preview()
            resp = self.client.post(
                f'/api/v1/projects/{self.project.id}/search/preview/',
                {'panel_flags': {'by_year': False, 'year_buckets': None}},
                format='json',
            )
            self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_year_buckets_1900_aceito(self):
        """Limite inferior é 1900 (inclusivo)."""
        with patch('rust_engine.pubmed_magnitude_preview') as mock_preview:
            mock_preview.return_value = _fake_magnitude_preview()
            resp = self.client.post(
                f'/api/v1/projects/{self.project.id}/search/preview/',
                {'panel_flags': {'year_buckets': [1900]}},
                format='json',
            )
            self.assertEqual(resp.status_code, status.HTTP_200_OK)


# ─── Grupo 4: Isolamento cross-user ──────────────────────────────────────────

class TestIsolamentoCrossUser(APITestCase):
    """Usuário B não pode acessar endpoints do projeto de usuário A."""

    def setUp(self):
        self.user_a = User.objects.create_user(username='user_a', password='pw')
        self.user_b = User.objects.create_user(username='user_b', password='pw')
        self.project_a = _make_project(self.user_a, query_term='cancer')
        self.client_b = APIClient()
        self.client_b.force_authenticate(user=self.user_b)

    @patch('rust_engine.mesh_suggest')
    def test_mesh_suggest_projeto_alheio_retorna_404(self, mock_suggest):
        mock_suggest.return_value = [_fake_mesh_suggestion()]
        resp = self.client_b.post(
            f'/api/v1/projects/{self.project_a.id}/mesh/suggest/',
            {'term': 'diabetes'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    @patch('rust_engine.pubmed_magnitude_preview')
    def test_search_preview_projeto_alheio_retorna_404(self, mock_preview):
        mock_preview.return_value = _fake_magnitude_preview()
        resp = self.client_b.post(
            f'/api/v1/projects/{self.project_a.id}/search/preview/',
            {'selected_mesh': []},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_unauthenticated_mesh_suggest_retorna_401_ou_403(self):
        unauth_client = APIClient()
        resp = unauth_client.post(
            f'/api/v1/projects/{self.project_a.id}/mesh/suggest/',
            {'term': 'diabetes'},
            format='json',
        )
        self.assertIn(resp.status_code, [
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        ])

    def test_unauthenticated_search_preview_retorna_401_ou_403(self):
        unauth_client = APIClient()
        resp = unauth_client.post(
            f'/api/v1/projects/{self.project_a.id}/search/preview/',
            {},
            format='json',
        )
        self.assertIn(resp.status_code, [
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        ])

    @patch('rust_engine.mesh_suggest')
    def test_ncbi_key_nao_aparece_na_resposta_mesh_suggest(self, mock_suggest):
        """A NCBI API key nunca deve vazar na resposta."""
        mock_suggest.return_value = [_fake_mesh_suggestion()]
        # Criar perfil com ncbi_api_key (se existir no modelo de accounts)
        # A resposta não deve conter string que pareça uma chave NCBI
        client_a = APIClient()
        client_a.force_authenticate(user=self.user_a)
        resp = client_a.post(
            f'/api/v1/projects/{self.project_a.id}/mesh/suggest/',
            {'term': 'diabetes'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # A chave NCBI nunca deve aparecer no payload
        response_text = str(resp.data)
        self.assertNotIn('ncbi_api_key', response_text)

    @patch('rust_engine.pubmed_magnitude_preview')
    def test_ncbi_key_nao_aparece_na_resposta_search_preview(self, mock_preview):
        """A NCBI API key nunca deve vazar no response do preview."""
        mock_preview.return_value = _fake_magnitude_preview()
        client_a = APIClient()
        client_a.force_authenticate(user=self.user_a)
        resp = client_a.post(
            f'/api/v1/projects/{self.project_a.id}/search/preview/',
            {'selected_mesh': []},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        response_text = str(resp.data)
        self.assertNotIn('ncbi_api_key', response_text)


# ─── Grupo 5: Read-only — search_preview não cria Job/Paper nem altera projeto ─

class TestSearchPreviewReadOnly(APITestCase):

    def setUp(self):
        self.user = User.objects.create_user(username='u_ro', password='pw')
        self.client.force_authenticate(user=self.user)
        self.project = _make_project(self.user, query_term='diabetes')
        self.url = f'/api/v1/projects/{self.project.id}/search/preview/'

    @patch('rust_engine.pubmed_magnitude_preview')
    def test_nao_cria_ingestion_job(self, mock_preview):
        mock_preview.return_value = _fake_magnitude_preview()
        jobs_antes = IngestionJob.objects.count()
        resp = self.client.post(
            self.url,
            {'selected_mesh': [_mesh_entry('Diabetes Mellitus')]},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(IngestionJob.objects.count(), jobs_antes,
                         'search_preview nao deve criar IngestionJob')

    @patch('rust_engine.pubmed_magnitude_preview')
    def test_nao_cria_paper(self, mock_preview):
        mock_preview.return_value = _fake_magnitude_preview()
        papers_antes = Paper.objects.count()
        resp = self.client.post(
            self.url,
            {'selected_mesh': []},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(Paper.objects.count(), papers_antes,
                         'search_preview nao deve criar Paper')

    @patch('rust_engine.pubmed_magnitude_preview')
    def test_nao_altera_projeto(self, mock_preview):
        mock_preview.return_value = _fake_magnitude_preview()
        # Estado do projeto antes
        project_before = DaVinciProject.objects.get(id=self.project.id)
        status_before = project_before.status
        mesh_before = project_before.selected_mesh
        snapshot_before = project_before.magnitude_snapshot

        resp = self.client.post(
            self.url,
            {
                'selected_mesh': [_mesh_entry('Neoplasms', mode='and')],
                'mesh_default_mode': 'or',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        # Recarrega do banco
        project_after = DaVinciProject.objects.get(id=self.project.id)
        self.assertEqual(project_after.status, status_before,
                         'status do projeto nao deve mudar')
        self.assertEqual(project_after.selected_mesh, mesh_before,
                         'selected_mesh do projeto nao deve mudar')
        self.assertEqual(project_after.magnitude_snapshot, snapshot_before,
                         'magnitude_snapshot nao deve ser atualizado pelo preview')

    @patch('rust_engine.pubmed_magnitude_preview')
    def test_resposta_contem_query_used(self, mock_preview):
        """A resposta deve incluir query_used para que o cliente possa verificar paridade."""
        mock_preview.return_value = _fake_magnitude_preview()
        resp = self.client.post(
            self.url,
            {'selected_mesh': []},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('query_used', resp.data)
        self.assertIsInstance(resp.data['query_used'], str)
        self.assertTrue(len(resp.data['query_used']) > 0)


# ─── Grupo 6: Comportamento do endpoint mesh/suggest ─────────────────────────

class TestMeshSuggestEndpoint(APITestCase):

    def setUp(self):
        self.user = User.objects.create_user(username='u_mesh', password='pw')
        self.client.force_authenticate(user=self.user)
        self.project = _make_project(self.user, query_term='hidradenitis suppurativa',
                                     synonyms=['HS', 'acne inversa'])

    @patch('rust_engine.mesh_suggest')
    def test_retorna_lista_de_sugestoes(self, mock_suggest):
        mock_suggest.return_value = [
            _fake_mesh_suggestion('Hidradenitis Suppurativa', 'D006623'),
            _fake_mesh_suggestion('Hidradenitis', 'D006622'),
        ]
        resp = self.client.post(
            f'/api/v1/projects/{self.project.id}/mesh/suggest/',
            {'term': 'hidradenitis'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIsInstance(resp.data, list)
        self.assertEqual(len(resp.data), 2)
        self.assertEqual(resp.data[0]['descriptor'], 'Hidradenitis Suppurativa')

    @patch('rust_engine.mesh_suggest')
    def test_sem_term_usa_query_do_projeto(self, mock_suggest):
        """Sem 'term' no body → usa query_term + synonyms do projeto."""
        mock_suggest.return_value = [_fake_mesh_suggestion()]
        resp = self.client.post(
            f'/api/v1/projects/{self.project.id}/mesh/suggest/',
            {},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # Verifica que o rust foi chamado com algo derivado do projeto
        call_args = mock_suggest.call_args
        term_chamado = call_args[0][0] if call_args[0] else call_args[1].get('term', '')
        # Deve conter a query_term do projeto
        self.assertIn('hidradenitis suppurativa', term_chamado.lower())

    @patch('rust_engine.mesh_suggest', side_effect=RuntimeError('NCBI timeout'))
    def test_erro_rust_retorna_503(self, mock_suggest):
        resp = self.client.post(
            f'/api/v1/projects/{self.project.id}/mesh/suggest/',
            {'term': 'diabetes'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    @patch('rust_engine.pubmed_magnitude_preview', side_effect=RuntimeError('Rust error'))
    def test_erro_rust_preview_retorna_503(self, mock_preview):
        resp = self.client.post(
            f'/api/v1/projects/{self.project.id}/search/preview/',
            {'selected_mesh': []},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    @patch('rust_engine.mesh_suggest')
    def test_estrutura_de_campo_sugestao(self, mock_suggest):
        """Todos os campos obrigatórios de MeshSuggestion estão presentes."""
        mock_suggest.return_value = [_fake_mesh_suggestion('Diabetes Mellitus', 'D003920')]
        resp = self.client.post(
            f'/api/v1/projects/{self.project.id}/mesh/suggest/',
            {'term': 'diabetes'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        suggestion = resp.data[0]
        for campo in ('descriptor', 'ui', 'tree_numbers', 'scope_note',
                      'allowable_qualifiers', 'pubmed_count'):
            self.assertIn(campo, suggestion, f'Campo {campo!r} ausente na resposta')


# ─── Grupo 7: build_free_text_query — testes unitários puros ─────────────────

class TestBuildFreeTextQuery(APITestCase):
    """
    Testes unitários para build_free_text_query.

    Garantem que:
    - A função retorna apenas o bloco de texto livre (sem [mh] / [majr]).
    - A string retornada é byte-idêntica ao trecho free-text embutido em
      build_pubmed_query quando MeSH está ativo — sem divergência de parênteses
      ou sanitização.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='u_ftq', password='pw')

    def test_sem_sinonimos_retorna_so_free_text(self):
        """query_term simples → bloco sem qualificadores MeSH."""
        p = _make_project(self.user, query_term='cancer', advanced=False)
        ft = build_free_text_query(p)
        self.assertNotIn('[mh]', ft)
        self.assertNotIn('[majr]', ft)
        self.assertIn('cancer', ft)

    def test_com_sinonimo_retorna_so_free_text(self):
        """query_term + sinônimo → OR de termos livres, sem MeSH."""
        p = _make_project(self.user, query_term='cancer', synonyms=['neoplasm'],
                          advanced=False)
        ft = build_free_text_query(p)
        self.assertNotIn('[mh]', ft)
        self.assertNotIn('[majr]', ft)
        self.assertIn('cancer', ft)
        self.assertIn('neoplasm', ft)
        self.assertIn(' OR ', ft)

    def test_free_text_query_identico_ao_prefixo_de_build_pubmed_query(self):
        """
        REGRESSÃO DE PARIDADE: build_free_text_query(p) deve ser byte-idêntico
        ao trecho de texto livre embutido em build_pubmed_query(p) quando MeSH
        está ativo.

        Verifica que os dois helpers compartilham o mesmo _build_free_text_part
        interno — sem duplicação de lógica que pudesse divergir.
        """
        mesh = [_mesh_entry('Neoplasms', mode='and')]
        p = _make_project(self.user, query_term='cancer', synonyms=['neoplasm'],
                          advanced=True, mesh=mesh)

        ft = build_free_text_query(p)
        combined = build_pubmed_query(p)

        # O bloco free-text deve aparecer no início da query combinada.
        self.assertTrue(
            combined.startswith(ft),
            f'build_pubmed_query não começa com build_free_text_query.\n'
            f'free_text={ft!r}\ncombined ={combined!r}',
        )

    def test_free_text_query_com_mesh_ativo_ignora_mesh(self):
        """
        Mesmo com advanced_search_enabled=True e selected_mesh preenchido,
        build_free_text_query retorna apenas o bloco livre — sem blocos MeSH.
        """
        mesh = [_mesh_entry('Diabetes Mellitus', mode='and')]
        p = _make_project(self.user, query_term='diabetes', advanced=True, mesh=mesh)
        ft = build_free_text_query(p)
        self.assertNotIn('[mh]', ft)
        self.assertNotIn('[majr]', ft)
        self.assertIn('diabetes', ft)


# ─── Grupo 8: Regressão — free_text enviado ao Rust não carrega MeSH ─────────

class TestFreeTextEnviadoAoRust(APITestCase):
    """
    REGRESSÃO DIRETA DO BUG descrito no plano 2026-06-20-fix-contagem-texto-livre-vs-mesh.

    Antes do fix, a view passava build_pubmed_query(proxy) — query COMBINADA — como
    parâmetro free_text ao Rust. O Rust então acumulava os termos MeSH uma segunda vez
    ao montar combined_count, fazendo free_text_count ≡ mesh_count ≡ combined_count.

    Estes testes capturam os call_args reais de rust_engine.pubmed_magnitude_preview
    e afirmam:
    1. free_text NÃO contém [mh] nem [majr].
    2. free_text == build_free_text_query(proxy) para aquele projeto/overrides.
    3. combined == build_pubmed_query(proxy) (paridade preview↔ingestão).
    """

    def setUp(self):
        self.user = User.objects.create_user(username='u_reg', password='pw')
        self.client.force_authenticate(user=self.user)

    def _make_proxy(self, project, selected_mesh, mesh_default_mode='and'):
        """Replica o SimpleNamespace equivalente ao _ProjectProxy da view."""
        return SimpleNamespace(
            query_term=project.query_term,
            query_synonyms=project.query_synonyms,
            advanced_search_enabled=bool(selected_mesh),
            selected_mesh=selected_mesh,
            mesh_default_mode=mesh_default_mode,
            date_from=project.date_from,
            date_to=project.date_to,
        )

    @patch('rust_engine.pubmed_magnitude_preview')
    def test_free_text_nao_contem_mh_quando_mesh_selecionado(self, mock_preview):
        """
        Com selected_mesh não-vazio, free_text enviado ao Rust NÃO deve conter
        [mh] nem [majr] — regressão do bug original.
        """
        mock_preview.return_value = _fake_magnitude_preview()
        project = _make_project(self.user, query_term='cancer', advanced=False)
        mesh_override = [_mesh_entry('Neoplasms', mode='and')]

        resp = self.client.post(
            f'/api/v1/projects/{project.id}/search/preview/',
            {'selected_mesh': mesh_override, 'mesh_default_mode': 'and', 'panel_flags': {}},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(mock_preview.called, 'rust_engine.pubmed_magnitude_preview não foi chamado')

        kwargs = mock_preview.call_args.kwargs
        free_text_arg = kwargs.get('free_text', '')

        self.assertNotIn('[mh]', free_text_arg,
                         f'free_text contém [mh] — bug regressivo: {free_text_arg!r}')
        self.assertNotIn('[majr]', free_text_arg,
                         f'free_text contém [majr] — bug regressivo: {free_text_arg!r}')

    @patch('rust_engine.pubmed_magnitude_preview')
    def test_free_text_igual_a_build_free_text_query(self, mock_preview):
        """
        free_text passado ao Rust deve ser idêntico a build_free_text_query(proxy)
        com os mesmos overrides que a view aplica.
        """
        mock_preview.return_value = _fake_magnitude_preview()
        project = _make_project(self.user, query_term='cancer', synonyms=['neoplasm'],
                                advanced=False)
        mesh_override = [_mesh_entry('Neoplasms', mode='and')]

        resp = self.client.post(
            f'/api/v1/projects/{project.id}/search/preview/',
            {'selected_mesh': mesh_override, 'mesh_default_mode': 'and', 'panel_flags': {}},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        proxy = self._make_proxy(project, mesh_override, 'and')
        expected_free_text = build_free_text_query(proxy)

        kwargs = mock_preview.call_args.kwargs
        actual_free_text = kwargs.get('free_text', '')

        self.assertEqual(
            actual_free_text, expected_free_text,
            f'free_text enviado ao Rust diverge de build_free_text_query.\n'
            f'enviado  ={actual_free_text!r}\nesperado ={expected_free_text!r}',
        )

    @patch('rust_engine.pubmed_magnitude_preview')
    def test_combined_igual_a_build_pubmed_query(self, mock_preview):
        """
        combined passado ao Rust deve ser idêntico a build_pubmed_query(proxy),
        garantindo paridade preview↔ingestão (princípio inegociável do plano premium).
        """
        mock_preview.return_value = _fake_magnitude_preview()
        project = _make_project(self.user, query_term='cancer', synonyms=['neoplasm'],
                                advanced=False)
        mesh_override = [_mesh_entry('Neoplasms', mode='and')]

        resp = self.client.post(
            f'/api/v1/projects/{project.id}/search/preview/',
            {'selected_mesh': mesh_override, 'mesh_default_mode': 'and', 'panel_flags': {}},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        proxy = self._make_proxy(project, mesh_override, 'and')
        expected_combined = build_pubmed_query(proxy)

        kwargs = mock_preview.call_args.kwargs
        actual_combined = kwargs.get('combined', '')

        self.assertEqual(
            actual_combined, expected_combined,
            f'combined enviado ao Rust diverge de build_pubmed_query.\n'
            f'enviado  ={actual_combined!r}\nesperado ={expected_combined!r}',
        )
        # Sanidade: a query combinada deve conter [mh] (bloco MeSH está lá, mas UMA vez)
        self.assertIn('[mh]', actual_combined,
                      'combined deveria conter [mh] com MeSH selecionado')

    @patch('rust_engine.pubmed_magnitude_preview')
    def test_free_text_e_combined_diferem_quando_mesh_selecionado(self, mock_preview):
        """
        Quando MeSH está selecionado, free_text e combined devem ser strings distintas:
        free_text é apenas o bloco livre; combined inclui o bloco MeSH além do free-text.
        """
        mock_preview.return_value = _fake_magnitude_preview()
        project = _make_project(self.user, query_term='cancer', advanced=False)
        mesh_override = [_mesh_entry('Neoplasms', mode='and')]

        resp = self.client.post(
            f'/api/v1/projects/{project.id}/search/preview/',
            {'selected_mesh': mesh_override, 'mesh_default_mode': 'and', 'panel_flags': {}},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        kwargs = mock_preview.call_args.kwargs
        self.assertNotEqual(
            kwargs.get('free_text'), kwargs.get('combined'),
            'free_text e combined são iguais com MeSH selecionado — bug regressivo',
        )

    @patch('rust_engine.pubmed_magnitude_preview')
    def test_sem_mesh_free_text_e_combined_sao_equivalentes(self, mock_preview):
        """
        Sem MeSH (selected_mesh=[]), free_text e combined devem expressar a mesma
        busca — ambos são o bloco de texto livre puro.
        """
        mock_preview.return_value = _fake_magnitude_preview()
        project = _make_project(self.user, query_term='cancer', advanced=False)

        resp = self.client.post(
            f'/api/v1/projects/{project.id}/search/preview/',
            {'selected_mesh': [], 'panel_flags': {}},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        kwargs = mock_preview.call_args.kwargs
        free_text_arg = kwargs.get('free_text', '')
        combined_arg = kwargs.get('combined', '')

        # Sem MeSH, a query combinada é igual ao texto livre puro.
        # Nenhum dos dois deve conter qualificadores MeSH.
        self.assertNotIn('[mh]', free_text_arg)
        self.assertNotIn('[majr]', free_text_arg)
        self.assertNotIn('[mh]', combined_arg)
        self.assertNotIn('[majr]', combined_arg)

    @patch('rust_engine.pubmed_magnitude_preview')
    def test_query_used_continua_igual_a_build_pubmed_query(self, mock_preview):
        """
        PARIDADE: query_used na resposta deve ser idêntico a build_pubmed_query(proxy)
        com os overrides aplicados — garante que o número mostrado ao usuário
        corresponde à query que será ingerida.

        Extensão do teste existente em TestParidadePreviewIngestao — cobre o caso
        com MeSH não-vazio explicitamente.
        """
        mock_preview.return_value = _fake_magnitude_preview()
        project = _make_project(self.user, query_term='cancer', advanced=False)
        mesh_override = [_mesh_entry('Neoplasms', mode='and')]

        resp = self.client.post(
            f'/api/v1/projects/{project.id}/search/preview/',
            {'selected_mesh': mesh_override, 'mesh_default_mode': 'and', 'panel_flags': {}},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        proxy = self._make_proxy(project, mesh_override, 'and')
        expected_query = build_pubmed_query(proxy)

        self.assertEqual(
            resp.data['query_used'], expected_query,
            f'query_used diverge de build_pubmed_query.\n'
            f'resposta ={resp.data["query_used"]!r}\nesperado ={expected_query!r}',
        )
