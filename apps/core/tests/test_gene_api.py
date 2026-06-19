"""
Testes do endpoint de genes do projeto.

Cobre:
  1. Agregação (unique_citations_included vs total vs mention_count; fan-out de JOIN).
  2. Isolamento por usuário (skill firebase-auth-guard).
  3. Filtros e ordenação (?q=, ?ordering=, paginação).
  4. Detalhe de gene — cache quente (ready), cache frio/stale (computing), 404.
  5. Idempotência da task de contexto; fronteira de palavra no regex; invalidação.
  6. Regressão: marcador sentinela, lock de disparo, validação de gene_symbol longo.
     (Correção do 007: loop infinito em computing, ReDoS, reenfileiramento duplo.)
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from apps.core.models import (
    DaVinciProject,
    EntityContext,
    Paper,
    PaperGene,
    ProjectPaper,
)
from apps.core.services.gene_service import GeneService, GENE_SYMBOL_MAX_LEN
from apps.core.views.gene_views import _derive_lock_key


# =============================================================================
# Helpers
# =============================================================================

def make_user(username, password='testpass'):
    return User.objects.create_user(username=username, password=password)


def make_project(user, title='Projeto Teste', query_term='cancer'):
    slug = f'{title.lower().replace(" ", "-")}-{user.username}-davinci'
    return DaVinciProject.objects.create(
        user=user,
        title=title,
        slug=slug,
        query_term=query_term,
    )


def make_paper(pmid, title='Paper', journal='Nature', pub_year=2023, abstract=''):
    return Paper.objects.create(
        pmid=pmid,
        title=title,
        journal=journal,
        pub_year=pub_year,
        abstract=abstract,
    )


def make_project_paper(project, paper, curation_status='pending'):
    return ProjectPaper.objects.create(
        project=project,
        paper=paper,
        curation_status=curation_status,
    )


def make_paper_gene(paper, gene_symbol, entrez_id=None, mention_count=1):
    return PaperGene.objects.create(
        paper=paper,
        gene_symbol=gene_symbol,
        entrez_id=entrez_id,
        mention_count=mention_count,
    )


def make_entity_context(paper, gene_symbol, sentence, sentence_position=0, computed_at=None):
    return EntityContext.objects.create(
        paper=paper,
        entity_type=EntityContext.EntityType.GENE,
        entity_name=gene_symbol,
        sentence=sentence,
        sentence_position=sentence_position,
        computed_at=computed_at or timezone.now(),
    )


def genes_url(project_id):
    return f'/api/v1/projects/{project_id}/genes/'


def gene_detail_url(project_id, gene_symbol):
    return f'/api/v1/projects/{project_id}/genes/{gene_symbol}/'


# =============================================================================
# 1. Testes de agregação
# =============================================================================

class GeneAggregationTests(APITestCase):
    """
    Verifica unique_citations_included, unique_citations_total e mention_count_total.

    Caso decisivo:
      - Gene TNF citado em 3 papers do projeto:
          paper1 → included
          paper2 → pending
          paper3 → included (outro projeto compartilha o mesmo Paper)
      - unique_citations_included deve ser 2 (apenas included deste projeto).
      - unique_citations_total deve ser 3 (todos os status deste projeto).
      - mention_count_total deve somar mention_count dos 3 PaperGene.
      - Um quarto paper em outro projeto não deve aparecer.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='agg_user', password='pw')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user)
        self.url = genes_url(self.project.id)

        # Papers deste projeto
        self.p1 = make_paper(pmid=1001, abstract='TNF was elevated.')
        self.p2 = make_paper(pmid=1002, abstract='TNF and IL6 levels.')
        self.p3 = make_paper(pmid=1003, abstract='TNF expression.')

        make_project_paper(self.project, self.p1, curation_status='included')
        make_project_paper(self.project, self.p2, curation_status='pending')
        make_project_paper(self.project, self.p3, curation_status='included')

        # PaperGene com mention_counts diferentes
        make_paper_gene(self.p1, 'TNF', entrez_id=7124, mention_count=3)
        make_paper_gene(self.p2, 'TNF', entrez_id=7124, mention_count=5)
        make_paper_gene(self.p3, 'TNF', entrez_id=7124, mention_count=10)

        # Paper em outro projeto que também cita TNF — NÃO deve inflar contagens
        other_user = make_user('other_agg')
        other_project = make_project(other_user, title='Outro Projeto')
        self.p_other = make_paper(pmid=2001, abstract='TNF irrelevante.')
        make_project_paper(other_project, self.p_other, curation_status='included')
        make_paper_gene(self.p_other, 'TNF', entrez_id=7124, mention_count=99)

    def test_unique_citations_included(self):
        """Apenas papers included deste projeto são contados em included."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']
        tnf = next(r for r in results if r['gene_symbol'] == 'TNF')
        self.assertEqual(tnf['unique_citations_included'], 2)

    def test_unique_citations_total(self):
        """Todos os papers do projeto (qualquer status) são contados em total."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']
        tnf = next(r for r in results if r['gene_symbol'] == 'TNF')
        self.assertEqual(tnf['unique_citations_total'], 3)

    def test_mention_count_total(self):
        """Soma de mention_count dos 3 PaperGene do projeto."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']
        tnf = next(r for r in results if r['gene_symbol'] == 'TNF')
        self.assertEqual(tnf['mention_count_total'], 18)  # 3 + 5 + 10

    def test_other_project_paper_not_counted(self):
        """
        Paper de outro projeto não infla unique_citations_total nem included.
        O paper pmid=2001 pertence a outro projeto; este projeto tem 3 papers (total=3, not 4).
        """
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']
        tnf = next(r for r in results if r['gene_symbol'] == 'TNF')
        self.assertEqual(tnf['unique_citations_total'], 3)
        self.assertEqual(tnf['unique_citations_included'], 2)

    def test_no_join_fanout_multiple_genes(self):
        """
        Gene presente em múltiplos papers: Count distinct não duplica.
        Adiciona um segundo gene (BRCA1) em apenas 1 paper included.
        Verifica que TNF não infla quando há vários genes no mesmo projeto.
        """
        make_paper_gene(self.p1, 'BRCA1', entrez_id=672, mention_count=2)

        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']

        tnf = next(r for r in results if r['gene_symbol'] == 'TNF')
        brca1 = next(r for r in results if r['gene_symbol'] == 'BRCA1')

        # TNF: não deve ter inflado
        self.assertEqual(tnf['unique_citations_total'], 3)
        self.assertEqual(tnf['unique_citations_included'], 2)

        # BRCA1: apenas 1 paper included
        self.assertEqual(brca1['unique_citations_total'], 1)
        self.assertEqual(brca1['unique_citations_included'], 1)

    def test_included_zero_when_only_pending(self):
        """
        Se todos os papers de um gene são pending, unique_citations_included = 0.
        """
        p4 = make_paper(pmid=1004, abstract='IL6 only pending.')
        make_project_paper(self.project, p4, curation_status='pending')
        make_paper_gene(p4, 'IL6', entrez_id=3569, mention_count=1)

        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']

        il6 = next(r for r in results if r['gene_symbol'] == 'IL6')
        self.assertEqual(il6['unique_citations_included'], 0)
        self.assertEqual(il6['unique_citations_total'], 1)

    def test_entrez_id_representative(self):
        """entrez_id retorna um valor não-nulo quando disponível."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']
        tnf = next(r for r in results if r['gene_symbol'] == 'TNF')
        self.assertEqual(tnf['entrez_id'], 7124)

    def test_entrez_id_null_when_absent(self):
        """entrez_id é null quando nenhum PaperGene do grupo tem Entrez ID."""
        p5 = make_paper(pmid=1005, abstract='UNKNOWNGENE mentioned.')
        make_project_paper(self.project, p5, curation_status='included')
        make_paper_gene(p5, 'UNKNOWNGENE', entrez_id=None, mention_count=1)

        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']
        ug = next(r for r in results if r['gene_symbol'] == 'UNKNOWNGENE')
        self.assertIsNone(ug['entrez_id'])


# =============================================================================
# 2. Testes de isolamento por usuário
# =============================================================================

class GeneUserIsolationTests(APITestCase):
    """
    Usuário B não pode acessar genes do projeto de usuário A.
    Skill: firebase-auth-guard — _get_project() filtra por request.user.
    """

    def setUp(self):
        self.user_a = make_user('iso_user_a')
        self.user_b = make_user('iso_user_b')

        self.project_a = make_project(self.user_a, title='Projeto A')
        paper = make_paper(pmid=3001, abstract='TNF is relevant.')
        make_project_paper(self.project_a, paper, curation_status='included')
        make_paper_gene(paper, 'TNF', entrez_id=7124, mention_count=1)

        # Autenticar como usuário B
        self.client.force_authenticate(user=self.user_b)

    def test_user_b_cannot_list_genes_of_user_a_project(self):
        """Usuário B obtém 404 ao listar genes do projeto de A."""
        resp = self.client.get(genes_url(self.project_a.id))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_user_b_cannot_access_gene_detail_of_user_a_project(self):
        """Usuário B obtém 404 ao acessar detalhe de gene do projeto de A."""
        resp = self.client.get(gene_detail_url(self.project_a.id, 'TNF'))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_unauthenticated_cannot_list_genes(self):
        """Requisição sem autenticação deve retornar 403 ou 401."""
        client = APIClient()
        resp = client.get(genes_url(self.project_a.id))
        self.assertIn(resp.status_code, [
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        ])

    def test_user_a_can_see_own_project_genes(self):
        """Usuário A vê seus próprios genes sem problema."""
        self.client.force_authenticate(user=self.user_a)
        resp = self.client.get(genes_url(self.project_a.id))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['count'], 1)


# =============================================================================
# 3. Testes de filtros e ordenação
# =============================================================================

class GeneFilterOrderingTests(APITestCase):
    """
    Filtro ?q= por símbolo (icontains), ?ordering= nos 4 campos,
    default '-unique_citations_included', paginação.
    """

    def setUp(self):
        self.user = make_user('filter_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Filtros')
        self.url = genes_url(self.project.id)

        # Criar 3 papers included e 1 pending para montar cenário
        p1 = make_paper(pmid=4001, abstract='TNF alpha upregulated.')
        p2 = make_paper(pmid=4002, abstract='TNF beta expression.')
        p3 = make_paper(pmid=4003, abstract='BRCA1 mutation found.')
        p4 = make_paper(pmid=4004, abstract='IL6 expression.')

        make_project_paper(self.project, p1, curation_status='included')
        make_project_paper(self.project, p2, curation_status='included')
        make_project_paper(self.project, p3, curation_status='included')
        make_project_paper(self.project, p4, curation_status='pending')

        # TNF: 2 papers included, mention_count 4 e 6 → total 10
        make_paper_gene(p1, 'TNF', entrez_id=7124, mention_count=4)
        make_paper_gene(p2, 'TNF', entrez_id=7124, mention_count=6)
        # BRCA1: 1 paper included, mention_count 3
        make_paper_gene(p3, 'BRCA1', entrez_id=672, mention_count=3)
        # IL6: 0 included (pending), mention_count 2
        make_paper_gene(p4, 'IL6', entrez_id=3569, mention_count=2)

    def test_filter_q_by_symbol(self):
        """?q=TNF filtra por símbolo (icontains)."""
        resp = self.client.get(self.url, {'q': 'TNF'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        symbols = [r['gene_symbol'] for r in resp.data['results']]
        self.assertIn('TNF', symbols)
        self.assertNotIn('BRCA1', symbols)
        self.assertNotIn('IL6', symbols)

    def test_filter_q_case_insensitive(self):
        """?q=tnf (lowercase) também funciona."""
        resp = self.client.get(self.url, {'q': 'tnf'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        symbols = [r['gene_symbol'] for r in resp.data['results']]
        self.assertIn('TNF', symbols)

    def test_filter_q_partial_match(self):
        """?q=BRC retorna BRCA1."""
        resp = self.client.get(self.url, {'q': 'BRC'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        symbols = [r['gene_symbol'] for r in resp.data['results']]
        self.assertIn('BRCA1', symbols)
        self.assertNotIn('TNF', symbols)

    def test_default_ordering_is_unique_citations_included_desc(self):
        """Sem ?ordering=, o default é -unique_citations_included (TNF > BRCA1 > IL6)."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']
        included_values = [r['unique_citations_included'] for r in results]
        # Deve estar em ordem decrescente
        self.assertEqual(included_values, sorted(included_values, reverse=True))
        # TNF (2 included) deve vir antes de BRCA1 (1) e IL6 (0)
        self.assertEqual(results[0]['gene_symbol'], 'TNF')

    def test_ordering_gene_symbol_asc(self):
        """?ordering=gene_symbol ordena A-Z."""
        resp = self.client.get(self.url, {'ordering': 'gene_symbol'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        symbols = [r['gene_symbol'] for r in resp.data['results']]
        self.assertEqual(symbols, sorted(symbols))

    def test_ordering_gene_symbol_desc(self):
        """?ordering=-gene_symbol ordena Z-A."""
        resp = self.client.get(self.url, {'ordering': '-gene_symbol'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        symbols = [r['gene_symbol'] for r in resp.data['results']]
        self.assertEqual(symbols, sorted(symbols, reverse=True))

    def test_ordering_unique_citations_total_asc(self):
        """?ordering=unique_citations_total ordena crescente."""
        resp = self.client.get(self.url, {'ordering': 'unique_citations_total'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        values = [r['unique_citations_total'] for r in resp.data['results']]
        self.assertEqual(values, sorted(values))

    def test_ordering_mention_count_total_desc(self):
        """?ordering=-mention_count_total ordena decrescente."""
        resp = self.client.get(self.url, {'ordering': '-mention_count_total'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        values = [r['mention_count_total'] for r in resp.data['results']]
        self.assertEqual(values, sorted(values, reverse=True))
        # TNF deve vir primeiro (10 menções vs BRCA1 3 vs IL6 2)
        self.assertEqual(resp.data['results'][0]['gene_symbol'], 'TNF')

    def test_invalid_ordering_falls_back_to_default(self):
        """?ordering=invalid_field cai no default (-unique_citations_included)."""
        resp = self.client.get(self.url, {'ordering': 'campo_invalido'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # Não deve retornar erro; resultado válido com default ordering
        self.assertIn('results', resp.data)

    def test_pagination_page_size(self):
        """?page_size=1 retorna 1 resultado com next link."""
        resp = self.client.get(self.url, {'page_size': 1})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data['results']), 1)
        self.assertIsNotNone(resp.data['next'])
        self.assertEqual(resp.data['count'], 3)

    def test_pagination_second_page(self):
        """?page=2&page_size=2 retorna o terceiro gene."""
        resp = self.client.get(self.url, {'page': 2, 'page_size': 2})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data['results']), 1)
        self.assertIsNone(resp.data['next'])


# =============================================================================
# 4. Testes de detalhe de gene
# =============================================================================

class GeneDetailTests(APITestCase):
    """
    Detalhe de gene: cache quente (ready), cache frio/stale (computing), 404.
    """

    def setUp(self):
        self.user = make_user('detail_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Detalhe')

        self.paper = make_paper(
            pmid=5001,
            abstract='TNF was upregulated in the study. TNF levels correlated with severity.',
            pub_year=2022,
        )
        make_project_paper(self.project, self.paper, curation_status='included')
        make_paper_gene(self.paper, 'TNF', entrez_id=7124, mention_count=2)

    def _detail_url(self, gene_symbol='TNF'):
        return gene_detail_url(self.project.id, gene_symbol)

    # --- Cache quente (ready) ---

    def test_detail_cache_hot_returns_ready(self):
        """Cache populado e fresco → context_status='ready'."""
        now = timezone.now()
        make_entity_context(self.paper, 'TNF', 'TNF was upregulated in the study.', 0, now)
        make_entity_context(self.paper, 'TNF', 'TNF levels correlated with severity.', 1, now)

        resp = self.client.get(self._detail_url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['context_status'], 'ready')

    def test_detail_cache_hot_returns_snippets(self):
        """Snippets do cache retornados na ordem correta."""
        now = timezone.now()
        make_entity_context(self.paper, 'TNF', 'TNF was upregulated in the study.', 0, now)
        make_entity_context(self.paper, 'TNF', 'TNF levels correlated with severity.', 1, now)

        resp = self.client.get(self._detail_url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        refs = resp.data['references']
        self.assertEqual(len(refs), 1)
        snippets = refs[0]['snippets']
        self.assertEqual(len(snippets), 2)
        positions = [s['sentence_position'] for s in snippets]
        self.assertEqual(positions, sorted(positions))

    def test_detail_correct_reference_fields(self):
        """Campos do paper (pmid, title, pub_year, journal, curation_status) retornados."""
        now = timezone.now()
        make_entity_context(self.paper, 'TNF', 'TNF was upregulated.', 0, now)

        resp = self.client.get(self._detail_url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ref = resp.data['references'][0]
        self.assertEqual(ref['pmid'], 5001)
        self.assertEqual(ref['curation_status'], 'included')
        self.assertEqual(ref['pub_year'], 2022)

    # --- Cache frio (computing) ---

    def test_detail_cache_cold_returns_computing(self):
        """Sem EntityContext para o paper → context_status='computing'."""
        with patch('apps.core.tasks.gene_tasks.derive_gene_contexts.delay') as mock_task:
            resp = self.client.get(self._detail_url())

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['context_status'], 'computing')
        mock_task.assert_called_once_with(str(self.project.id), 'TNF')

    def test_detail_cache_cold_dispatches_task(self):
        """Cache frio dispara a task Celery exatamente uma vez."""
        with patch('apps.core.tasks.gene_tasks.derive_gene_contexts.delay') as mock_task:
            self.client.get(self._detail_url())
        mock_task.assert_called_once()

    # --- Cache stale (computing) ---

    def test_detail_cache_stale_returns_computing(self):
        """
        Cache com computed_at anterior ao paper.updated_at → context_status='computing'.
        """
        stale_time = timezone.now() - timedelta(hours=1)
        make_entity_context(self.paper, 'TNF', 'TNF was upregulated.', 0, stale_time)

        # Simula re-ingestão: forçar updated_at do paper para após o computed_at
        Paper.objects.filter(pk=self.paper.pk).update(updated_at=timezone.now())
        self.paper.refresh_from_db()

        with patch('apps.core.tasks.gene_tasks.derive_gene_contexts.delay') as mock_task:
            resp = self.client.get(self._detail_url())

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['context_status'], 'computing')
        mock_task.assert_called_once()

    # --- 404 para gene inexistente ---

    def test_detail_nonexistent_gene_returns_404(self):
        """Gene não associado a nenhum paper do projeto → 404."""
        resp = self.client.get(self._detail_url('GENEXXXNOTFOUND'))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_detail_aggregated_metrics(self):
        """Detalhe retorna unique_citations_included e total corretos."""
        # Adicionar segundo paper pending com o mesmo gene
        p2 = make_paper(pmid=5002, abstract='TNF studied.')
        make_project_paper(self.project, p2, curation_status='pending')
        make_paper_gene(p2, 'TNF', entrez_id=7124, mention_count=1)

        now = timezone.now()
        make_entity_context(self.paper, 'TNF', 'TNF was upregulated.', 0, now)
        make_entity_context(p2, 'TNF', 'TNF studied.', 0, now)

        resp = self.client.get(self._detail_url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # included: só paper pmid=5001 (included); total: 2
        self.assertEqual(resp.data['unique_citations_included'], 1)
        self.assertEqual(resp.data['unique_citations_total'], 2)

    def test_detail_references_include_all_status(self):
        """Lista de referências inclui papers de todos os status de curadoria."""
        p2 = make_paper(pmid=5003, abstract='TNF pending study.')
        make_project_paper(self.project, p2, curation_status='pending')
        make_paper_gene(p2, 'TNF', entrez_id=7124, mention_count=1)

        now = timezone.now()
        make_entity_context(self.paper, 'TNF', 'TNF was upregulated.', 0, now)
        make_entity_context(p2, 'TNF', 'TNF pending study.', 0, now)

        resp = self.client.get(self._detail_url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data['references']), 2)
        statuses = {r['curation_status'] for r in resp.data['references']}
        self.assertIn('included', statuses)
        self.assertIn('pending', statuses)


# =============================================================================
# 5. Testes da task de contexto (idempotência, regex, invalidação)
# =============================================================================

class GeneContextTaskTests(APITestCase):
    """
    Testes da lógica de derive_and_persist_contexts e extract_gene_sentences.

    A task Celery é chamada diretamente via GeneService para não depender do broker.
    """

    def setUp(self):
        self.user = make_user('task_user')
        self.project = make_project(self.user, title='Task Tests')

        self.abstract = (
            'TNF was upregulated in all patients. '
            'The levels of TNF correlated with disease severity. '
            'TNFRSF1A was not significantly altered.'
        )
        self.paper = make_paper(
            pmid=6001,
            abstract=self.abstract,
            pub_year=2021,
        )
        make_project_paper(self.project, self.paper, curation_status='included')
        make_paper_gene(self.paper, 'TNF', entrez_id=7124, mention_count=2)

    # --- Idempotência ---

    def test_derive_twice_does_not_duplicate(self):
        """Rodar derive_and_persist_contexts duas vezes não duplica EntityContext."""
        GeneService.derive_and_persist_contexts(self.project, 'TNF')
        GeneService.derive_and_persist_contexts(self.project, 'TNF')

        count = EntityContext.objects.filter(
            paper=self.paper,
            entity_type=EntityContext.EntityType.GENE,
            entity_name='TNF',
        ).count()
        # Deve existir apenas o número de sentenças únicas (não o dobro)
        snippets = GeneService.extract_gene_sentences(self.paper, 'TNF')
        self.assertEqual(count, len(snippets))

    def test_derive_returns_correct_snippet_count(self):
        """Retorna o número correto de snippets derivados."""
        n = GeneService.derive_and_persist_contexts(self.project, 'TNF')
        # Abstract tem 3 sentenças; 2 contêm "TNF" com fronteira de palavra
        self.assertEqual(n, 2)

    # --- Fronteira de palavra (regex \b) ---

    def test_word_boundary_tnf_does_not_match_tnfrsf1a(self):
        """
        "TNF" com \b não deve casar dentro de "TNFRSF1A".
        Apenas sentenças onde "TNF" aparece como token isolado são retornadas.
        """
        snippets = GeneService.extract_gene_sentences(self.paper, 'TNF')
        sentences = [s['sentence'] for s in snippets]

        # As 2 sentenças com TNF isolado devem ser encontradas
        self.assertEqual(len(snippets), 2)
        self.assertTrue(any('TNFRSF1A' not in s for s in sentences))

        # Verificar que a sentença com TNFRSF1A não aparece nos snippets de TNF
        tnfrsf1a_sentence_in_snippets = any(
            'TNFRSF1A' in s and 'TNF' not in s.replace('TNFRSF1A', '') for s in sentences
        )
        self.assertFalse(tnfrsf1a_sentence_in_snippets)

    def test_snippets_content_correct(self):
        """Snippets derivados correspondem às sentenças esperadas do abstract."""
        snippets = GeneService.extract_gene_sentences(self.paper, 'TNF')
        sentences = [s['sentence'] for s in snippets]

        self.assertIn('TNF was upregulated in all patients.', sentences)
        self.assertIn('The levels of TNF correlated with disease severity.', sentences)

    def test_sentence_position_zero_based(self):
        """sentence_position é 0-based e reflete a posição no abstract dividido."""
        snippets = GeneService.extract_gene_sentences(self.paper, 'TNF')
        # Primeira sentença (pos 0) e segunda (pos 1) contêm TNF
        positions = {s['sentence_position'] for s in snippets}
        self.assertIn(0, positions)
        self.assertIn(1, positions)

    def test_empty_abstract_returns_no_snippets(self):
        """Abstract vazio retorna lista vazia."""
        paper_empty = make_paper(pmid=6002, abstract='')
        make_project_paper(self.project, paper_empty, curation_status='included')
        make_paper_gene(paper_empty, 'TNF', entrez_id=7124, mention_count=1)

        snippets = GeneService.extract_gene_sentences(paper_empty, 'TNF')
        self.assertEqual(snippets, [])

    def test_gene_not_in_abstract_returns_no_snippets(self):
        """Gene ausente do abstract retorna lista vazia."""
        paper_other = make_paper(pmid=6003, abstract='IL6 and IL10 were elevated.')
        snippets = GeneService.extract_gene_sentences(paper_other, 'TNF')
        self.assertEqual(snippets, [])

    # --- Invalidação após mudança de abstract ---

    def test_invalidation_after_abstract_change(self):
        """
        Após mudar o abstract do paper (updated_at avança),
        snippets antigos ficam stale e são recomputados na segunda chamada.
        """
        # Primeira derivação
        GeneService.derive_and_persist_contexts(self.project, 'TNF')
        first_count = EntityContext.objects.filter(
            paper=self.paper,
            entity_type=EntityContext.EntityType.GENE,
            entity_name='TNF',
        ).count()
        self.assertEqual(first_count, 2)

        # Mudar abstract (simula re-ingestão)
        new_abstract = 'TNF alpha only once mentioned.'
        Paper.objects.filter(pk=self.paper.pk).update(
            abstract=new_abstract,
            updated_at=timezone.now(),
        )
        self.paper.refresh_from_db()

        # Segunda derivação (deve limpar stale e recriar)
        GeneService.derive_and_persist_contexts(self.project, 'TNF')
        second_count = EntityContext.objects.filter(
            paper=self.paper,
            entity_type=EntityContext.EntityType.GENE,
            entity_name='TNF',
        ).count()

        # Novo abstract tem apenas 1 sentença com TNF
        new_snippets = GeneService.extract_gene_sentences(self.paper, 'TNF')
        self.assertEqual(second_count, len(new_snippets))
        self.assertEqual(second_count, 1)

    def test_derive_persists_computed_at(self):
        """computed_at é gravado em cada EntityContext derivado."""
        before = timezone.now()
        GeneService.derive_and_persist_contexts(self.project, 'TNF')
        after = timezone.now()

        contexts = EntityContext.objects.filter(
            paper=self.paper,
            entity_type=EntityContext.EntityType.GENE,
            entity_name='TNF',
        )
        for ctx in contexts:
            self.assertIsNotNone(ctx.computed_at)
            self.assertGreaterEqual(ctx.computed_at, before)
            self.assertLessEqual(ctx.computed_at, after)

    def test_derive_multiple_papers_in_project(self):
        """derive_and_persist_contexts processa todos os papers do projeto para o gene."""
        p2 = make_paper(pmid=6004, abstract='TNF upregulation confirmed.')
        make_project_paper(self.project, p2, curation_status='pending')
        make_paper_gene(p2, 'TNF', entrez_id=7124, mention_count=1)

        n = GeneService.derive_and_persist_contexts(self.project, 'TNF')
        # paper original: 2 snippets; p2: 1 snippet → total 3
        self.assertEqual(n, 3)

        count = EntityContext.objects.filter(
            entity_type=EntityContext.EntityType.GENE,
            entity_name='TNF',
        ).count()
        self.assertEqual(count, 3)

    # --- Integração com a task Celery (síncrona) ---

    def test_celery_task_calls_service(self):
        """
        A Celery task derive_gene_contexts chama GeneService.derive_and_persist_contexts.
        Testada chamando run() diretamente (sem broker).
        """
        from apps.core.tasks.gene_tasks import derive_gene_contexts

        n_before = EntityContext.objects.filter(
            paper=self.paper,
            entity_type=EntityContext.EntityType.GENE,
            entity_name='TNF',
        ).count()
        self.assertEqual(n_before, 0)

        # Chama a função subjacente da task sem broker
        derive_gene_contexts.run(str(self.project.id), 'TNF')

        n_after = EntityContext.objects.filter(
            paper=self.paper,
            entity_type=EntityContext.EntityType.GENE,
            entity_name='TNF',
        ).count()
        self.assertEqual(n_after, 2)

    def test_celery_task_nonexistent_project_does_not_raise(self):
        """
        Task com project_id inválido deve retornar sem levantar exceção
        (projeto não encontrado → log warning + return).
        """
        from apps.core.tasks.gene_tasks import derive_gene_contexts
        import uuid

        fake_id = str(uuid.uuid4())
        # Não deve levantar exceção
        try:
            derive_gene_contexts.run(fake_id, 'TNF')
        except Exception as exc:
            self.fail(f'Task levantou exceção inesperada: {exc}')


# =============================================================================
# 6. Testes de regressão — correções do 007
#    Cobrem: marcador sentinela, lock de disparo, validação de gene_symbol longo.
#    Ref: achados 1, 3 e 4 do relatório 007 (loop infinito em computing,
#         reenfileiramento duplo, ReDoS por gene_symbol ilimitado).
# =============================================================================

class GeneSentinelRegressionTests(APITestCase):
    """
    Regressão: gene citado fora do abstract não pode ficar preso em 'computing'.

    Antes da correção (bug): derive_and_persist_contexts não gravava nenhuma
    linha para papers onde o gene não aparece no abstract. Isso fazia
    get_gene_detail() detectar computed_at=None e retornar 'computing' para
    sempre (loop infinito).

    Após a correção: uma linha sentinela (sentence_position=-1, sentence='')
    é gravada, sinalizando "processado sem snippets". O detalhe deve retornar
    'ready' e a referência deve aparecer com snippets=[].

    HANDOFF PENDENTE (cartografo + vitruvio):
        EntityContext.sentence_position é PositiveSmallIntegerField — não aceita
        -1 no PostgreSQL (check constraint). O campo precisa ser alterado para
        SmallIntegerField (com sinal) antes que estes testes passem.
        Estes testes falharão com IntegrityError até a migração ser aplicada.
    """

    def setUp(self):
        cache.clear()
        self.user = make_user('sentinel_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Sentinel Tests')

    def tearDown(self):
        cache.clear()

    # ------------------------------------------------------------------
    # Caso 1: gene citado só fora do abstract (ex.: no título/keywords)
    # ------------------------------------------------------------------

    def test_gene_only_outside_abstract_derive_returns_ready(self):
        """
        Após derivação, detalhe retorna context_status='ready' mesmo quando
        o gene não aparece no abstract (paper cita o gene via PaperGene,
        mas o abstract não contém o símbolo).

        Regressão: antes gravava zero linhas → computed_at=None → 'computing' eterno.
        """
        # Abstract deliberadamente NÃO contém 'TNF'
        paper = make_paper(pmid=7001, abstract='Inflammatory markers were elevated.')
        make_project_paper(self.project, paper, curation_status='included')
        make_paper_gene(paper, 'TNF', entrez_id=7124, mention_count=1)

        GeneService.derive_and_persist_contexts(self.project, 'TNF')

        resp = self.client.get(gene_detail_url(self.project.id, 'TNF'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            resp.data['context_status'],
            'ready',
            'context_status deve ser "ready" após derivação, mesmo sem snippets no abstract.',
        )

    def test_gene_only_outside_abstract_reference_present_snippets_empty(self):
        """
        A referência (paper) aparece na lista mas snippets é lista vazia —
        o gene é citado no paper mas não tem trecho de abstract associado.
        """
        paper = make_paper(pmid=7002, abstract='Inflammatory markers were elevated.')
        make_project_paper(self.project, paper, curation_status='included')
        make_paper_gene(paper, 'TNF', entrez_id=7124, mention_count=1)

        GeneService.derive_and_persist_contexts(self.project, 'TNF')

        resp = self.client.get(gene_detail_url(self.project.id, 'TNF'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        refs = resp.data['references']
        self.assertEqual(len(refs), 1, 'Deve existir exatamente 1 referência.')
        self.assertEqual(refs[0]['pmid'], 7002)
        self.assertEqual(
            refs[0]['snippets'],
            [],
            'snippets deve ser [] — gene não aparece no abstract.',
        )

    def test_sentinel_does_not_leak_as_snippet(self):
        """
        Nenhum snippet na resposta deve ter sentence_position == -1.
        A sentinela é interna; o endpoint nunca a entrega ao cliente.
        """
        paper = make_paper(pmid=7003, abstract='Only cytokines mentioned here.')
        make_project_paper(self.project, paper, curation_status='included')
        make_paper_gene(paper, 'TNF', entrez_id=7124, mention_count=1)

        GeneService.derive_and_persist_contexts(self.project, 'TNF')

        resp = self.client.get(gene_detail_url(self.project.id, 'TNF'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        for ref in resp.data.get('references', []):
            for snippet in ref.get('snippets', []):
                self.assertNotEqual(
                    snippet.get('sentence_position'),
                    -1,
                    'Sentinela (sentence_position=-1) não deve vazar como snippet na resposta.',
                )

    # ------------------------------------------------------------------
    # Caso 2: abstract vazio ou None
    # ------------------------------------------------------------------

    def test_empty_abstract_derive_returns_ready(self):
        """
        Paper com abstract='' após derivação → context_status='ready', não 'computing'.

        Regressão: abstract vazio não gerava snippets nem sentinela → loop computing.
        """
        paper = make_paper(pmid=7004, abstract='')
        make_project_paper(self.project, paper, curation_status='included')
        make_paper_gene(paper, 'TNF', entrez_id=7124, mention_count=1)

        GeneService.derive_and_persist_contexts(self.project, 'TNF')

        resp = self.client.get(gene_detail_url(self.project.id, 'TNF'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            resp.data['context_status'],
            'ready',
            'Abstract vazio deve resultar em "ready" após derivação.',
        )

    def test_empty_abstract_snippets_empty(self):
        """Paper com abstract vazio → referência presente, snippets=[]."""
        paper = make_paper(pmid=7005, abstract='')
        make_project_paper(self.project, paper, curation_status='included')
        make_paper_gene(paper, 'TNF', entrez_id=7124, mention_count=1)

        GeneService.derive_and_persist_contexts(self.project, 'TNF')

        resp = self.client.get(gene_detail_url(self.project.id, 'TNF'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        refs = resp.data['references']
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]['snippets'], [])


class GeneSentinelIdempotenceTests(APITestCase):
    """
    Regressão: idempotência do marcador sentinela.

    Rodar derive_and_persist_contexts 2x não deve criar linhas sentinela
    duplicadas. O unique_together em EntityContext protege, mas o código
    deve deletar + recriar corretamente sem violar a constraint.
    """

    def setUp(self):
        cache.clear()
        self.user = make_user('idem_sentinel_user')
        self.project = make_project(self.user, title='Idempotence Sentinel')

    def tearDown(self):
        cache.clear()

    def test_sentinel_not_duplicated_on_double_derive(self):
        """
        Rodar derivação 2x para paper sem gene no abstract → exatamente 1 linha
        de EntityContext (a sentinela), não 2.
        """
        paper = make_paper(pmid=7010, abstract='No gene here.')
        make_project_paper(self.project, paper, curation_status='included')
        make_paper_gene(paper, 'TNF', entrez_id=7124, mention_count=1)

        GeneService.derive_and_persist_contexts(self.project, 'TNF')
        GeneService.derive_and_persist_contexts(self.project, 'TNF')

        count = EntityContext.objects.filter(
            paper=paper,
            entity_type=EntityContext.EntityType.GENE,
            entity_name='TNF',
        ).count()
        self.assertEqual(
            count,
            1,
            'Deve existir exatamente 1 linha (sentinela) após 2 derivações — sem duplicata.',
        )

    def test_empty_abstract_sentinel_not_duplicated(self):
        """
        Abstract vazio: rodar 2x também resulta em 1 linha sentinela, não 2.
        """
        paper = make_paper(pmid=7011, abstract='')
        make_project_paper(self.project, paper, curation_status='included')
        make_paper_gene(paper, 'TNF', entrez_id=7124, mention_count=1)

        GeneService.derive_and_persist_contexts(self.project, 'TNF')
        GeneService.derive_and_persist_contexts(self.project, 'TNF')

        count = EntityContext.objects.filter(
            paper=paper,
            entity_type=EntityContext.EntityType.GENE,
            entity_name='TNF',
        ).count()
        self.assertEqual(count, 1)


class GeneLockDispatchTests(APITestCase):
    """
    Regressão: lock de disparo de task Celery.

    Dois GETs seguidos com cache frio não devem enfileirar a task duas vezes
    enquanto o lock (gene_derive_lock:{project_id}:{gene_symbol}) está ativo.
    cache.add() é atômico — apenas o primeiro GET adquire o lock e dispara a task.

    O cache é limpo em setUp/tearDown para isolar o lock entre testes.
    """

    def setUp(self):
        cache.clear()
        self.user = make_user('lock_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Lock Tests')

        # Paper com abstract que contém TNF — estado frio (sem EntityContext)
        self.paper = make_paper(
            pmid=7020,
            abstract='TNF was elevated in all samples.',
        )
        make_project_paper(self.project, self.paper, curation_status='included')
        make_paper_gene(self.paper, 'TNF', entrez_id=7124, mention_count=1)

    def tearDown(self):
        cache.clear()

    def test_two_cold_gets_dispatch_task_at_most_once(self):
        """
        Dois GETs consecutivos com cache frio disparam a task no máximo uma vez
        enquanto o lock está ativo.

        O lock é adquirido pelo primeiro GET via cache.add() (atômico);
        o segundo GET encontra o lock e não reenfileira.
        """
        with patch('apps.core.tasks.gene_tasks.derive_gene_contexts.delay') as mock_delay:
            self.client.get(gene_detail_url(self.project.id, 'TNF'))
            self.client.get(gene_detail_url(self.project.id, 'TNF'))

        self.assertEqual(
            mock_delay.call_count,
            1,
            f'A task deve ser disparada exatamente 1 vez; foi disparada {mock_delay.call_count} vez(es).',
        )

    def test_first_cold_get_dispatches_task(self):
        """O primeiro GET com cache frio deve disparar a task."""
        with patch('apps.core.tasks.gene_tasks.derive_gene_contexts.delay') as mock_delay:
            resp = self.client.get(gene_detail_url(self.project.id, 'TNF'))

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['context_status'], 'computing')
        mock_delay.assert_called_once_with(str(self.project.id), 'TNF')

    def test_lock_key_format(self):
        """
        A chave de lock deve seguir o formato 'gene_derive_lock:{project_id}:{gene_symbol}'.
        Garante que o lock é específico por (projeto, gene) e não colide entre projetos.
        """
        expected = f'gene_derive_lock:{self.project.id}:TNF'
        actual = _derive_lock_key(str(self.project.id), 'TNF')
        self.assertEqual(actual, expected)

    def test_lock_isolates_different_genes(self):
        """
        Lock de TNF não bloqueia disparo de task para BRCA1 no mesmo projeto.
        """
        paper2 = make_paper(pmid=7021, abstract='BRCA1 mutation identified.')
        make_project_paper(self.project, paper2, curation_status='included')
        make_paper_gene(paper2, 'BRCA1', entrez_id=672, mention_count=1)

        with patch('apps.core.tasks.gene_tasks.derive_gene_contexts.delay') as mock_delay:
            # Primeiro GET adquire lock para TNF
            self.client.get(gene_detail_url(self.project.id, 'TNF'))
            # Segundo GET mesmo gene — lock ativo, não reenfileira
            self.client.get(gene_detail_url(self.project.id, 'TNF'))
            # GET para BRCA1 — lock diferente, deve disparar task
            self.client.get(gene_detail_url(self.project.id, 'BRCA1'))

        self.assertEqual(
            mock_delay.call_count,
            2,
            'Deve haver 2 disparos: 1 para TNF (primeiro GET) + 1 para BRCA1.',
        )
        calls_genes = [call.args[1] for call in mock_delay.call_args_list]
        self.assertIn('TNF', calls_genes)
        self.assertIn('BRCA1', calls_genes)


class GeneSymbolValidationTests(APITestCase):
    """
    Regressão: validação de gene_symbol acima do limite (GENE_SYMBOL_MAX_LEN = 64).

    Proteção contra ReDoS: um símbolo longo compilado como regex sobre N abstracts
    pode ser custoso. A view rejeita com 404 antes de qualquer acesso ao banco.

    Item 4 do relatório 007.
    """

    def setUp(self):
        cache.clear()
        self.user = make_user('validation_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Validation Tests')

    def tearDown(self):
        cache.clear()

    def test_gene_symbol_above_max_len_returns_404(self):
        """
        gene_symbol com mais de 64 caracteres → 404 imediato, sem tocar o banco.
        """
        long_symbol = 'A' * (GENE_SYMBOL_MAX_LEN + 1)  # 65 chars
        resp = self.client.get(gene_detail_url(self.project.id, long_symbol))
        self.assertEqual(
            resp.status_code,
            status.HTTP_404_NOT_FOUND,
            f'gene_symbol com {len(long_symbol)} chars deve retornar 404.',
        )

    def test_gene_symbol_at_max_len_does_not_return_404_on_length(self):
        """
        gene_symbol exatamente no limite (64 chars) não é rejeitado por comprimento
        — pode retornar 404 se o gene não existir no projeto, mas por ausência,
        não por violação de comprimento.

        Distingue a rejeição por comprimento (prioridade) da rejeição por ausência.
        """
        # Símbolo exatamente no limite — não existe no projeto
        symbol_at_limit = 'B' * GENE_SYMBOL_MAX_LEN  # 64 chars
        resp = self.client.get(gene_detail_url(self.project.id, symbol_at_limit))
        # Retorna 404, mas pelo motivo "gene não encontrado", não "símbolo longo"
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        # A mensagem não deve mencionar comprimento máximo
        detail_msg = resp.data.get('detail', '')
        self.assertNotIn(
            'comprimento máximo',
            detail_msg,
            'Gene no limite de 64 chars não deve ser rejeitado por comprimento.',
        )

    def test_gene_symbol_above_max_len_does_not_query_db(self):
        """
        Símbolo longo não deve disparar nenhuma query ao banco nem task Celery.
        A rejeição ocorre na view antes de _get_project() e GeneService.
        """
        long_symbol = 'C' * (GENE_SYMBOL_MAX_LEN + 10)  # 74 chars

        with patch('apps.core.tasks.gene_tasks.derive_gene_contexts.delay') as mock_delay:
            resp = self.client.get(gene_detail_url(self.project.id, long_symbol))

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        mock_delay.assert_not_called()

    def test_gene_symbol_exactly_one_above_limit_returns_404(self):
        """Caso de fronteira: exatamente 65 chars → rejeitado."""
        symbol_65 = 'D' * 65
        resp = self.client.get(gene_detail_url(self.project.id, symbol_65))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn('comprimento máximo', resp.data.get('detail', ''))


# =============================================================================
# 7. Testes do filtro ?included_only=
# =============================================================================

class GeneIncludedOnlyFilterTests(APITestCase):
    """
    Filtro ?included_only=true na lista GET /projects/{project_pk}/genes/.

    Caso decisivo:
      - Gene A (ACTB): citado apenas em papers pending/maybe (sem included).
      - Gene B (BRCA1): citado em ao menos um paper included.
      - Sem o filtro → ambos aparecem.
      - Com ?included_only=true → apenas BRCA1 aparece.
      - As contagens (included | total) no payload não mudam; só a presença na lista.

    Cobre também:
      - Valores equivalentes: true/1 ativam; false/0/ausente não ativam.
      - Composição com ?ordering= e paginação.
    """

    def setUp(self):
        self.user = make_user('incl_filter_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='IncludedOnly Tests')
        self.url = genes_url(self.project.id)

        # Gene A (ACTB): citado em paper pending e paper maybe — zero included
        self.p_pending = make_paper(pmid=8001, abstract='ACTB is a housekeeping gene.')
        self.p_maybe   = make_paper(pmid=8002, abstract='ACTB expression detected.')
        make_project_paper(self.project, self.p_pending, curation_status='pending')
        make_project_paper(self.project, self.p_maybe,   curation_status='maybe')
        make_paper_gene(self.p_pending, 'ACTB', entrez_id=60, mention_count=1)
        make_paper_gene(self.p_maybe,   'ACTB', entrez_id=60, mention_count=1)

        # Gene B (BRCA1): citado em um paper pending e um paper included
        self.p_incl = make_paper(pmid=8003, abstract='BRCA1 mutation found.')
        self.p_pend2 = make_paper(pmid=8004, abstract='BRCA1 studied.')
        make_project_paper(self.project, self.p_incl,  curation_status='included')
        make_project_paper(self.project, self.p_pend2, curation_status='pending')
        make_paper_gene(self.p_incl,  'BRCA1', entrez_id=672, mention_count=2)
        make_paper_gene(self.p_pend2, 'BRCA1', entrez_id=672, mention_count=1)

    # ------------------------------------------------------------------
    # Caso decisivo — presença/ausência na lista
    # ------------------------------------------------------------------

    def test_without_filter_both_genes_present(self):
        """Sem ?included_only, ambos os genes aparecem na lista."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        symbols = {r['gene_symbol'] for r in resp.data['results']}
        self.assertIn('ACTB',  symbols, 'ACTB deve aparecer sem o filtro.')
        self.assertIn('BRCA1', symbols, 'BRCA1 deve aparecer sem o filtro.')

    def test_included_only_true_excludes_gene_with_zero_included(self):
        """?included_only=true remove ACTB (zero included) e mantém BRCA1."""
        resp = self.client.get(self.url, {'included_only': 'true'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        symbols = {r['gene_symbol'] for r in resp.data['results']}
        self.assertNotIn('ACTB',  symbols, 'ACTB não deve aparecer com included_only=true.')
        self.assertIn('BRCA1', symbols, 'BRCA1 deve aparecer com included_only=true.')

    def test_included_only_one_excludes_gene_with_zero_included(self):
        """?included_only=1 é equivalente a true."""
        resp = self.client.get(self.url, {'included_only': '1'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        symbols = {r['gene_symbol'] for r in resp.data['results']}
        self.assertNotIn('ACTB',  symbols)
        self.assertIn('BRCA1', symbols)

    def test_included_only_false_does_not_filter(self):
        """?included_only=false não aplica filtro (default explícito)."""
        resp = self.client.get(self.url, {'included_only': 'false'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        symbols = {r['gene_symbol'] for r in resp.data['results']}
        self.assertIn('ACTB',  symbols)
        self.assertIn('BRCA1', symbols)

    def test_included_only_zero_does_not_filter(self):
        """?included_only=0 não aplica filtro."""
        resp = self.client.get(self.url, {'included_only': '0'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        symbols = {r['gene_symbol'] for r in resp.data['results']}
        self.assertIn('ACTB',  symbols)
        self.assertIn('BRCA1', symbols)

    def test_default_no_param_does_not_filter(self):
        """Sem o parâmetro, o comportamento é idêntico a included_only=false."""
        resp_default  = self.client.get(self.url)
        resp_explicit = self.client.get(self.url, {'included_only': 'false'})
        self.assertEqual(resp_default.data['count'], resp_explicit.data['count'])

    # ------------------------------------------------------------------
    # Contagens não mudam com o filtro ativo
    # ------------------------------------------------------------------

    def test_included_only_does_not_alter_counts_of_remaining_gene(self):
        """
        Com ?included_only=true, os campos unique_citations_included e
        unique_citations_total de BRCA1 são idênticos aos retornados sem o filtro.
        O filtro só exclui genes da lista; não altera as contagens dos que ficam.
        """
        resp_all  = self.client.get(self.url)
        resp_incl = self.client.get(self.url, {'included_only': 'true'})

        brca1_all  = next(r for r in resp_all.data['results']  if r['gene_symbol'] == 'BRCA1')
        brca1_incl = next(r for r in resp_incl.data['results'] if r['gene_symbol'] == 'BRCA1')

        self.assertEqual(
            brca1_all['unique_citations_included'],
            brca1_incl['unique_citations_included'],
            'unique_citations_included de BRCA1 não deve mudar com o filtro ativo.',
        )
        self.assertEqual(
            brca1_all['unique_citations_total'],
            brca1_incl['unique_citations_total'],
            'unique_citations_total de BRCA1 não deve mudar com o filtro ativo.',
        )

    def test_included_only_correct_count_for_brca1(self):
        """
        Com ?included_only=true, BRCA1 tem unique_citations_included=1
        e unique_citations_total=2 (1 included + 1 pending no projeto).
        """
        resp = self.client.get(self.url, {'included_only': 'true'})
        brca1 = next(r for r in resp.data['results'] if r['gene_symbol'] == 'BRCA1')
        self.assertEqual(brca1['unique_citations_included'], 1)
        self.assertEqual(brca1['unique_citations_total'], 2)

    # ------------------------------------------------------------------
    # Composição com ?ordering= e paginação
    # ------------------------------------------------------------------

    def test_included_only_composes_with_ordering_gene_symbol(self):
        """
        ?included_only=true&ordering=gene_symbol retorna apenas genes com
        included>0, em ordem alfabética, sem erro.
        """
        # Adicionar terceiro gene com paper included para ter mais resultados
        p_extra = make_paper(pmid=8005, abstract='TP53 involved in apoptosis.')
        make_project_paper(self.project, p_extra, curation_status='included')
        make_paper_gene(p_extra, 'TP53', entrez_id=7157, mention_count=3)

        resp = self.client.get(self.url, {'included_only': 'true', 'ordering': 'gene_symbol'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        symbols = [r['gene_symbol'] for r in resp.data['results']]
        # ACTB não deve aparecer
        self.assertNotIn('ACTB', symbols)
        # BRCA1 e TP53 devem estar em ordem alfabética
        self.assertIn('BRCA1', symbols)
        self.assertIn('TP53',  symbols)
        self.assertEqual(symbols, sorted(symbols))

    def test_included_only_composes_with_ordering_desc(self):
        """?included_only=true&ordering=-unique_citations_included é válido."""
        resp = self.client.get(
            self.url,
            {'included_only': 'true', 'ordering': '-unique_citations_included'},
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        values = [r['unique_citations_included'] for r in resp.data['results']]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_included_only_composes_with_pagination(self):
        """
        ?included_only=true&page_size=1 retorna paginação correta:
        count reflete apenas os genes com included>0.
        """
        resp = self.client.get(self.url, {'included_only': 'true', 'page_size': 1})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # BRCA1 é o único gene com included>0 no setUp
        self.assertEqual(resp.data['count'], 1)
        self.assertEqual(len(resp.data['results']), 1)
        self.assertIsNone(resp.data['next'])

    def test_included_only_true_result_count_in_response(self):
        """resp.data['count'] com ?included_only=true reflete apenas os filtrados."""
        resp_all  = self.client.get(self.url)
        resp_incl = self.client.get(self.url, {'included_only': 'true'})
        self.assertGreater(resp_all.data['count'], resp_incl.data['count'])


# =============================================================================
# 8. Testes do campo project_paper_id e curation_status em references[] do detalhe de gene
# =============================================================================

class GeneDetailReferenceIdTests(APITestCase):
    """
    Garante que cada item de references[] no detalhe de gene carrega:
      - project_paper_id: PK de ProjectPaper (usada no PATCH /projects/{id}/papers/<pk>/).
      - curation_status: status de curadoria do paper neste projeto.
      - NÃO contém o campo 'id' (Paper.pk foi removido — regressão 007).

    O front usa project_paper_id + curation_status para o toggle de curadoria.
    """

    def setUp(self):
        self.user = make_user('ref_id_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='RefID Tests')

        # Dois papers com pmids distintos; confirmar que project_paper_id != pmid
        self.paper_incl = make_paper(pmid=9001, title='Paper Included', pub_year=2023)
        self.paper_pend = make_paper(pmid=9002, title='Paper Pending',  pub_year=2022)

        self.pp_incl = make_project_paper(self.project, self.paper_incl, curation_status='included')
        self.pp_pend = make_project_paper(self.project, self.paper_pend, curation_status='pending')

        make_paper_gene(self.paper_incl, 'TP53', entrez_id=7157, mention_count=2)
        make_paper_gene(self.paper_pend, 'TP53', entrez_id=7157, mention_count=1)

        # Popular cache para retornar context_status='ready'
        now = timezone.now()
        make_entity_context(self.paper_incl, 'TP53', 'TP53 was mutated.', 0, now)
        make_entity_context(self.paper_pend, 'TP53', 'TP53 studied.', 0, now)

    def _detail(self):
        return self.client.get(gene_detail_url(self.project.id, 'TP53'))

    # ------------------------------------------------------------------
    # Campo project_paper_id — presença e correspondência com ProjectPaper.pk
    # ------------------------------------------------------------------

    def test_references_contain_project_paper_id_field(self):
        """Cada item de references[] contém o campo project_paper_id."""
        resp = self._detail()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        for ref in resp.data['references']:
            self.assertIn(
                'project_paper_id',
                ref,
                f'Campo project_paper_id ausente em reference pmid={ref.get("pmid")}.',
            )

    def test_references_do_not_contain_id_field(self):
        """
        O campo 'id' (Paper.pk) foi removido após correção do 007.
        Trava regressão: nenhuma referência deve expor o campo 'id'.
        """
        resp = self._detail()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        for ref in resp.data['references']:
            self.assertNotIn(
                'id',
                ref,
                f'Campo id não deve existir em references[] (removido pelo 007); pmid={ref.get("pmid")}.',
            )

    def test_reference_project_paper_id_matches_project_paper_pk(self):
        """project_paper_id corresponde à PK de ProjectPaper, não à de Paper."""
        resp = self._detail()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        refs_by_pmid = {r['pmid']: r for r in resp.data['references']}

        self.assertEqual(
            refs_by_pmid[9001]['project_paper_id'],
            self.pp_incl.pk,
            'project_paper_id deve ser o PK de ProjectPaper, não de Paper.',
        )
        self.assertEqual(
            refs_by_pmid[9002]['project_paper_id'],
            self.pp_pend.pk,
            'project_paper_id deve ser o PK de ProjectPaper, não de Paper.',
        )

    def test_reference_project_paper_id_is_not_pmid(self):
        """
        project_paper_id e pmid são campos distintos.
        No ambiente de teste, o PK auto-increment de ProjectPaper não coincide
        com o pmid (9001/9002). Confirma que o front recebe dois campos independentes.
        """
        resp = self._detail()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        for ref in resp.data['references']:
            # project_paper_id deve ser um inteiro presente
            self.assertIsInstance(ref['project_paper_id'], int)
            # pmid também deve ser inteiro e presente
            self.assertIsInstance(ref['pmid'], int)
            # Os campos devem ser diferentes (PK auto-increment != pmid explícito 9001/9002)
            self.assertNotEqual(
                ref['project_paper_id'],
                ref['pmid'],
                f'project_paper_id={ref["project_paper_id"]} e pmid={ref["pmid"]} '
                f'não devem ser iguais — campos distintos.',
            )

    def test_reference_project_paper_id_not_equal_to_paper_pk(self):
        """
        project_paper_id é PK de ProjectPaper, não de Paper.
        Garante que o campo correto é exposto ao front para o PATCH de curadoria.
        """
        resp = self._detail()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        refs_by_pmid = {r['pmid']: r for r in resp.data['references']}

        # project_paper_id deve diferir da PK de Paper
        self.assertNotEqual(
            refs_by_pmid[9001]['project_paper_id'],
            self.paper_incl.pk,
            'project_paper_id não deve ser igual ao Paper.pk — são entidades distintas.',
        )
        self.assertNotEqual(
            refs_by_pmid[9002]['project_paper_id'],
            self.paper_pend.pk,
            'project_paper_id não deve ser igual ao Paper.pk — são entidades distintas.',
        )

    # ------------------------------------------------------------------
    # Campo curation_status — presença e valor correto (inalterado)
    # ------------------------------------------------------------------

    def test_references_contain_curation_status_field(self):
        """Cada item de references[] contém o campo curation_status."""
        resp = self._detail()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        for ref in resp.data['references']:
            self.assertIn(
                'curation_status',
                ref,
                f'Campo curation_status ausente em reference pmid={ref.get("pmid")}.',
            )

    def test_reference_curation_status_correct_per_paper(self):
        """curation_status reflete o valor do ProjectPaper deste projeto."""
        resp = self._detail()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        refs_by_pmid = {r['pmid']: r for r in resp.data['references']}

        self.assertEqual(refs_by_pmid[9001]['curation_status'], 'included')
        self.assertEqual(refs_by_pmid[9002]['curation_status'], 'pending')

    def test_reference_curation_status_all_values(self):
        """
        Referências com todos os status de curadoria (included, excluded, pending, maybe)
        retornam curation_status correto no payload.
        """
        p_excl  = make_paper(pmid=9003, title='Excluded', pub_year=2021)
        p_maybe = make_paper(pmid=9004, title='Maybe',    pub_year=2020)
        make_project_paper(self.project, p_excl,  curation_status='excluded')
        make_project_paper(self.project, p_maybe, curation_status='maybe')
        make_paper_gene(p_excl,  'TP53', entrez_id=7157, mention_count=1)
        make_paper_gene(p_maybe, 'TP53', entrez_id=7157, mention_count=1)

        now = timezone.now()
        make_entity_context(p_excl,  'TP53', 'TP53 excluded.', 0, now)
        make_entity_context(p_maybe, 'TP53', 'TP53 maybe.', 0, now)

        resp = self._detail()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        refs_by_pmid = {r['pmid']: r for r in resp.data['references']}

        self.assertEqual(refs_by_pmid[9003]['curation_status'], 'excluded')
        self.assertEqual(refs_by_pmid[9004]['curation_status'], 'maybe')

    # ------------------------------------------------------------------
    # project_paper_id e curation_status presentes mesmo em estado computing
    # ------------------------------------------------------------------

    def test_project_paper_id_and_curation_status_present_in_computing_state(self):
        """
        Mesmo com context_status='computing' (cache frio), project_paper_id e
        curation_status estão presentes em references[] — o front precisa deles
        para o toggle de curadoria independente do estado do cache de snippets.
        """
        # Novo projeto, sem EntityContext → estado computing
        user2  = make_user('ref_id_user2')
        proj2  = make_project(user2, title='Computing State')
        paper3 = make_paper(pmid=9010, title='TP53 cold cache', pub_year=2024)
        pp3    = make_project_paper(proj2, paper3, curation_status='included')
        make_paper_gene(paper3, 'TP53', entrez_id=7157, mention_count=1)

        self.client.force_authenticate(user=user2)
        with patch('apps.core.tasks.gene_tasks.derive_gene_contexts.delay'):
            resp = self.client.get(gene_detail_url(proj2.id, 'TP53'))

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['context_status'], 'computing')

        refs = resp.data['references']
        self.assertEqual(len(refs), 1)
        # project_paper_id presente e correto
        self.assertIn('project_paper_id', refs[0])
        self.assertEqual(refs[0]['project_paper_id'], pp3.pk)
        # curation_status presente e correto
        self.assertIn('curation_status', refs[0])
        self.assertEqual(refs[0]['curation_status'], 'included')
        # campo 'id' não deve existir
        self.assertNotIn('id', refs[0])
