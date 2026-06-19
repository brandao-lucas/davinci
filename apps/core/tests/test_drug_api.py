"""
Testes do endpoint de medicamentos do projeto.

Cobre:
  1. Agregação (unique_citations_included vs total vs mention_count_total;
     colapso de drug_name_lower com caixas diferentes; drugbank_id representativo;
     fan-out de JOIN não infla contagens).
  2. Isolamento por usuário (skill firebase-auth-guard).
  3. Filtros e ordenação (?q=, ?ordering=, paginação).
  4. Filtro ?included_only= (remove drug sem citação incluída, sem alterar
     contagens; aceita true/false/1/0; compõe com ordering/paginação).
  5. URLs no payload: drugbank_url presente apenas quando drugbank_id não-vazio;
     pubchem_search_url sempre presente e bem-formada (URL-encoded).
  6. Detalhe — cache quente (ready), cache frio/stale (computing), 404, campos
     de referência (pmid, title, pub_year, journal, curation_status, snippets).
  7. TRAVAS DE REGRESSÃO (fix CRÍTICO 007): project_paper_id == ProjectPaper.pk,
     != Paper.pk, 'id' não em references[] (fixtures com PKs divergentes
     forçadas por papers fantasma — idêntico ao padrão de test_gene_api.py).
  8. Task derive_drug_contexts — idempotência, regex \b + case-insensitive,
     sentinela -1, invalidação após mudança de abstract.
"""

import hashlib
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
    PaperDrug,
    ProjectPaper,
)
from apps.core.services.drug_service import DrugService, DRUG_NAME_MAX_LEN
from apps.core.views.drug_views import _derive_lock_key


# =============================================================================
# Helpers
# =============================================================================

def make_user(username, password='testpass'):
    return User.objects.create_user(username=username, password=password)


def make_project(user, title='Projeto Drug Teste', query_term='cancer'):
    slug = f'{title.lower().replace(" ", "-")}-{user.username}-drug-test'
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


def make_paper_drug(paper, drug_name, drug_name_lower=None, drugbank_id='', mention_count=1):
    return PaperDrug.objects.create(
        paper=paper,
        drug_name=drug_name,
        drug_name_lower=drug_name_lower if drug_name_lower is not None else drug_name.lower(),
        drugbank_id=drugbank_id,
        mention_count=mention_count,
    )


def make_entity_context_drug(paper, drug_name_lower, sentence, sentence_position=0, computed_at=None):
    return EntityContext.objects.create(
        paper=paper,
        entity_type=EntityContext.EntityType.DRUG,
        entity_name=drug_name_lower,
        sentence=sentence,
        sentence_position=sentence_position,
        computed_at=computed_at or timezone.now(),
    )


def drugs_url(project_id):
    return f'/api/v1/projects/{project_id}/drugs/'


def drug_detail_url(project_id, drug_name_lower):
    return f'/api/v1/projects/{project_id}/drugs/{drug_name_lower}/'


# =============================================================================
# 1. Testes de agregação
# =============================================================================

class DrugAggregationTests(APITestCase):
    """
    Verifica unique_citations_included, unique_citations_total, mention_count_total,
    colapso de drug_name_lower com caixas diferentes e drugbank_id representativo.

    Caso decisivo:
      - Metformin citado em 3 papers do projeto:
          paper1 → included  (drug_name='Metformin',  drugbank_id='DB00331')
          paper2 → pending   (drug_name='METFORMIN',  drugbank_id='')
          paper3 → included  (drug_name='metformin',  drugbank_id='')
      - Os 3 PaperDrug têm drug_name_lower='metformin' → colapsam num grupo.
      - unique_citations_included deve ser 2 (apenas included deste projeto).
      - unique_citations_total deve ser 3 (todos os status deste projeto).
      - mention_count_total deve somar mention_count dos 3 PaperDrug.
      - drugbank_id representativo: Max('drugbank_id') → 'DB00331' (não-vazio).
      - Paper em outro projeto que também cita Metformin NÃO infla contagens.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='drug_agg_user', password='pw')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user)
        self.url = drugs_url(self.project.id)

        # Papers deste projeto com drug_name em caixas diferentes (mesmo drug_name_lower)
        self.p1 = make_paper(pmid=1001, abstract='Metformin was administered.')
        self.p2 = make_paper(pmid=1002, abstract='METFORMIN levels measured.')
        self.p3 = make_paper(pmid=1003, abstract='metformin reduces glucose.')

        make_project_paper(self.project, self.p1, curation_status='included')
        make_project_paper(self.project, self.p2, curation_status='pending')
        make_project_paper(self.project, self.p3, curation_status='included')

        # PaperDrug com caixas diferentes e mention_counts variados
        make_paper_drug(self.p1, 'Metformin',  drug_name_lower='metformin', drugbank_id='DB00331', mention_count=3)
        make_paper_drug(self.p2, 'METFORMIN',  drug_name_lower='metformin', drugbank_id='',        mention_count=5)
        make_paper_drug(self.p3, 'metformin',  drug_name_lower='metformin', drugbank_id='',        mention_count=10)

        # Paper em outro projeto que também cita Metformin — NÃO deve inflar contagens
        other_user = make_user('other_drug_agg')
        other_project = make_project(other_user, title='Outro Projeto Drug')
        self.p_other = make_paper(pmid=2001, abstract='Metformin irrelevante.')
        make_project_paper(other_project, self.p_other, curation_status='included')
        make_paper_drug(self.p_other, 'Metformin', drug_name_lower='metformin', mention_count=99)

    def _get_metformin(self, params=None):
        resp = self.client.get(self.url, params or {})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']
        # drug_name_lower não é exposto no serializer de lista; usamos drug_name.lower()
        return next(r for r in results if r['drug_name'].lower() == 'metformin')

    def test_same_drug_different_case_collapses_to_one_group(self):
        """Metformin, METFORMIN e metformin colapsam num único grupo pela drug_name_lower."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']
        # O campo exposto é drug_name (representativo). Como os 3 registros têm o mesmo
        # drug_name_lower, devem resultar num único item no payload.
        drug_names_lower = [r['drug_name'].lower() for r in results]
        self.assertEqual(drug_names_lower.count('metformin'), 1, 'Deve existir apenas 1 grupo para metformin.')

    def test_unique_citations_included(self):
        """Apenas papers included deste projeto são contados em unique_citations_included."""
        met = self._get_metformin()
        self.assertEqual(met['unique_citations_included'], 2)

    def test_unique_citations_total(self):
        """Todos os papers do projeto (qualquer status) são contados em unique_citations_total."""
        met = self._get_metformin()
        self.assertEqual(met['unique_citations_total'], 3)

    def test_mention_count_total(self):
        """Soma de mention_count dos 3 PaperDrug do projeto: 3 + 5 + 10 = 18."""
        met = self._get_metformin()
        self.assertEqual(met['mention_count_total'], 18)

    def test_other_project_paper_not_counted(self):
        """Paper de outro projeto não infla unique_citations_total nem included."""
        met = self._get_metformin()
        # Este projeto tem 3 papers; paper do outro projeto não conta.
        self.assertEqual(met['unique_citations_total'], 3)
        self.assertEqual(met['unique_citations_included'], 2)

    def test_drugbank_id_representative_picks_nonempty(self):
        """
        drugbank_id representativo via Max('drugbank_id'): o valor não-vazio
        'DB00331' deve ser retornado pois é maior lexicograficamente que ''.
        """
        met = self._get_metformin()
        self.assertEqual(met['drugbank_id'], 'DB00331')

    def test_drugbank_id_empty_when_all_papers_lack_id(self):
        """
        Quando nenhum paper do grupo tem drugbank_id, o campo é '' ou null.
        """
        p4 = make_paper(pmid=1004, abstract='Aspirin study.')
        make_project_paper(self.project, p4, curation_status='included')
        make_paper_drug(p4, 'Aspirin', drug_name_lower='aspirin', drugbank_id='', mention_count=2)

        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']
        aspirin = next(r for r in results if r['drug_name'].lower() == 'aspirin')
        # drugbank_id vazio → '' ou None (serializer pode normalizar para '')
        self.assertIn(aspirin['drugbank_id'], ('', None))

    def test_no_join_fanout_multiple_drugs(self):
        """
        Drug presente em múltiplos papers: Count distinct não duplica.
        Adiciona segundo drug (aspirin) em apenas 1 paper included.
        Verifica que metformin não infla quando há vários drugs no mesmo projeto.
        """
        p4 = make_paper(pmid=1005, abstract='Aspirin study.')
        make_project_paper(self.project, p4, curation_status='included')
        make_paper_drug(p4, 'Aspirin', drug_name_lower='aspirin', mention_count=2)

        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']

        met = next(r for r in results if r['drug_name'].lower() == 'metformin')
        asp = next(r for r in results if r['drug_name'].lower() == 'aspirin')

        # Metformin: não deve ter inflado
        self.assertEqual(met['unique_citations_total'], 3)
        self.assertEqual(met['unique_citations_included'], 2)

        # Aspirin: apenas 1 paper included
        self.assertEqual(asp['unique_citations_total'], 1)
        self.assertEqual(asp['unique_citations_included'], 1)

    def test_included_zero_when_only_pending(self):
        """
        Se todos os papers de um drug são pending, unique_citations_included = 0.
        """
        p5 = make_paper(pmid=1006, abstract='Insulin pending study.')
        make_project_paper(self.project, p5, curation_status='pending')
        make_paper_drug(p5, 'Insulin', drug_name_lower='insulin', mention_count=1)

        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']

        insulin = next(r for r in results if r['drug_name'].lower() == 'insulin')
        self.assertEqual(insulin['unique_citations_included'], 0)
        self.assertEqual(insulin['unique_citations_total'], 1)


# =============================================================================
# 2. Testes de URLs no payload (drugbank_url e pubchem_search_url)
# =============================================================================

class DrugUrlPayloadTests(APITestCase):
    """
    drugbank_url: presente apenas quando drugbank_id não-vazio.
    pubchem_search_url: sempre presente, bem-formada e URL-encoded.
    """

    def setUp(self):
        self.user = make_user('drug_url_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='URL Tests')
        self.url = drugs_url(self.project.id)

        # Drug com drugbank_id
        p1 = make_paper(pmid=3001, abstract='Metformin study.')
        make_project_paper(self.project, p1, curation_status='included')
        make_paper_drug(p1, 'Metformin', drug_name_lower='metformin', drugbank_id='DB00331')

        # Drug sem drugbank_id (campo em branco)
        p2 = make_paper(pmid=3002, abstract='Aspirin study.')
        make_project_paper(self.project, p2, curation_status='included')
        make_paper_drug(p2, 'Aspirin', drug_name_lower='aspirin', drugbank_id='')

        # Drug com nome multi-palavra (para testar URL-encode)
        p3 = make_paper(pmid=3003, abstract='Sodium chloride used.')
        make_project_paper(self.project, p3, curation_status='pending')
        make_paper_drug(p3, 'Sodium Chloride', drug_name_lower='sodium chloride', drugbank_id='')

    def test_drugbank_url_present_when_drugbank_id_exists(self):
        """drugbank_url é construída quando drugbank_id não-vazio."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = {r['drug_name'].lower(): r for r in resp.data['results']}

        met = results['metformin']
        self.assertIsNotNone(met['drugbank_url'])
        self.assertEqual(met['drugbank_url'], 'https://go.drugbank.com/drugs/DB00331')

    def test_drugbank_url_null_when_drugbank_id_absent(self):
        """drugbank_url é null quando drugbank_id é vazio."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = {r['drug_name'].lower(): r for r in resp.data['results']}

        asp = results['aspirin']
        self.assertIsNone(asp['drugbank_url'])

    def test_pubchem_search_url_always_present(self):
        """pubchem_search_url está presente em todos os itens, mesmo sem drugbank_id."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        for item in resp.data['results']:
            self.assertIn('pubchem_search_url', item)
            self.assertIsNotNone(item['pubchem_search_url'])
            self.assertTrue(item['pubchem_search_url'].startswith('https://pubchem.ncbi.nlm.nih.gov/#query='))

    def test_pubchem_search_url_well_formed_simple_name(self):
        """pubchem_search_url para nome simples (Metformin) é bem-formada."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = {r['drug_name'].lower(): r for r in resp.data['results']}

        met = results['metformin']
        # Nome representativo (Max) é 'Metformin' — URL deve conter o nome
        self.assertIn('Metformin', met['pubchem_search_url'])

    def test_pubchem_search_url_encodes_spaces(self):
        """pubchem_search_url URL-encoda espaços no nome multi-palavra."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = {r['drug_name'].lower(): r for r in resp.data['results']}

        sc = results['sodium chloride']
        # Espaços devem ser encoded (%20 ou +)
        self.assertNotIn(' ', sc['pubchem_search_url'].split('#query=')[1],
                         'Espaços não devem aparecer literais na URL.')

    def test_payload_has_both_url_fields(self):
        """Ambos os campos de URL estão presentes no payload de cada item."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        for item in resp.data['results']:
            self.assertIn('drugbank_url', item)
            self.assertIn('pubchem_search_url', item)


# =============================================================================
# 3. Testes de isolamento por usuário
# =============================================================================

class DrugUserIsolationTests(APITestCase):
    """
    Usuário B não pode acessar drugs do projeto de usuário A.
    Skill: firebase-auth-guard — _get_project() filtra por request.user.
    """

    def setUp(self):
        self.user_a = make_user('drug_iso_user_a')
        self.user_b = make_user('drug_iso_user_b')

        self.project_a = make_project(self.user_a, title='Projeto Drug A')
        paper = make_paper(pmid=4001, abstract='Metformin is relevant.')
        make_project_paper(self.project_a, paper, curation_status='included')
        make_paper_drug(paper, 'Metformin', drug_name_lower='metformin')

        # Autenticar como usuário B
        self.client.force_authenticate(user=self.user_b)

    def test_user_b_cannot_list_drugs_of_user_a_project(self):
        """Usuário B obtém 404 ao listar drugs do projeto de A."""
        resp = self.client.get(drugs_url(self.project_a.id))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_user_b_cannot_access_drug_detail_of_user_a_project(self):
        """Usuário B obtém 404 ao acessar detalhe de drug do projeto de A."""
        resp = self.client.get(drug_detail_url(self.project_a.id, 'metformin'))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_unauthenticated_cannot_list_drugs(self):
        """Requisição sem autenticação deve retornar 403 ou 401."""
        client = APIClient()
        resp = client.get(drugs_url(self.project_a.id))
        self.assertIn(resp.status_code, [
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        ])

    def test_user_a_can_see_own_project_drugs(self):
        """Usuário A vê seus próprios drugs sem problema."""
        self.client.force_authenticate(user=self.user_a)
        resp = self.client.get(drugs_url(self.project_a.id))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['count'], 1)


# =============================================================================
# 4. Testes de filtros e ordenação
# =============================================================================

class DrugFilterOrderingTests(APITestCase):
    """
    Filtro ?q= por nome (icontains sobre drug_name_lower), ?ordering= nos 4 campos,
    default '-unique_citations_included', paginação.
    """

    def setUp(self):
        self.user = make_user('drug_filter_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Drug Filtros')
        self.url = drugs_url(self.project.id)

        # Metformin: 2 papers included → unique_citations_included=2, mention_count=10
        p1 = make_paper(pmid=5001, abstract='Metformin reduces glucose.')
        p2 = make_paper(pmid=5002, abstract='Metformin HbA1c.')
        make_project_paper(self.project, p1, curation_status='included')
        make_project_paper(self.project, p2, curation_status='included')
        make_paper_drug(p1, 'Metformin', drug_name_lower='metformin', mention_count=4)
        make_paper_drug(p2, 'Metformin', drug_name_lower='metformin', mention_count=6)

        # Aspirin: 1 paper included, mention_count=3
        p3 = make_paper(pmid=5003, abstract='Aspirin study.')
        make_project_paper(self.project, p3, curation_status='included')
        make_paper_drug(p3, 'Aspirin', drug_name_lower='aspirin', mention_count=3)

        # Insulin: 0 included (pending), mention_count=2
        p4 = make_paper(pmid=5004, abstract='Insulin pending.')
        make_project_paper(self.project, p4, curation_status='pending')
        make_paper_drug(p4, 'Insulin', drug_name_lower='insulin', mention_count=2)

    def test_filter_q_by_name(self):
        """?q=metformin filtra por drug_name_lower (icontains)."""
        resp = self.client.get(self.url, {'q': 'metformin'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        names = [r['drug_name'].lower() for r in resp.data['results']]
        self.assertIn('metformin', names)
        self.assertNotIn('aspirin', names)
        self.assertNotIn('insulin', names)

    def test_filter_q_case_insensitive(self):
        """?q=METF (uppercase) também encontra metformin."""
        resp = self.client.get(self.url, {'q': 'METF'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        names = [r['drug_name'].lower() for r in resp.data['results']]
        self.assertIn('metformin', names)

    def test_filter_q_partial_match(self):
        """?q=asp retorna aspirin."""
        resp = self.client.get(self.url, {'q': 'asp'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        names = [r['drug_name'].lower() for r in resp.data['results']]
        self.assertIn('aspirin', names)
        self.assertNotIn('metformin', names)

    def test_default_ordering_is_unique_citations_included_desc(self):
        """Sem ?ordering=, o default é -unique_citations_included (Metformin > Aspirin > Insulin)."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']
        included_values = [r['unique_citations_included'] for r in results]
        self.assertEqual(included_values, sorted(included_values, reverse=True))
        self.assertEqual(results[0]['drug_name'].lower(), 'metformin')

    def test_ordering_drug_name_asc(self):
        """?ordering=drug_name ordena A-Z (pelo drug_name representativo)."""
        resp = self.client.get(self.url, {'ordering': 'drug_name'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        names = [r['drug_name'] for r in resp.data['results']]
        self.assertEqual(names, sorted(names))

    def test_ordering_drug_name_desc(self):
        """?ordering=-drug_name ordena Z-A."""
        resp = self.client.get(self.url, {'ordering': '-drug_name'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        names = [r['drug_name'] for r in resp.data['results']]
        self.assertEqual(names, sorted(names, reverse=True))

    def test_ordering_unique_citations_total_asc(self):
        """?ordering=unique_citations_total ordena crescente."""
        resp = self.client.get(self.url, {'ordering': 'unique_citations_total'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        values = [r['unique_citations_total'] for r in resp.data['results']]
        self.assertEqual(values, sorted(values))

    def test_ordering_mention_count_total_desc(self):
        """?ordering=-mention_count_total: Metformin (10) > Aspirin (3) > Insulin (2)."""
        resp = self.client.get(self.url, {'ordering': '-mention_count_total'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        values = [r['mention_count_total'] for r in resp.data['results']]
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertEqual(resp.data['results'][0]['drug_name'].lower(), 'metformin')

    def test_invalid_ordering_falls_back_to_default(self):
        """?ordering=campo_invalido cai no default sem erro."""
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
        """?page=2&page_size=2 retorna o terceiro drug."""
        resp = self.client.get(self.url, {'page': 2, 'page_size': 2})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data['results']), 1)
        self.assertIsNone(resp.data['next'])


# =============================================================================
# 5. Testes do filtro ?included_only=
# =============================================================================

class DrugIncludedOnlyFilterTests(APITestCase):
    """
    Filtro ?included_only=true na lista GET /projects/{project_pk}/drugs/.

    Caso decisivo:
      - Drug A (insulin): citado apenas em papers pending/maybe (zero included).
      - Drug B (metformin): citado em ao menos um paper included.
      - Sem o filtro → ambos aparecem.
      - Com ?included_only=true → apenas metformin aparece.
      - As contagens (included | total) no payload de metformin não mudam.

    Cobre também:
      - Valores equivalentes: true/1 ativam; false/0/ausente não ativam.
      - Composição com ?ordering= e paginação.
    """

    def setUp(self):
        self.user = make_user('drug_incl_filter_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Drug IncludedOnly Tests')
        self.url = drugs_url(self.project.id)

        # Drug A (insulin): citado em paper pending e paper maybe — zero included
        self.p_pending = make_paper(pmid=6001, abstract='Insulin is a hormone.')
        self.p_maybe   = make_paper(pmid=6002, abstract='Insulin secretion.')
        make_project_paper(self.project, self.p_pending, curation_status='pending')
        make_project_paper(self.project, self.p_maybe,   curation_status='maybe')
        make_paper_drug(self.p_pending, 'Insulin', drug_name_lower='insulin', mention_count=1)
        make_paper_drug(self.p_maybe,   'Insulin', drug_name_lower='insulin', mention_count=1)

        # Drug B (metformin): citado em um paper pending e um paper included
        self.p_incl  = make_paper(pmid=6003, abstract='Metformin reduces glucose.')
        self.p_pend2 = make_paper(pmid=6004, abstract='Metformin HbA1c study.')
        make_project_paper(self.project, self.p_incl,  curation_status='included')
        make_project_paper(self.project, self.p_pend2, curation_status='pending')
        make_paper_drug(self.p_incl,  'Metformin', drug_name_lower='metformin', mention_count=2)
        make_paper_drug(self.p_pend2, 'Metformin', drug_name_lower='metformin', mention_count=1)

    def test_without_filter_both_drugs_present(self):
        """Sem ?included_only, ambos os drugs aparecem na lista."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        names = {r['drug_name'].lower() for r in resp.data['results']}
        self.assertIn('insulin',   names, 'Insulin deve aparecer sem o filtro.')
        self.assertIn('metformin', names, 'Metformin deve aparecer sem o filtro.')

    def test_included_only_true_excludes_drug_with_zero_included(self):
        """?included_only=true remove insulin (zero included) e mantém metformin."""
        resp = self.client.get(self.url, {'included_only': 'true'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        names = {r['drug_name'].lower() for r in resp.data['results']}
        self.assertNotIn('insulin',   names, 'Insulin não deve aparecer com included_only=true.')
        self.assertIn('metformin', names, 'Metformin deve aparecer com included_only=true.')

    def test_included_only_one_excludes_drug_with_zero_included(self):
        """?included_only=1 é equivalente a true."""
        resp = self.client.get(self.url, {'included_only': '1'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        names = {r['drug_name'].lower() for r in resp.data['results']}
        self.assertNotIn('insulin',   names)
        self.assertIn('metformin', names)

    def test_included_only_false_does_not_filter(self):
        """?included_only=false não aplica filtro (default explícito)."""
        resp = self.client.get(self.url, {'included_only': 'false'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        names = {r['drug_name'].lower() for r in resp.data['results']}
        self.assertIn('insulin',   names)
        self.assertIn('metformin', names)

    def test_included_only_zero_does_not_filter(self):
        """?included_only=0 não aplica filtro."""
        resp = self.client.get(self.url, {'included_only': '0'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        names = {r['drug_name'].lower() for r in resp.data['results']}
        self.assertIn('insulin',   names)
        self.assertIn('metformin', names)

    def test_default_no_param_does_not_filter(self):
        """Sem o parâmetro, o comportamento é idêntico a included_only=false."""
        resp_default  = self.client.get(self.url)
        resp_explicit = self.client.get(self.url, {'included_only': 'false'})
        self.assertEqual(resp_default.data['count'], resp_explicit.data['count'])

    def test_included_only_does_not_alter_counts_of_remaining_drug(self):
        """
        Com ?included_only=true, unique_citations_included e unique_citations_total
        de metformin são idênticos aos retornados sem o filtro.
        O filtro só exclui drugs da lista; não altera as contagens dos que ficam.
        """
        resp_all  = self.client.get(self.url)
        resp_incl = self.client.get(self.url, {'included_only': 'true'})

        met_all  = next(r for r in resp_all.data['results']  if r['drug_name'].lower() == 'metformin')
        met_incl = next(r for r in resp_incl.data['results'] if r['drug_name'].lower() == 'metformin')

        self.assertEqual(
            met_all['unique_citations_included'],
            met_incl['unique_citations_included'],
            'unique_citations_included de metformin não deve mudar com o filtro ativo.',
        )
        self.assertEqual(
            met_all['unique_citations_total'],
            met_incl['unique_citations_total'],
            'unique_citations_total de metformin não deve mudar com o filtro ativo.',
        )

    def test_included_only_correct_count_for_metformin(self):
        """
        Com ?included_only=true, metformin tem unique_citations_included=1
        e unique_citations_total=2 (1 included + 1 pending no projeto).
        """
        resp = self.client.get(self.url, {'included_only': 'true'})
        met = next(r for r in resp.data['results'] if r['drug_name'].lower() == 'metformin')
        self.assertEqual(met['unique_citations_included'], 1)
        self.assertEqual(met['unique_citations_total'], 2)

    def test_included_only_composes_with_ordering_drug_name(self):
        """
        ?included_only=true&ordering=drug_name retorna apenas drugs com
        included>0, em ordem alfabética, sem erro.
        """
        # Adicionar terceiro drug com paper included
        p_extra = make_paper(pmid=6005, abstract='Aspirin reduces pain.')
        make_project_paper(self.project, p_extra, curation_status='included')
        make_paper_drug(p_extra, 'Aspirin', drug_name_lower='aspirin', mention_count=3)

        resp = self.client.get(self.url, {'included_only': 'true', 'ordering': 'drug_name'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        names = [r['drug_name'].lower() for r in resp.data['results']]
        self.assertNotIn('insulin', names)
        self.assertIn('aspirin',   names)
        self.assertIn('metformin', names)
        # Ordem A-Z pelo drug_name representativo
        drug_names = [r['drug_name'] for r in resp.data['results']]
        self.assertEqual(drug_names, sorted(drug_names))

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
        ?included_only=true&page_size=1: count reflete apenas os drugs com included>0.
        """
        resp = self.client.get(self.url, {'included_only': 'true', 'page_size': 1})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # Metformin é o único drug com included>0 no setUp
        self.assertEqual(resp.data['count'], 1)
        self.assertEqual(len(resp.data['results']), 1)
        self.assertIsNone(resp.data['next'])

    def test_included_only_true_result_count_in_response(self):
        """resp.data['count'] com ?included_only=true reflete apenas os filtrados."""
        resp_all  = self.client.get(self.url)
        resp_incl = self.client.get(self.url, {'included_only': 'true'})
        self.assertGreater(resp_all.data['count'], resp_incl.data['count'])


# =============================================================================
# 6. Testes de detalhe de drug
# =============================================================================

class DrugDetailTests(APITestCase):
    """
    Detalhe de drug: cache quente (ready), cache frio/stale (computing), 404.
    """

    def setUp(self):
        self.user = make_user('drug_detail_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Drug Detalhe')

        self.paper = make_paper(
            pmid=7001,
            abstract='Metformin was prescribed. Metformin reduced HbA1c levels significantly.',
            pub_year=2022,
        )
        make_project_paper(self.project, self.paper, curation_status='included')
        make_paper_drug(self.paper, 'Metformin', drug_name_lower='metformin',
                        drugbank_id='DB00331', mention_count=2)

    def _detail_url(self, drug_name_lower='metformin'):
        return drug_detail_url(self.project.id, drug_name_lower)

    # --- Cache quente (ready) ---

    def test_detail_cache_hot_returns_ready(self):
        """Cache populado e fresco → context_status='ready'."""
        now = timezone.now()
        make_entity_context_drug(self.paper, 'metformin', 'Metformin was prescribed.', 0, now)
        make_entity_context_drug(self.paper, 'metformin', 'Metformin reduced HbA1c levels significantly.', 1, now)

        resp = self.client.get(self._detail_url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['context_status'], 'ready')

    def test_detail_cache_hot_returns_snippets(self):
        """Snippets do cache retornados na ordem correta."""
        now = timezone.now()
        make_entity_context_drug(self.paper, 'metformin', 'Metformin was prescribed.', 0, now)
        make_entity_context_drug(self.paper, 'metformin', 'Metformin reduced HbA1c levels significantly.', 1, now)

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
        make_entity_context_drug(self.paper, 'metformin', 'Metformin was prescribed.', 0, now)

        resp = self.client.get(self._detail_url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ref = resp.data['references'][0]
        self.assertEqual(ref['pmid'], 7001)
        self.assertEqual(ref['curation_status'], 'included')
        self.assertEqual(ref['pub_year'], 2022)
        self.assertIn('title', ref)
        self.assertIn('journal', ref)
        self.assertIn('snippets', ref)

    # --- Cache frio (computing) ---

    def test_detail_cache_cold_returns_computing(self):
        """Sem EntityContext para o paper → context_status='computing'."""
        with patch('apps.core.tasks.drug_tasks.derive_drug_contexts.delay') as mock_task:
            resp = self.client.get(self._detail_url())

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['context_status'], 'computing')
        mock_task.assert_called_once_with(str(self.project.id), 'metformin')

    def test_detail_cache_cold_dispatches_task(self):
        """Cache frio dispara a task Celery exatamente uma vez."""
        with patch('apps.core.tasks.drug_tasks.derive_drug_contexts.delay') as mock_task:
            self.client.get(self._detail_url())
        mock_task.assert_called_once()

    # --- Cache stale (computing) ---

    def test_detail_cache_stale_returns_computing(self):
        """
        Cache com computed_at anterior ao paper.updated_at → context_status='computing'.
        """
        stale_time = timezone.now() - timedelta(hours=1)
        make_entity_context_drug(self.paper, 'metformin', 'Metformin was prescribed.', 0, stale_time)

        # Simula re-ingestão: forçar updated_at do paper para após o computed_at
        Paper.objects.filter(pk=self.paper.pk).update(updated_at=timezone.now())
        self.paper.refresh_from_db()

        with patch('apps.core.tasks.drug_tasks.derive_drug_contexts.delay') as mock_task:
            resp = self.client.get(self._detail_url())

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['context_status'], 'computing')
        mock_task.assert_called_once()

    # --- 404 para drug inexistente ---

    def test_detail_nonexistent_drug_returns_404(self):
        """Drug não associado a nenhum paper do projeto → 404."""
        resp = self.client.get(self._detail_url('xxxdrugnotfound'))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_detail_drug_name_above_max_len_returns_404(self):
        """drug_name_lower > 255 chars → 404 com mensagem de comprimento máximo."""
        long_name = 'a' * (DRUG_NAME_MAX_LEN + 1)
        resp = self.client.get(self._detail_url(long_name))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn('comprimento máximo', resp.data.get('detail', ''))

    def test_detail_drug_name_at_max_len_not_rejected_by_length(self):
        """
        drug_name_lower exatamente no limite (255 chars) não é rejeitado por comprimento;
        pode retornar 404 por ausência, não por violação de tamanho.
        """
        name_at_limit = 'b' * DRUG_NAME_MAX_LEN
        resp = self.client.get(self._detail_url(name_at_limit))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        detail_msg = resp.data.get('detail', '')
        self.assertNotIn('comprimento máximo', detail_msg,
                         'Drug no limite de 255 chars não deve ser rejeitado por comprimento.')

    def test_detail_aggregated_metrics(self):
        """Detalhe retorna unique_citations_included e total corretos."""
        p2 = make_paper(pmid=7002, abstract='Metformin studied.')
        make_project_paper(self.project, p2, curation_status='pending')
        make_paper_drug(p2, 'Metformin', drug_name_lower='metformin', mention_count=1)

        now = timezone.now()
        make_entity_context_drug(self.paper, 'metformin', 'Metformin was prescribed.', 0, now)
        make_entity_context_drug(p2, 'metformin', 'Metformin studied.', 0, now)

        resp = self.client.get(self._detail_url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # included: só paper pmid=7001 (included); total: 2
        self.assertEqual(resp.data['unique_citations_included'], 1)
        self.assertEqual(resp.data['unique_citations_total'], 2)

    def test_detail_references_include_all_status(self):
        """Lista de referências inclui papers de todos os status de curadoria."""
        p2 = make_paper(pmid=7003, abstract='Metformin pending study.')
        make_project_paper(self.project, p2, curation_status='pending')
        make_paper_drug(p2, 'Metformin', drug_name_lower='metformin', mention_count=1)

        now = timezone.now()
        make_entity_context_drug(self.paper, 'metformin', 'Metformin was prescribed.', 0, now)
        make_entity_context_drug(p2, 'metformin', 'Metformin pending study.', 0, now)

        resp = self.client.get(self._detail_url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data['references']), 2)
        statuses = {r['curation_status'] for r in resp.data['references']}
        self.assertIn('included', statuses)
        self.assertIn('pending', statuses)

    def test_detail_lookup_by_drug_name_lower(self):
        """
        Lookup por drug_name_lower é consistente com a chave gravada na task.
        Criado com drug_name='Metformin', entity_name='metformin' — acesso
        via URL com 'metformin' deve retornar 200.
        """
        now = timezone.now()
        make_entity_context_drug(self.paper, 'metformin', 'Metformin was prescribed.', 0, now)

        resp = self.client.get(self._detail_url('metformin'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['context_status'], 'ready')


# =============================================================================
# 7. Travas de regressão: project_paper_id vs Paper.pk (fix CRÍTICO 007)
# =============================================================================

class DrugDetailReferenceIdTests(APITestCase):
    """
    Garante que cada item de references[] no detalhe de drug carrega:
      - project_paper_id: PK de ProjectPaper (usada no PATCH /projects/{id}/papers/<pk>/).
      - curation_status: status de curadoria do paper neste projeto.
      - NÃO contém o campo 'id' (Paper.pk removido — regressão 007).
    """

    def setUp(self):
        cache.clear()
        self.user = make_user('drug_ref_id_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Drug RefID Tests')

        # Dois papers distintos; confirmar que project_paper_id != pmid
        self.paper_incl = make_paper(pmid=9101, title='Metformin Included', pub_year=2023)
        self.paper_pend = make_paper(pmid=9102, title='Metformin Pending',  pub_year=2022)

        self.pp_incl = make_project_paper(self.project, self.paper_incl, curation_status='included')
        self.pp_pend = make_project_paper(self.project, self.paper_pend, curation_status='pending')

        make_paper_drug(self.paper_incl, 'Metformin', drug_name_lower='metformin', mention_count=2)
        make_paper_drug(self.paper_pend, 'Metformin', drug_name_lower='metformin', mention_count=1)

        # Popular cache para retornar context_status='ready'
        now = timezone.now()
        make_entity_context_drug(self.paper_incl, 'metformin', 'Metformin was given.', 0, now)
        make_entity_context_drug(self.paper_pend, 'metformin', 'Metformin studied.', 0, now)

    def tearDown(self):
        cache.clear()

    def _detail(self):
        return self.client.get(drug_detail_url(self.project.id, 'metformin'))

    def test_references_contain_project_paper_id_field(self):
        """Cada item de references[] contém o campo project_paper_id."""
        resp = self._detail()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        for ref in resp.data['references']:
            self.assertIn(
                'project_paper_id', ref,
                f'Campo project_paper_id ausente em reference pmid={ref.get("pmid")}.',
            )

    def test_references_do_not_contain_id_field(self):
        """
        Trava de regressão: nenhuma referência deve expor o campo 'id' (Paper.pk).
        """
        resp = self._detail()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        for ref in resp.data['references']:
            self.assertNotIn(
                'id', ref,
                f'Campo id não deve existir em references[] (regressão 007); pmid={ref.get("pmid")}.',
            )

    def test_reference_project_paper_id_matches_project_paper_pk(self):
        """project_paper_id corresponde à PK de ProjectPaper, não à de Paper."""
        resp = self._detail()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        refs_by_pmid = {r['pmid']: r for r in resp.data['references']}

        self.assertEqual(
            refs_by_pmid[9101]['project_paper_id'], self.pp_incl.pk,
            'project_paper_id deve ser o PK de ProjectPaper, não de Paper.',
        )
        self.assertEqual(
            refs_by_pmid[9102]['project_paper_id'], self.pp_pend.pk,
            'project_paper_id deve ser o PK de ProjectPaper, não de Paper.',
        )

    def test_reference_curation_status_correct_per_paper(self):
        """curation_status reflete o valor do ProjectPaper deste projeto."""
        resp = self._detail()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        refs_by_pmid = {r['pmid']: r for r in resp.data['references']}
        self.assertEqual(refs_by_pmid[9101]['curation_status'], 'included')
        self.assertEqual(refs_by_pmid[9102]['curation_status'], 'pending')

    def test_project_paper_id_and_curation_status_present_in_computing_state(self):
        """
        Mesmo com context_status='computing' (cache frio), project_paper_id e
        curation_status estão presentes em references[].
        """
        user2  = make_user('drug_ref_id_user2')
        proj2  = make_project(user2, title='Drug Computing State')
        paper3 = make_paper(pmid=9110, title='Metformin cold cache', pub_year=2024)
        pp3    = make_project_paper(proj2, paper3, curation_status='included')
        make_paper_drug(paper3, 'Metformin', drug_name_lower='metformin', mention_count=1)

        self.client.force_authenticate(user=user2)
        with patch('apps.core.tasks.drug_tasks.derive_drug_contexts.delay'):
            resp = self.client.get(drug_detail_url(proj2.id, 'metformin'))

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['context_status'], 'computing')

        refs = resp.data['references']
        self.assertEqual(len(refs), 1)
        self.assertIn('project_paper_id', refs[0])
        self.assertEqual(refs[0]['project_paper_id'], pp3.pk)
        self.assertIn('curation_status', refs[0])
        self.assertEqual(refs[0]['curation_status'], 'included')
        self.assertNotIn('id', refs[0])


class DrugDetailPatchCurationTests(APITestCase):
    """
    Travas de regressão do fix 007 com PKs divergentes forçadas por papers fantasma.

    Estratégia: Criamos Papers "fantasma" antes dos papers do teste para avançar
    o auto-increment de Paper.pk. Os ProjectPaper são criados depois com contador
    independente — garantindo Paper.pk != ProjectPaper.pk em ao menos um par.

    Cobre:
      a) Divergência de PKs: Paper.pk != ProjectPaper.pk.
      b) project_paper_id == ProjectPaper.pk; 'id' ausente (regressão).
      c) PATCH end-to-end usando project_paper_id do detalhe de drug.
      d) PATCH com ProjectPaper.pk de outro projeto/usuário → 404.
    """

    def setUp(self):
        cache.clear()
        self.user = make_user('drug_patch_curation_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Drug Patch Curation Tests')

        # --- Divergência forçada de PKs ---
        # Papers fantasma para avançar o auto-increment de Paper.pk
        for i in range(5):
            Paper.objects.create(
                pmid=29900 + i,
                title=f'Drug Fantasma {i}',
                journal='Ghost',
                pub_year=2000,
                abstract='',
            )

        # Papers reais do teste
        self.paper_a = make_paper(
            pmid=29910, title='Metformin study', pub_year=2023,
            abstract='Metformin reduces blood glucose levels.',
        )
        self.paper_b = make_paper(
            pmid=29911, title='Metformin meta', pub_year=2022,
            abstract='Metformin is widely prescribed for diabetes.',
        )

        # ProjectPaper para o projeto do usuário
        self.pp_a = make_project_paper(self.project, self.paper_a, curation_status='pending')
        self.pp_b = make_project_paper(self.project, self.paper_b, curation_status='pending')

        make_paper_drug(self.paper_a, 'Metformin', drug_name_lower='metformin', mention_count=2)
        make_paper_drug(self.paper_b, 'Metformin', drug_name_lower='metformin', mention_count=1)

        # Popular EntityContext para context_status='ready'
        now = timezone.now()
        make_entity_context_drug(self.paper_a, 'metformin',
                                 'Metformin reduces blood glucose levels.', 0, now)
        make_entity_context_drug(self.paper_b, 'metformin',
                                 'Metformin is widely prescribed for diabetes.', 0, now)

    def tearDown(self):
        cache.clear()

    # ------------------------------------------------------------------
    # a) Divergência de PKs
    # ------------------------------------------------------------------

    def test_paper_pk_and_project_paper_pk_differ(self):
        """
        Garante que Paper.pk != ProjectPaper.pk para ao menos um dos papers.
        Se este teste falhar, a trava de regressão abaixo seria ineficaz.
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

    # ------------------------------------------------------------------
    # b) project_paper_id == ProjectPaper.pk; 'id' ausente
    # ------------------------------------------------------------------

    def test_project_paper_id_equals_pp_pk_with_divergent_fixtures(self):
        """
        project_paper_id no detalhe de drug == ProjectPaper.pk,
        em fixture onde Paper.pk e ProjectPaper.pk necessariamente divergem.
        """
        resp = self.client.get(drug_detail_url(self.project.id, 'metformin'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        refs_by_pmid = {r['pmid']: r for r in resp.data['references']}

        self.assertEqual(
            refs_by_pmid[29910]['project_paper_id'], self.pp_a.pk,
            f'project_paper_id ({refs_by_pmid[29910]["project_paper_id"]}) '
            f'deve ser pp_a.pk ({self.pp_a.pk}), não paper_a.pk ({self.paper_a.pk}).',
        )
        self.assertEqual(
            refs_by_pmid[29911]['project_paper_id'], self.pp_b.pk,
            f'project_paper_id ({refs_by_pmid[29911]["project_paper_id"]}) '
            f'deve ser pp_b.pk ({self.pp_b.pk}), não paper_b.pk ({self.paper_b.pk}).',
        )

    def test_project_paper_id_not_equal_to_paper_pk_with_divergent_fixtures(self):
        """
        Trava do bug original: project_paper_id NÃO deve ser igual ao Paper.pk.
        """
        resp = self.client.get(drug_detail_url(self.project.id, 'metformin'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        refs_by_pmid = {r['pmid']: r for r in resp.data['references']}

        mismatch_a = refs_by_pmid[29910]['project_paper_id'] != self.paper_a.pk
        mismatch_b = refs_by_pmid[29911]['project_paper_id'] != self.paper_b.pk
        self.assertTrue(
            mismatch_a or mismatch_b,
            f'project_paper_id coincide com Paper.pk em ambos os pares — '
            f'isso invalida a trava de regressão. '
            f'paper_a.pk={self.paper_a.pk} pp_a.pk={self.pp_a.pk}; '
            f'paper_b.pk={self.paper_b.pk} pp_b.pk={self.pp_b.pk}.',
        )

    def test_id_field_absent_in_references_with_divergent_fixtures(self):
        """
        Campo 'id' (Paper.pk, removido pelo fix 007) não deve aparecer em references[].
        """
        resp = self.client.get(drug_detail_url(self.project.id, 'metformin'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        for ref in resp.data['references']:
            self.assertNotIn(
                'id', ref,
                f'Campo id não deve existir em references[] (regressão 007); '
                f'pmid={ref.get("pmid")}.',
            )

    # ------------------------------------------------------------------
    # c) PATCH end-to-end usando project_paper_id do detalhe de drug
    # ------------------------------------------------------------------

    def test_patch_using_project_paper_id_from_drug_detail_changes_correct_record(self):
        """
        Fluxo completo do toggle de curadoria via painel de drugs:
          1. GET detalhe do drug → obter project_paper_id de cada referência.
          2. PATCH papers/<project_paper_id>/ com curation_status='included'.
          3. Verificar que o ProjectPaper correto foi alterado no banco.
          4. Verificar que o outro ProjectPaper NÃO foi alterado.
        """
        # Passo 1: obter project_paper_id do detalhe de drug
        resp_detail = self.client.get(drug_detail_url(self.project.id, 'metformin'))
        self.assertEqual(resp_detail.status_code, status.HTTP_200_OK)

        refs_by_pmid = {r['pmid']: r for r in resp_detail.data['references']}
        pp_id_a = refs_by_pmid[29910]['project_paper_id']

        # Passo 2: PATCH usando a PK vinda do detalhe de drug
        patch_url = f'/api/v1/projects/{self.project.id}/papers/{pp_id_a}/'
        resp_patch = self.client.patch(
            patch_url,
            data={'curation_status': 'included'},
            format='json',
        )
        self.assertEqual(
            resp_patch.status_code, status.HTTP_200_OK,
            f'PATCH {patch_url} retornou {resp_patch.status_code}: {resp_patch.data}',
        )

        # Passo 3: verificar que pp_a foi atualizado
        self.pp_a.refresh_from_db()
        self.assertEqual(
            self.pp_a.curation_status, 'included',
            'ProjectPaper alvo deve ter curation_status=included após o PATCH.',
        )

        # Passo 4: verificar que pp_b NÃO foi alterado
        self.pp_b.refresh_from_db()
        self.assertEqual(
            self.pp_b.curation_status, 'pending',
            'ProjectPaper não-alvo NÃO deve ter sido alterado pelo PATCH.',
        )

    def test_patch_using_project_paper_id_preserves_curated_at(self):
        """
        Skill curation-audit-trail: curated_at deve ser gravado no PATCH.
        """
        resp_detail = self.client.get(drug_detail_url(self.project.id, 'metformin'))
        self.assertEqual(resp_detail.status_code, status.HTTP_200_OK)

        refs_by_pmid = {r['pmid']: r for r in resp_detail.data['references']}
        pp_id_a = refs_by_pmid[29910]['project_paper_id']

        before = timezone.now()
        self.client.patch(
            f'/api/v1/projects/{self.project.id}/papers/{pp_id_a}/',
            data={'curation_status': 'included'},
            format='json',
        )
        after = timezone.now()

        self.pp_a.refresh_from_db()
        self.assertIsNotNone(
            self.pp_a.curated_at,
            'curated_at deve ser gravado após PATCH (skill curation-audit-trail).',
        )
        self.assertGreaterEqual(self.pp_a.curated_at, before)
        self.assertLessEqual(self.pp_a.curated_at, after)

    # ------------------------------------------------------------------
    # d) Isolamento cross-user: PATCH com ProjectPaper.pk de outro projeto → 404
    # ------------------------------------------------------------------

    def test_patch_with_other_users_project_paper_pk_returns_404(self):
        """
        Isolamento: PATCH papers/<pp_pk>/ onde pp_pk pertence a projeto de outro
        usuário deve retornar 404 — não vaza dados nem corrompe curadoria cross-user.
        """
        other_user    = make_user('other_drug_patch_user')
        other_project = make_project(other_user, title='Outro Projeto Drug Patch')
        other_paper   = make_paper(pmid=29920, abstract='Metformin other.')
        other_pp      = make_project_paper(other_project, other_paper, curation_status='pending')
        make_paper_drug(other_paper, 'Metformin', drug_name_lower='metformin')

        # Usuário logado tenta PATCH no ProjectPaper do outro usuário
        patch_url = f'/api/v1/projects/{self.project.id}/papers/{other_pp.pk}/'
        resp = self.client.patch(
            patch_url,
            data={'curation_status': 'included'},
            format='json',
        )
        self.assertEqual(
            resp.status_code, status.HTTP_404_NOT_FOUND,
            f'PATCH com ProjectPaper.pk de outro usuário deve retornar 404; '
            f'retornou {resp.status_code}.',
        )

        # ProjectPaper do outro usuário não deve ter sido alterado
        other_pp.refresh_from_db()
        self.assertEqual(other_pp.curation_status, 'pending')


# =============================================================================
# 8. Testes da task de contexto (idempotência, regex \b, sentinela, invalidação)
# =============================================================================

class DrugContextTaskTests(APITestCase):
    """
    Testes da lógica de derive_and_persist_contexts e extract_drug_sentences.
    A task Celery é chamada diretamente via DrugService para não depender do broker.
    """

    def setUp(self):
        self.user = make_user('drug_task_user')
        self.project = make_project(self.user, title='Drug Task Tests')

        self.abstract = (
            'Metformin was administered to all patients. '
            'The levels of Metformin correlated with HbA1c reduction. '
            'Metformin-treated patients showed improvement.'
        )
        self.paper = make_paper(
            pmid=8001,
            abstract=self.abstract,
            pub_year=2021,
        )
        make_project_paper(self.project, self.paper, curation_status='included')
        make_paper_drug(self.paper, 'Metformin', drug_name_lower='metformin', mention_count=3)

    # --- Idempotência ---

    def test_derive_twice_does_not_duplicate(self):
        """Rodar derive_and_persist_contexts duas vezes não duplica EntityContext."""
        DrugService.derive_and_persist_contexts(self.project, 'metformin')
        DrugService.derive_and_persist_contexts(self.project, 'metformin')

        count = EntityContext.objects.filter(
            paper=self.paper,
            entity_type=EntityContext.EntityType.DRUG,
            entity_name='metformin',
        ).count()
        # Deve existir apenas o número de sentenças únicas (não o dobro)
        snippets = DrugService.extract_drug_sentences(self.paper, 'Metformin')
        self.assertEqual(count, len(snippets))

    def test_derive_returns_correct_snippet_count(self):
        """Retorna o número correto de snippets derivados (sentinelas não contam)."""
        # Abstract tem 3 sentenças; todas contêm 'Metformin' com fronteira de palavra
        n = DrugService.derive_and_persist_contexts(self.project, 'metformin')
        self.assertEqual(n, 3)

    # --- Fronteira de palavra (regex \b) ---

    def test_word_boundary_metformin_matches_all_occurrences(self):
        """
        'Metformin' com \b deve casar nas 3 sentenças do abstract.
        """
        snippets = DrugService.extract_drug_sentences(self.paper, 'Metformin')
        self.assertEqual(len(snippets), 3)

    def test_word_boundary_does_not_match_partial_name(self):
        """
        Regex \b não deve casar 'Met' dentro de 'Metformin' (verificação de
        que a busca por substring não expande além da fronteira).
        """
        # Se buscarmos por 'Met' com \b, não deve casar com 'Metformin'
        paper_met = make_paper(pmid=8010, abstract='Met protein levels were low. Metformin was given.')
        snippets_met = DrugService.extract_drug_sentences(paper_met, 'Met')
        # 'Met' com \b deve casar com 'Met protein levels were low.' mas não 'Metformin was given.'
        sentences = [s['sentence'] for s in snippets_met]
        # 'Metformin was given.' não deve aparecer na busca de 'Met'
        metformin_leaked = any('Metformin' in s and 'Met' not in s.replace('Metformin', '') for s in sentences)
        self.assertFalse(metformin_leaked,
                         'Busca por "Met" com \\b não deve casar dentro de "Metformin".')

    def test_case_insensitive_match(self):
        """Match é case-insensitive: busca por 'METFORMIN' encontra 'Metformin'."""
        snippets = DrugService.extract_drug_sentences(self.paper, 'METFORMIN')
        self.assertEqual(len(snippets), 3)

    def test_drug_name_rep_used_for_match_not_lowercase(self):
        """
        A task usa drug_name representativo (ex: 'Metformin') para o match no abstract,
        não drug_name_lower ('metformin'). O resultado deve ser o mesmo (case-insensitive).
        """
        n = DrugService.derive_and_persist_contexts(self.project, 'metformin')
        self.assertEqual(n, 3, 'Deve encontrar 3 snippets usando drug_name representativo.')

    def test_entity_name_is_drug_name_lower(self):
        """
        entity_name gravado no EntityContext é drug_name_lower (chave canônica),
        não drug_name representativo.
        """
        DrugService.derive_and_persist_contexts(self.project, 'metformin')
        contexts = EntityContext.objects.filter(
            paper=self.paper,
            entity_type=EntityContext.EntityType.DRUG,
        )
        for ctx in contexts:
            self.assertEqual(
                ctx.entity_name, 'metformin',
                f'entity_name deve ser drug_name_lower "metformin", não "{ctx.entity_name}".',
            )

    def test_snippets_content_correct(self):
        """Snippets derivados correspondem às sentenças esperadas do abstract."""
        snippets = DrugService.extract_drug_sentences(self.paper, 'Metformin')
        sentences = [s['sentence'] for s in snippets]

        self.assertIn('Metformin was administered to all patients.', sentences)
        self.assertIn('The levels of Metformin correlated with HbA1c reduction.', sentences)
        self.assertIn('Metformin-treated patients showed improvement.', sentences)

    def test_sentence_position_zero_based(self):
        """sentence_position é 0-based e reflete a posição no abstract dividido."""
        snippets = DrugService.extract_drug_sentences(self.paper, 'Metformin')
        positions = {s['sentence_position'] for s in snippets}
        self.assertIn(0, positions)
        self.assertIn(1, positions)
        self.assertIn(2, positions)

    def test_empty_abstract_returns_no_snippets(self):
        """Abstract vazio retorna lista vazia."""
        paper_empty = make_paper(pmid=8002, abstract='')
        make_project_paper(self.project, paper_empty, curation_status='included')
        make_paper_drug(paper_empty, 'Metformin', drug_name_lower='metformin')

        snippets = DrugService.extract_drug_sentences(paper_empty, 'Metformin')
        self.assertEqual(snippets, [])

    def test_drug_not_in_abstract_returns_no_snippets(self):
        """Drug ausente do abstract retorna lista vazia."""
        paper_other = make_paper(pmid=8003, abstract='IL6 and TNF were elevated.')
        snippets = DrugService.extract_drug_sentences(paper_other, 'Metformin')
        self.assertEqual(snippets, [])

    # --- Sentinela -1 ---

    def test_sentinel_written_when_drug_absent_from_abstract(self):
        """
        Quando o drug não aparece no abstract, grava sentinela (sentence_position=-1).
        Regressão: sem sentinela, context_status ficaria 'computing' para sempre.
        """
        paper = make_paper(pmid=8004, abstract='Only cytokines mentioned here.')
        make_project_paper(self.project, paper, curation_status='included')
        make_paper_drug(paper, 'Metformin', drug_name_lower='metformin', mention_count=1)

        DrugService.derive_and_persist_contexts(self.project, 'metformin')

        sentinel = EntityContext.objects.filter(
            paper=paper,
            entity_type=EntityContext.EntityType.DRUG,
            entity_name='metformin',
            sentence_position=-1,
        )
        self.assertTrue(sentinel.exists(), 'Sentinela deve ser gravada quando drug não aparece no abstract.')

    def test_sentinel_written_for_empty_abstract(self):
        """Abstract vazio → sentinela gravada."""
        paper = make_paper(pmid=8005, abstract='')
        make_project_paper(self.project, paper, curation_status='included')
        make_paper_drug(paper, 'Metformin', drug_name_lower='metformin', mention_count=1)

        DrugService.derive_and_persist_contexts(self.project, 'metformin')

        sentinel = EntityContext.objects.filter(
            paper=paper,
            entity_type=EntityContext.EntityType.DRUG,
            entity_name='metformin',
            sentence_position=-1,
        )
        self.assertTrue(sentinel.exists(), 'Sentinela deve ser gravada para abstract vazio.')

    def test_sentinel_not_duplicated_on_double_derive(self):
        """
        Rodar derivação 2x para paper sem drug no abstract → exatamente 1 linha
        (sentinela), não 2.
        """
        paper = make_paper(pmid=8006, abstract='No drug here.')
        make_project_paper(self.project, paper, curation_status='included')
        make_paper_drug(paper, 'Metformin', drug_name_lower='metformin', mention_count=1)

        DrugService.derive_and_persist_contexts(self.project, 'metformin')
        DrugService.derive_and_persist_contexts(self.project, 'metformin')

        count = EntityContext.objects.filter(
            paper=paper,
            entity_type=EntityContext.EntityType.DRUG,
            entity_name='metformin',
        ).count()
        self.assertEqual(count, 1, 'Deve existir exatamente 1 linha (sentinela) após 2 derivações.')

    def test_sentinel_does_not_leak_as_snippet_in_response(self):
        """
        Nenhum snippet na resposta deve ter sentence_position == -1.
        A sentinela é interna; o endpoint nunca a entrega ao cliente.
        """
        user2 = make_user('drug_sentinel_leak_user')
        proj2 = make_project(user2, title='Drug Sentinel Leak Test')
        paper = make_paper(pmid=8007, abstract='Only cytokines here.')
        make_project_paper(proj2, paper, curation_status='included')
        make_paper_drug(paper, 'Metformin', drug_name_lower='metformin', mention_count=1)

        DrugService.derive_and_persist_contexts(proj2, 'metformin')

        self.client.force_authenticate(user=user2)
        resp = self.client.get(drug_detail_url(proj2.id, 'metformin'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        for ref in resp.data.get('references', []):
            for snippet in ref.get('snippets', []):
                self.assertNotEqual(
                    snippet.get('sentence_position'), -1,
                    'Sentinela (sentence_position=-1) não deve vazar como snippet na resposta.',
                )

    def test_drug_absent_from_abstract_returns_ready_after_derive(self):
        """
        Após derivação, detalhe retorna context_status='ready' mesmo quando
        o drug não aparece no abstract.
        Regressão: antes gravava zero linhas → computed_at=None → 'computing' eterno.
        """
        user2 = make_user('drug_absent_abstract_user')
        proj2 = make_project(user2, title='Drug Absent Abstract Test')
        paper = make_paper(pmid=8008, abstract='Inflammatory markers were elevated.')
        make_project_paper(proj2, paper, curation_status='included')
        make_paper_drug(paper, 'Metformin', drug_name_lower='metformin', mention_count=1)

        DrugService.derive_and_persist_contexts(proj2, 'metformin')

        self.client.force_authenticate(user=user2)
        resp = self.client.get(drug_detail_url(proj2.id, 'metformin'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            resp.data['context_status'], 'ready',
            'context_status deve ser "ready" após derivação, mesmo sem snippets no abstract.',
        )

    def test_drug_absent_reference_present_snippets_empty(self):
        """
        A referência aparece na lista mas snippets é lista vazia —
        o drug é citado no paper mas não tem trecho de abstract associado.
        """
        user2 = make_user('drug_absent_abs_user2')
        proj2 = make_project(user2, title='Drug Absent Abs Ref Test')
        paper = make_paper(pmid=8009, abstract='Inflammatory markers were elevated.')
        make_project_paper(proj2, paper, curation_status='included')
        make_paper_drug(paper, 'Metformin', drug_name_lower='metformin', mention_count=1)

        DrugService.derive_and_persist_contexts(proj2, 'metformin')

        self.client.force_authenticate(user=user2)
        resp = self.client.get(drug_detail_url(proj2.id, 'metformin'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        refs = resp.data['references']
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]['pmid'], 8009)
        self.assertEqual(refs[0]['snippets'], [], 'snippets deve ser [] — drug não aparece no abstract.')

    # --- Invalidação após mudança de abstract ---

    def test_invalidation_after_abstract_change(self):
        """
        Após mudar o abstract do paper (updated_at avança),
        snippets antigos ficam stale e são recomputados na segunda chamada.
        """
        # Primeira derivação
        DrugService.derive_and_persist_contexts(self.project, 'metformin')
        first_count = EntityContext.objects.filter(
            paper=self.paper,
            entity_type=EntityContext.EntityType.DRUG,
            entity_name='metformin',
        ).count()
        self.assertEqual(first_count, 3)

        # Mudar abstract (simula re-ingestão)
        new_abstract = 'Metformin was given once.'
        Paper.objects.filter(pk=self.paper.pk).update(
            abstract=new_abstract,
            updated_at=timezone.now(),
        )
        self.paper.refresh_from_db()

        # Segunda derivação (deve limpar stale e recriar)
        DrugService.derive_and_persist_contexts(self.project, 'metformin')
        second_count = EntityContext.objects.filter(
            paper=self.paper,
            entity_type=EntityContext.EntityType.DRUG,
            entity_name='metformin',
        ).count()

        # Novo abstract tem 1 sentença com Metformin
        new_snippets = DrugService.extract_drug_sentences(self.paper, 'Metformin')
        self.assertEqual(second_count, len(new_snippets))
        self.assertEqual(second_count, 1)

    def test_derive_persists_computed_at(self):
        """computed_at é gravado em cada EntityContext derivado."""
        before = timezone.now()
        DrugService.derive_and_persist_contexts(self.project, 'metformin')
        after = timezone.now()

        contexts = EntityContext.objects.filter(
            paper=self.paper,
            entity_type=EntityContext.EntityType.DRUG,
            entity_name='metformin',
        )
        for ctx in contexts:
            self.assertIsNotNone(ctx.computed_at)
            self.assertGreaterEqual(ctx.computed_at, before)
            self.assertLessEqual(ctx.computed_at, after)

    def test_derive_multiple_papers_in_project(self):
        """derive_and_persist_contexts processa todos os papers do projeto para o drug."""
        p2 = make_paper(pmid=8020, abstract='Metformin therapy confirmed.')
        make_project_paper(self.project, p2, curation_status='pending')
        make_paper_drug(p2, 'Metformin', drug_name_lower='metformin', mention_count=1)

        n = DrugService.derive_and_persist_contexts(self.project, 'metformin')
        # paper original: 3 snippets; p2: 1 snippet → total 4
        self.assertEqual(n, 4)

        count = EntityContext.objects.filter(
            entity_type=EntityContext.EntityType.DRUG,
            entity_name='metformin',
        ).count()
        self.assertEqual(count, 4)

    # --- Integração com a task Celery (síncrona) ---

    def test_celery_task_calls_service(self):
        """
        A Celery task derive_drug_contexts chama DrugService.derive_and_persist_contexts.
        Testada chamando run() diretamente (sem broker).
        """
        from apps.core.tasks.drug_tasks import derive_drug_contexts

        n_before = EntityContext.objects.filter(
            paper=self.paper,
            entity_type=EntityContext.EntityType.DRUG,
            entity_name='metformin',
        ).count()
        self.assertEqual(n_before, 0)

        # Chama a função subjacente da task sem broker
        derive_drug_contexts.run(str(self.project.id), 'metformin')

        n_after = EntityContext.objects.filter(
            paper=self.paper,
            entity_type=EntityContext.EntityType.DRUG,
            entity_name='metformin',
        ).count()
        self.assertEqual(n_after, 3)

    def test_celery_task_nonexistent_project_does_not_raise(self):
        """
        Task com project_id inválido deve retornar sem levantar exceção
        (projeto não encontrado → log warning + return).
        """
        from apps.core.tasks.drug_tasks import derive_drug_contexts

        fake_id = str(uuid.uuid4())
        try:
            derive_drug_contexts.run(fake_id, 'metformin')
        except Exception as exc:
            self.fail(f'Task levantou exceção inesperada: {exc}')


# =============================================================================
# 9. Testes do lock de disparo de task Celery
# =============================================================================

class DrugLockDispatchTests(APITestCase):
    """
    Regressão: lock de disparo de task Celery.

    Dois GETs seguidos com cache frio não devem enfileirar a task duas vezes
    enquanto o lock está ativo. cache.add() é atômico — apenas o primeiro GET
    adquire o lock e dispara a task.

    Nota: _derive_lock_key usa MD5(drug_name_lower)[:16] para evitar caracteres
    especiais no nome do medicamento (como espaços). O formato do lock é:
        drug_derive_lock:{project_id}:{md5_hash_16chars}
    """

    def setUp(self):
        cache.clear()
        self.user = make_user('drug_lock_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Drug Lock Tests')

        # Paper com abstract que contém Metformin — estado frio (sem EntityContext)
        self.paper = make_paper(
            pmid=8030,
            abstract='Metformin was elevated in all samples.',
        )
        make_project_paper(self.project, self.paper, curation_status='included')
        make_paper_drug(self.paper, 'Metformin', drug_name_lower='metformin', mention_count=1)

    def tearDown(self):
        cache.clear()

    def test_two_cold_gets_dispatch_task_at_most_once(self):
        """
        Dois GETs consecutivos com cache frio disparam a task no máximo uma vez
        enquanto o lock está ativo.
        """
        with patch('apps.core.tasks.drug_tasks.derive_drug_contexts.delay') as mock_delay:
            self.client.get(drug_detail_url(self.project.id, 'metformin'))
            self.client.get(drug_detail_url(self.project.id, 'metformin'))

        self.assertEqual(
            mock_delay.call_count, 1,
            f'A task deve ser disparada exatamente 1 vez; foi disparada {mock_delay.call_count} vez(es).',
        )

    def test_first_cold_get_dispatches_task(self):
        """O primeiro GET com cache frio deve disparar a task."""
        with patch('apps.core.tasks.drug_tasks.derive_drug_contexts.delay') as mock_delay:
            resp = self.client.get(drug_detail_url(self.project.id, 'metformin'))

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['context_status'], 'computing')
        mock_delay.assert_called_once_with(str(self.project.id), 'metformin')

    def test_lock_key_format(self):
        """
        A chave de lock usa MD5 do drug_name_lower (primeiros 16 hex chars).
        Garante que a chave é específica por (projeto, drug_name_lower).
        """
        name_hash = hashlib.md5('metformin'.encode()).hexdigest()[:16]
        expected = f'drug_derive_lock:{self.project.id}:{name_hash}'
        actual = _derive_lock_key(str(self.project.id), 'metformin')
        self.assertEqual(actual, expected)

    def test_lock_isolates_different_drugs(self):
        """
        Lock de metformin não bloqueia disparo de task para aspirin no mesmo projeto.
        """
        paper2 = make_paper(pmid=8031, abstract='Aspirin reduced fever.')
        make_project_paper(self.project, paper2, curation_status='included')
        make_paper_drug(paper2, 'Aspirin', drug_name_lower='aspirin', mention_count=1)

        with patch('apps.core.tasks.drug_tasks.derive_drug_contexts.delay') as mock_delay:
            # Primeiro GET adquire lock para metformin
            self.client.get(drug_detail_url(self.project.id, 'metformin'))
            # Segundo GET mesmo drug — lock ativo, não reenfileira
            self.client.get(drug_detail_url(self.project.id, 'metformin'))
            # GET para aspirin — lock diferente, deve disparar task
            self.client.get(drug_detail_url(self.project.id, 'aspirin'))

        self.assertEqual(
            mock_delay.call_count, 2,
            'Deve haver 2 disparos: 1 para metformin (primeiro GET) + 1 para aspirin.',
        )
        calls_drugs = [call.args[1] for call in mock_delay.call_args_list]
        self.assertIn('metformin', calls_drugs)
        self.assertIn('aspirin', calls_drugs)
