"""
Testes do endpoint de termos MeSH do projeto.

Cobre:
  1. Agregação (major_topic_count, unique_citations_included, unique_citations_total;
     fan-out de JOIN com múltiplos qualifiers; mesmo descriptor major num paper e
     não-major noutro).
  2. Isolamento por usuário (skill firebase-auth-guard).
  3. Filtros e ordenação (?q=, ?ordering=, paginação).
  4. Filtro ?included_only= (remove descriptor sem citação incluída, sem alterar
     contagens; compõe com ordering/paginação).
  5. Detalhe de descriptor — cache quente (ready), cache frio/stale (computing),
     qualifiers, 404, descriptor multi-palavra com espaço.
  6. TRAVAS DE REGRESSÃO: project_paper_id == ProjectPaper.pk, != Paper.pk,
     'id' não em references[] (fixtures com PKs divergentes forçadas por papers
     fantasma, idêntico à trava do test_gene_api.py).
  7. Task derive_mesh_contexts — idempotência, regex multi-palavra, sentinela -1,
     invalidação após mudança de abstract.
"""

import uuid
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
    PaperMeSHTerm,
    ProjectPaper,
)
from apps.core.services.mesh_service import MeshService, MESH_DESCRIPTOR_MAX_LEN
from apps.core.views.mesh_views import _derive_lock_key


# =============================================================================
# Helpers
# =============================================================================

def make_user(username, password='testpass'):
    return User.objects.create_user(username=username, password=password)


def make_project(user, title='Projeto MeSH Teste', query_term='cancer'):
    slug = f'{title.lower().replace(" ", "-")}-{user.username}-mesh-test'
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


def make_mesh_term(paper, descriptor, qualifier='', is_major_topic=False):
    return PaperMeSHTerm.objects.create(
        paper=paper,
        descriptor=descriptor,
        qualifier=qualifier,
        is_major_topic=is_major_topic,
    )


def make_entity_context_mesh(paper, descriptor, sentence, sentence_position=0, computed_at=None):
    return EntityContext.objects.create(
        paper=paper,
        entity_type=EntityContext.EntityType.MESH,
        entity_name=descriptor,
        sentence=sentence,
        sentence_position=sentence_position,
        computed_at=computed_at or timezone.now(),
    )


def mesh_list_url(project_id):
    return f'/api/v1/projects/{project_id}/mesh/'


def mesh_detail_url(project_id, descriptor):
    from urllib.parse import quote
    return f'/api/v1/projects/{project_id}/mesh/{quote(descriptor, safe="")}/'


# =============================================================================
# 1. Testes de agregação
# =============================================================================

class MeSHAggregationTests(APITestCase):
    """
    Verifica major_topic_count, unique_citations_included e unique_citations_total.

    Caso decisivo (especificidade MeSH):
      - Descriptor "Diabetes Mellitus" presente em 3 papers do projeto:
          paper1 → included, is_major_topic=True
          paper2 → pending,  is_major_topic=False
          paper3 → included, is_major_topic=False  (mesmo descriptor, não-major)
      - major_topic_count deve ser 1 (apenas paper1: included E major_topic=True).
      - unique_citations_included deve ser 2 (paper1 + paper3, ambos included).
      - unique_citations_total deve ser 3 (todos os status deste projeto).
      - Um quarto paper em outro projeto NÃO deve inflar contagens.

    Caso fan-out de JOIN:
      - Mesmo descriptor com múltiplos qualifiers no mesmo paper → Count distinct
        não duplica (PaperMeSHTerm.unique_together inclui qualifier,
        mas a contagem é por paper, não por linha de PaperMeSHTerm).
    """

    def setUp(self):
        self.user = User.objects.create_user(username='mesh_agg_user', password='pw')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user)
        self.url = mesh_list_url(self.project.id)

        # Papers deste projeto
        self.p1 = make_paper(pmid=10001, abstract='Diabetes Mellitus study.')
        self.p2 = make_paper(pmid=10002, abstract='Diabetes Mellitus levels.')
        self.p3 = make_paper(pmid=10003, abstract='Diabetes Mellitus expression.')

        make_project_paper(self.project, self.p1, curation_status='included')
        make_project_paper(self.project, self.p2, curation_status='pending')
        make_project_paper(self.project, self.p3, curation_status='included')

        # Descriptor em p1: is_major_topic=True, qualifier vazio
        make_mesh_term(self.p1, 'Diabetes Mellitus', qualifier='', is_major_topic=True)
        # Descriptor em p2: is_major_topic=False, qualifier 'epidemiology'
        make_mesh_term(self.p2, 'Diabetes Mellitus', qualifier='epidemiology', is_major_topic=False)
        # Descriptor em p3: is_major_topic=False, qualifier 'therapy'
        make_mesh_term(self.p3, 'Diabetes Mellitus', qualifier='therapy', is_major_topic=False)

        # Paper em outro projeto que também cita o descriptor — NÃO deve inflar
        other_user = make_user('mesh_agg_other')
        other_project = make_project(other_user, title='Outro Projeto MeSH')
        self.p_other = make_paper(pmid=20001, abstract='Diabetes Mellitus irrelevante.')
        make_project_paper(other_project, self.p_other, curation_status='included')
        make_mesh_term(self.p_other, 'Diabetes Mellitus', qualifier='', is_major_topic=True)

    def _get_descriptor(self, descriptor='Diabetes Mellitus'):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']
        return next((r for r in results if r['descriptor'] == descriptor), None)

    def test_major_topic_count_only_counts_included_major_topic(self):
        """
        major_topic_count conta apenas papers included E is_major_topic=True.
        p1 (included, major=True) → 1; p2 (pending) e p3 (included, major=False) → não contam.
        """
        item = self._get_descriptor()
        self.assertIsNotNone(item)
        self.assertEqual(item['major_topic_count'], 1)

    def test_major_topic_count_same_descriptor_major_one_not_other(self):
        """
        Mesmo descriptor: p1 é major topic (included), p3 não é (included).
        major_topic_count deve ser 1 (apenas p1), não 2.
        """
        item = self._get_descriptor()
        self.assertIsNotNone(item)
        # p3 é included mas is_major_topic=False — não entra em major_topic_count
        self.assertNotEqual(item['major_topic_count'], 2,
                            'p3 (included, não-major) não deve ser contado em major_topic_count.')
        self.assertEqual(item['major_topic_count'], 1)

    def test_unique_citations_included(self):
        """unique_citations_included conta papers included (qualquer is_major_topic)."""
        item = self._get_descriptor()
        self.assertIsNotNone(item)
        # p1 (included, major=True) + p3 (included, major=False) = 2
        self.assertEqual(item['unique_citations_included'], 2)

    def test_unique_citations_total(self):
        """unique_citations_total conta todos os papers do projeto (qualquer status)."""
        item = self._get_descriptor()
        self.assertIsNotNone(item)
        # p1 + p2 + p3 = 3 (p_other é outro projeto)
        self.assertEqual(item['unique_citations_total'], 3)

    def test_other_project_paper_not_counted(self):
        """Paper de outro projeto não infla nenhuma contagem."""
        item = self._get_descriptor()
        self.assertIsNotNone(item)
        self.assertEqual(item['unique_citations_total'], 3)
        self.assertEqual(item['unique_citations_included'], 2)
        self.assertEqual(item['major_topic_count'], 1)

    def test_fan_out_not_inflating_counts_with_multiple_qualifiers(self):
        """
        Fan-out de JOIN: um mesmo paper com múltiplos qualifiers (múltiplas linhas
        PaperMeSHTerm com mesmo descriptor mas qualifier diferente) não deve
        inflar unique_citations_total nem unique_citations_included.

        Adiciona um segundo qualifier para p1 (que já tem qualifier='') → p1 passa
        a ter 2 linhas PaperMeSHTerm para 'Diabetes Mellitus'. Contagem deve continuar 3 e 2.
        """
        # Adicionar outro qualifier para p1 → 2 linhas em PaperMeSHTerm para p1
        make_mesh_term(self.p1, 'Diabetes Mellitus', qualifier='genetics', is_major_topic=True)

        item = self._get_descriptor()
        self.assertIsNotNone(item)
        # Apesar de 2 linhas para p1, única_citations_total ainda é 3 (distinct)
        self.assertEqual(item['unique_citations_total'], 3,
                         'Fan-out: múltiplos qualifiers no p1 não devem inflar total.')
        self.assertEqual(item['unique_citations_included'], 2,
                         'Fan-out: múltiplos qualifiers no p1 não devem inflar included.')

    def test_fan_out_not_inflating_major_topic_count(self):
        """
        Mesmo paper com 2 linhas PaperMeSHTerm (is_major_topic=True em ambas):
        major_topic_count deve continuar 1, não 2.
        """
        # p1 já tem qualifier='' is_major_topic=True; adicionar outro qualifier major=True
        make_mesh_term(self.p1, 'Diabetes Mellitus', qualifier='genetics', is_major_topic=True)

        item = self._get_descriptor()
        self.assertIsNotNone(item)
        self.assertEqual(item['major_topic_count'], 1,
                         'Fan-out: múltiplos qualifiers major=True no mesmo paper não devem duplicar major_topic_count.')

    def test_ncbi_mesh_url_present_and_correct(self):
        """ncbi_mesh_url deve estar presente e apontar para NCBI MeSH."""
        item = self._get_descriptor()
        self.assertIsNotNone(item)
        self.assertIn('ncbi_mesh_url', item)
        self.assertIn('ncbi.nlm.nih.gov/mesh', item['ncbi_mesh_url'])
        # Descriptor multi-palavra deve ser URL-encoded
        self.assertIn('Diabetes', item['ncbi_mesh_url'])

    def test_descriptor_with_zero_included_shows_major_topic_count_zero(self):
        """
        Se todos os papers de um descriptor são pending,
        major_topic_count=0 e unique_citations_included=0.
        """
        p4 = make_paper(pmid=10004, abstract='Neoplasms only pending.')
        make_project_paper(self.project, p4, curation_status='pending')
        make_mesh_term(p4, 'Neoplasms', qualifier='', is_major_topic=True)

        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']
        neo = next((r for r in results if r['descriptor'] == 'Neoplasms'), None)
        self.assertIsNotNone(neo)
        self.assertEqual(neo['major_topic_count'], 0)
        self.assertEqual(neo['unique_citations_included'], 0)
        self.assertEqual(neo['unique_citations_total'], 1)


# =============================================================================
# 2. Testes de isolamento por usuário
# =============================================================================

class MeSHUserIsolationTests(APITestCase):
    """
    Usuário B não pode acessar MeSH do projeto de usuário A.
    Skill: firebase-auth-guard — _get_project() filtra por request.user.
    """

    def setUp(self):
        self.user_a = make_user('mesh_iso_user_a')
        self.user_b = make_user('mesh_iso_user_b')

        self.project_a = make_project(self.user_a, title='Projeto MeSH A')
        paper = make_paper(pmid=30001, abstract='Diabetes Mellitus study.')
        make_project_paper(self.project_a, paper, curation_status='included')
        make_mesh_term(paper, 'Diabetes Mellitus', qualifier='', is_major_topic=True)

        # Autenticar como usuário B
        self.client.force_authenticate(user=self.user_b)

    def test_user_b_cannot_list_mesh_of_user_a_project(self):
        """Usuário B obtém 404 ao listar MeSH do projeto de A."""
        resp = self.client.get(mesh_list_url(self.project_a.id))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_user_b_cannot_access_mesh_detail_of_user_a_project(self):
        """Usuário B obtém 404 ao acessar detalhe de descriptor do projeto de A."""
        resp = self.client.get(mesh_detail_url(self.project_a.id, 'Diabetes Mellitus'))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_unauthenticated_cannot_list_mesh(self):
        """Requisição sem autenticação deve retornar 403 ou 401."""
        client = APIClient()
        resp = client.get(mesh_list_url(self.project_a.id))
        self.assertIn(resp.status_code, [
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        ])

    def test_unauthenticated_cannot_access_mesh_detail(self):
        """Requisição de detalhe sem autenticação deve retornar 403 ou 401."""
        client = APIClient()
        resp = client.get(mesh_detail_url(self.project_a.id, 'Diabetes Mellitus'))
        self.assertIn(resp.status_code, [
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        ])

    def test_user_a_can_see_own_project_mesh(self):
        """Usuário A vê seus próprios termos MeSH sem problema."""
        self.client.force_authenticate(user=self.user_a)
        resp = self.client.get(mesh_list_url(self.project_a.id))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['count'], 1)


# =============================================================================
# 3. Testes de filtros e ordenação
# =============================================================================

class MeSHFilterOrderingTests(APITestCase):
    """
    Filtro ?q= por descriptor (icontains), ?ordering= nos 4 campos,
    default '-major_topic_count', paginação.
    """

    def setUp(self):
        self.user = make_user('mesh_filter_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='MeSH Filtros')
        self.url = mesh_list_url(self.project.id)

        # Criar papers e termos MeSH para montar cenário
        # "Diabetes Mellitus": 2 included (1 major topic) → major_topic_count=1, included=2, total=3
        self.p1 = make_paper(pmid=40001, abstract='Diabetes study.')
        self.p2 = make_paper(pmid=40002, abstract='Diabetes levels.')
        self.p3 = make_paper(pmid=40003, abstract='Diabetes pending.')

        make_project_paper(self.project, self.p1, curation_status='included')
        make_project_paper(self.project, self.p2, curation_status='included')
        make_project_paper(self.project, self.p3, curation_status='pending')

        make_mesh_term(self.p1, 'Diabetes Mellitus', qualifier='', is_major_topic=True)
        make_mesh_term(self.p2, 'Diabetes Mellitus', qualifier='epidemiology', is_major_topic=False)
        make_mesh_term(self.p3, 'Diabetes Mellitus', qualifier='therapy', is_major_topic=False)

        # "Neoplasms": 1 included (1 major topic) → major_topic_count=1, included=1, total=1
        self.p4 = make_paper(pmid=40004, abstract='Neoplasms study.')
        make_project_paper(self.project, self.p4, curation_status='included')
        make_mesh_term(self.p4, 'Neoplasms', qualifier='', is_major_topic=True)

        # "Inflammation": 0 included (1 pending), major_topic_count=0 → included=0, total=1
        self.p5 = make_paper(pmid=40005, abstract='Inflammation pending.')
        make_project_paper(self.project, self.p5, curation_status='pending')
        make_mesh_term(self.p5, 'Inflammation', qualifier='', is_major_topic=False)

    def test_filter_q_by_descriptor(self):
        """?q=Diabetes filtra por descriptor (icontains)."""
        resp = self.client.get(self.url, {'q': 'Diabetes'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        descriptors = [r['descriptor'] for r in resp.data['results']]
        self.assertIn('Diabetes Mellitus', descriptors)
        self.assertNotIn('Neoplasms', descriptors)
        self.assertNotIn('Inflammation', descriptors)

    def test_filter_q_case_insensitive(self):
        """?q=diabetes (lowercase) também funciona."""
        resp = self.client.get(self.url, {'q': 'diabetes'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        descriptors = [r['descriptor'] for r in resp.data['results']]
        self.assertIn('Diabetes Mellitus', descriptors)

    def test_filter_q_partial_match(self):
        """?q=neo retorna Neoplasms."""
        resp = self.client.get(self.url, {'q': 'neo'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        descriptors = [r['descriptor'] for r in resp.data['results']]
        self.assertIn('Neoplasms', descriptors)
        self.assertNotIn('Diabetes Mellitus', descriptors)

    def test_filter_q_multi_word_descriptor(self):
        """?q=Diabetes+Mellitus retorna o descriptor multi-palavra."""
        resp = self.client.get(self.url, {'q': 'Diabetes Mellitus'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        descriptors = [r['descriptor'] for r in resp.data['results']]
        self.assertIn('Diabetes Mellitus', descriptors)
        self.assertEqual(len(descriptors), 1)

    def test_default_ordering_is_major_topic_count_desc(self):
        """Sem ?ordering=, o default é -major_topic_count."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']
        major_counts = [r['major_topic_count'] for r in results]
        # Deve estar em ordem decrescente
        self.assertEqual(major_counts, sorted(major_counts, reverse=True))

    def test_ordering_descriptor_asc(self):
        """?ordering=descriptor ordena A-Z."""
        resp = self.client.get(self.url, {'ordering': 'descriptor'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        descs = [r['descriptor'] for r in resp.data['results']]
        self.assertEqual(descs, sorted(descs))

    def test_ordering_descriptor_desc(self):
        """?ordering=-descriptor ordena Z-A."""
        resp = self.client.get(self.url, {'ordering': '-descriptor'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        descs = [r['descriptor'] for r in resp.data['results']]
        self.assertEqual(descs, sorted(descs, reverse=True))

    def test_ordering_unique_citations_included_asc(self):
        """?ordering=unique_citations_included ordena crescente."""
        resp = self.client.get(self.url, {'ordering': 'unique_citations_included'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        values = [r['unique_citations_included'] for r in resp.data['results']]
        self.assertEqual(values, sorted(values))

    def test_ordering_unique_citations_total_desc(self):
        """?ordering=-unique_citations_total ordena decrescente."""
        resp = self.client.get(self.url, {'ordering': '-unique_citations_total'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        values = [r['unique_citations_total'] for r in resp.data['results']]
        self.assertEqual(values, sorted(values, reverse=True))
        # Diabetes Mellitus (total=3) deve vir primeiro
        self.assertEqual(resp.data['results'][0]['descriptor'], 'Diabetes Mellitus')

    def test_ordering_major_topic_count_asc(self):
        """?ordering=major_topic_count ordena crescente."""
        resp = self.client.get(self.url, {'ordering': 'major_topic_count'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        values = [r['major_topic_count'] for r in resp.data['results']]
        self.assertEqual(values, sorted(values))

    def test_invalid_ordering_falls_back_to_default(self):
        """?ordering=campo_invalido cai no default (-major_topic_count), sem erro."""
        resp = self.client.get(self.url, {'ordering': 'campo_invalido'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('results', resp.data)

    def test_pagination_page_size(self):
        """?page_size=1 retorna 1 resultado com next link."""
        resp = self.client.get(self.url, {'page_size': 1})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data['results']), 1)
        self.assertIsNotNone(resp.data['next'])
        self.assertEqual(resp.data['count'], 3)

    def test_pagination_second_page(self):
        """?page=2&page_size=2 retorna o terceiro descriptor."""
        resp = self.client.get(self.url, {'page': 2, 'page_size': 2})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data['results']), 1)
        self.assertIsNone(resp.data['next'])


# =============================================================================
# 4. Testes do filtro ?included_only=
# =============================================================================

class MeSHIncludedOnlyFilterTests(APITestCase):
    """
    Filtro ?included_only=true na lista GET /projects/{project_pk}/mesh/.

    Caso decisivo:
      - Descriptor A (Inflammation): citado apenas em papers pending.
      - Descriptor B (Diabetes Mellitus): citado em ao menos um paper included.
      - Sem filtro → ambos aparecem.
      - Com ?included_only=true → apenas Diabetes Mellitus aparece.
      - As contagens (included | total | major_topic_count) não mudam.

    Cobre também:
      - Valores equivalentes: true/1 ativam; false/0/ausente não ativam.
      - Composição com ?ordering= e paginação.
    """

    def setUp(self):
        self.user = make_user('mesh_incl_filter_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='MeSH IncludedOnly Tests')
        self.url = mesh_list_url(self.project.id)

        # Descriptor A (Inflammation): só em paper pending — zero included
        self.p_pending = make_paper(pmid=50001, abstract='Inflammation is studied.')
        self.p_maybe   = make_paper(pmid=50002, abstract='Inflammation detected.')
        make_project_paper(self.project, self.p_pending, curation_status='pending')
        make_project_paper(self.project, self.p_maybe,   curation_status='maybe')
        make_mesh_term(self.p_pending, 'Inflammation', qualifier='', is_major_topic=True)
        make_mesh_term(self.p_maybe,   'Inflammation', qualifier='', is_major_topic=False)

        # Descriptor B (Diabetes Mellitus): 1 included (major), 1 pending
        self.p_incl  = make_paper(pmid=50003, abstract='Diabetes Mellitus major.')
        self.p_pend2 = make_paper(pmid=50004, abstract='Diabetes Mellitus studied.')
        make_project_paper(self.project, self.p_incl,  curation_status='included')
        make_project_paper(self.project, self.p_pend2, curation_status='pending')
        make_mesh_term(self.p_incl,  'Diabetes Mellitus', qualifier='', is_major_topic=True)
        make_mesh_term(self.p_pend2, 'Diabetes Mellitus', qualifier='epidemiology', is_major_topic=False)

    def test_without_filter_both_descriptors_present(self):
        """Sem ?included_only, ambos os descriptors aparecem."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        descs = {r['descriptor'] for r in resp.data['results']}
        self.assertIn('Inflammation',     descs)
        self.assertIn('Diabetes Mellitus', descs)

    def test_included_only_true_excludes_descriptor_with_zero_included(self):
        """?included_only=true remove Inflammation (zero included) e mantém Diabetes Mellitus."""
        resp = self.client.get(self.url, {'included_only': 'true'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        descs = {r['descriptor'] for r in resp.data['results']}
        self.assertNotIn('Inflammation',     descs)
        self.assertIn('Diabetes Mellitus', descs)

    def test_included_only_one_equivalent_to_true(self):
        """?included_only=1 é equivalente a true."""
        resp = self.client.get(self.url, {'included_only': '1'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        descs = {r['descriptor'] for r in resp.data['results']}
        self.assertNotIn('Inflammation', descs)
        self.assertIn('Diabetes Mellitus', descs)

    def test_included_only_false_does_not_filter(self):
        """?included_only=false não aplica filtro."""
        resp = self.client.get(self.url, {'included_only': 'false'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        descs = {r['descriptor'] for r in resp.data['results']}
        self.assertIn('Inflammation',     descs)
        self.assertIn('Diabetes Mellitus', descs)

    def test_included_only_zero_does_not_filter(self):
        """?included_only=0 não aplica filtro."""
        resp = self.client.get(self.url, {'included_only': '0'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        descs = {r['descriptor'] for r in resp.data['results']}
        self.assertIn('Inflammation', descs)

    def test_default_no_param_same_as_false(self):
        """Sem o parâmetro, comportamento idêntico a included_only=false."""
        resp_default  = self.client.get(self.url)
        resp_explicit = self.client.get(self.url, {'included_only': 'false'})
        self.assertEqual(resp_default.data['count'], resp_explicit.data['count'])

    def test_included_only_does_not_alter_counts_of_remaining_descriptor(self):
        """
        Com ?included_only=true, os campos de Diabetes Mellitus são idênticos
        aos retornados sem o filtro. O filtro não altera contagens.
        """
        resp_all  = self.client.get(self.url)
        resp_incl = self.client.get(self.url, {'included_only': 'true'})

        dm_all  = next(r for r in resp_all.data['results']  if r['descriptor'] == 'Diabetes Mellitus')
        dm_incl = next(r for r in resp_incl.data['results'] if r['descriptor'] == 'Diabetes Mellitus')

        self.assertEqual(dm_all['major_topic_count'],          dm_incl['major_topic_count'])
        self.assertEqual(dm_all['unique_citations_included'],  dm_incl['unique_citations_included'])
        self.assertEqual(dm_all['unique_citations_total'],     dm_incl['unique_citations_total'])

    def test_included_only_correct_counts_for_diabetes_mellitus(self):
        """
        Com ?included_only=true, Diabetes Mellitus mantém:
        major_topic_count=1, unique_citations_included=1, unique_citations_total=2.
        """
        resp = self.client.get(self.url, {'included_only': 'true'})
        dm = next(r for r in resp.data['results'] if r['descriptor'] == 'Diabetes Mellitus')
        self.assertEqual(dm['major_topic_count'],         1)
        self.assertEqual(dm['unique_citations_included'], 1)
        self.assertEqual(dm['unique_citations_total'],    2)

    def test_included_only_composes_with_ordering_descriptor_asc(self):
        """?included_only=true&ordering=descriptor retorna só included, em ordem A-Z."""
        # Adicionar terceiro descriptor included para ter mais resultados
        p_extra = make_paper(pmid=50005, abstract='Neoplasms study included.')
        make_project_paper(self.project, p_extra, curation_status='included')
        make_mesh_term(p_extra, 'Neoplasms', qualifier='', is_major_topic=False)

        resp = self.client.get(self.url, {'included_only': 'true', 'ordering': 'descriptor'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        descs = [r['descriptor'] for r in resp.data['results']]
        self.assertNotIn('Inflammation', descs)
        self.assertIn('Diabetes Mellitus', descs)
        self.assertIn('Neoplasms', descs)
        self.assertEqual(descs, sorted(descs))

    def test_included_only_composes_with_pagination(self):
        """?included_only=true&page_size=1: count reflete apenas descriptors com included>0."""
        resp = self.client.get(self.url, {'included_only': 'true', 'page_size': 1})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # Diabetes Mellitus é o único com included>0 no setUp
        self.assertEqual(resp.data['count'], 1)
        self.assertEqual(len(resp.data['results']), 1)
        self.assertIsNone(resp.data['next'])

    def test_included_only_true_result_count_less_than_without(self):
        """resp.data['count'] com ?included_only=true é menor que sem o filtro."""
        resp_all  = self.client.get(self.url)
        resp_incl = self.client.get(self.url, {'included_only': 'true'})
        self.assertGreater(resp_all.data['count'], resp_incl.data['count'])


# =============================================================================
# 5. Testes de detalhe de descriptor
# =============================================================================

class MeSHDetailTests(APITestCase):
    """
    Detalhe de descriptor: cache quente (ready), cache frio/stale (computing),
    qualifiers, descriptor multi-palavra, 404.
    """

    def setUp(self):
        self.user = make_user('mesh_detail_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='MeSH Detalhe')

        self.paper = make_paper(
            pmid=60001,
            abstract=(
                'Diabetes Mellitus was studied in the population. '
                'The incidence of Diabetes Mellitus increased significantly.'
            ),
            pub_year=2022,
        )
        make_project_paper(self.project, self.paper, curation_status='included')
        make_mesh_term(self.paper, 'Diabetes Mellitus', qualifier='epidemiology', is_major_topic=True)

    def _detail_url(self, descriptor='Diabetes Mellitus'):
        return mesh_detail_url(self.project.id, descriptor)

    # --- Cache quente (ready) ---

    def test_detail_cache_hot_returns_ready(self):
        """Cache populado e fresco → context_status='ready'."""
        now = timezone.now()
        make_entity_context_mesh(self.paper, 'Diabetes Mellitus',
                                 'Diabetes Mellitus was studied in the population.', 0, now)
        make_entity_context_mesh(self.paper, 'Diabetes Mellitus',
                                 'The incidence of Diabetes Mellitus increased significantly.', 1, now)

        resp = self.client.get(self._detail_url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['context_status'], 'ready')

    def test_detail_cache_hot_returns_snippets_in_order(self):
        """Snippets do cache retornados em ordem de sentence_position."""
        now = timezone.now()
        make_entity_context_mesh(self.paper, 'Diabetes Mellitus',
                                 'Diabetes Mellitus was studied in the population.', 0, now)
        make_entity_context_mesh(self.paper, 'Diabetes Mellitus',
                                 'The incidence of Diabetes Mellitus increased significantly.', 1, now)

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
        make_entity_context_mesh(self.paper, 'Diabetes Mellitus', 'Snippet.', 0, now)

        resp = self.client.get(self._detail_url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ref = resp.data['references'][0]
        self.assertEqual(ref['pmid'],           60001)
        self.assertEqual(ref['curation_status'], 'included')
        self.assertEqual(ref['pub_year'],        2022)

    def test_detail_reference_is_major_topic(self):
        """is_major_topic é True para paper onde o descriptor é tópico principal."""
        now = timezone.now()
        make_entity_context_mesh(self.paper, 'Diabetes Mellitus', 'Snippet.', 0, now)

        resp = self.client.get(self._detail_url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ref = resp.data['references'][0]
        self.assertIn('is_major_topic', ref)
        self.assertTrue(ref['is_major_topic'])

    def test_detail_is_major_topic_false_for_non_major(self):
        """
        Paper onde o descriptor é is_major_topic=False →
        is_major_topic=False no payload da referência.
        """
        p2 = make_paper(pmid=60002, abstract='Diabetes Mellitus non-major.')
        make_project_paper(self.project, p2, curation_status='included')
        make_mesh_term(p2, 'Diabetes Mellitus', qualifier='therapy', is_major_topic=False)

        now = timezone.now()
        make_entity_context_mesh(self.paper, 'Diabetes Mellitus', 'Snippet.', 0, now)
        make_entity_context_mesh(p2,         'Diabetes Mellitus', 'Snippet non-major.', 0, now)

        resp = self.client.get(self._detail_url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        refs_by_pmid = {r['pmid']: r for r in resp.data['references']}
        self.assertTrue(refs_by_pmid[60001]['is_major_topic'])
        self.assertFalse(refs_by_pmid[60002]['is_major_topic'])

    def test_detail_qualifiers_distinct_non_empty(self):
        """qualifiers[] contém qualifiers distintos não-vazios do projeto para o descriptor."""
        p2 = make_paper(pmid=60003, abstract='Diabetes Mellitus therapy.')
        p3 = make_paper(pmid=60004, abstract='Diabetes Mellitus genetics.')
        make_project_paper(self.project, p2, curation_status='included')
        make_project_paper(self.project, p3, curation_status='pending')
        make_mesh_term(p2, 'Diabetes Mellitus', qualifier='therapy',   is_major_topic=False)
        make_mesh_term(p3, 'Diabetes Mellitus', qualifier='genetics',  is_major_topic=False)
        # Qualifer vazio no papel original (setUp) não deve aparecer
        make_mesh_term(self.paper, 'Diabetes Mellitus', qualifier='', is_major_topic=False)

        now = timezone.now()
        make_entity_context_mesh(self.paper, 'Diabetes Mellitus', 'S.', 0, now)
        make_entity_context_mesh(p2,         'Diabetes Mellitus', 'S.', 0, now)
        make_entity_context_mesh(p3,         'Diabetes Mellitus', 'S.', 0, now)

        resp = self.client.get(self._detail_url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        qualifiers = resp.data['qualifiers']
        self.assertIn('epidemiology', qualifiers)  # do setUp
        self.assertIn('therapy',      qualifiers)
        self.assertIn('genetics',     qualifiers)
        # Qualifier vazio não deve aparecer
        self.assertNotIn('', qualifiers)
        # Sem duplicatas
        self.assertEqual(len(qualifiers), len(set(qualifiers)))

    def test_detail_references_include_all_curation_statuses(self):
        """Lista de referências inclui papers de todos os status de curadoria."""
        p_pend = make_paper(pmid=60005, abstract='Diabetes Mellitus pending.')
        make_project_paper(self.project, p_pend, curation_status='pending')
        make_mesh_term(p_pend, 'Diabetes Mellitus', qualifier='', is_major_topic=False)

        now = timezone.now()
        make_entity_context_mesh(self.paper, 'Diabetes Mellitus', 'S1.', 0, now)
        make_entity_context_mesh(p_pend,     'Diabetes Mellitus', 'S2.', 0, now)

        resp = self.client.get(self._detail_url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        statuses = {r['curation_status'] for r in resp.data['references']}
        self.assertIn('included', statuses)
        self.assertIn('pending',  statuses)

    def test_detail_aggregated_metrics_major_topic_count(self):
        """Detalhe retorna major_topic_count correto."""
        now = timezone.now()
        make_entity_context_mesh(self.paper, 'Diabetes Mellitus', 'S.', 0, now)

        resp = self.client.get(self._detail_url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['major_topic_count'],         1)
        self.assertEqual(resp.data['unique_citations_included'], 1)
        self.assertEqual(resp.data['unique_citations_total'],    1)

    # --- Cache frio (computing) ---

    def test_detail_cache_cold_returns_computing(self):
        """Sem EntityContext para o paper → context_status='computing'."""
        with patch('apps.core.tasks.mesh_tasks.derive_mesh_contexts.delay') as mock_task:
            resp = self.client.get(self._detail_url())

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['context_status'], 'computing')
        mock_task.assert_called_once_with(str(self.project.id), 'Diabetes Mellitus')

    def test_detail_cache_cold_dispatches_task_once(self):
        """Cache frio dispara a task Celery exatamente uma vez."""
        with patch('apps.core.tasks.mesh_tasks.derive_mesh_contexts.delay') as mock_task:
            self.client.get(self._detail_url())
        mock_task.assert_called_once()

    # --- Cache stale (computing) ---

    def test_detail_cache_stale_returns_computing(self):
        """Cache com computed_at anterior ao paper.updated_at → context_status='computing'."""
        stale_time = timezone.now() - timedelta(hours=1)
        make_entity_context_mesh(self.paper, 'Diabetes Mellitus', 'Old snippet.', 0, stale_time)

        # Simula re-ingestão: forçar updated_at do paper para após o computed_at
        Paper.objects.filter(pk=self.paper.pk).update(updated_at=timezone.now())
        self.paper.refresh_from_db()

        with patch('apps.core.tasks.mesh_tasks.derive_mesh_contexts.delay') as mock_task:
            resp = self.client.get(self._detail_url())

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['context_status'], 'computing')
        mock_task.assert_called_once()

    # --- 404 para descriptor inexistente ---

    def test_detail_nonexistent_descriptor_returns_404(self):
        """Descriptor não associado a nenhum paper do projeto → 404."""
        resp = self.client.get(mesh_detail_url(self.project.id, 'DescriptorXXXNotFound'))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    # --- Descriptor multi-palavra com espaço no path ---

    def test_detail_multi_word_descriptor_with_space_resolves_correctly(self):
        """
        Descriptor multi-palavra com espaço ('Diabetes Mellitus') resolve corretamente
        via URL path com encodeURIComponent. O servidor decodifica e encontra o descriptor.
        """
        now = timezone.now()
        make_entity_context_mesh(self.paper, 'Diabetes Mellitus', 'Snippet.', 0, now)

        resp = self.client.get(self._detail_url('Diabetes Mellitus'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['descriptor'], 'Diabetes Mellitus')

    def test_detail_descriptor_too_long_returns_404(self):
        """Descriptor com mais de 255 chars → 404 imediato."""
        long_desc = 'A' * (MESH_DESCRIPTOR_MAX_LEN + 1)
        resp = self.client.get(mesh_detail_url(self.project.id, long_desc))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_detail_ncbi_mesh_url_in_detail_response(self):
        """ncbi_mesh_url está presente no payload de detalhe."""
        now = timezone.now()
        make_entity_context_mesh(self.paper, 'Diabetes Mellitus', 'Snippet.', 0, now)

        resp = self.client.get(self._detail_url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('ncbi_mesh_url', resp.data)
        self.assertIn('ncbi.nlm.nih.gov/mesh', resp.data['ncbi_mesh_url'])


# =============================================================================
# 6. Testes de regressão — project_paper_id vs Paper.pk (mismatch)
#
# Espelha exatamente a estrutura de GeneDetailPatchCurationTests.
# Papers fantasma criam gap no auto-increment de Paper.pk para garantir
# que ProjectPaper.pk != Paper.pk (trava de regressão do bug crítico do 007).
# =============================================================================

class MeSHDetailReferenceIdTests(APITestCase):
    """
    Garante que cada item de references[] no detalhe de descriptor MeSH carrega:
      - project_paper_id: PK de ProjectPaper (usada no PATCH /projects/{id}/papers/<pk>/).
      - NÃO contém o campo 'id' (Paper.pk — proibido: trava de regressão).

    O front usa project_paper_id para o toggle de curadoria.
    """

    def setUp(self):
        self.user = make_user('mesh_ref_id_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='MeSH RefID Tests')

        # Papers "fantasma" para avançar auto-increment de Paper.pk
        # → Paper.pk dos nossos papers de teste será alto,
        # enquanto ProjectPaper.pk parte de contador independente.
        for i in range(5):
            Paper.objects.create(
                pmid=29900 + i,
                title=f'MeSH Fantasma {i}',
                journal='Ghost',
                pub_year=2000,
                abstract='',
            )

        # Papers reais do teste
        self.paper_a = make_paper(
            pmid=29910, title='Diabetes study A', pub_year=2023,
            abstract='Diabetes Mellitus was studied.',
        )
        self.paper_b = make_paper(
            pmid=29911, title='Diabetes study B', pub_year=2022,
            abstract='Diabetes Mellitus is a metabolic disease.',
        )

        self.pp_a = make_project_paper(self.project, self.paper_a, curation_status='included')
        self.pp_b = make_project_paper(self.project, self.paper_b, curation_status='pending')

        make_mesh_term(self.paper_a, 'Diabetes Mellitus', qualifier='',            is_major_topic=True)
        make_mesh_term(self.paper_b, 'Diabetes Mellitus', qualifier='epidemiology', is_major_topic=False)

        # Popular EntityContext para context_status='ready'
        now = timezone.now()
        make_entity_context_mesh(self.paper_a, 'Diabetes Mellitus',
                                 'Diabetes Mellitus was studied.', 0, now)
        make_entity_context_mesh(self.paper_b, 'Diabetes Mellitus',
                                 'Diabetes Mellitus is a metabolic disease.', 0, now)

    def _detail(self):
        return self.client.get(mesh_detail_url(self.project.id, 'Diabetes Mellitus'))

    # --- Divergência forçada de PKs ---

    def test_paper_pk_and_project_paper_pk_differ(self):
        """
        Garante que Paper.pk != ProjectPaper.pk para ao menos um paper.
        Se falhar, os papers fantasma não estão criando o gap necessário.
        """
        diverge_a = self.paper_a.pk != self.pp_a.pk
        diverge_b = self.paper_b.pk != self.pp_b.pk
        self.assertTrue(
            diverge_a or diverge_b,
            f'Paper.pk e ProjectPaper.pk coincidem em ambos os pares — '
            f'paper_a.pk={self.paper_a.pk} pp_a.pk={self.pp_a.pk}; '
            f'paper_b.pk={self.paper_b.pk} pp_b.pk={self.pp_b.pk}. '
            f'A trava de regressão seria ineficaz.',
        )

    # --- project_paper_id presente e correto ---

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
        Campo 'id' (Paper.pk) não deve aparecer em nenhum item de references[].
        Trava de regressão: 'id' foi removido para evitar mismatch no PATCH.
        """
        resp = self._detail()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        for ref in resp.data['references']:
            self.assertNotIn(
                'id',
                ref,
                f'Campo id não deve existir em references[] (trava de regressão 007); '
                f'pmid={ref.get("pmid")}.',
            )

    def test_reference_project_paper_id_matches_project_paper_pk(self):
        """project_paper_id corresponde à PK de ProjectPaper, não à de Paper."""
        resp = self._detail()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        refs_by_pmid = {r['pmid']: r for r in resp.data['references']}

        self.assertEqual(
            refs_by_pmid[29910]['project_paper_id'],
            self.pp_a.pk,
            'project_paper_id deve ser ProjectPaper.pk, não Paper.pk.',
        )
        self.assertEqual(
            refs_by_pmid[29911]['project_paper_id'],
            self.pp_b.pk,
            'project_paper_id deve ser ProjectPaper.pk, não Paper.pk.',
        )

    def test_reference_project_paper_id_not_equal_to_paper_pk(self):
        """
        Trava do bug original: project_paper_id NÃO deve ser igual ao Paper.pk.
        Com papers fantasma, ao menos um par diverge.
        """
        resp = self._detail()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        refs_by_pmid = {r['pmid']: r for r in resp.data['references']}

        mismatch_a = refs_by_pmid[29910]['project_paper_id'] != self.paper_a.pk
        mismatch_b = refs_by_pmid[29911]['project_paper_id'] != self.paper_b.pk
        self.assertTrue(
            mismatch_a or mismatch_b,
            f'project_paper_id coincide com Paper.pk em ambos os pares — '
            f'paper_a.pk={self.paper_a.pk} pp_a.pk={self.pp_a.pk}; '
            f'paper_b.pk={self.paper_b.pk} pp_b.pk={self.pp_b.pk}.',
        )

    def test_reference_project_paper_id_is_not_pmid(self):
        """project_paper_id e pmid são campos distintos."""
        resp = self._detail()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        for ref in resp.data['references']:
            self.assertIsInstance(ref['project_paper_id'], int)
            self.assertIsInstance(ref['pmid'], int)
            self.assertNotEqual(
                ref['project_paper_id'],
                ref['pmid'],
                f'project_paper_id={ref["project_paper_id"]} e pmid={ref["pmid"]} '
                f'não devem ser iguais — campos distintos.',
            )

    def test_project_paper_id_and_curation_status_present_in_computing_state(self):
        """
        Mesmo com context_status='computing' (cache frio), project_paper_id e
        curation_status estão presentes em references[].
        O front precisa deles para o toggle de curadoria, independente do cache.
        """
        user2   = make_user('mesh_ref_computing_user')
        proj2   = make_project(user2, title='MeSH Computing State')
        paper3  = make_paper(pmid=29920, title='Cold cache DM', pub_year=2024,
                             abstract='Diabetes Mellitus cold.')
        pp3     = make_project_paper(proj2, paper3, curation_status='included')
        make_mesh_term(paper3, 'Diabetes Mellitus', qualifier='', is_major_topic=True)

        self.client.force_authenticate(user=user2)
        with patch('apps.core.tasks.mesh_tasks.derive_mesh_contexts.delay'):
            resp = self.client.get(mesh_detail_url(proj2.id, 'Diabetes Mellitus'))

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['context_status'], 'computing')

        refs = resp.data['references']
        self.assertEqual(len(refs), 1)
        self.assertIn('project_paper_id', refs[0])
        self.assertEqual(refs[0]['project_paper_id'], pp3.pk)
        self.assertIn('curation_status', refs[0])
        self.assertEqual(refs[0]['curation_status'], 'included')
        self.assertNotIn('id', refs[0])


# =============================================================================
# 7. Testes da task de contexto (idempotência, regex multi-palavra, sentinela, invalidação)
# =============================================================================

class MeSHContextTaskTests(APITestCase):
    """
    Testes da lógica de derive_and_persist_contexts e extract_mesh_sentences.

    A task Celery é chamada diretamente via MeshService para não depender do broker.
    """

    def setUp(self):
        self.user = make_user('mesh_task_user')
        self.project = make_project(self.user, title='MeSH Task Tests')

        # Abstract com descriptor multi-palavra "Diabetes Mellitus" em 2 sentenças
        # e substring "Diabetes" em outra (não deve casar como frase completa)
        self.abstract = (
            'Diabetes Mellitus was studied in the population. '
            'The incidence of Diabetes Mellitus increased significantly. '
            'Diabetes without Mellitus is a different condition.'
        )
        self.paper = make_paper(
            pmid=70001,
            abstract=self.abstract,
            pub_year=2021,
        )
        make_project_paper(self.project, self.paper, curation_status='included')
        make_mesh_term(self.paper, 'Diabetes Mellitus', qualifier='', is_major_topic=True)

    # --- Idempotência ---

    def test_derive_twice_does_not_duplicate(self):
        """Rodar derive_and_persist_contexts duas vezes não duplica EntityContext."""
        MeshService.derive_and_persist_contexts(self.project, 'Diabetes Mellitus')
        MeshService.derive_and_persist_contexts(self.project, 'Diabetes Mellitus')

        count = EntityContext.objects.filter(
            paper=self.paper,
            entity_type=EntityContext.EntityType.MESH,
            entity_name='Diabetes Mellitus',
        ).count()
        # Deve existir apenas o número de sentenças únicas (não o dobro)
        snippets = MeshService.extract_mesh_sentences(self.paper, 'Diabetes Mellitus')
        self.assertEqual(count, len(snippets))

    def test_derive_returns_correct_snippet_count(self):
        """Retorna o número correto de snippets reais derivados."""
        n = MeshService.derive_and_persist_contexts(self.project, 'Diabetes Mellitus')
        # Abstract tem 3 sentenças; as 2 primeiras contêm 'Diabetes Mellitus' (frase completa)
        # A terceira tem 'Diabetes' e 'Mellitus' separados — NÃO deve casar com \b...\b
        self.assertEqual(n, 2)

    # --- Regex multi-palavra e fronteira de palavra ---

    def test_multi_word_descriptor_matches_exact_phrase(self):
        """
        'Diabetes Mellitus' com \b casa a frase completa (multi-palavra),
        não palavras individuais.
        """
        snippets = MeshService.extract_mesh_sentences(self.paper, 'Diabetes Mellitus')
        sentences = [s['sentence'] for s in snippets]

        self.assertEqual(len(snippets), 2)
        self.assertIn('Diabetes Mellitus was studied in the population.', sentences)
        self.assertIn('The incidence of Diabetes Mellitus increased significantly.', sentences)

    def test_multi_word_descriptor_does_not_match_partial_words(self):
        """
        A sentença 'Diabetes without Mellitus...' contém as palavras individuais
        mas NÃO a frase 'Diabetes Mellitus' — o regex multi-palavra não deve casá-la.
        """
        snippets = MeshService.extract_mesh_sentences(self.paper, 'Diabetes Mellitus')
        sentences = [s['sentence'] for s in snippets]

        # A terceira sentença NÃO deve aparecer como snippet de 'Diabetes Mellitus'
        self.assertNotIn('Diabetes without Mellitus is a different condition.', sentences)

    def test_case_insensitive_match(self):
        """Regex é case-insensitive: 'DIABETES MELLITUS' deve ser encontrado."""
        paper_upper = make_paper(
            pmid=70002,
            abstract='DIABETES MELLITUS was elevated. Another sentence.',
        )
        make_project_paper(self.project, paper_upper, curation_status='included')
        make_mesh_term(paper_upper, 'Diabetes Mellitus', qualifier='', is_major_topic=False)

        snippets = MeshService.extract_mesh_sentences(paper_upper, 'Diabetes Mellitus')
        self.assertEqual(len(snippets), 1)
        self.assertIn('DIABETES MELLITUS was elevated.', snippets[0]['sentence'])

    def test_empty_abstract_returns_no_snippets(self):
        """Abstract vazio retorna lista vazia."""
        paper_empty = make_paper(pmid=70003, abstract='')
        snippets = MeshService.extract_mesh_sentences(paper_empty, 'Diabetes Mellitus')
        self.assertEqual(snippets, [])

    def test_descriptor_not_in_abstract_returns_empty(self):
        """Descriptor ausente do abstract retorna lista vazia (caso comum em MeSH)."""
        paper_other = make_paper(pmid=70004, abstract='Inflammation and cytokines were elevated.')
        snippets = MeshService.extract_mesh_sentences(paper_other, 'Diabetes Mellitus')
        self.assertEqual(snippets, [])

    def test_sentence_position_zero_based(self):
        """sentence_position é 0-based."""
        snippets = MeshService.extract_mesh_sentences(self.paper, 'Diabetes Mellitus')
        positions = {s['sentence_position'] for s in snippets}
        self.assertIn(0, positions)
        self.assertIn(1, positions)

    # --- Sentinela -1 para descriptor ausente do abstract ---

    def test_descriptor_absent_from_abstract_writes_sentinel(self):
        """
        Quando o descriptor não aparece literalmente no abstract,
        derive_and_persist_contexts grava a sentinela (sentence_position=-1).
        Isso evita loop infinito: context_status fica 'computing' eterno.
        """
        paper_no_match = make_paper(pmid=70005, abstract='Only inflammation here.')
        make_project_paper(self.project, paper_no_match, curation_status='included')
        make_mesh_term(paper_no_match, 'Diabetes Mellitus', qualifier='', is_major_topic=False)

        MeshService.derive_and_persist_contexts(self.project, 'Diabetes Mellitus')

        sentinel = EntityContext.objects.filter(
            paper=paper_no_match,
            entity_type=EntityContext.EntityType.MESH,
            entity_name='Diabetes Mellitus',
            sentence_position=-1,
        )
        self.assertTrue(
            sentinel.exists(),
            'Sentinela (sentence_position=-1) deve ser gravada quando descriptor não aparece no abstract.',
        )

    def test_sentinel_makes_context_status_ready(self):
        """
        Após derivação com sentinela, context_status do detalhe é 'ready' — não 'computing'.

        Regressão: antes não havia sentinela → computed_at=None → loop 'computing' eterno.
        """
        paper_no_match = make_paper(pmid=70006, abstract='Only inflammation here.')
        make_project_paper(self.project, paper_no_match, curation_status='included')
        make_mesh_term(paper_no_match, 'Diabetes Mellitus', qualifier='', is_major_topic=False)

        # Remover o paper original do setUp (tem abstract com o descriptor) para testar só o no-match
        # Criar projeto isolado para este teste
        user2   = make_user('mesh_sentinel_user')
        proj2   = make_project(user2, title='Sentinel Ready Test')
        paper_s = make_paper(pmid=70007, abstract='Cytokines only.')
        make_project_paper(proj2, paper_s, curation_status='included')
        make_mesh_term(paper_s, 'Neoplasms', qualifier='', is_major_topic=False)

        MeshService.derive_and_persist_contexts(proj2, 'Neoplasms')

        client2 = APIClient()
        client2.force_authenticate(user=user2)
        resp = client2.get(mesh_detail_url(proj2.id, 'Neoplasms'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['context_status'], 'ready',
                         'Sentinela gravada → context_status deve ser ready, não computing.')

    def test_sentinel_does_not_leak_as_snippet(self):
        """
        Nenhum snippet na resposta deve ter sentence_position == -1.
        A sentinela é interna; o endpoint nunca a entrega ao cliente.
        """
        user2   = make_user('mesh_sentinel_leak_user')
        proj2   = make_project(user2, title='Sentinel Leak Test')
        paper_s = make_paper(pmid=70008, abstract='Only cytokines here.')
        make_project_paper(proj2, paper_s, curation_status='included')
        make_mesh_term(paper_s, 'Neoplasms', qualifier='', is_major_topic=False)

        MeshService.derive_and_persist_contexts(proj2, 'Neoplasms')

        client2 = APIClient()
        client2.force_authenticate(user=user2)
        resp = client2.get(mesh_detail_url(proj2.id, 'Neoplasms'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        for ref in resp.data.get('references', []):
            for snippet in ref.get('snippets', []):
                self.assertNotEqual(
                    snippet.get('sentence_position'),
                    -1,
                    'Sentinela (sentence_position=-1) não deve vazar como snippet na resposta.',
                )

    def test_empty_abstract_sentinel_makes_ready(self):
        """
        Paper com abstract='' após derivação → context_status='ready', não 'computing'.
        """
        user2   = make_user('mesh_empty_abstract_user')
        proj2   = make_project(user2, title='Empty Abstract Test')
        paper_e = make_paper(pmid=70009, abstract='')
        make_project_paper(proj2, paper_e, curation_status='included')
        make_mesh_term(paper_e, 'Neoplasms', qualifier='', is_major_topic=True)

        MeshService.derive_and_persist_contexts(proj2, 'Neoplasms')

        client2 = APIClient()
        client2.force_authenticate(user=user2)
        resp = client2.get(mesh_detail_url(proj2.id, 'Neoplasms'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['context_status'], 'ready',
                         'Abstract vazio deve resultar em "ready" após derivação.')

    def test_empty_abstract_reference_present_snippets_empty(self):
        """Paper com abstract vazio → referência presente, snippets=[]."""
        user2   = make_user('mesh_empty_snip_user')
        proj2   = make_project(user2, title='Empty Snippets Test')
        paper_e = make_paper(pmid=70010, abstract='')
        make_project_paper(proj2, paper_e, curation_status='included')
        make_mesh_term(paper_e, 'Neoplasms', qualifier='', is_major_topic=True)

        MeshService.derive_and_persist_contexts(proj2, 'Neoplasms')

        client2 = APIClient()
        client2.force_authenticate(user=user2)
        resp = client2.get(mesh_detail_url(proj2.id, 'Neoplasms'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        refs = resp.data['references']
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]['snippets'], [],
                         'Abstract vazio → snippets deve ser [].')

    # --- Idempotência da sentinela ---

    def test_sentinel_not_duplicated_on_double_derive(self):
        """
        Rodar derivação 2x para paper sem descriptor no abstract →
        exatamente 1 linha EntityContext (a sentinela), não 2.
        """
        user2   = make_user('mesh_idem_sentinel_user')
        proj2   = make_project(user2, title='Idempotence Sentinel')
        paper_s = make_paper(pmid=70011, abstract='No mesh term here.')
        make_project_paper(proj2, paper_s, curation_status='included')
        make_mesh_term(paper_s, 'Neoplasms', qualifier='', is_major_topic=False)

        MeshService.derive_and_persist_contexts(proj2, 'Neoplasms')
        MeshService.derive_and_persist_contexts(proj2, 'Neoplasms')

        count = EntityContext.objects.filter(
            paper=paper_s,
            entity_type=EntityContext.EntityType.MESH,
            entity_name='Neoplasms',
        ).count()
        self.assertEqual(count, 1,
                         'Deve existir exatamente 1 linha (sentinela) após 2 derivações.')

    # --- Invalidação após mudança de abstract ---

    def test_invalidation_after_abstract_change(self):
        """
        Após mudar o abstract (updated_at avança), snippets antigos ficam stale
        e são recomputados na segunda derivação.
        """
        # Primeira derivação
        MeshService.derive_and_persist_contexts(self.project, 'Diabetes Mellitus')
        first_count = EntityContext.objects.filter(
            paper=self.paper,
            entity_type=EntityContext.EntityType.MESH,
            entity_name='Diabetes Mellitus',
            sentence_position__gte=0,  # excluir sentinela
        ).count()
        self.assertEqual(first_count, 2)

        # Mudar abstract (simula re-ingestão)
        new_abstract = 'Diabetes Mellitus mentioned only once now.'
        Paper.objects.filter(pk=self.paper.pk).update(
            abstract=new_abstract,
            updated_at=timezone.now(),
        )
        self.paper.refresh_from_db()

        # Segunda derivação (deve limpar stale e recriar)
        MeshService.derive_and_persist_contexts(self.project, 'Diabetes Mellitus')
        second_count = EntityContext.objects.filter(
            paper=self.paper,
            entity_type=EntityContext.EntityType.MESH,
            entity_name='Diabetes Mellitus',
            sentence_position__gte=0,
        ).count()

        new_snippets = MeshService.extract_mesh_sentences(self.paper, 'Diabetes Mellitus')
        self.assertEqual(second_count, len(new_snippets))
        self.assertEqual(second_count, 1)

    def test_derive_persists_computed_at(self):
        """computed_at é gravado em cada EntityContext derivado."""
        before = timezone.now()
        MeshService.derive_and_persist_contexts(self.project, 'Diabetes Mellitus')
        after = timezone.now()

        contexts = EntityContext.objects.filter(
            paper=self.paper,
            entity_type=EntityContext.EntityType.MESH,
            entity_name='Diabetes Mellitus',
        )
        for ctx in contexts:
            self.assertIsNotNone(ctx.computed_at)
            self.assertGreaterEqual(ctx.computed_at, before)
            self.assertLessEqual(ctx.computed_at, after)

    def test_derive_multiple_papers_in_project(self):
        """derive_and_persist_contexts processa todos os papers do projeto para o descriptor."""
        p2 = make_paper(pmid=70012, abstract='Diabetes Mellitus confirmed.')
        make_project_paper(self.project, p2, curation_status='pending')
        make_mesh_term(p2, 'Diabetes Mellitus', qualifier='', is_major_topic=False)

        n = MeshService.derive_and_persist_contexts(self.project, 'Diabetes Mellitus')
        # paper original: 2 snippets; p2: 1 snippet → total 3
        self.assertEqual(n, 3)

        count = EntityContext.objects.filter(
            entity_type=EntityContext.EntityType.MESH,
            entity_name='Diabetes Mellitus',
            sentence_position__gte=0,  # excluir sentinelas
        ).count()
        self.assertEqual(count, 3)

    # --- Integração com a task Celery (síncrona) ---

    def test_celery_task_calls_service(self):
        """
        A Celery task derive_mesh_contexts chama MeshService.derive_and_persist_contexts.
        Testada chamando run() diretamente (sem broker).
        """
        from apps.core.tasks.mesh_tasks import derive_mesh_contexts

        n_before = EntityContext.objects.filter(
            paper=self.paper,
            entity_type=EntityContext.EntityType.MESH,
            entity_name='Diabetes Mellitus',
        ).count()
        self.assertEqual(n_before, 0)

        derive_mesh_contexts.run(str(self.project.id), 'Diabetes Mellitus')

        n_after = EntityContext.objects.filter(
            paper=self.paper,
            entity_type=EntityContext.EntityType.MESH,
            entity_name='Diabetes Mellitus',
        ).count()
        self.assertGreater(n_after, 0)

    def test_celery_task_nonexistent_project_does_not_raise(self):
        """
        Task com project_id inválido deve retornar sem levantar exceção.
        (projeto não encontrado → log warning + return).
        """
        from apps.core.tasks.mesh_tasks import derive_mesh_contexts

        fake_id = str(uuid.uuid4())
        try:
            derive_mesh_contexts.run(fake_id, 'Diabetes Mellitus')
        except Exception as exc:
            self.fail(f'Task levantou exceção inesperada: {exc}')


# =============================================================================
# 8. Testes de lock de disparo (regressão: não reenfileirar task com lock ativo)
# =============================================================================

class MeSHLockDispatchTests(APITestCase):
    """
    Regressão: lock de disparo de task Celery.

    Dois GETs seguidos com cache frio não devem enfileirar a task duas vezes
    enquanto o lock (mesh_derive_lock:{project_id}:{descriptor}) está ativo.
    cache.add() é atômico — apenas o primeiro GET adquire o lock e dispara.
    """

    def setUp(self):
        cache.clear()
        self.user = make_user('mesh_lock_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='MeSH Lock Tests')

        self.paper = make_paper(
            pmid=80001,
            abstract='Diabetes Mellitus was elevated in all samples.',
        )
        make_project_paper(self.project, self.paper, curation_status='included')
        make_mesh_term(self.paper, 'Diabetes Mellitus', qualifier='', is_major_topic=True)

    def tearDown(self):
        cache.clear()

    def test_two_cold_gets_dispatch_task_at_most_once(self):
        """
        Dois GETs consecutivos com cache frio disparam a task no máximo uma vez.
        """
        with patch('apps.core.tasks.mesh_tasks.derive_mesh_contexts.delay') as mock_delay:
            self.client.get(mesh_detail_url(self.project.id, 'Diabetes Mellitus'))
            self.client.get(mesh_detail_url(self.project.id, 'Diabetes Mellitus'))

        self.assertEqual(
            mock_delay.call_count,
            1,
            f'A task deve ser disparada exatamente 1 vez; foi disparada {mock_delay.call_count} vez(es).',
        )

    def test_first_cold_get_dispatches_task(self):
        """O primeiro GET com cache frio deve disparar a task."""
        with patch('apps.core.tasks.mesh_tasks.derive_mesh_contexts.delay') as mock_delay:
            resp = self.client.get(mesh_detail_url(self.project.id, 'Diabetes Mellitus'))

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['context_status'], 'computing')
        mock_delay.assert_called_once_with(str(self.project.id), 'Diabetes Mellitus')

    def test_lock_key_format(self):
        """
        Chave de lock deve seguir o formato 'mesh_derive_lock:{project_id}:{md5_hash}'.
        O descriptor é hasheado (MD5, 16 hex chars) para evitar espaços e caracteres
        especiais que gerariam CacheKeyWarning com Memcached.
        Garante especificidade por (projeto, descriptor) sem colisão entre projetos.
        """
        import hashlib
        descriptor = 'Diabetes Mellitus'
        descriptor_hash = hashlib.md5(descriptor.encode()).hexdigest()[:16]
        expected = f'mesh_derive_lock:{self.project.id}:{descriptor_hash}'
        actual   = _derive_lock_key(str(self.project.id), descriptor)
        self.assertEqual(actual, expected)

    def test_lock_isolates_different_descriptors(self):
        """
        Lock de 'Diabetes Mellitus' não bloqueia disparo de task para 'Neoplasms'.
        """
        paper2 = make_paper(pmid=80002, abstract='Neoplasms were identified.')
        make_project_paper(self.project, paper2, curation_status='included')
        make_mesh_term(paper2, 'Neoplasms', qualifier='', is_major_topic=False)

        with patch('apps.core.tasks.mesh_tasks.derive_mesh_contexts.delay') as mock_delay:
            # Primeiro GET adquire lock para 'Diabetes Mellitus'
            self.client.get(mesh_detail_url(self.project.id, 'Diabetes Mellitus'))
            # Segundo GET mesmo descriptor — lock ativo, não reenfileira
            self.client.get(mesh_detail_url(self.project.id, 'Diabetes Mellitus'))
            # GET para 'Neoplasms' — lock diferente, deve disparar task
            self.client.get(mesh_detail_url(self.project.id, 'Neoplasms'))

        self.assertEqual(
            mock_delay.call_count,
            2,
            'Deve haver 2 disparos: 1 para Diabetes Mellitus + 1 para Neoplasms.',
        )
        calls_descriptors = [call.args[1] for call in mock_delay.call_args_list]
        self.assertIn('Diabetes Mellitus', calls_descriptors)
        self.assertIn('Neoplasms',          calls_descriptors)
