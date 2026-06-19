"""
Testes do endpoint de variantes genéticas do projeto.

Cobre:
  1. Agregação (unique_citations_included vs total vs mention_count_total;
     fan-out de JOIN; variante só em pending → included=0).
  2. Isolamento por usuário (skill firebase-auth-guard): usuário B recebe 404
     ao acessar /variants/ e /variants/<rs>/ de projeto do usuário A.
  3. Anotação clínica — variante COM VariantAnnotation; variante SEM annotation
     → annotation: null sem quebrar (D2: mostrar todas).
  4. Filtros e ordenação (?q=, ?ordering=, paginação, ?included_only=).
  5. top_variants nas stats — formato [{rs_number, count}], agrega mention_count
     via Sum apenas de papers included.
  6. derive_variant_contexts — idempotência, regex com fronteira de palavra,
     marcador sentinela (sentence_position=-1), invalidação após mudança de abstract.
  7. Trava de regressão: lock de disparo (dois GETs frios disparam task 1x).
  8. Validação de rs_number longo (anti-ReDoS): > RS_NUMBER_MAX_LEN → 404.
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
    PaperVariant,
    ProjectPaper,
    VariantAnnotation,
)
from apps.core.services.variant_service import VariantService, RS_NUMBER_MAX_LEN
from apps.core.views.variant_views import _derive_lock_key


# =============================================================================
# Helpers
# =============================================================================

def make_user(username, password='testpass'):
    return User.objects.create_user(username=username, password=password)


def make_project(user, title='Projeto Variantes', query_term='cancer'):
    slug = f'{title.lower().replace(" ", "-")}-{user.username}-var-test'
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


def make_paper_variant(paper, rs_number, mention_count=1):
    return PaperVariant.objects.create(
        paper=paper,
        rs_number=rs_number,
        mention_count=mention_count,
    )


def make_variant_annotation(rs_number, gene_symbol='MTHFR', gene_name='methylenetetrahydrofolate reductase',
                             entrez_id=4524, chromosome='1', position=11856378,
                             alleles='A/G', maf=0.32, clinical_significance='pathogenic'):
    return VariantAnnotation.objects.create(
        rs_number=rs_number,
        gene_symbol=gene_symbol,
        gene_name=gene_name,
        entrez_id=entrez_id,
        chromosome=chromosome,
        position=position,
        alleles=alleles,
        maf=maf,
        clinical_significance=clinical_significance,
    )


def make_entity_context_variant(paper, rs_number, sentence, sentence_position=0, computed_at=None):
    return EntityContext.objects.create(
        paper=paper,
        entity_type=EntityContext.EntityType.VARIANT,
        entity_name=rs_number,
        sentence=sentence,
        sentence_position=sentence_position,
        computed_at=computed_at or timezone.now(),
    )


def variants_url(project_id):
    return f'/api/v1/projects/{project_id}/variants/'


def variant_detail_url(project_id, rs_number):
    return f'/api/v1/projects/{project_id}/variants/{rs_number}/'


# =============================================================================
# 1. Testes de agregação
# =============================================================================

class VariantAggregationTests(APITestCase):
    """
    Verifica unique_citations_included, unique_citations_total e mention_count_total.

    Caso decisivo:
      - rs1801133 citado em 3 papers do projeto:
          paper1 → included
          paper2 → pending
          paper3 → included
      - unique_citations_included deve ser 2 (apenas included deste projeto).
      - unique_citations_total deve ser 3 (todos os status deste projeto).
      - mention_count_total deve somar mention_count dos 3 PaperVariant.
      - Um quarto paper em outro projeto não deve aparecer.
    """

    def setUp(self):
        self.user = make_user('var_agg_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user)
        self.url = variants_url(self.project.id)

        # Papers deste projeto
        self.p1 = make_paper(pmid=11001, abstract='rs1801133 variant was studied.')
        self.p2 = make_paper(pmid=11002, abstract='rs1801133 levels measured.')
        self.p3 = make_paper(pmid=11003, abstract='rs1801133 found elevated.')

        make_project_paper(self.project, self.p1, curation_status='included')
        make_project_paper(self.project, self.p2, curation_status='pending')
        make_project_paper(self.project, self.p3, curation_status='included')

        # PaperVariant com mention_counts diferentes
        make_paper_variant(self.p1, 'rs1801133', mention_count=3)
        make_paper_variant(self.p2, 'rs1801133', mention_count=5)
        make_paper_variant(self.p3, 'rs1801133', mention_count=10)

        # Paper em outro projeto que também cita rs1801133 — NÃO deve inflar contagens
        other_user = make_user('var_other_agg')
        other_project = make_project(other_user, title='Outro Var Projeto')
        self.p_other = make_paper(pmid=12001, abstract='rs1801133 irrelevant.')
        make_project_paper(other_project, self.p_other, curation_status='included')
        make_paper_variant(self.p_other, 'rs1801133', mention_count=99)

    def test_unique_citations_included(self):
        """Apenas papers included deste projeto são contados em included."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']
        v = next(r for r in results if r['rs_number'] == 'rs1801133')
        self.assertEqual(v['unique_citations_included'], 2)

    def test_unique_citations_total(self):
        """Todos os papers do projeto (qualquer status) são contados em total."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']
        v = next(r for r in results if r['rs_number'] == 'rs1801133')
        self.assertEqual(v['unique_citations_total'], 3)

    def test_mention_count_total(self):
        """Soma de mention_count dos 3 PaperVariant do projeto."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']
        v = next(r for r in results if r['rs_number'] == 'rs1801133')
        self.assertEqual(v['mention_count_total'], 18)  # 3 + 5 + 10

    def test_other_project_paper_not_counted(self):
        """Paper de outro projeto não infla unique_citations_total nem included."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']
        v = next(r for r in results if r['rs_number'] == 'rs1801133')
        self.assertEqual(v['unique_citations_total'], 3)
        self.assertEqual(v['unique_citations_included'], 2)

    def test_included_zero_when_only_pending(self):
        """Se todos os papers de uma variante são pending, unique_citations_included = 0."""
        p4 = make_paper(pmid=11004, abstract='rs429358 was analyzed.')
        make_project_paper(self.project, p4, curation_status='pending')
        make_paper_variant(p4, 'rs429358', mention_count=1)

        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']
        v = next(r for r in results if r['rs_number'] == 'rs429358')
        self.assertEqual(v['unique_citations_included'], 0)
        self.assertEqual(v['unique_citations_total'], 1)

    def test_no_join_fanout_multiple_variants(self):
        """
        Múltiplas variantes no mesmo projeto não inflam as contagens umas das outras.
        """
        make_paper_variant(self.p1, 'rs429358', mention_count=2)

        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']

        v_main = next(r for r in results if r['rs_number'] == 'rs1801133')
        v_other = next(r for r in results if r['rs_number'] == 'rs429358')

        # rs1801133 não deve ter inflado
        self.assertEqual(v_main['unique_citations_total'], 3)
        self.assertEqual(v_main['unique_citations_included'], 2)

        # rs429358: apenas 1 paper (included)
        self.assertEqual(v_other['unique_citations_total'], 1)
        self.assertEqual(v_other['unique_citations_included'], 1)


# =============================================================================
# 2. Testes de isolamento por usuário (firebase-auth-guard — OBRIGATÓRIO)
# =============================================================================

class VariantUserIsolationTests(APITestCase):
    """
    Usuário B não pode acessar variantes do projeto de usuário A.
    Skill: firebase-auth-guard — _get_project() filtra por request.user.
    """

    def setUp(self):
        self.user_a = make_user('var_iso_user_a')
        self.user_b = make_user('var_iso_user_b')

        self.project_a = make_project(self.user_a, title='Var Projeto A')
        paper = make_paper(pmid=13001, abstract='rs1801133 is relevant.')
        make_project_paper(self.project_a, paper, curation_status='included')
        make_paper_variant(paper, 'rs1801133', mention_count=1)

        # Autenticar como usuário B
        self.client.force_authenticate(user=self.user_b)

    def test_user_b_cannot_list_variants_of_user_a_project(self):
        """Usuário B obtém 404 ao listar variantes do projeto de A."""
        resp = self.client.get(variants_url(self.project_a.id))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_user_b_cannot_access_variant_detail_of_user_a_project(self):
        """Usuário B obtém 404 ao acessar detalhe de variante do projeto de A."""
        resp = self.client.get(variant_detail_url(self.project_a.id, 'rs1801133'))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_unauthenticated_cannot_list_variants(self):
        """Requisição sem autenticação deve retornar 403 ou 401."""
        client = APIClient()
        resp = client.get(variants_url(self.project_a.id))
        self.assertIn(resp.status_code, [
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        ])

    def test_unauthenticated_cannot_access_variant_detail(self):
        """Requisição sem autenticação deve retornar 403 ou 401 no detalhe."""
        client = APIClient()
        resp = client.get(variant_detail_url(self.project_a.id, 'rs1801133'))
        self.assertIn(resp.status_code, [
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        ])

    def test_user_a_can_see_own_project_variants(self):
        """Usuário A vê suas próprias variantes sem problema."""
        self.client.force_authenticate(user=self.user_a)
        resp = self.client.get(variants_url(self.project_a.id))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['count'], 1)


# =============================================================================
# 3. Testes de anotação clínica
# =============================================================================

class VariantAnnotationTests(APITestCase):
    """
    Variante COM VariantAnnotation → bloco annotation populado.
    Variante SEM VariantAnnotation → annotation: null sem quebrar (D2).
    """

    def setUp(self):
        self.user = make_user('var_ann_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Annotation Tests')

        # Variante com anotação
        self.p1 = make_paper(pmid=14001, abstract='rs1801133 was detected.')
        make_project_paper(self.project, self.p1, curation_status='included')
        make_paper_variant(self.p1, 'rs1801133', mention_count=2)
        self.annotation = make_variant_annotation(
            rs_number='rs1801133',
            gene_symbol='MTHFR',
            gene_name='methylenetetrahydrofolate reductase',
            entrez_id=4524,
            chromosome='1',
            position=11856378,
            alleles='A/G',
            maf=0.32,
            clinical_significance='pathogenic',
        )

        # Variante SEM anotação
        self.p2 = make_paper(pmid=14002, abstract='rs429358 was studied.')
        make_project_paper(self.project, self.p2, curation_status='included')
        make_paper_variant(self.p2, 'rs429358', mention_count=1)

    # --- Lista ---

    def test_list_variant_with_annotation_has_annotation_block(self):
        """Variante com VariantAnnotation tem annotation != null na lista."""
        resp = self.client.get(variants_url(self.project.id))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']
        v = next(r for r in results if r['rs_number'] == 'rs1801133')
        self.assertIsNotNone(v['annotation'])
        self.assertEqual(v['annotation']['gene_symbol'], 'MTHFR')
        self.assertEqual(v['annotation']['clinical_significance'], 'pathogenic')
        self.assertEqual(v['annotation']['chromosome'], '1')
        self.assertAlmostEqual(float(v['annotation']['maf']), 0.32, places=4)

    def test_list_variant_without_annotation_has_null_annotation(self):
        """Variante sem VariantAnnotation tem annotation: null na lista — sem quebrar."""
        resp = self.client.get(variants_url(self.project.id))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']
        v = next(r for r in results if r['rs_number'] == 'rs429358')
        self.assertIsNone(v['annotation'])

    # --- Detalhe ---

    def test_detail_variant_with_annotation_has_full_annotation_block(self):
        """Detalhe com VariantAnnotation retorna bloco completo (gene_name, entrez_id, position, alleles)."""
        now = timezone.now()
        make_entity_context_variant(self.p1, 'rs1801133', 'rs1801133 was detected.', 0, now)

        resp = self.client.get(variant_detail_url(self.project.id, 'rs1801133'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ann = resp.data['annotation']
        self.assertIsNotNone(ann)
        self.assertEqual(ann['gene_symbol'], 'MTHFR')
        self.assertEqual(ann['gene_name'], 'methylenetetrahydrofolate reductase')
        self.assertEqual(ann['entrez_id'], 4524)
        self.assertEqual(ann['chromosome'], '1')
        self.assertEqual(ann['position'], 11856378)
        self.assertEqual(ann['alleles'], 'A/G')
        self.assertAlmostEqual(float(ann['maf']), 0.32, places=4)
        self.assertEqual(ann['clinical_significance'], 'pathogenic')

    def test_detail_variant_without_annotation_has_null_annotation(self):
        """Detalhe sem VariantAnnotation tem annotation: null — sem quebrar (D2)."""
        now = timezone.now()
        make_entity_context_variant(self.p2, 'rs429358', 'rs429358 was studied.', 0, now)

        resp = self.client.get(variant_detail_url(self.project.id, 'rs429358'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIsNone(resp.data['annotation'])

    def test_detail_nonexistent_variant_returns_404(self):
        """Variante não associada a nenhum paper do projeto → 404."""
        resp = self.client.get(variant_detail_url(self.project.id, 'rs9999999'))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_detail_aggregated_metrics(self):
        """Detalhe retorna unique_citations_included e total corretos."""
        # p1 → included (1 included)
        # Adicionar p3 → pending (total=2, included=1)
        p3 = make_paper(pmid=14003, abstract='rs1801133 confirmed.')
        make_project_paper(self.project, p3, curation_status='pending')
        make_paper_variant(p3, 'rs1801133', mention_count=1)

        now = timezone.now()
        make_entity_context_variant(self.p1, 'rs1801133', 'rs1801133 was detected.', 0, now)
        make_entity_context_variant(p3, 'rs1801133', 'rs1801133 confirmed.', 0, now)

        resp = self.client.get(variant_detail_url(self.project.id, 'rs1801133'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['unique_citations_included'], 1)
        self.assertEqual(resp.data['unique_citations_total'], 2)

    def test_detail_correct_reference_fields(self):
        """Campos do paper (pmid, title, pub_year, journal, curation_status) retornados."""
        now = timezone.now()
        make_entity_context_variant(self.p1, 'rs1801133', 'rs1801133 was detected.', 0, now)

        resp = self.client.get(variant_detail_url(self.project.id, 'rs1801133'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ref = resp.data['references'][0]
        self.assertEqual(ref['pmid'], 14001)
        self.assertEqual(ref['curation_status'], 'included')
        self.assertEqual(ref['pub_year'], 2023)
        self.assertIn('project_paper_id', ref)
        self.assertIn('snippets', ref)


# =============================================================================
# 4. Testes de filtros e ordenação
# =============================================================================

class VariantFilterOrderingTests(APITestCase):
    """
    Filtro ?q= por rs_number (icontains), ?ordering= nos campos,
    default '-unique_citations_included', paginação, ?included_only=.
    """

    def setUp(self):
        self.user = make_user('var_filter_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Var Filtros')
        self.url = variants_url(self.project.id)

        # rs1801133: 2 papers included, mention_count 4 + 6 = 10
        p1 = make_paper(pmid=15001, abstract='rs1801133 detected.')
        p2 = make_paper(pmid=15002, abstract='rs1801133 confirmed.')
        # rs429358: 1 paper included, mention_count 3
        p3 = make_paper(pmid=15003, abstract='rs429358 found.')
        # rs334: 0 included (pending only), mention_count 2
        p4 = make_paper(pmid=15004, abstract='rs334 mentioned.')

        make_project_paper(self.project, p1, curation_status='included')
        make_project_paper(self.project, p2, curation_status='included')
        make_project_paper(self.project, p3, curation_status='included')
        make_project_paper(self.project, p4, curation_status='pending')

        make_paper_variant(p1, 'rs1801133', mention_count=4)
        make_paper_variant(p2, 'rs1801133', mention_count=6)
        make_paper_variant(p3, 'rs429358', mention_count=3)
        make_paper_variant(p4, 'rs334', mention_count=2)

    def test_filter_q_by_rs_number(self):
        """?q=rs1801133 filtra por rs_number (icontains)."""
        resp = self.client.get(self.url, {'q': 'rs1801133'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rs_numbers = [r['rs_number'] for r in resp.data['results']]
        self.assertIn('rs1801133', rs_numbers)
        self.assertNotIn('rs429358', rs_numbers)
        self.assertNotIn('rs334', rs_numbers)

    def test_filter_q_case_insensitive(self):
        """?q=RS1801133 (uppercase) também funciona."""
        resp = self.client.get(self.url, {'q': 'RS1801133'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rs_numbers = [r['rs_number'] for r in resp.data['results']]
        self.assertIn('rs1801133', rs_numbers)

    def test_filter_q_partial_match(self):
        """?q=429 retorna rs429358."""
        resp = self.client.get(self.url, {'q': '429'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rs_numbers = [r['rs_number'] for r in resp.data['results']]
        self.assertIn('rs429358', rs_numbers)
        self.assertNotIn('rs1801133', rs_numbers)

    def test_default_ordering_is_unique_citations_included_desc(self):
        """Sem ?ordering=, o default é -unique_citations_included."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']
        included_values = [r['unique_citations_included'] for r in results]
        self.assertEqual(included_values, sorted(included_values, reverse=True))
        # rs1801133 (2 included) deve vir primeiro
        self.assertEqual(results[0]['rs_number'], 'rs1801133')

    def test_ordering_rs_number_asc(self):
        """?ordering=rs_number ordena A-Z."""
        resp = self.client.get(self.url, {'ordering': 'rs_number'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rs_numbers = [r['rs_number'] for r in resp.data['results']]
        self.assertEqual(rs_numbers, sorted(rs_numbers))

    def test_ordering_rs_number_desc(self):
        """?ordering=-rs_number ordena Z-A."""
        resp = self.client.get(self.url, {'ordering': '-rs_number'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rs_numbers = [r['rs_number'] for r in resp.data['results']]
        self.assertEqual(rs_numbers, sorted(rs_numbers, reverse=True))

    def test_ordering_mention_count_total_desc(self):
        """?ordering=-mention_count_total coloca rs1801133 primeiro (10 > 3 > 2)."""
        resp = self.client.get(self.url, {'ordering': '-mention_count_total'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        values = [r['mention_count_total'] for r in resp.data['results']]
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertEqual(resp.data['results'][0]['rs_number'], 'rs1801133')

    def test_ordering_unique_citations_total_asc(self):
        """?ordering=unique_citations_total ordena crescente."""
        resp = self.client.get(self.url, {'ordering': 'unique_citations_total'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        values = [r['unique_citations_total'] for r in resp.data['results']]
        self.assertEqual(values, sorted(values))

    def test_invalid_ordering_falls_back_to_default(self):
        """?ordering=campo_invalido não retorna erro — usa default."""
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
        """?page=2&page_size=2 retorna o terceiro item."""
        resp = self.client.get(self.url, {'page': 2, 'page_size': 2})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data['results']), 1)
        self.assertIsNone(resp.data['next'])

    # --- ?included_only= ---

    def test_included_only_true_excludes_zero_included(self):
        """?included_only=true remove rs334 (único paper é pending) e mantém as demais."""
        resp = self.client.get(self.url, {'included_only': 'true'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rs_numbers = {r['rs_number'] for r in resp.data['results']}
        self.assertNotIn('rs334', rs_numbers)
        self.assertIn('rs1801133', rs_numbers)
        self.assertIn('rs429358', rs_numbers)

    def test_included_only_one_is_equivalent_to_true(self):
        """?included_only=1 equivale a true."""
        resp = self.client.get(self.url, {'included_only': '1'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rs_numbers = {r['rs_number'] for r in resp.data['results']}
        self.assertNotIn('rs334', rs_numbers)

    def test_without_included_only_all_variants_present(self):
        """Sem ?included_only, todas as variantes aparecem (inclusive rs334 com pending)."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rs_numbers = {r['rs_number'] for r in resp.data['results']}
        self.assertIn('rs334', rs_numbers)


# =============================================================================
# 5. Testes de top_variants nas stats
# =============================================================================

class VariantStatsTopVariantsTests(APITestCase):
    """
    top_variants nas ProjectStats:
      - Formato: [{rs_number, count}]
      - Agrega mention_count via Sum APENAS de papers included
      - rs_numbers ordenados por count DESC
    """

    def setUp(self):
        self.user = make_user('var_stats_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Var Stats Tests')

        # Paper included com 2 variantes
        self.p_incl = make_paper(pmid=16001, abstract='rs1801133 and rs429358 detected.')
        make_project_paper(self.project, self.p_incl, curation_status='included')
        make_paper_variant(self.p_incl, 'rs1801133', mention_count=5)
        make_paper_variant(self.p_incl, 'rs429358', mention_count=2)

        # Paper excluded: menciona rs334 — NÃO deve aparecer em top_variants
        self.p_excl = make_paper(pmid=16002, abstract='rs334 was excluded.')
        make_project_paper(self.project, self.p_excl, curation_status='excluded')
        make_paper_variant(self.p_excl, 'rs334', mention_count=100)

        # Paper pending: menciona rs1801133 com alta contagem — NÃO deve somar
        self.p_pend = make_paper(pmid=16003, abstract='rs1801133 pending data.')
        make_project_paper(self.project, self.p_pend, curation_status='pending')
        make_paper_variant(self.p_pend, 'rs1801133', mention_count=50)

    def _get_stats(self):
        """Chama o endpoint de stats diretamente (recalcula na hora)."""
        resp = self.client.get(f'/api/v1/projects/{self.project.id}/stats/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        return resp.data

    def test_top_variants_present_in_stats(self):
        """top_variants aparece no payload de stats."""
        data = self._get_stats()
        self.assertIn('top_variants', data)

    def test_top_variants_format(self):
        """Cada item de top_variants tem rs_number e count."""
        data = self._get_stats()
        for item in data['top_variants']:
            self.assertIn('rs_number', item)
            self.assertIn('count', item)

    def test_top_variants_only_included_papers(self):
        """
        top_variants conta apenas papers included.
        rs334 (excluded) e a menção extra de rs1801133 (pending) não entram.
        """
        data = self._get_stats()
        rs_numbers_in_top = {item['rs_number'] for item in data['top_variants']}
        # rs334 é de paper excluded → não deve aparecer
        self.assertNotIn('rs334', rs_numbers_in_top)
        # rs1801133 e rs429358 são de paper included → devem aparecer
        self.assertIn('rs1801133', rs_numbers_in_top)
        self.assertIn('rs429358', rs_numbers_in_top)

    def test_top_variants_count_correct(self):
        """
        count de rs1801133 deve ser 5 (only from included paper),
        não 55 (5 + 50 from pending paper).
        """
        data = self._get_stats()
        item = next(i for i in data['top_variants'] if i['rs_number'] == 'rs1801133')
        self.assertEqual(item['count'], 5)

    def test_top_variants_ordered_by_count_desc(self):
        """top_variants está ordenado por count decrescente."""
        data = self._get_stats()
        counts = [item['count'] for item in data['top_variants']]
        self.assertEqual(counts, sorted(counts, reverse=True))


# =============================================================================
# 6. Testes do detalhe — cache quente, frio, stale
# =============================================================================

class VariantDetailCacheTests(APITestCase):
    """
    Detalhe de variante: cache quente (ready), cache frio/stale (computing), 404.
    """

    def setUp(self):
        self.user = make_user('var_detail_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Var Detalhe')

        self.paper = make_paper(
            pmid=17001,
            abstract='rs1801133 was detected in all patients. rs1801133 levels correlated with severity.',
            pub_year=2022,
        )
        make_project_paper(self.project, self.paper, curation_status='included')
        make_paper_variant(self.paper, 'rs1801133', mention_count=2)

    def _detail_url(self, rs_number='rs1801133'):
        return variant_detail_url(self.project.id, rs_number)

    def test_detail_cache_hot_returns_ready(self):
        """Cache populado e fresco → context_status='ready'."""
        now = timezone.now()
        make_entity_context_variant(self.paper, 'rs1801133', 'rs1801133 was detected in all patients.', 0, now)
        make_entity_context_variant(self.paper, 'rs1801133', 'rs1801133 levels correlated with severity.', 1, now)

        resp = self.client.get(self._detail_url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['context_status'], 'ready')

    def test_detail_cache_hot_returns_snippets(self):
        """Snippets do cache retornados com sentence e sentence_position."""
        now = timezone.now()
        make_entity_context_variant(self.paper, 'rs1801133', 'rs1801133 was detected in all patients.', 0, now)
        make_entity_context_variant(self.paper, 'rs1801133', 'rs1801133 levels correlated with severity.', 1, now)

        resp = self.client.get(self._detail_url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        refs = resp.data['references']
        self.assertEqual(len(refs), 1)
        snippets = refs[0]['snippets']
        self.assertEqual(len(snippets), 2)
        positions = [s['sentence_position'] for s in snippets]
        self.assertEqual(positions, sorted(positions))

    def test_detail_cache_cold_returns_computing(self):
        """Sem EntityContext para o paper → context_status='computing'."""
        with patch('apps.core.tasks.variant_tasks.derive_variant_contexts.delay') as mock_task:
            resp = self.client.get(self._detail_url())

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['context_status'], 'computing')
        mock_task.assert_called_once_with(str(self.project.id), 'rs1801133')

    def test_detail_cache_cold_dispatches_task(self):
        """Cache frio dispara a task Celery exatamente uma vez."""
        with patch('apps.core.tasks.variant_tasks.derive_variant_contexts.delay') as mock_task:
            self.client.get(self._detail_url())
        mock_task.assert_called_once()

    def test_detail_cache_stale_returns_computing(self):
        """
        Cache com computed_at anterior ao paper.updated_at → context_status='computing'.
        """
        stale_time = timezone.now() - timedelta(hours=1)
        make_entity_context_variant(self.paper, 'rs1801133', 'rs1801133 was detected.', 0, stale_time)

        # Simula re-ingestão: forçar updated_at do paper para após o computed_at
        Paper.objects.filter(pk=self.paper.pk).update(updated_at=timezone.now())
        self.paper.refresh_from_db()

        with patch('apps.core.tasks.variant_tasks.derive_variant_contexts.delay') as mock_task:
            resp = self.client.get(self._detail_url())

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['context_status'], 'computing')
        mock_task.assert_called_once()

    def test_detail_nonexistent_variant_returns_404(self):
        """Variante não associada a nenhum paper do projeto → 404."""
        resp = self.client.get(self._detail_url('rs9999999'))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_detail_references_include_all_curation_statuses(self):
        """Lista de referências inclui papers de todos os status de curadoria."""
        p2 = make_paper(pmid=17002, abstract='rs1801133 pending study.')
        make_project_paper(self.project, p2, curation_status='pending')
        make_paper_variant(p2, 'rs1801133', mention_count=1)

        now = timezone.now()
        make_entity_context_variant(self.paper, 'rs1801133', 'rs1801133 was detected.', 0, now)
        make_entity_context_variant(p2, 'rs1801133', 'rs1801133 pending study.', 0, now)

        resp = self.client.get(self._detail_url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data['references']), 2)
        statuses = {r['curation_status'] for r in resp.data['references']}
        self.assertIn('included', statuses)
        self.assertIn('pending', statuses)


# =============================================================================
# 7. Testes da task de contexto (idempotência, regex, sentinela, invalidação)
# =============================================================================

class VariantContextTaskTests(APITestCase):
    """
    Testes da lógica de derive_and_persist_contexts e extract_variant_sentences.

    A task Celery é chamada diretamente via VariantService para não depender do broker.
    """

    def setUp(self):
        self.user = make_user('var_task_user')
        self.project = make_project(self.user, title='Var Task Tests')

        self.abstract = (
            'rs1801133 was elevated in all patients. '
            'The frequency of rs1801133 correlated with disease severity. '
            'rs334 was not significantly altered.'
        )
        self.paper = make_paper(
            pmid=18001,
            abstract=self.abstract,
            pub_year=2021,
        )
        make_project_paper(self.project, self.paper, curation_status='included')
        make_paper_variant(self.paper, 'rs1801133', mention_count=2)

    # --- Idempotência ---

    def test_derive_twice_does_not_duplicate(self):
        """Rodar derive_and_persist_contexts duas vezes não duplica EntityContext."""
        VariantService.derive_and_persist_contexts(self.project, 'rs1801133')
        VariantService.derive_and_persist_contexts(self.project, 'rs1801133')

        count = EntityContext.objects.filter(
            paper=self.paper,
            entity_type=EntityContext.EntityType.VARIANT,
            entity_name='rs1801133',
        ).count()
        snippets = VariantService.extract_variant_sentences(self.paper, 'rs1801133')
        self.assertEqual(count, len(snippets))

    def test_derive_returns_correct_snippet_count(self):
        """Retorna o número correto de snippets derivados."""
        n = VariantService.derive_and_persist_contexts(self.project, 'rs1801133')
        # Abstract tem 3 sentenças; 2 contêm 'rs1801133'
        self.assertEqual(n, 2)

    # --- Fronteira de palavra (regex \b) ---

    def test_word_boundary_does_not_confuse_rs1801133_with_rs334(self):
        """
        'rs1801133' com \b não deve casar dentro de outra variante.
        Snippets de rs1801133 não devem incluir a sentença onde só rs334 aparece.
        """
        snippets = VariantService.extract_variant_sentences(self.paper, 'rs1801133')
        sentences = [s['sentence'] for s in snippets]
        self.assertEqual(len(snippets), 2)

        # A sentença com rs334 não deve aparecer nos snippets de rs1801133
        for sentence in sentences:
            # Cada sentença deve conter rs1801133
            self.assertIn('rs1801133', sentence.lower())

    def test_snippets_content_correct(self):
        """Snippets derivados correspondem às sentenças esperadas do abstract."""
        snippets = VariantService.extract_variant_sentences(self.paper, 'rs1801133')
        sentences = [s['sentence'] for s in snippets]

        self.assertIn('rs1801133 was elevated in all patients.', sentences)
        self.assertIn('The frequency of rs1801133 correlated with disease severity.', sentences)

    def test_sentence_position_zero_based(self):
        """sentence_position é 0-based."""
        snippets = VariantService.extract_variant_sentences(self.paper, 'rs1801133')
        positions = {s['sentence_position'] for s in snippets}
        self.assertIn(0, positions)
        self.assertIn(1, positions)

    def test_empty_abstract_returns_no_snippets(self):
        """Abstract vazio retorna lista vazia."""
        paper_empty = make_paper(pmid=18002, abstract='')
        make_project_paper(self.project, paper_empty, curation_status='included')
        make_paper_variant(paper_empty, 'rs1801133', mention_count=1)

        snippets = VariantService.extract_variant_sentences(paper_empty, 'rs1801133')
        self.assertEqual(snippets, [])

    def test_variant_not_in_abstract_returns_no_snippets(self):
        """Variante ausente do abstract retorna lista vazia."""
        paper_other = make_paper(pmid=18003, abstract='Some unrelated content here.')
        snippets = VariantService.extract_variant_sentences(paper_other, 'rs1801133')
        self.assertEqual(snippets, [])

    # --- Marcador sentinela ---

    def test_sentinel_written_when_variant_not_in_abstract(self):
        """
        Quando a variante não aparece no abstract, uma linha sentinela
        (sentence_position=-1) é gravada para marcar 'processado'.
        """
        paper = make_paper(pmid=18010, abstract='Inflammation was observed.')
        make_project_paper(self.project, paper, curation_status='included')
        make_paper_variant(paper, 'rs1801133', mention_count=1)

        VariantService.derive_and_persist_contexts(self.project, 'rs1801133')

        sentinel_count = EntityContext.objects.filter(
            paper=paper,
            entity_type=EntityContext.EntityType.VARIANT,
            entity_name='rs1801133',
            sentence_position=-1,
        ).count()
        self.assertEqual(sentinel_count, 1, 'Deve existir exatamente 1 sentinela.')

    def test_sentinel_written_for_empty_abstract(self):
        """Abstract vazio também grava sentinela."""
        paper = make_paper(pmid=18011, abstract='')
        make_project_paper(self.project, paper, curation_status='included')
        make_paper_variant(paper, 'rs1801133', mention_count=1)

        VariantService.derive_and_persist_contexts(self.project, 'rs1801133')

        sentinel_count = EntityContext.objects.filter(
            paper=paper,
            entity_type=EntityContext.EntityType.VARIANT,
            entity_name='rs1801133',
            sentence_position=-1,
        ).count()
        self.assertEqual(sentinel_count, 1)

    def test_sentinel_makes_context_status_ready(self):
        """
        Após derivação em paper sem abstract com a variante,
        context_status deve ser 'ready' (não 'computing' eterno).

        Regressão: sem sentinela, computed_at=None → loop infinito de computing.
        """
        self.client.force_authenticate(user=self.user)
        paper = make_paper(pmid=18012, abstract='No variant here at all.')
        make_project_paper(self.project, paper, curation_status='included')
        make_paper_variant(paper, 'rs1801133', mention_count=1)

        VariantService.derive_and_persist_contexts(self.project, 'rs1801133')

        resp = self.client.get(variant_detail_url(self.project.id, 'rs1801133'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            resp.data['context_status'],
            'ready',
            'Após derivação, context_status deve ser "ready" mesmo sem snippets.',
        )

    def test_sentinel_does_not_leak_as_snippet(self):
        """Nenhum snippet com sentence_position=-1 deve ser entregue ao cliente."""
        self.client.force_authenticate(user=self.user)
        paper = make_paper(pmid=18013, abstract='Unrelated content only.')
        make_project_paper(self.project, paper, curation_status='included')
        make_paper_variant(paper, 'rs1801133', mention_count=1)

        VariantService.derive_and_persist_contexts(self.project, 'rs1801133')

        resp = self.client.get(variant_detail_url(self.project.id, 'rs1801133'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        for ref in resp.data.get('references', []):
            for snippet in ref.get('snippets', []):
                self.assertNotEqual(
                    snippet.get('sentence_position'),
                    -1,
                    'Sentinela (sentence_position=-1) não deve vazar como snippet.',
                )

    def test_sentinel_not_duplicated_on_double_derive(self):
        """Rodar derivação 2x para paper sem variante no abstract → exatamente 1 sentinela."""
        paper = make_paper(pmid=18014, abstract='No variant mentioned.')
        make_project_paper(self.project, paper, curation_status='included')
        make_paper_variant(paper, 'rs1801133', mention_count=1)

        VariantService.derive_and_persist_contexts(self.project, 'rs1801133')
        VariantService.derive_and_persist_contexts(self.project, 'rs1801133')

        count = EntityContext.objects.filter(
            paper=paper,
            entity_type=EntityContext.EntityType.VARIANT,
            entity_name='rs1801133',
        ).count()
        self.assertEqual(count, 1, 'Deve existir exatamente 1 linha (sentinela) após 2 derivações.')

    # --- Invalidação após mudança de abstract ---

    def test_invalidation_after_abstract_change(self):
        """
        Após mudar o abstract do paper (updated_at avança),
        snippets antigos ficam stale e são recomputados na segunda chamada.
        """
        VariantService.derive_and_persist_contexts(self.project, 'rs1801133')
        first_count = EntityContext.objects.filter(
            paper=self.paper,
            entity_type=EntityContext.EntityType.VARIANT,
            entity_name='rs1801133',
        ).count()
        self.assertEqual(first_count, 2)

        # Mudar abstract (simula re-ingestão)
        new_abstract = 'rs1801133 only once mentioned.'
        Paper.objects.filter(pk=self.paper.pk).update(
            abstract=new_abstract,
            updated_at=timezone.now(),
        )
        self.paper.refresh_from_db()

        VariantService.derive_and_persist_contexts(self.project, 'rs1801133')
        second_count = EntityContext.objects.filter(
            paper=self.paper,
            entity_type=EntityContext.EntityType.VARIANT,
            entity_name='rs1801133',
        ).count()

        # Novo abstract tem apenas 1 sentença com rs1801133
        new_snippets = VariantService.extract_variant_sentences(self.paper, 'rs1801133')
        self.assertEqual(second_count, len(new_snippets))
        self.assertEqual(second_count, 1)

    def test_derive_persists_computed_at(self):
        """computed_at é gravado em cada EntityContext derivado."""
        before = timezone.now()
        VariantService.derive_and_persist_contexts(self.project, 'rs1801133')
        after = timezone.now()

        contexts = EntityContext.objects.filter(
            paper=self.paper,
            entity_type=EntityContext.EntityType.VARIANT,
            entity_name='rs1801133',
        )
        for ctx in contexts:
            self.assertIsNotNone(ctx.computed_at)
            self.assertGreaterEqual(ctx.computed_at, before)
            self.assertLessEqual(ctx.computed_at, after)

    def test_derive_multiple_papers_in_project(self):
        """derive_and_persist_contexts processa todos os papers do projeto para a variante."""
        p2 = make_paper(pmid=18015, abstract='rs1801133 upregulation confirmed.')
        make_project_paper(self.project, p2, curation_status='pending')
        make_paper_variant(p2, 'rs1801133', mention_count=1)

        n = VariantService.derive_and_persist_contexts(self.project, 'rs1801133')
        # paper original: 2 snippets; p2: 1 snippet → total 3
        self.assertEqual(n, 3)

        count = EntityContext.objects.filter(
            entity_type=EntityContext.EntityType.VARIANT,
            entity_name='rs1801133',
        ).count()
        self.assertEqual(count, 3)

    # --- Integração com a task Celery (síncrona) ---

    def test_celery_task_calls_service(self):
        """A Celery task derive_variant_contexts chama VariantService.derive_and_persist_contexts."""
        from apps.core.tasks.variant_tasks import derive_variant_contexts

        n_before = EntityContext.objects.filter(
            paper=self.paper,
            entity_type=EntityContext.EntityType.VARIANT,
            entity_name='rs1801133',
        ).count()
        self.assertEqual(n_before, 0)

        derive_variant_contexts.run(str(self.project.id), 'rs1801133')

        n_after = EntityContext.objects.filter(
            paper=self.paper,
            entity_type=EntityContext.EntityType.VARIANT,
            entity_name='rs1801133',
        ).count()
        self.assertEqual(n_after, 2)

    def test_celery_task_nonexistent_project_does_not_raise(self):
        """Task com project_id inválido retorna sem levantar exceção."""
        from apps.core.tasks.variant_tasks import derive_variant_contexts

        fake_id = str(uuid.uuid4())
        try:
            derive_variant_contexts.run(fake_id, 'rs1801133')
        except Exception as exc:
            self.fail(f'Task levantou exceção inesperada: {exc}')


# =============================================================================
# 8. Trava de regressão: lock de disparo e validação de rs_number longo
# =============================================================================

class VariantLockDispatchTests(APITestCase):
    """
    Regressão: lock de disparo de task Celery.

    Dois GETs seguidos com cache frio não devem enfileirar a task duas vezes
    enquanto o lock (variant_derive_lock:{project_id}:{rs_number}) está ativo.
    """

    def setUp(self):
        cache.clear()
        self.user = make_user('var_lock_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Var Lock Tests')

        self.paper = make_paper(pmid=19001, abstract='rs1801133 was elevated in all samples.')
        make_project_paper(self.project, self.paper, curation_status='included')
        make_paper_variant(self.paper, 'rs1801133', mention_count=1)

    def tearDown(self):
        cache.clear()

    def test_two_cold_gets_dispatch_task_at_most_once(self):
        """
        Dois GETs consecutivos com cache frio disparam a task no máximo uma vez
        enquanto o lock está ativo.
        """
        with patch('apps.core.tasks.variant_tasks.derive_variant_contexts.delay') as mock_delay:
            self.client.get(variant_detail_url(self.project.id, 'rs1801133'))
            self.client.get(variant_detail_url(self.project.id, 'rs1801133'))

        self.assertEqual(
            mock_delay.call_count,
            1,
            f'A task deve ser disparada exatamente 1 vez; foi disparada {mock_delay.call_count} vez(es).',
        )

    def test_first_cold_get_dispatches_task(self):
        """O primeiro GET com cache frio deve disparar a task."""
        with patch('apps.core.tasks.variant_tasks.derive_variant_contexts.delay') as mock_delay:
            resp = self.client.get(variant_detail_url(self.project.id, 'rs1801133'))

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['context_status'], 'computing')
        mock_delay.assert_called_once_with(str(self.project.id), 'rs1801133')

    def test_lock_key_format(self):
        """Chave de lock segue o formato 'variant_derive_lock:{project_id}:{rs_number}'."""
        expected = f'variant_derive_lock:{self.project.id}:rs1801133'
        actual = _derive_lock_key(str(self.project.id), 'rs1801133')
        self.assertEqual(actual, expected)

    def test_lock_isolates_different_variants(self):
        """
        Lock de rs1801133 não bloqueia disparo de task para rs429358 no mesmo projeto.
        """
        p2 = make_paper(pmid=19002, abstract='rs429358 mutation identified.')
        make_project_paper(self.project, p2, curation_status='included')
        make_paper_variant(p2, 'rs429358', mention_count=1)

        with patch('apps.core.tasks.variant_tasks.derive_variant_contexts.delay') as mock_delay:
            self.client.get(variant_detail_url(self.project.id, 'rs1801133'))
            self.client.get(variant_detail_url(self.project.id, 'rs1801133'))  # lock ativo
            self.client.get(variant_detail_url(self.project.id, 'rs429358'))   # lock diferente

        self.assertEqual(
            mock_delay.call_count,
            2,
            'Deve haver 2 disparos: 1 para rs1801133 (primeiro GET) + 1 para rs429358.',
        )
        called_rs = [call.args[1] for call in mock_delay.call_args_list]
        self.assertIn('rs1801133', called_rs)
        self.assertIn('rs429358', called_rs)


class VariantRsNumberValidationTests(APITestCase):
    """
    Regressão: validação de rs_number acima do limite (RS_NUMBER_MAX_LEN = 32).

    Proteção anti-ReDoS: rs_number longo usado como regex sobre N abstracts pode
    ser custoso. A view rejeita com 404 antes de qualquer regex.

    Espelha GeneSymbolValidationTests do test_gene_api.py.
    """

    def setUp(self):
        cache.clear()
        self.user = make_user('var_validation_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Var Validation Tests')

    def tearDown(self):
        cache.clear()

    def test_rs_number_above_max_len_returns_404(self):
        """rs_number com mais de 32 caracteres → 404 imediato."""
        long_rs = 'rs' + '1' * (RS_NUMBER_MAX_LEN + 1)  # 34 chars
        resp = self.client.get(variant_detail_url(self.project.id, long_rs))
        self.assertEqual(
            resp.status_code,
            status.HTTP_404_NOT_FOUND,
            f'rs_number com {len(long_rs)} chars deve retornar 404.',
        )

    def test_rs_number_above_max_len_contains_detail_message(self):
        """Resposta 404 por rs_number longo contém mensagem de comprimento máximo."""
        long_rs = 'rs' + '1' * (RS_NUMBER_MAX_LEN + 1)
        resp = self.client.get(variant_detail_url(self.project.id, long_rs))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn('comprimento máximo', resp.data.get('detail', ''))

    def test_rs_number_above_max_len_does_not_dispatch_task(self):
        """rs_number longo não dispara task Celery — rejeição ocorre antes."""
        long_rs = 'rs' + '9' * (RS_NUMBER_MAX_LEN + 5)

        with patch('apps.core.tasks.variant_tasks.derive_variant_contexts.delay') as mock_delay:
            resp = self.client.get(variant_detail_url(self.project.id, long_rs))

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        mock_delay.assert_not_called()

    def test_rs_number_at_max_len_not_rejected_by_length(self):
        """
        rs_number exatamente no limite (32 chars) não é rejeitado por comprimento.
        Pode retornar 404 por ausência da variante, não por comprimento.
        """
        rs_at_limit = 'rs' + '1' * (RS_NUMBER_MAX_LEN - 2)  # 32 chars total
        resp = self.client.get(variant_detail_url(self.project.id, rs_at_limit))
        # 404 por variante não encontrada é OK; não deve mencionar comprimento
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        detail_msg = resp.data.get('detail', '')
        self.assertNotIn('comprimento máximo', detail_msg)
