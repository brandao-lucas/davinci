"""
Testes de QA para os campos de contrato OmnisPathway (migrations 0017/0018).

Cobre:
  1. Defaults pós-migration em OmicDataset criado minimamente.
  2. CheckConstraints (integridade, valores fora de enum, omics_layers inválido).
  3. Tri-estado has_control_group: distingue 'unknown' de 'no' semanticamente.
  4. Backfill (migration 0018): lógica de derivação de omics_layers/omics_count.
  5. Filtros via API: has_control_group, disease_axis, omics_count_min/max,
     omics_layer (containment), has_sample_join_key.
  6. Não-regressão de ingestão: OmicDataset criado via helper existente tem defaults corretos.
  7. Isolamento por usuário: user B não filtra/bulk-cura datasets de projeto de user A.

Padrão: sem pytest, usa APITestCase do DRF.
Sem chamadas NCBI: tasks Celery são mockadas onde necessário.
Requer Postgres real (ArrayField, CheckConstraint, JSONB).
"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from apps.core.models import (
    DaVinciProject,
    IngestionJob,
    OmicDataset,
    ProjectDataset,
)


# ─── Helpers reutilizáveis ────────────────────────────────────────────────────


def make_user(username='contract_user', password='pw'):
    return User.objects.create_user(username=username, password=password)


def make_project(user, title='Contract Project', query_term='cancer'):
    return DaVinciProject.objects.create(
        user=user,
        title=title,
        slug=f'{title.lower().replace(" ", "-")}-{user.username}-davinci-{title[:3]}',
        query_term=query_term,
    )


def make_dataset_minimal(accession='GSE_MINIMAL', source_db='geo', title='Minimal Dataset',
                          **kwargs):
    """Cria OmicDataset com apenas os campos obrigatórios + kwargs adicionais."""
    return OmicDataset.objects.create(
        accession=accession,
        source_db=source_db,
        title=title,
        **kwargs,
    )


def make_project_dataset(project, dataset, curation_status='pending',
                          relevance_score=None, notes='', ingestion_job=None):
    return ProjectDataset.objects.create(
        project=project,
        dataset=dataset,
        curation_status=curation_status,
        relevance_score=relevance_score,
        notes=notes,
        ingestion_job=ingestion_job,
    )


# ─── 1. Defaults pós-migration ────────────────────────────────────────────────


class OmicDatasetContractDefaultsTests(APITestCase):
    """
    Verifica que OmicDataset criado com apenas os campos obrigatórios
    (accession, source_db, title) tem os 9 campos de contrato nos defaults corretos.
    """

    def setUp(self):
        self.ds = make_dataset_minimal(accession='GSE_DEFAULTS', source_db='geo',
                                       title='Defaults Test Dataset')

    def test_omics_layers_default_empty_list(self):
        """omics_layers deve ser [] por padrão."""
        self.assertEqual(self.ds.omics_layers, [])

    def test_sample_join_key_default_empty_list(self):
        """sample_join_key deve ser [] por padrão."""
        self.assertEqual(self.ds.sample_join_key, [])

    def test_contract_confidence_default_empty_dict(self):
        """contract_confidence deve ser {} por padrão."""
        self.assertEqual(self.ds.contract_confidence, {})

    def test_is_single_cell_default_unknown(self):
        """is_single_cell deve ser 'unknown' por padrão."""
        self.assertEqual(self.ds.is_single_cell, 'unknown')

    def test_has_control_group_default_unknown(self):
        """has_control_group deve ser 'unknown' por padrão."""
        self.assertEqual(self.ds.has_control_group, 'unknown')

    def test_disease_axis_default_indeterminate(self):
        """disease_axis deve ser 'indeterminate' por padrão."""
        self.assertEqual(self.ds.disease_axis, 'indeterminate')

    def test_data_format_default_unknown(self):
        """data_format deve ser 'unknown' por padrão."""
        self.assertEqual(self.ds.data_format, 'unknown')

    def test_access_type_default_unknown(self):
        """access_type deve ser 'unknown' por padrão."""
        self.assertEqual(self.ds.access_type, 'unknown')

    def test_omics_count_default_none(self):
        """omics_count deve ser None (não avaliado) por padrão."""
        self.assertIsNone(self.ds.omics_count)

    def test_defaults_persisted_in_db(self):
        """
        Recarrega do banco para garantir que os defaults foram persistidos,
        não apenas definidos em memória pelo Python.
        """
        from_db = OmicDataset.objects.get(pk=self.ds.pk)
        self.assertEqual(from_db.omics_layers, [])
        self.assertEqual(from_db.sample_join_key, [])
        self.assertEqual(from_db.contract_confidence, {})
        self.assertEqual(from_db.is_single_cell, 'unknown')
        self.assertEqual(from_db.has_control_group, 'unknown')
        self.assertEqual(from_db.disease_axis, 'indeterminate')
        self.assertEqual(from_db.data_format, 'unknown')
        self.assertEqual(from_db.access_type, 'unknown')
        self.assertIsNone(from_db.omics_count)


# ─── 2. CheckConstraints ─────────────────────────────────────────────────────


class OmicDatasetCheckConstraintTests(APITestCase):
    """
    Verifica que CheckConstraints do banco rejeitam valores fora do enum.
    Cada caso usa transaction.atomic() para isolar a transação com falha.
    """

    def test_access_type_invalid_value_raises_integrity_error(self):
        """access_type='publico' (fora do enum public/controlled/unknown) → IntegrityError."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OmicDataset.objects.create(
                    accession='GSE_BAD_ACCESS',
                    source_db='geo',
                    title='Bad access_type',
                    access_type='publico',
                )

    def test_disease_axis_invalid_value_raises_integrity_error(self):
        """disease_axis='xpto' (fora do enum) → IntegrityError."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OmicDataset.objects.create(
                    accession='GSE_BAD_AXIS',
                    source_db='geo',
                    title='Bad disease_axis',
                    disease_axis='xpto',
                )

    def test_has_control_group_invalid_value_raises_integrity_error(self):
        """has_control_group='talvez' (fora de yes/no/unknown) → IntegrityError."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OmicDataset.objects.create(
                    accession='GSE_BAD_CTRL',
                    source_db='geo',
                    title='Bad has_control_group',
                    has_control_group='talvez',
                )

    def test_is_single_cell_invalid_value_raises_integrity_error(self):
        """is_single_cell='spatial' (ainda não no vocabulário) → IntegrityError."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OmicDataset.objects.create(
                    accession='GSE_BAD_SC',
                    source_db='geo',
                    title='Bad is_single_cell',
                    is_single_cell='spatial',
                )

    def test_data_format_invalid_value_raises_integrity_error(self):
        """data_format='semi-processed' (fora do enum) → IntegrityError."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OmicDataset.objects.create(
                    accession='GSE_BAD_FORMAT',
                    source_db='geo',
                    title='Bad data_format',
                    data_format='semi-processed',
                )

    def test_omics_layers_invalid_token_raises_integrity_error(self):
        """
        omics_layers com token inválido ['xyz'] viola containment constraint
        (deve ser subconjunto do vocabulário canônico) → IntegrityError.
        """
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OmicDataset.objects.create(
                    accession='GSE_BAD_LAYER',
                    source_db='geo',
                    title='Bad omics_layers',
                    omics_layers=['xyz'],
                )

    def test_omics_layers_mixed_valid_invalid_raises_integrity_error(self):
        """
        ['transcriptomic', 'invalid_layer'] — qualquer token inválido viola a constraint.
        """
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OmicDataset.objects.create(
                    accession='GSE_BAD_MIXED',
                    source_db='geo',
                    title='Mixed layers',
                    omics_layers=['transcriptomic', 'invalid_layer'],
                )

    def test_valid_values_do_not_raise(self):
        """Valores canônicos corretos são aceitos sem exceção."""
        ds = OmicDataset.objects.create(
            accession='GSE_VALID_ALL',
            source_db='geo',
            title='All valid contract fields',
            access_type='public',
            disease_axis='monogenic',
            has_control_group='yes',
            is_single_cell='bulk',
            data_format='raw',
            omics_layers=['genomic', 'transcriptomic'],
            omics_count=2,
            sample_join_key=['sample_id', 'donor_id'],
            contract_confidence={'disease_axis': 0.9},
        )
        from_db = OmicDataset.objects.get(pk=ds.pk)
        self.assertEqual(from_db.access_type, 'public')
        self.assertEqual(from_db.disease_axis, 'monogenic')
        self.assertEqual(from_db.omics_layers, ['genomic', 'transcriptomic'])


# ─── 3. Tri-estado has_control_group ─────────────────────────────────────────


class TriStateHasControlGroupTests(APITestCase):
    """
    'unknown', 'yes' e 'no' são estados distintos e distinguíveis.

    Semântica:
      - 'unknown' + ausência de chave em contract_confidence
        = "ainda não classificado" (Fase 0, default pós-ingestão)
      - 'unknown' + chave 'has_control_group' em contract_confidence com score baixo
        = "classificado como indeterminado nas Fases 2/3 com baixa confiança"
      - 'no' = ausência de grupo controle confirmada
      - 'yes' = grupo controle confirmado presente
    """

    def setUp(self):
        self.ds_unknown = make_dataset_minimal(
            accession='GSE_CTRL_UNK',
            source_db='geo',
            title='Control Unknown',
            has_control_group='unknown',
        )
        self.ds_no = make_dataset_minimal(
            accession='GSE_CTRL_NO',
            source_db='geo',
            title='Control No',
            has_control_group='no',
        )
        self.ds_yes = make_dataset_minimal(
            accession='GSE_CTRL_YES',
            source_db='geo',
            title='Control Yes',
            has_control_group='yes',
        )

    def test_filter_unknown_does_not_return_no(self):
        """filter(has_control_group='unknown') NÃO inclui o dataset com 'no'."""
        qs = OmicDataset.objects.filter(has_control_group='unknown',
                                        accession__startswith='GSE_CTRL')
        accessions = list(qs.values_list('accession', flat=True))
        self.assertIn('GSE_CTRL_UNK', accessions)
        self.assertNotIn('GSE_CTRL_NO', accessions)
        self.assertNotIn('GSE_CTRL_YES', accessions)

    def test_filter_no_does_not_return_unknown(self):
        """filter(has_control_group='no') NÃO inclui o dataset com 'unknown'."""
        qs = OmicDataset.objects.filter(has_control_group='no',
                                        accession__startswith='GSE_CTRL')
        accessions = list(qs.values_list('accession', flat=True))
        self.assertIn('GSE_CTRL_NO', accessions)
        self.assertNotIn('GSE_CTRL_UNK', accessions)
        self.assertNotIn('GSE_CTRL_YES', accessions)

    def test_filter_yes_exclusive(self):
        """filter(has_control_group='yes') retorna apenas o dataset com 'yes'."""
        qs = OmicDataset.objects.filter(has_control_group='yes',
                                        accession__startswith='GSE_CTRL')
        accessions = list(qs.values_list('accession', flat=True))
        self.assertIn('GSE_CTRL_YES', accessions)
        self.assertNotIn('GSE_CTRL_UNK', accessions)
        self.assertNotIn('GSE_CTRL_NO', accessions)

    def test_semantic_unknown_not_classified_vs_indeterminate(self):
        """
        Semântica documentada: 'unknown' com contract_confidence vazio
        = "não classificado ainda" (Fase 0, default).

        Após Fases 2/3 o campo pode permanecer 'unknown' com
        contract_confidence contendo 'has_control_group' e um score baixo,
        o que significa "classificado como indeterminado com baixa confiança".

        Os dois casos são distintos via contract_confidence — testamos aqui
        que os objetos são persistíveis e distinguíveis.
        """
        # Caso 1: não classificado — default Fase 0
        ds_not_classified = make_dataset_minimal(
            accession='GSE_SEMANTIC_UNK',
            source_db='geo',
            title='Semantic: not classified yet',
            has_control_group='unknown',
            contract_confidence={},
        )

        # Caso 2: classificado como indeterminado nas Fases 2/3
        ds_indeterminate = make_dataset_minimal(
            accession='GSE_SEMANTIC_INDET',
            source_db='geo',
            title='Semantic: classified indeterminate',
            has_control_group='unknown',
            contract_confidence={'has_control_group': 0.3},
        )

        from_db_nc = OmicDataset.objects.get(pk=ds_not_classified.pk)
        from_db_id = OmicDataset.objects.get(pk=ds_indeterminate.pk)

        # Ambos têm has_control_group='unknown' — indistinguíveis por esse filtro
        self.assertEqual(from_db_nc.has_control_group, 'unknown')
        self.assertEqual(from_db_id.has_control_group, 'unknown')

        # Distinguem-se pelo contract_confidence
        self.assertEqual(from_db_nc.contract_confidence, {})
        self.assertIn('has_control_group', from_db_id.contract_confidence)
        self.assertEqual(from_db_id.contract_confidence['has_control_group'], 0.3)


# ─── 4. Backfill (lógica de derivação da migration 0018) ─────────────────────


class BackfillDerivationTests(APITestCase):
    """
    Testa a lógica de derivação de omics_layers / omics_count que
    a migration 0018 executa via RunPython.

    Abordagem: replicate a função _derive_layers da migration e testa o mapeamento.
    Também testa o efeito persistido via OmicDataset criado com omic_type preenchido
    e chamada manual da lógica de backfill (simula o que a migration faria).
    """

    # Replica do _OMIC_TYPE_TO_LAYER da migration 0018 para teste unitário
    _OMIC_TYPE_TO_LAYER = {
        'genomic': 'genomic',
        'transcriptomic': 'transcriptomic',
        'proteomic': 'proteomic',
        'metabolomic': 'metabolomic',
        'epigenomic': 'epigenomic',
        'metagenomic': 'metagenomic',
        'microbiome': 'microbiome',
    }

    def _derive_layers(self, omic_type):
        """Réplica local da função de derivação da migration 0018."""
        if not omic_type:
            return []
        layers = []
        for raw in omic_type.split(','):
            token = raw.strip().lower()
            layer = self._OMIC_TYPE_TO_LAYER.get(token)
            if layer and layer not in layers:
                layers.append(layer)
        return layers

    # --- Testes unitários da função de derivação ---

    def test_derive_single_transcriptomic(self):
        """omic_type='transcriptomic' → ['transcriptomic'], count=1."""
        layers = self._derive_layers('transcriptomic')
        self.assertEqual(layers, ['transcriptomic'])
        self.assertEqual(len(layers), 1)

    def test_derive_comma_separated_two_layers(self):
        """omic_type='transcriptomic,genomic' → ['transcriptomic', 'genomic'], count=2."""
        layers = self._derive_layers('transcriptomic,genomic')
        self.assertEqual(layers, ['transcriptomic', 'genomic'])
        self.assertEqual(len(layers), 2)

    def test_derive_order_preserved(self):
        """A ordem de primeira aparição é preservada."""
        layers = self._derive_layers('genomic,transcriptomic,proteomic')
        self.assertEqual(layers, ['genomic', 'transcriptomic', 'proteomic'])

    def test_derive_deduplicates(self):
        """Token duplicado no omic_type é deduplicado."""
        layers = self._derive_layers('transcriptomic,transcriptomic')
        self.assertEqual(layers, ['transcriptomic'])

    def test_derive_multi_omic_returns_empty(self):
        """
        omic_type='multi_omic' não está no mapa → [] (sem contagem confiável).
        Mantém defaults (omics_layers=[], omics_count=NULL).
        """
        layers = self._derive_layers('multi_omic')
        self.assertEqual(layers, [])

    def test_derive_other_returns_empty(self):
        """omic_type='other' não está no mapa → []."""
        layers = self._derive_layers('other')
        self.assertEqual(layers, [])

    def test_derive_empty_string_returns_empty(self):
        """omic_type vazio → []."""
        layers = self._derive_layers('')
        self.assertEqual(layers, [])

    def test_derive_whitespace_trimmed(self):
        """Espaços em torno de tokens são removidos (strip)."""
        layers = self._derive_layers(' transcriptomic , genomic ')
        self.assertEqual(layers, ['transcriptomic', 'genomic'])

    def test_derive_all_canonical_layers(self):
        """Todos os 7 tokens do vocabulário são mapeados corretamente."""
        tokens = 'genomic,transcriptomic,proteomic,metabolomic,epigenomic,metagenomic,microbiome'
        layers = self._derive_layers(tokens)
        self.assertEqual(len(layers), 7)
        for expected in ['genomic', 'transcriptomic', 'proteomic', 'metabolomic',
                         'epigenomic', 'metagenomic', 'microbiome']:
            self.assertIn(expected, layers)

    # --- Testes de efeito persistido ---

    def test_backfill_effect_transcriptomic_genomic(self):
        """
        Dataset com omic_type='transcriptomic,genomic' → após aplicar lógica de backfill
        tem omics_layers=['transcriptomic','genomic'] e omics_count=2.
        """
        ds = make_dataset_minimal(
            accession='GSE_BACKFILL_01',
            source_db='geo',
            title='Backfill test',
            omic_type='transcriptomic,genomic',
        )
        # Aplica a lógica de derivação (simula o que a migration faz)
        layers = self._derive_layers(ds.omic_type)
        ds.omics_layers = layers
        ds.omics_count = len(layers)
        ds.save(update_fields=['omics_layers', 'omics_count'])

        from_db = OmicDataset.objects.get(pk=ds.pk)
        self.assertEqual(from_db.omics_layers, ['transcriptomic', 'genomic'])
        self.assertEqual(from_db.omics_count, 2)

    def test_backfill_effect_single_layer(self):
        """
        Dataset com omic_type='proteomic' → omics_layers=['proteomic'], omics_count=1.
        """
        ds = make_dataset_minimal(
            accession='GSE_BACKFILL_02',
            source_db='geo',
            title='Proteomic backfill',
            omic_type='proteomic',
        )
        layers = self._derive_layers(ds.omic_type)
        ds.omics_layers = layers
        ds.omics_count = len(layers)
        ds.save(update_fields=['omics_layers', 'omics_count'])

        from_db = OmicDataset.objects.get(pk=ds.pk)
        self.assertEqual(from_db.omics_layers, ['proteomic'])
        self.assertEqual(from_db.omics_count, 1)

    def test_backfill_multi_omic_keeps_defaults(self):
        """
        Dataset com omic_type='multi_omic' → não mapeável → defaults mantidos
        (omics_layers=[], omics_count=None).
        """
        ds = make_dataset_minimal(
            accession='GSE_BACKFILL_MULTI',
            source_db='geo',
            title='Multi-omic backfill',
            omic_type='multi_omic',
        )
        layers = self._derive_layers(ds.omic_type)
        if not layers:
            pass  # não salva: mantém defaults, idempotente
        # Valores continuam nos defaults (a migration não toca)
        from_db = OmicDataset.objects.get(pk=ds.pk)
        self.assertEqual(from_db.omics_layers, [])
        self.assertIsNone(from_db.omics_count)


# ─── 5. Filtros via API ───────────────────────────────────────────────────────


class ContractFieldFiltersAPITests(APITestCase):
    """
    Testa os novos filtros de contrato via API (bulk_curate com filters e listagem).

    Usa o padrão de APITestCase como test_project_status_and_bulk_filter.py.
    """

    def setUp(self):
        self.user = make_user('contract_api_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Contract Filters Project')
        self.base = f'/api/v1/projects/{self.project.id}/datasets/'

        # ds_yes_mono: has_control_group=yes, disease_axis=monogenic, omics_count=1,
        #              omics_layers=['transcriptomic'], access_type=public
        self.ds_yes_mono = make_dataset_minimal(
            accession='GSE_FILTER_01', source_db='geo', title='Yes Mono',
            has_control_group='yes',
            disease_axis='monogenic',
            omics_count=1,
            omics_layers=['transcriptomic'],
            is_single_cell='bulk',
            data_format='raw',
            access_type='public',
            sample_join_key=[],
        )
        self.pd_yes_mono = make_project_dataset(
            self.project, self.ds_yes_mono, curation_status='pending',
        )

        # ds_no_multi: has_control_group=no, disease_axis=multifactorial, omics_count=2,
        #              omics_layers=['genomic','transcriptomic'], access_type=controlled
        self.ds_no_multi = make_dataset_minimal(
            accession='GSE_FILTER_02', source_db='geo', title='No Multi',
            has_control_group='no',
            disease_axis='multifactorial',
            omics_count=2,
            omics_layers=['genomic', 'transcriptomic'],
            is_single_cell='single_cell',
            data_format='processed',
            access_type='controlled',
            sample_join_key=['donor_id'],
        )
        self.pd_no_multi = make_project_dataset(
            self.project, self.ds_no_multi, curation_status='pending',
        )

        # ds_unk_indet: has_control_group=unknown, disease_axis=indeterminate,
        #               omics_count=None, omics_layers=[], sample_join_key=[]
        self.ds_unk_indet = make_dataset_minimal(
            accession='GSE_FILTER_03', source_db='geo', title='Unknown Indet',
            # defaults: has_control_group='unknown', disease_axis='indeterminate',
            # omics_count=None, omics_layers=[], sample_join_key=[]
        )
        self.pd_unk_indet = make_project_dataset(
            self.project, self.ds_unk_indet, curation_status='pending',
        )

    # --- has_control_group ---

    def test_filter_has_control_group_yes(self):
        """bulk_curate com filters={has_control_group: 'yes'} afeta só ds_yes_mono."""
        with patch('apps.core.views.dataset_views.run_sample_ingestion'):
            response = self.client.post(
                f'{self.base}bulk_curate/',
                {
                    'filters': {'has_control_group': 'yes'},
                    'curation_status': 'excluded',
                },
                format='json',
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['updated'], 1)

        self.pd_yes_mono.refresh_from_db()
        self.pd_no_multi.refresh_from_db()
        self.pd_unk_indet.refresh_from_db()

        self.assertEqual(self.pd_yes_mono.curation_status, 'excluded')
        self.assertEqual(self.pd_no_multi.curation_status, 'pending')
        self.assertEqual(self.pd_unk_indet.curation_status, 'pending')

    def test_filter_has_control_group_unknown_does_not_match_no(self):
        """has_control_group='unknown' NÃO retorna dataset com 'no' (tri-estado)."""
        with patch('apps.core.views.dataset_views.run_sample_ingestion'):
            response = self.client.post(
                f'{self.base}bulk_curate/',
                {
                    'filters': {'has_control_group': 'unknown'},
                    'curation_status': 'excluded',
                },
                format='json',
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Apenas pd_unk_indet tem has_control_group='unknown'
        self.assertEqual(response.data['updated'], 1)

        self.pd_unk_indet.refresh_from_db()
        self.pd_no_multi.refresh_from_db()
        self.assertEqual(self.pd_unk_indet.curation_status, 'excluded')
        self.assertEqual(self.pd_no_multi.curation_status, 'pending')

    # --- disease_axis ---

    def test_filter_disease_axis_exact(self):
        """filters={disease_axis: 'monogenic'} afeta só ds_yes_mono."""
        with patch('apps.core.views.dataset_views.run_sample_ingestion'):
            response = self.client.post(
                f'{self.base}bulk_curate/',
                {
                    'filters': {'disease_axis': 'monogenic'},
                    'curation_status': 'excluded',
                },
                format='json',
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['updated'], 1)

        self.pd_yes_mono.refresh_from_db()
        self.pd_no_multi.refresh_from_db()
        self.assertEqual(self.pd_yes_mono.curation_status, 'excluded')
        self.assertEqual(self.pd_no_multi.curation_status, 'pending')

    def test_filter_disease_axis_multifactorial(self):
        """filters={disease_axis: 'multifactorial'} afeta só ds_no_multi."""
        with patch('apps.core.views.dataset_views.run_sample_ingestion'):
            response = self.client.post(
                f'{self.base}bulk_curate/',
                {
                    'filters': {'disease_axis': 'multifactorial'},
                    'curation_status': 'included',
                },
                format='json',
            )
        self.assertEqual(response.data['updated'], 1)
        self.pd_no_multi.refresh_from_db()
        self.pd_yes_mono.refresh_from_db()
        self.assertEqual(self.pd_no_multi.curation_status, 'included')
        self.assertEqual(self.pd_yes_mono.curation_status, 'pending')

    # --- omics_count range ---

    def test_filter_omics_count_min_excludes_lower(self):
        """
        omics_count_min=2 deve excluir datasets com count=1 e count=NULL.
        ds_no_multi (count=2) aparece; ds_yes_mono (count=1) e ds_unk_indet (count=NULL) não.
        """
        with patch('apps.core.views.dataset_views.run_sample_ingestion'):
            response = self.client.post(
                f'{self.base}bulk_curate/',
                {
                    'filters': {'omics_count_min': 2},
                    'curation_status': 'excluded',
                },
                format='json',
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['updated'], 1)

        self.pd_no_multi.refresh_from_db()
        self.pd_yes_mono.refresh_from_db()
        self.pd_unk_indet.refresh_from_db()

        self.assertEqual(self.pd_no_multi.curation_status, 'excluded')
        self.assertEqual(self.pd_yes_mono.curation_status, 'pending')
        self.assertEqual(self.pd_unk_indet.curation_status, 'pending')

    def test_filter_omics_count_max(self):
        """
        omics_count_max=1 traz apenas ds_yes_mono (count=1).
        ds_no_multi (count=2) e ds_unk_indet (count=NULL) ficam fora.
        """
        with patch('apps.core.views.dataset_views.run_sample_ingestion'):
            response = self.client.post(
                f'{self.base}bulk_curate/',
                {
                    'filters': {'omics_count_max': 1},
                    'curation_status': 'excluded',
                },
                format='json',
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['updated'], 1)

        self.pd_yes_mono.refresh_from_db()
        self.pd_no_multi.refresh_from_db()
        self.assertEqual(self.pd_yes_mono.curation_status, 'excluded')
        self.assertEqual(self.pd_no_multi.curation_status, 'pending')

    def test_filter_omics_count_range(self):
        """
        omics_count_min=1 + omics_count_max=1 retorna só ds_yes_mono.
        """
        with patch('apps.core.views.dataset_views.run_sample_ingestion'):
            response = self.client.post(
                f'{self.base}bulk_curate/',
                {
                    'filters': {'omics_count_min': 1, 'omics_count_max': 1},
                    'curation_status': 'excluded',
                },
                format='json',
            )
        self.assertEqual(response.data['updated'], 1)
        self.pd_yes_mono.refresh_from_db()
        self.pd_no_multi.refresh_from_db()
        self.assertEqual(self.pd_yes_mono.curation_status, 'excluded')
        self.assertEqual(self.pd_no_multi.curation_status, 'pending')

    # --- omics_layer containment ---

    def test_filter_omics_layer_containment_transcriptomic(self):
        """
        filters={omics_layer: 'transcriptomic'} retorna datasets que CONTÊM
        'transcriptomic' em omics_layers: ds_yes_mono E ds_no_multi.
        ds_unk_indet (omics_layers=[]) não é retornado.
        """
        with patch('apps.core.views.dataset_views.run_sample_ingestion'):
            response = self.client.post(
                f'{self.base}bulk_curate/',
                {
                    'filters': {'omics_layer': 'transcriptomic'},
                    'curation_status': 'excluded',
                },
                format='json',
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['updated'], 2)

        self.pd_yes_mono.refresh_from_db()
        self.pd_no_multi.refresh_from_db()
        self.pd_unk_indet.refresh_from_db()

        self.assertEqual(self.pd_yes_mono.curation_status, 'excluded')
        self.assertEqual(self.pd_no_multi.curation_status, 'excluded')
        self.assertEqual(self.pd_unk_indet.curation_status, 'pending')

    def test_filter_omics_layer_containment_genomic_only(self):
        """
        filters={omics_layer: 'genomic'} retorna só ds_no_multi
        (ds_yes_mono tem apenas 'transcriptomic').
        """
        with patch('apps.core.views.dataset_views.run_sample_ingestion'):
            response = self.client.post(
                f'{self.base}bulk_curate/',
                {
                    'filters': {'omics_layer': 'genomic'},
                    'curation_status': 'excluded',
                },
                format='json',
            )
        self.assertEqual(response.data['updated'], 1)
        self.pd_no_multi.refresh_from_db()
        self.pd_yes_mono.refresh_from_db()
        self.assertEqual(self.pd_no_multi.curation_status, 'excluded')
        self.assertEqual(self.pd_yes_mono.curation_status, 'pending')

    # --- has_sample_join_key ---

    def test_filter_has_sample_join_key_true(self):
        """
        filters={has_sample_join_key: true} retorna só ds_no_multi
        (sample_join_key=['donor_id'] não vazio).
        ds_yes_mono e ds_unk_indet têm sample_join_key=[].
        """
        with patch('apps.core.views.dataset_views.run_sample_ingestion'):
            response = self.client.post(
                f'{self.base}bulk_curate/',
                {
                    'filters': {'has_sample_join_key': True},
                    'curation_status': 'excluded',
                },
                format='json',
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['updated'], 1)

        self.pd_no_multi.refresh_from_db()
        self.pd_yes_mono.refresh_from_db()
        self.pd_unk_indet.refresh_from_db()

        self.assertEqual(self.pd_no_multi.curation_status, 'excluded')
        self.assertEqual(self.pd_yes_mono.curation_status, 'pending')
        self.assertEqual(self.pd_unk_indet.curation_status, 'pending')

    # --- bulk_curate com audit-trail nos novos eixos ---

    def test_bulk_curate_new_axis_filter_sets_curated_at(self):
        """bulk_curate via filtro de contrato OmnisPathway preenche curated_at."""
        self.assertIsNone(self.pd_yes_mono.curated_at)
        with patch('apps.core.views.dataset_views.run_sample_ingestion'):
            self.client.post(
                f'{self.base}bulk_curate/',
                {
                    'filters': {'disease_axis': 'monogenic'},
                    'curation_status': 'excluded',
                },
                format='json',
            )
        self.pd_yes_mono.refresh_from_db()
        self.assertIsNotNone(self.pd_yes_mono.curated_at)

    def test_bulk_curate_new_axis_preserves_notes(self):
        """bulk_curate via filtro de contrato não apaga notes (curation-audit-trail)."""
        self.pd_yes_mono.notes = 'nota importante do eixo'
        self.pd_yes_mono.save(update_fields=['notes'])

        with patch('apps.core.views.dataset_views.run_sample_ingestion'):
            self.client.post(
                f'{self.base}bulk_curate/',
                {
                    'filters': {'has_control_group': 'yes'},
                    'curation_status': 'excluded',
                },
                format='json',
            )
        self.pd_yes_mono.refresh_from_db()
        self.assertEqual(self.pd_yes_mono.notes, 'nota importante do eixo')

    # --- Combinação de filtros ---

    def test_combined_filters_has_control_group_and_omics_layer(self):
        """
        Combinação: has_control_group='no' AND omics_layer='genomic'
        → afeta só ds_no_multi (tem ambas as condições).
        """
        with patch('apps.core.views.dataset_views.run_sample_ingestion'):
            response = self.client.post(
                f'{self.base}bulk_curate/',
                {
                    'filters': {
                        'has_control_group': 'no',
                        'omics_layer': 'genomic',
                    },
                    'curation_status': 'excluded',
                },
                format='json',
            )
        self.assertEqual(response.data['updated'], 1)
        self.pd_no_multi.refresh_from_db()
        self.pd_yes_mono.refresh_from_db()
        self.assertEqual(self.pd_no_multi.curation_status, 'excluded')
        self.assertEqual(self.pd_yes_mono.curation_status, 'pending')


# ─── 6. Não-regressão de ingestão ────────────────────────────────────────────


class IngestContractDefaultsRegressionTests(APITestCase):
    """
    Garante que OmicDataset criado pelo helper make_dataset_minimal
    (com apenas accession/source_db/title — estilo ingestão) mantém os
    campos de contrato nos defaults corretos.

    Simula o comportamento do Rust que cria o registro via COPY com apenas
    os campos existentes antes das migrations 0017/0018; os novos campos
    devem ficar nos defaults (não quebrar a ingestão existente).
    """

    def test_ingestion_style_dataset_has_contract_defaults(self):
        """
        Dataset criado com apenas os 3 campos obrigatórios (estilo ingestão Rust)
        tem todos os campos de contrato nos defaults esperados.
        """
        ds = make_dataset_minimal(
            accession='GSE_INGEST_REG',
            source_db='geo',
            title='Ingestion regression dataset',
        )
        from_db = OmicDataset.objects.get(pk=ds.pk)

        self.assertEqual(from_db.omics_layers, [])
        self.assertEqual(from_db.sample_join_key, [])
        self.assertEqual(from_db.contract_confidence, {})
        self.assertEqual(from_db.is_single_cell, 'unknown')
        self.assertEqual(from_db.has_control_group, 'unknown')
        self.assertEqual(from_db.disease_axis, 'indeterminate')
        self.assertEqual(from_db.data_format, 'unknown')
        self.assertEqual(from_db.access_type, 'unknown')
        self.assertIsNone(from_db.omics_count)

    def test_existing_make_dataset_helper_has_contract_defaults(self):
        """
        O helper make_dataset() de test_project_status_and_bulk_filter.py
        cria OmicDataset com omic_type e organism — esses campos extras
        não violam os defaults dos campos de contrato.
        """
        # Replica o helper existente de test_project_status_and_bulk_filter.py
        ds = OmicDataset.objects.create(
            accession='GSE_COMPAT_REG',
            source_db='geo',
            title='Compat regression',
            omic_type='transcriptomic',
            organism='Homo sapiens',
        )
        from_db = OmicDataset.objects.get(pk=ds.pk)

        # Campos de contrato nos defaults — não devem ser afetados por campos legados
        self.assertEqual(from_db.omics_layers, [])
        self.assertEqual(from_db.sample_join_key, [])
        self.assertEqual(from_db.contract_confidence, {})
        self.assertEqual(from_db.is_single_cell, 'unknown')
        self.assertEqual(from_db.has_control_group, 'unknown')
        self.assertEqual(from_db.disease_axis, 'indeterminate')
        self.assertEqual(from_db.data_format, 'unknown')
        self.assertEqual(from_db.access_type, 'unknown')
        self.assertIsNone(from_db.omics_count)


# ─── 7. Isolamento por usuário ────────────────────────────────────────────────


class ContractFieldsUserIsolationTests(APITestCase):
    """
    User B não pode filtrar/bulk-curar datasets de projeto de user A
    usando os novos filtros de contrato OmnisPathway.

    Reutiliza o padrão de BulkCurateIsolationTests de
    test_project_status_and_bulk_filter.py.
    """

    def setUp(self):
        self.user_a = make_user('isolate_contract_a')
        self.user_b = make_user('isolate_contract_b')

        self.client_a = APIClient()
        self.client_b = APIClient()
        self.client_a.force_authenticate(user=self.user_a)
        self.client_b.force_authenticate(user=self.user_b)

        self.project_a = make_project(self.user_a, title='Isolation Contract A')

        self.ds = make_dataset_minimal(
            accession='GSE_ISO_CONTRACT',
            source_db='geo',
            title='Isolation dataset',
            has_control_group='yes',
            disease_axis='monogenic',
            omics_layers=['transcriptomic'],
            omics_count=1,
        )
        self.pd = make_project_dataset(self.project_a, self.ds)

    def test_user_b_cannot_filter_with_contract_axis_on_project_a(self):
        """
        User B com filters={has_control_group: 'yes'} no projeto de A recebe 404.
        """
        response = self.client_b.post(
            f'/api/v1/projects/{self.project_a.id}/datasets/bulk_curate/',
            {
                'filters': {'has_control_group': 'yes'},
                'curation_status': 'excluded',
            },
            format='json',
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND,
            'User B deve receber 404 ao tentar bulk_curate via filtro de contrato '
            'em projeto alheio (firebase-auth-guard).',
        )

        # Confirma que nenhum registro foi alterado
        self.pd.refresh_from_db()
        self.assertEqual(self.pd.curation_status, 'pending')

    def test_user_b_cannot_filter_with_omics_layer_on_project_a(self):
        """
        User B com filters={omics_layer: 'transcriptomic'} no projeto de A recebe 404.
        """
        response = self.client_b.post(
            f'/api/v1/projects/{self.project_a.id}/datasets/bulk_curate/',
            {
                'filters': {'omics_layer': 'transcriptomic'},
                'curation_status': 'excluded',
            },
            format='json',
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND,
            'User B deve receber 404 ao tentar bulk_curate via omics_layer de projeto alheio.',
        )
        self.pd.refresh_from_db()
        self.assertEqual(self.pd.curation_status, 'pending')

    def test_user_a_can_filter_own_project(self):
        """
        User A (dono do projeto) com o mesmo filtro de contrato tem acesso normal.
        """
        with patch('apps.core.views.dataset_views.run_sample_ingestion'):
            response = self.client_a.post(
                f'/api/v1/projects/{self.project_a.id}/datasets/bulk_curate/',
                {
                    'filters': {'has_control_group': 'yes'},
                    'curation_status': 'excluded',
                },
                format='json',
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['updated'], 1)
        self.pd.refresh_from_db()
        self.assertEqual(self.pd.curation_status, 'excluded')
