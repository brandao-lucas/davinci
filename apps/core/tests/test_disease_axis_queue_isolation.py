"""
Testes de QA — DiseaseAxisCurationQueueView (Fase 3, OmnisPathway).

Cobre o isolamento por usuário (Regra #3 — firebase-auth-guard) e as
políticas de conteúdo do endpoint:

  GET /api/v1/projects/{project_pk}/disease-axis-queue/
  → DiseaseAxisCurationQueueView, name='project-disease-axis-queue-list'

Casos:
  1. Acesso cruzado: usuário B tenta GET da fila do projeto A → 404.
  2. Sem vazamento de queryset: fila de A retorna SOMENTE datasets do projeto A.
  3. Política de conteúdo: mixed + discordância aparecem; indeterminate puro EXCLUI.
  4. queue_reason correto: 'mixed' para disease_axis='mixed', 'discordance' para
     monogenic_gene_hit presente + axis fora de {monogenic, mixed}.
  5. Serializer não vaza: extra_metadata inteiro nem contract_confidence inteiro
     não aparecem na resposta (apenas sub-chaves curadas).
  6. Não autenticado: 401/403.

Padrão: APITestCase DRF (sem pytest).
Requer Postgres real (JSONB).
Sem chamadas externas (sem internet em testes).
"""

from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from apps.core.models import DaVinciProject, OmicDataset, ProjectDataset


# =============================================================================
# Helpers
# =============================================================================

def make_user(username, password='pw'):
    return User.objects.create_user(username=username, password=password)


def make_project(user, title='Test Project'):
    slug = f'{title.lower().replace(" ", "-")}-{user.username}-daqt'
    return DaVinciProject.objects.create(
        user=user,
        title=title,
        slug=slug,
        query_term='test',
    )


def make_dataset(accession, source_db='geo', title='Dataset',
                 summary='', disease_axis='indeterminate',
                 extra_metadata=None, contract_confidence=None, **kwargs):
    """Cria OmicDataset com campos mínimos e campos de Fase 3."""
    return OmicDataset.objects.create(
        accession=accession,
        source_db=source_db,
        title=title,
        summary=summary,
        disease_axis=disease_axis,
        extra_metadata=extra_metadata or {},
        contract_confidence=contract_confidence or {},
        **kwargs,
    )


def make_project_dataset(project, dataset, curation_status='pending', notes=''):
    return ProjectDataset.objects.create(
        project=project,
        dataset=dataset,
        curation_status=curation_status,
        notes=notes,
    )


def disease_axis_queue_url(project_pk):
    """URL da fila de disease_axis (project_pk é UUID)."""
    return f'/api/v1/projects/{project_pk}/disease-axis-queue/'


def _gene_hit_payload(genes=None):
    """Monta extra_metadata com monogenic_gene_hit para simular discordância."""
    genes = genes or ['CFTR']
    return {
        'contract': {
            'monogenic_gene_hit': {
                'genes': genes,
                'confidence': 0.6,
                'gene_details': [
                    {'gene_symbol': g, 'score': 0.6, 'sources': ['geo_metadata'],
                     'mendelian_confidence': 'moderate'}
                    for g in genes
                ],
            }
        }
    }


# =============================================================================
# 1. Acesso cruzado — usuário B tenta GET da fila do projeto A → 404
# =============================================================================

class DiseaseAxisQueueCrossUserAccessTests(APITestCase):
    """
    Regra #3: projeto alheio retorna 404 (não 403 — não vaza existência).
    _get_project() usa get_object_or_404(DaVinciProject, pk=..., user=request.user).
    """

    def setUp(self):
        self.user_a = make_user('daq_cross_a')
        self.user_b = make_user('daq_cross_b')

        self.client_a = APIClient()
        self.client_b = APIClient()
        self.client_a.force_authenticate(user=self.user_a)
        self.client_b.force_authenticate(user=self.user_b)

        self.project_a = make_project(self.user_a, title='Project A DAQ')
        self.project_b = make_project(self.user_b, title='Project B DAQ')

        # Dataset 'mixed' no projeto de A (apareceria na fila de A)
        self.ds_a = make_dataset(
            'DAQ_CROSS_DS_A',
            disease_axis='mixed',
            contract_confidence={
                'disease_axis': {
                    'score': 0.9,
                    'method': 'exact',
                    'n_candidates': 1,
                    'axis_hits': {
                        'monogenic': {'score': 1.0, 'method': 'exact',
                                      'matched_name': 'Cystic fibrosis',
                                      'matched_source': 'orphanet'},
                        'multifactorial': {'score': 1.0, 'method': 'exact',
                                           'matched_name': 'Asthma',
                                           'matched_source': 'gwas_catalog'},
                    },
                }
            },
        )
        make_project_dataset(self.project_a, self.ds_a)

    def test_user_b_gets_404_on_project_a_queue(self):
        """
        User B → GET fila do projeto A → 404.
        Não deve vazar que o projeto existe (sem 403).
        """
        response = self.client_b.get(disease_axis_queue_url(self.project_a.id))
        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND,
            'User B deve receber 404 ao acessar fila do projeto A '
            '(Regra #3: _get_project filtra por request.user).',
        )

    def test_user_a_gets_404_on_project_b_queue(self):
        """
        User A → GET fila do projeto B → 404.
        Isolamento simétrico.
        """
        response = self.client_a.get(disease_axis_queue_url(self.project_b.id))
        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND,
            'User A deve receber 404 ao acessar fila do projeto B.',
        )

    def test_user_a_can_access_own_queue(self):
        """
        User A → GET fila do próprio projeto → 200.
        Acesso legítimo não é bloqueado.
        """
        response = self.client_a.get(disease_axis_queue_url(self.project_a.id))
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            'User A deve acessar normalmente a fila do seu próprio projeto.',
        )


# =============================================================================
# 2. Sem vazamento de queryset — fila de A retorna somente datasets do projeto A
# =============================================================================

class DiseaseAxisQueueDatasetIsolationTests(APITestCase):
    """
    Mesmo que datasets de A e B coexistam no banco com disease_axis='mixed',
    a fila de A retorna SOMENTE os ProjectDatasets do projeto de A.
    high_value_queue_queryset() filtra por project=project (derivado do user).
    """

    def setUp(self):
        self.user_a = make_user('daq_iso_a')
        self.user_b = make_user('daq_iso_b')

        self.client_a = APIClient()
        self.client_b = APIClient()
        self.client_a.force_authenticate(user=self.user_a)
        self.client_b.force_authenticate(user=self.user_b)

        self.project_a = make_project(self.user_a, title='Isolation A')
        self.project_b = make_project(self.user_b, title='Isolation B')

        # Dataset 'mixed' no projeto de A
        self.ds_a = make_dataset(
            'DAQ_ISO_DS_A',
            title='Dataset of A',
            disease_axis='mixed',
        )
        self.pd_a = make_project_dataset(self.project_a, self.ds_a)

        # Dataset 'mixed' no projeto de B (mesmo banco, não deve aparecer para A)
        self.ds_b = make_dataset(
            'DAQ_ISO_DS_B',
            title='Dataset of B',
            disease_axis='mixed',
        )
        self.pd_b = make_project_dataset(self.project_b, self.ds_b)

        # Dataset discordante no projeto de B
        self.ds_b_disc = make_dataset(
            'DAQ_ISO_DS_B_DISC',
            title='Discordant Dataset of B',
            disease_axis='multifactorial',
            extra_metadata=_gene_hit_payload(['BRCA1']),
        )
        self.pd_b_disc = make_project_dataset(self.project_b, self.ds_b_disc)

    def test_user_a_sees_only_own_datasets(self):
        """
        Fila de A contém apenas o pd_a — pd_b e pd_b_disc de B não aparecem.
        Verifica ausência por PK de ProjectDataset.
        """
        response = self.client_a.get(disease_axis_queue_url(self.project_a.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        pks = [item['id'] for item in response.data]

        self.assertIn(
            self.pd_a.pk, pks,
            'pd_a (mixed, projeto A) deve aparecer na fila de A.',
        )
        self.assertNotIn(
            self.pd_b.pk, pks,
            'pd_b (mixed, projeto B) NÃO deve aparecer na fila de A.',
        )
        self.assertNotIn(
            self.pd_b_disc.pk, pks,
            'pd_b_disc (discordante, projeto B) NÃO deve aparecer na fila de A.',
        )

    def test_user_b_sees_only_own_datasets(self):
        """
        Fila de B contém pd_b e pd_b_disc — pd_a de A não aparece.
        """
        response = self.client_b.get(disease_axis_queue_url(self.project_b.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        pks = [item['id'] for item in response.data]

        self.assertNotIn(
            self.pd_a.pk, pks,
            'pd_a (mixed, projeto A) NÃO deve aparecer na fila de B.',
        )
        self.assertIn(
            self.pd_b.pk, pks,
            'pd_b (mixed, projeto B) deve aparecer na fila de B.',
        )
        self.assertIn(
            self.pd_b_disc.pk, pks,
            'pd_b_disc (discordante, projeto B) deve aparecer na fila de B.',
        )

    def test_queue_count_matches_only_own_project(self):
        """
        Contagem da fila de A é exatamente 1 (apenas pd_a), sem contágio de B.
        """
        response = self.client_a.get(disease_axis_queue_url(self.project_a.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            len(response.data),
            1,
            f'Fila de A deve ter exatamente 1 item, obteve {len(response.data)}.',
        )


# =============================================================================
# 3. Política de conteúdo — mixed + discordância entram; indeterminate puro NÃO
# =============================================================================

class DiseaseAxisQueueContentPolicyTests(APITestCase):
    """
    Verifica que apenas os dois grupos corretos aparecem na fila:
      Grupo 1: disease_axis == 'mixed'
      Grupo 2: monogenic_gene_hit presente MAS disease_axis ∉ {monogenic, mixed}
    E que indeterminate puro (sem gene hit) NÃO entra.
    """

    def setUp(self):
        self.user = make_user('daq_policy_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Policy Project')

    def test_mixed_appears_in_queue(self):
        """disease_axis='mixed' → entra na fila."""
        ds = make_dataset('DAQ_POL_MIXED', disease_axis='mixed')
        pd = make_project_dataset(self.project, ds)

        response = self.client.get(disease_axis_queue_url(self.project.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        pks = [item['id'] for item in response.data]
        self.assertIn(pd.pk, pks, 'mixed deve aparecer na fila.')

    def test_discordance_appears_in_queue(self):
        """
        monogenic_gene_hit presente + disease_axis='multifactorial'
        → discordância → entra na fila.
        """
        ds = make_dataset(
            'DAQ_POL_DISC',
            disease_axis='multifactorial',
            extra_metadata=_gene_hit_payload(['TP53']),
        )
        pd = make_project_dataset(self.project, ds)

        response = self.client.get(disease_axis_queue_url(self.project.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        pks = [item['id'] for item in response.data]
        self.assertIn(pd.pk, pks, 'Discordante (gene_hit + multifactorial) deve aparecer na fila.')

    def test_discordance_indeterminate_with_gene_hit_appears(self):
        """
        monogenic_gene_hit presente + disease_axis='indeterminate'
        → discordância (axis ∉ {monogenic, mixed}) → entra na fila.
        """
        ds = make_dataset(
            'DAQ_POL_DISC_INDET',
            disease_axis='indeterminate',
            extra_metadata=_gene_hit_payload(['LRRK2']),
        )
        pd = make_project_dataset(self.project, ds)

        response = self.client.get(disease_axis_queue_url(self.project.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        pks = [item['id'] for item in response.data]
        self.assertIn(
            pd.pk, pks,
            'indeterminate COM gene_hit deve aparecer como discordante.',
        )

    def test_indeterminate_pure_excluded_from_queue(self):
        """
        disease_axis='indeterminate' SEM monogenic_gene_hit → terminal → NÃO entra.
        """
        ds = make_dataset(
            'DAQ_POL_INDET_PURE',
            disease_axis='indeterminate',
            extra_metadata={},   # sem gene hit
        )
        pd = make_project_dataset(self.project, ds)

        response = self.client.get(disease_axis_queue_url(self.project.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        pks = [item['id'] for item in response.data]
        self.assertNotIn(pd.pk, pks, 'indeterminate puro NÃO deve aparecer na fila.')

    def test_monogenic_excluded_from_queue(self):
        """
        disease_axis='monogenic' → não é mixed, nem discordante → NÃO entra.
        """
        ds = make_dataset('DAQ_POL_MONO', disease_axis='monogenic')
        pd = make_project_dataset(self.project, ds)

        response = self.client.get(disease_axis_queue_url(self.project.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        pks = [item['id'] for item in response.data]
        self.assertNotIn(pd.pk, pks, 'monogenic puro NÃO deve aparecer na fila.')

    def test_multifactorial_without_gene_hit_excluded(self):
        """
        disease_axis='multifactorial' sem monogenic_gene_hit → NÃO entra.
        Discordância exige gene_hit presente.
        """
        ds = make_dataset(
            'DAQ_POL_MULTI_NOHIT',
            disease_axis='multifactorial',
            extra_metadata={},   # sem gene hit
        )
        pd = make_project_dataset(self.project, ds)

        response = self.client.get(disease_axis_queue_url(self.project.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        pks = [item['id'] for item in response.data]
        self.assertNotIn(
            pd.pk, pks,
            'multifactorial sem gene_hit NÃO deve aparecer (não há discordância).',
        )

    def test_mixed_has_priority_over_discordance(self):
        """
        disease_axis='mixed' com gene_hit → entra como 'mixed' (não 'discordance').
        Política: 'mixed' tem prioridade; 'discordance' exclui 'mixed' explicitamente.
        """
        ds = make_dataset(
            'DAQ_POL_MIXED_WITH_HIT',
            disease_axis='mixed',
            extra_metadata=_gene_hit_payload(['CFTR']),
        )
        pd = make_project_dataset(self.project, ds)

        response = self.client.get(disease_axis_queue_url(self.project.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        items_by_pk = {item['id']: item for item in response.data}
        self.assertIn(pd.pk, items_by_pk, 'mixed com gene_hit deve aparecer na fila.')
        # Deve estar no grupo 'mixed', não 'discordance'
        self.assertEqual(
            items_by_pk[pd.pk]['queue_reason'],
            'mixed',
            "disease_axis='mixed' deve ter queue_reason='mixed', não 'discordance'.",
        )

    def test_empty_gene_hit_genes_list_does_not_trigger_discordance(self):
        """
        monogenic_gene_hit presente mas lista 'genes' vazia → sem discordância.
        _has_monogenic_gene_hit() exige genes=[...] não-vazia.
        """
        ds = make_dataset(
            'DAQ_POL_EMPTY_GENES',
            disease_axis='multifactorial',
            extra_metadata={
                'contract': {
                    'monogenic_gene_hit': {
                        'genes': [],   # lista vazia — sem hit real
                        'confidence': 0.0,
                        'gene_details': [],
                    }
                }
            },
        )
        pd = make_project_dataset(self.project, ds)

        response = self.client.get(disease_axis_queue_url(self.project.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        pks = [item['id'] for item in response.data]
        self.assertNotIn(
            pd.pk, pks,
            'gene_hit com genes=[] NÃO deve disparar discordância.',
        )

    def test_empty_project_returns_empty_list(self):
        """Projeto sem datasets → fila vazia []."""
        response = self.client.get(disease_axis_queue_url(self.project.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data, list)
        self.assertEqual(len(response.data), 0)


# =============================================================================
# 4. queue_reason correto para cada grupo
# =============================================================================

class DiseaseAxisQueueReasonTests(APITestCase):
    """
    queue_reason deve ser 'mixed' ou 'discordance' conforme o critério de entrada.
    O campo é anotado por high_value_queue_queryset() via _queue_reason.
    """

    def setUp(self):
        self.user = make_user('daq_reason_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Reason Project')

    def test_queue_reason_mixed_for_disease_axis_mixed(self):
        """disease_axis='mixed' → queue_reason='mixed'."""
        ds = make_dataset('DAQ_RSN_MIXED', disease_axis='mixed')
        pd = make_project_dataset(self.project, ds)

        response = self.client.get(disease_axis_queue_url(self.project.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        items = {item['id']: item for item in response.data}
        self.assertIn(pd.pk, items)
        self.assertEqual(
            items[pd.pk]['queue_reason'],
            'mixed',
            "queue_reason deve ser 'mixed' para disease_axis='mixed'.",
        )

    def test_queue_reason_discordance_for_gene_hit_with_multifactorial(self):
        """
        monogenic_gene_hit + disease_axis='multifactorial' → queue_reason='discordance'.
        """
        ds = make_dataset(
            'DAQ_RSN_DISC_MULTI',
            disease_axis='multifactorial',
            extra_metadata=_gene_hit_payload(['KRAS']),
        )
        pd = make_project_dataset(self.project, ds)

        response = self.client.get(disease_axis_queue_url(self.project.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        items = {item['id']: item for item in response.data}
        self.assertIn(pd.pk, items)
        self.assertEqual(
            items[pd.pk]['queue_reason'],
            'discordance',
            "queue_reason deve ser 'discordance' para gene_hit + multifactorial.",
        )

    def test_queue_reason_discordance_for_gene_hit_with_indeterminate(self):
        """
        monogenic_gene_hit + disease_axis='indeterminate' → queue_reason='discordance'.
        indeterminate ∉ {monogenic, mixed} — satisfaz a condição de discordância.
        """
        ds = make_dataset(
            'DAQ_RSN_DISC_INDET',
            disease_axis='indeterminate',
            extra_metadata=_gene_hit_payload(['PTEN']),
        )
        pd = make_project_dataset(self.project, ds)

        response = self.client.get(disease_axis_queue_url(self.project.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        items = {item['id']: item for item in response.data}
        self.assertIn(pd.pk, items)
        self.assertEqual(
            items[pd.pk]['queue_reason'],
            'discordance',
            "queue_reason deve ser 'discordance' para gene_hit + indeterminate.",
        )

    def test_both_groups_present_with_correct_reasons(self):
        """
        Projeto com mixed e discordante → ambos aparecem com queue_reason correto.
        """
        ds_mixed = make_dataset('DAQ_RSN_BOTH_MIX', disease_axis='mixed')
        pd_mixed = make_project_dataset(self.project, ds_mixed)

        ds_disc = make_dataset(
            'DAQ_RSN_BOTH_DISC',
            disease_axis='multifactorial',
            extra_metadata=_gene_hit_payload(['MLH1']),
        )
        pd_disc = make_project_dataset(self.project, ds_disc)

        response = self.client.get(disease_axis_queue_url(self.project.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        items = {item['id']: item for item in response.data}

        self.assertEqual(items[pd_mixed.pk]['queue_reason'], 'mixed')
        self.assertEqual(items[pd_disc.pk]['queue_reason'], 'discordance')


# =============================================================================
# 5. Serializer não vaza extra_metadata inteiro nem contract_confidence inteiro
# =============================================================================

class DiseaseAxisQueueSerializerFieldTests(APITestCase):
    """
    DiseaseAxisQueueItemSerializer deve expor SOMENTE os campos declarados em
    Meta.fields. extra_metadata inteiro e contract_confidence inteiro NÃO devem
    aparecer na resposta.
    """

    # Campos esperados conforme Meta.fields do DiseaseAxisQueueItemSerializer
    EXPECTED_FIELDS = {
        'id',
        'dataset_id',
        'accession',
        'source_db',
        'title',
        'disease_axis',
        'disease_axis_confidence',
        'monogenic_gene_hit',
        'queue_reason',
        'curation_status',
        'notes',
        'added_at',
        'curated_at',
    }

    def setUp(self):
        self.user = make_user('daq_serial_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Serializer Project')

        self.ds = make_dataset(
            'DAQ_SER_DS',
            disease_axis='mixed',
            title='Serializer Test Dataset',
            source_db='geo',
            extra_metadata={
                'sensitive_key': 'should_not_appear',
                'contract': {
                    'disease_raw': ['Cystic fibrosis'],
                    'monogenic_gene_hit': {
                        'genes': ['CFTR'],
                        'confidence': 0.9,
                        'gene_details': [
                            {'gene_symbol': 'CFTR', 'score': 0.9,
                             'sources': ['geo_metadata'],
                             'mendelian_confidence': 'definitive'}
                        ],
                    },
                },
            },
            contract_confidence={
                'has_control_group': 0.3,
                'other_axis_key': 'should_not_appear_raw',
                'disease_axis': {
                    'score': 0.95,
                    'method': 'exact',
                    'n_candidates': 2,
                    'axis_hits': {
                        'monogenic': {
                            'score': 1.0, 'method': 'exact',
                            'matched_name': 'Cystic fibrosis',
                            'matched_source': 'orphanet',
                        },
                        'multifactorial': {
                            'score': 1.0, 'method': 'exact',
                            'matched_name': 'Asthma',
                            'matched_source': 'gwas_catalog',
                        },
                    },
                },
            },
        )
        self.pd = make_project_dataset(self.project, self.ds, notes='nota do curador')

    def test_extra_metadata_not_exposed(self):
        """
        extra_metadata inteiro NÃO deve aparecer na resposta.
        Reduz superfície de dado potencialmente sensível.
        """
        response = self.client.get(disease_axis_queue_url(self.project.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        item = response.data[0]
        self.assertNotIn(
            'extra_metadata',
            item,
            'extra_metadata inteiro NÃO deve ser exposto na fila de disease_axis.',
        )

    def test_contract_confidence_full_not_exposed(self):
        """
        contract_confidence inteiro NÃO deve aparecer na resposta.
        Apenas disease_axis_confidence (sub-chave) é exposta.
        """
        response = self.client.get(disease_axis_queue_url(self.project.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        item = response.data[0]
        self.assertNotIn(
            'contract_confidence',
            item,
            'contract_confidence inteiro NÃO deve ser exposto na fila de disease_axis.',
        )

    def test_disease_axis_confidence_is_sub_key_only(self):
        """
        disease_axis_confidence expõe apenas contract_confidence['disease_axis'].
        NÃO expõe has_control_group nem outras chaves do contract_confidence.
        """
        response = self.client.get(disease_axis_queue_url(self.project.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        item = response.data[0]
        self.assertIn(
            'disease_axis_confidence',
            item,
            'disease_axis_confidence deve estar presente.',
        )
        dac = item['disease_axis_confidence']
        self.assertIsNotNone(dac)

        # Chaves esperadas da sub-dict disease_axis
        self.assertIn('score', dac)
        self.assertIn('method', dac)
        self.assertIn('axis_hits', dac)

        # has_control_group NÃO deve vazar pela disease_axis_confidence
        self.assertNotIn(
            'has_control_group',
            dac,
            'has_control_group NÃO deve aparecer em disease_axis_confidence.',
        )
        self.assertNotIn(
            'other_axis_key',
            dac,
            'other_axis_key (outra chave de contract_confidence) NÃO deve aparecer.',
        )

    def test_monogenic_gene_hit_is_sub_key_only(self):
        """
        monogenic_gene_hit expõe apenas extra_metadata['contract']['monogenic_gene_hit'].
        NÃO expõe disease_raw nem outras chaves do contract.
        """
        response = self.client.get(disease_axis_queue_url(self.project.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        item = response.data[0]
        self.assertIn(
            'monogenic_gene_hit',
            item,
            'monogenic_gene_hit deve estar presente.',
        )
        mgh = item['monogenic_gene_hit']
        self.assertIsNotNone(mgh)

        # Chaves esperadas do payload de gene_hit
        self.assertIn('genes', mgh)
        self.assertIn('confidence', mgh)
        self.assertIn('gene_details', mgh)
        self.assertIn('CFTR', mgh['genes'])

        # disease_raw NÃO deve vazar via monogenic_gene_hit
        self.assertNotIn(
            'disease_raw',
            mgh,
            'disease_raw NÃO deve aparecer dentro de monogenic_gene_hit.',
        )
        self.assertNotIn(
            'sensitive_key',
            mgh,
            'Chave sensível de extra_metadata NÃO deve vazar via monogenic_gene_hit.',
        )

    def test_response_fields_are_exactly_expected(self):
        """
        Os campos da resposta correspondem EXATAMENTE a EXPECTED_FIELDS.
        Sem campos extras não documentados.
        """
        response = self.client.get(disease_axis_queue_url(self.project.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        item = response.data[0]
        actual_fields = set(item.keys())

        unexpected = actual_fields - self.EXPECTED_FIELDS
        self.assertFalse(
            unexpected,
            f'Campos inesperados na resposta: {unexpected}. '
            f'Serializer expõe mais do que o declarado em Meta.fields.',
        )

        missing = self.EXPECTED_FIELDS - actual_fields
        self.assertFalse(
            missing,
            f'Campos esperados ausentes na resposta: {missing}.',
        )

    def test_monogenic_gene_hit_is_none_when_absent(self):
        """
        Dataset sem monogenic_gene_hit em extra_metadata → campo é None na resposta.
        """
        ds_no_hit = make_dataset(
            'DAQ_SER_NO_HIT',
            disease_axis='mixed',
            extra_metadata={},
            contract_confidence={},
        )
        pd_no_hit = make_project_dataset(self.project, ds_no_hit)

        response = self.client.get(disease_axis_queue_url(self.project.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        items = {item['id']: item for item in response.data}
        self.assertIn(pd_no_hit.pk, items)
        self.assertIsNone(
            items[pd_no_hit.pk]['monogenic_gene_hit'],
            'monogenic_gene_hit deve ser None quando ausente.',
        )

    def test_disease_axis_confidence_is_none_when_absent(self):
        """
        Dataset sem disease_axis no contract_confidence → disease_axis_confidence=None.
        """
        ds_no_conf = make_dataset(
            'DAQ_SER_NO_CONF',
            disease_axis='mixed',
            extra_metadata={},
            contract_confidence={},
        )
        pd_no_conf = make_project_dataset(self.project, ds_no_conf)

        response = self.client.get(disease_axis_queue_url(self.project.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        items = {item['id']: item for item in response.data}
        self.assertIn(pd_no_conf.pk, items)
        self.assertIsNone(
            items[pd_no_conf.pk]['disease_axis_confidence'],
            'disease_axis_confidence deve ser None quando chave ausente em contract_confidence.',
        )

    def test_notes_and_curation_fields_present(self):
        """
        notes, curation_status, added_at, curated_at estão presentes na resposta.
        Campos de auditoria de curadoria (skill curation-audit-trail).
        """
        response = self.client.get(disease_axis_queue_url(self.project.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        item = response.data[0]
        self.assertIn('notes', item)
        self.assertIn('curation_status', item)
        self.assertIn('added_at', item)
        self.assertIn('curated_at', item)

        # Confirma valores
        self.assertEqual(item['notes'], 'nota do curador')
        self.assertEqual(item['curation_status'], 'pending')
        self.assertIsNone(item['curated_at'])


# =============================================================================
# 6. Não autenticado → 401/403
# =============================================================================

class DiseaseAxisQueueAuthTests(APITestCase):
    """
    Endpoint exige autenticação. Request sem credencial → 401/403.
    """

    def setUp(self):
        self.user = make_user('daq_auth_user')
        self.project = make_project(self.user, title='Auth Project')

    def test_unauthenticated_request_is_rejected(self):
        """
        GET sem token/session → 401 ou 403.
        Confirma que o endpoint não é público.
        """
        unauth_client = APIClient()
        response = unauth_client.get(disease_axis_queue_url(self.project.id))
        self.assertIn(
            response.status_code,
            [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN],
            f'Endpoint público detectado — obteve {response.status_code}. '
            'DiseaseAxisCurationQueueView deve exigir autenticação.',
        )

    def test_authenticated_user_can_access_own_queue(self):
        """
        Controle: usuário autenticado acessa a própria fila sem bloqueio.
        """
        self.client.force_authenticate(user=self.user)
        response = self.client.get(disease_axis_queue_url(self.project.id))
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            'Usuário autenticado deve acessar a própria fila normalmente.',
        )
