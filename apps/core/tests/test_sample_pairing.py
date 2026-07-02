"""
test_sample_pairing.py — Cobertura de SamplePairingService e command pair_samples.

OmnisPathway Objetivo 2, Fase 1, passo 1.3.

Áreas cobertas:
  1. Pareamento correto — case_id com tumor+normal gera 1 OmicSamplePairing
     com todos os campos canônicos: validated_overlap=True, confidence=1.0,
     pairing_basis='shared_case_id', tumor_sample/normal_sample corretos,
     case_id denormalizado correto.
  2. Sobreposição real (NÚCLEO) — case_id só-tumor e só-normal NÃO viram par;
     contabilizados corretamente em n_unpaired_tumor_only / n_unpaired_normal_only.
  3. Idempotência — run() 2× não duplica; n_pairs_created=0 na 2ª chamada.
  4. dry_run=True — relatório calculado corretamente mas 0 linhas gravadas.
  5. Fallback de accession — amostra sem characteristics['case_id'] usa sufixo
     _tumor/_normal; aparece em fallback_accession_used.
     Amostra sem characteristics algum → characteristics_missing.
  6. Multi-alíquota — dois samples tumor com mesmo case_id na mesma matriz
     NÃO produzem par espúrio; case_id aparece em multi_aliquota_tumor.
  7. Isolamento (Regra #3) — resolve_matrix rejeita matrix de dataset não
     vinculado ao projeto; projeto de outro usuário não acha a matriz.
  8. Command pair_samples — --dry-run não grava; execução normal grava;
     --project obrigatório; --matrix específico funciona; relatório legível.

Padrões obrigatórios:
  - SEM internet — nada de NCBI/CPTAC/SRA real. Fixtures ORM puro.
  - SEM pytest — usa django.test.TestCase (padrão do projeto).
  - Fixtures montadas diretamente via ORM: OmicMatrix + OmicMatrixSample + OmicSample.
  - characteristics={'case_id': ...} populado nas amostras (Fase 0 já faz isso).
"""

from __future__ import annotations

import io
import uuid

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.core.models import (
    DaVinciProject,
    OmicDataset,
    OmicMatrix,
    OmicMatrixSample,
    OmicSample,
    OmicSamplePairing,
    ProjectDataset,
)
from apps.core.services.sample_pairing_service import (
    PAIRING_VERSION,
    MatrixNotFoundError,
    SamplePairingService,
    _derive_case_id_from_accession,
    _extract_case_id,
)


# =============================================================================
# Helpers de factory — reutiliza padrões de test_matrix_load.py
# =============================================================================

def _make_user(username: str = 'pairing_user') -> User:
    return User.objects.create_user(username=username, password='pw')


def _make_project(user: User, title: str = 'Pairing Project') -> DaVinciProject:
    slug = f'{title.lower().replace(" ", "-")}-{user.username}'
    return DaVinciProject.objects.create(
        user=user, title=title, slug=slug, query_term='cptac',
    )


def _make_dataset(accession: str = 'CPTAC-TEST-PROT') -> OmicDataset:
    return OmicDataset.objects.create(
        accession=accession,
        source_db='cptac',
        title='CPTAC Test Dataset',
    )


def _link_dataset(project: DaVinciProject, dataset: OmicDataset) -> ProjectDataset:
    return ProjectDataset.objects.create(project=project, dataset=dataset)


def _make_matrix(dataset: OmicDataset, loader_version: str = 'test-v1') -> OmicMatrix:
    return OmicMatrix.objects.create(
        dataset=dataset,
        omics_layer='proteomic',
        data_format_level='intensities',
        feature_axis='gene',
        loader_version=loader_version,
        n_features=10,
        n_samples=4,
    )


def _make_sample(
    dataset: OmicDataset,
    accession: str,
    case_id: str | None,
) -> OmicSample:
    """
    Cria um OmicSample.

    Se case_id não for None, popula characteristics={'case_id': case_id}
    (espelha o que o loader da Fase 0 faz).
    Se case_id for None, deixa characteristics={} (força fallback de accession).
    """
    characteristics: dict = {}
    if case_id is not None:
        characteristics['case_id'] = case_id
    return OmicSample.objects.create(
        dataset=dataset,
        accession=accession,
        characteristics=characteristics,
    )


def _link_sample(
    matrix: OmicMatrix,
    sample: OmicSample,
    role: str,
    column_index: int,
) -> OmicMatrixSample:
    return OmicMatrixSample.objects.create(
        matrix=matrix,
        sample=sample,
        sample_role=role,
        column_index=column_index,
    )


# =============================================================================
# Fixture base: matriz com 3 case_ids — C1 (tumor+normal), C2 (só tumor),
# C3 (só normal). Reutilizada na maioria dos testes.
# =============================================================================

class _BasePairingFixture(TestCase):
    """
    Fixture padrão:
      C1 → tumor (index 0) + normal (index 1) → deve virar par
      C2 → tumor (index 2) apenas              → unpaired_tumor_only
      C3 → normal (index 3) apenas             → unpaired_normal_only
    """

    def setUp(self):
        super().setUp()
        self.user = _make_user('base_pairing_user')
        self.project = _make_project(self.user)
        self.dataset = _make_dataset('CPTAC-BASE-TEST')
        _link_dataset(self.project, self.dataset)
        self.matrix = _make_matrix(self.dataset)

        # Amostras com characteristics['case_id'] populado (padrão Fase 0)
        self.s_c1_tumor = _make_sample(self.dataset, 'C1_tumor', 'C1')
        self.s_c1_normal = _make_sample(self.dataset, 'C1_normal', 'C1')
        self.s_c2_tumor = _make_sample(self.dataset, 'C2_tumor', 'C2')
        self.s_c3_normal = _make_sample(self.dataset, 'C3_normal', 'C3')

        _link_sample(self.matrix, self.s_c1_tumor, 'tumor', 0)
        _link_sample(self.matrix, self.s_c1_normal, 'normal', 1)
        _link_sample(self.matrix, self.s_c2_tumor, 'tumor', 2)
        _link_sample(self.matrix, self.s_c3_normal, 'normal', 3)


# =============================================================================
# 1. Pareamento correto — campos canônicos
# =============================================================================

class SamplePairingCorrectPairTests(_BasePairingFixture):
    """Verifica que C1 (tumor+normal) gera exatamente 1 par com campos corretos."""

    def test_single_pair_created(self):
        """run() produz exatamente 1 OmicSamplePairing para C1."""
        service = SamplePairingService(self.matrix)
        service.run(dry_run=False)

        self.assertEqual(OmicSamplePairing.objects.filter(matrix=self.matrix).count(), 1)

    def test_pair_fields_canonical(self):
        """Par de C1 tem todos os campos canônicos corretos."""
        service = SamplePairingService(self.matrix)
        service.run(dry_run=False)

        pair = OmicSamplePairing.objects.get(matrix=self.matrix, case_id='C1')

        self.assertEqual(pair.tumor_sample, self.s_c1_tumor)
        self.assertEqual(pair.normal_sample, self.s_c1_normal)
        self.assertEqual(pair.case_id, 'C1')
        self.assertTrue(pair.validated_overlap)
        self.assertAlmostEqual(pair.confidence, 1.0)
        self.assertEqual(pair.pairing_basis, OmicSamplePairing.PairingBasis.SHARED_CASE_ID)
        self.assertEqual(pair.pairing_version, PAIRING_VERSION)

    def test_report_structure_complete(self):
        """Relatório retornado tem todas as chaves do contrato."""
        service = SamplePairingService(self.matrix)
        report = service.run(dry_run=False)

        expected_keys = {
            'matrix_id', 'matrix_dataset', 'matrix_layer', 'pairing_version',
            'n_matrix_samples', 'n_case_ids_total', 'n_pairs_created',
            'n_pairs_existing', 'n_unpaired_tumor_only', 'n_unpaired_normal_only',
            'multi_aliquota_tumor', 'multi_aliquota_normal',
            'fallback_accession_used', 'characteristics_missing', 'dry_run',
        }
        self.assertEqual(set(report.keys()), expected_keys)

    def test_report_counts_correct(self):
        """Relatório reporta 1 par criado, 1 tumor-only, 1 normal-only."""
        service = SamplePairingService(self.matrix)
        report = service.run(dry_run=False)

        self.assertEqual(report['n_pairs_created'], 1)
        self.assertEqual(report['n_pairs_existing'], 0)
        self.assertEqual(report['n_unpaired_tumor_only'], 1)   # C2
        self.assertEqual(report['n_unpaired_normal_only'], 1)  # C3
        self.assertEqual(report['n_case_ids_total'], 3)        # C1, C2, C3
        self.assertEqual(report['n_matrix_samples'], 4)
        self.assertFalse(report['dry_run'])

    def test_report_dataset_and_layer(self):
        """Relatório reporta accession e layer da matriz."""
        service = SamplePairingService(self.matrix)
        report = service.run(dry_run=False)

        self.assertEqual(report['matrix_id'], self.matrix.id)
        self.assertEqual(report['matrix_dataset'], 'CPTAC-BASE-TEST')
        self.assertEqual(report['matrix_layer'], 'proteomic')
        self.assertEqual(report['pairing_version'], PAIRING_VERSION)


# =============================================================================
# 2. Sobreposição real (NÚCLEO) — chave compartilhada mas papel faltando
# =============================================================================

class SamplePairingOverlapValidationTests(_BasePairingFixture):
    """
    Núcleo metodológico: "chave compartilhada = pareado" deve ser VALIDADO
    contra sobreposição real.

    C2 (só tumor) e C3 (só normal) NÃO geram par mesmo que suas chaves
    existam no banco — falta o papel complementar na MESMA matriz.
    """

    def test_tumor_only_not_paired(self):
        """C2 (só tumor na matriz) NÃO gera OmicSamplePairing."""
        service = SamplePairingService(self.matrix)
        service.run(dry_run=False)

        self.assertFalse(
            OmicSamplePairing.objects.filter(matrix=self.matrix, case_id='C2').exists()
        )

    def test_normal_only_not_paired(self):
        """C3 (só normal na matriz) NÃO gera OmicSamplePairing."""
        service = SamplePairingService(self.matrix)
        service.run(dry_run=False)

        self.assertFalse(
            OmicSamplePairing.objects.filter(matrix=self.matrix, case_id='C3').exists()
        )

    def test_unpaired_counts_correct(self):
        """C2 e C3 contabilizados em unpaired, não em pares."""
        service = SamplePairingService(self.matrix)
        report = service.run(dry_run=False)

        self.assertEqual(report['n_unpaired_tumor_only'], 1)
        self.assertEqual(report['n_unpaired_normal_only'], 1)
        # Apenas C1 vira par
        self.assertEqual(report['n_pairs_created'], 1)

    def test_cross_matrix_case_id_not_paired(self):
        """
        Mesmo case_id em matrizes diferentes NÃO produz par na 2ª matriz
        se ela não tiver os dois papéis.

        Cria uma 2ª matriz com só o tumor de C1 → não deve gerar par.
        """
        dataset2 = _make_dataset('CPTAC-SECOND-TEST')
        matrix2 = _make_matrix(dataset2, loader_version='test-v2')
        # Coloca só o tumor de C1 na 2ª matriz
        _link_sample(matrix2, self.s_c1_tumor, 'tumor', 0)

        service2 = SamplePairingService(matrix2)
        report2 = service2.run(dry_run=False)

        self.assertEqual(OmicSamplePairing.objects.filter(matrix=matrix2).count(), 0)
        self.assertEqual(report2['n_pairs_created'], 0)
        self.assertEqual(report2['n_unpaired_tumor_only'], 1)
        self.assertEqual(report2['n_unpaired_normal_only'], 0)


# =============================================================================
# 3. Idempotência — run() 2×
# =============================================================================

class SamplePairingIdempotencyTests(_BasePairingFixture):
    """run() 2× produz o mesmo conjunto sem duplicar linhas."""

    def test_second_run_creates_zero(self):
        """2ª execução reporta n_pairs_created=0."""
        service = SamplePairingService(self.matrix)
        service.run(dry_run=False)
        report2 = service.run(dry_run=False)

        self.assertEqual(report2['n_pairs_created'], 0)
        self.assertEqual(report2['n_pairs_existing'], 1)

    def test_no_duplicate_rows_after_second_run(self):
        """Não há duplicação de OmicSamplePairing após 2 runs."""
        service = SamplePairingService(self.matrix)
        service.run(dry_run=False)
        service.run(dry_run=False)

        self.assertEqual(OmicSamplePairing.objects.filter(matrix=self.matrix).count(), 1)

    def test_third_run_still_zero_created(self):
        """3ª execução também é idempotente."""
        service = SamplePairingService(self.matrix)
        service.run(dry_run=False)
        service.run(dry_run=False)
        report3 = service.run(dry_run=False)

        self.assertEqual(report3['n_pairs_created'], 0)
        self.assertEqual(report3['n_pairs_existing'], 1)
        self.assertEqual(OmicSamplePairing.objects.filter(matrix=self.matrix).count(), 1)


# =============================================================================
# 4. dry_run=True — relatório calculado, 0 linhas gravadas
# =============================================================================

class SamplePairingDryRunTests(_BasePairingFixture):
    """dry_run=True calcula corretamente mas não persiste nada."""

    def test_dry_run_writes_nothing(self):
        """dry_run=True → 0 OmicSamplePairing no banco."""
        service = SamplePairingService(self.matrix)
        service.run(dry_run=True)

        self.assertEqual(OmicSamplePairing.objects.filter(matrix=self.matrix).count(), 0)

    def test_dry_run_report_counts_correct(self):
        """dry_run=True → relatório reporta as contagens corretas."""
        service = SamplePairingService(self.matrix)
        report = service.run(dry_run=True)

        self.assertEqual(report['n_pairs_created'], 1)     # seria criado
        self.assertEqual(report['n_pairs_existing'], 0)    # nada existe ainda
        self.assertEqual(report['n_unpaired_tumor_only'], 1)
        self.assertEqual(report['n_unpaired_normal_only'], 1)
        self.assertTrue(report['dry_run'])

    def test_dry_run_true_in_report(self):
        """dry_run=True aparece no relatório."""
        service = SamplePairingService(self.matrix)
        report = service.run(dry_run=True)

        self.assertTrue(report['dry_run'])

    def test_dry_run_existing_reported_correctly(self):
        """
        dry_run=True com pares já existentes reporta n_pairs_existing correto.
        """
        # Materializa na primeira chamada
        SamplePairingService(self.matrix).run(dry_run=False)

        # dry_run em cima de dados existentes
        report = SamplePairingService(self.matrix).run(dry_run=True)

        # n_pairs_existing = o que existe no banco antes do (não-)insert
        self.assertEqual(report['n_pairs_existing'], 1)
        # n_pairs_created = quantos seriam criados (todos já existem → ainda 1,
        # pois dry_run não distingue — mas o banco continua com 1 linha)
        self.assertEqual(OmicSamplePairing.objects.filter(matrix=self.matrix).count(), 1)


# =============================================================================
# 5. Fallback de accession e characteristics_missing
# =============================================================================

class SamplePairingFallbackTests(TestCase):
    """Amostras sem characteristics['case_id'] devem usar fallback do accession."""

    def setUp(self):
        super().setUp()
        self.user = _make_user('fallback_user')
        self.project = _make_project(self.user, 'Fallback Project')
        self.dataset = _make_dataset('CPTAC-FALLBACK-TEST')
        _link_dataset(self.project, self.dataset)
        self.matrix = _make_matrix(self.dataset, loader_version='fallback-v1')

    def test_fallback_accession_used_and_reported(self):
        """
        Amostra sem characteristics['case_id'] → fallback via accession _tumor/_normal.
        Aparece em fallback_accession_used do relatório.
        """
        # Amostras sem case_id em characteristics (case_id=None → characteristics={})
        s_tumor = _make_sample(self.dataset, 'FB01_tumor', case_id=None)
        s_normal = _make_sample(self.dataset, 'FB01_normal', case_id=None)
        _link_sample(self.matrix, s_tumor, 'tumor', 0)
        _link_sample(self.matrix, s_normal, 'normal', 1)

        service = SamplePairingService(self.matrix)
        report = service.run(dry_run=False)

        # Deve ter extraído 'FB01' via fallback e criado 1 par
        self.assertEqual(report['n_pairs_created'], 1)
        self.assertIn('FB01_tumor', report['fallback_accession_used'])
        self.assertIn('FB01_normal', report['fallback_accession_used'])
        # characteristics estava vazio → também em characteristics_missing
        self.assertIn('FB01_tumor', report['characteristics_missing'])
        self.assertIn('FB01_normal', report['characteristics_missing'])

    def test_fallback_pair_fields_correct(self):
        """Par criado via fallback tem campos corretos."""
        s_tumor = _make_sample(self.dataset, 'FB02_tumor', case_id=None)
        s_normal = _make_sample(self.dataset, 'FB02_normal', case_id=None)
        _link_sample(self.matrix, s_tumor, 'tumor', 0)
        _link_sample(self.matrix, s_normal, 'normal', 1)

        SamplePairingService(self.matrix).run(dry_run=False)

        pair = OmicSamplePairing.objects.get(matrix=self.matrix, case_id='FB02')
        self.assertEqual(pair.tumor_sample, s_tumor)
        self.assertEqual(pair.normal_sample, s_normal)
        self.assertTrue(pair.validated_overlap)

    def test_accession_without_suffix_not_derivable(self):
        """
        Accession sem sufixo _tumor/_normal não produz derivação — mantém
        accession como case_id (comportamento definido em _extract_case_id).
        Não deve explodir; reporta characteristics_missing.
        """
        # Amostra com accession sem sufixo (não-CPTAC) e sem characteristics
        s = _make_sample(self.dataset, 'GSM999999', case_id=None)
        _link_sample(self.matrix, s, 'tumor', 0)

        service = SamplePairingService(self.matrix)
        report = service.run(dry_run=False)  # não deve levantar

        self.assertIn('GSM999999', report['characteristics_missing'])
        # Sem normal, não vira par
        self.assertEqual(report['n_pairs_created'], 0)
        self.assertEqual(report['n_unpaired_tumor_only'], 1)

    def test_characteristics_empty_dict_handled(self):
        """
        OmicSample com characteristics={} (sem case_id) → fallback via accession.
        O campo characteristics é NOT NULL no schema (default=dict),
        então testamos o caminho real com características vazias.
        """
        # characteristics={} → sem case_id → fallback via accession
        s_tumor = _make_sample(self.dataset, 'NL01_tumor', case_id=None)
        s_normal = _make_sample(self.dataset, 'NL01_normal', case_id=None)
        _link_sample(self.matrix, s_tumor, 'tumor', 0)
        _link_sample(self.matrix, s_normal, 'normal', 1)

        service = SamplePairingService(self.matrix)
        report = service.run(dry_run=False)  # não deve levantar

        # NL01_tumor usou fallback; NL01_normal também → par deve ser criado
        self.assertEqual(report['n_pairs_created'], 1)


# =============================================================================
# 6. Multi-alíquota — sem produto cartesiano espúrio
# =============================================================================

class SamplePairingMultiAliquotaTests(TestCase):
    """
    Dois samples com o mesmo case_id e role na mesma matriz NÃO produzem par.
    """

    def setUp(self):
        super().setUp()
        self.user = _make_user('multi_user')
        self.project = _make_project(self.user, 'Multi Project')
        self.dataset = _make_dataset('CPTAC-MULTI-TEST')
        _link_dataset(self.project, self.dataset)
        self.matrix = _make_matrix(self.dataset, loader_version='multi-v1')

    def test_multi_aliquota_tumor_no_pair_created(self):
        """
        Dois samples tumor com mesmo case_id 'MA01' → sem par (produto
        cartesiano espúrio bloqueado). MA01 aparece em multi_aliquota_tumor.
        """
        s_t1 = _make_sample(self.dataset, 'MA01_tumor_alq1', 'MA01')
        s_t2 = _make_sample(self.dataset, 'MA01_tumor_alq2', 'MA01')
        s_n = _make_sample(self.dataset, 'MA01_normal', 'MA01')

        _link_sample(self.matrix, s_t1, 'tumor', 0)
        _link_sample(self.matrix, s_t2, 'tumor', 1)
        _link_sample(self.matrix, s_n, 'normal', 2)

        service = SamplePairingService(self.matrix)
        report = service.run(dry_run=False)

        self.assertEqual(OmicSamplePairing.objects.filter(matrix=self.matrix).count(), 0)
        self.assertEqual(report['n_pairs_created'], 0)
        self.assertIn('MA01', report['multi_aliquota_tumor'])

    def test_multi_aliquota_normal_no_pair_created(self):
        """Dois samples normal com mesmo case_id → sem par. MA02 em multi_aliquota_normal."""
        s_t = _make_sample(self.dataset, 'MA02_tumor', 'MA02')
        s_n1 = _make_sample(self.dataset, 'MA02_normal_alq1', 'MA02')
        s_n2 = _make_sample(self.dataset, 'MA02_normal_alq2', 'MA02')

        _link_sample(self.matrix, s_t, 'tumor', 0)
        _link_sample(self.matrix, s_n1, 'normal', 1)
        _link_sample(self.matrix, s_n2, 'normal', 2)

        service = SamplePairingService(self.matrix)
        report = service.run(dry_run=False)

        self.assertEqual(OmicSamplePairing.objects.filter(matrix=self.matrix).count(), 0)
        self.assertEqual(report['n_pairs_created'], 0)
        self.assertIn('MA02', report['multi_aliquota_normal'])

    def test_multi_aliquota_mixed_with_valid_pair(self):
        """
        MA03 (multi-alíquota tumor) bloqueado. MA04 (tumor+normal 1:1) vira par.
        Os dois case_ids coexistem na mesma matriz.
        """
        # MA03: 2 tumores + 1 normal → bloqueado
        s_ma03_t1 = _make_sample(self.dataset, 'MA03_tumor_alq1', 'MA03')
        s_ma03_t2 = _make_sample(self.dataset, 'MA03_tumor_alq2', 'MA03')
        s_ma03_n = _make_sample(self.dataset, 'MA03_normal', 'MA03')
        # MA04: 1 tumor + 1 normal → vira par
        s_ma04_t = _make_sample(self.dataset, 'MA04_tumor', 'MA04')
        s_ma04_n = _make_sample(self.dataset, 'MA04_normal', 'MA04')

        _link_sample(self.matrix, s_ma03_t1, 'tumor', 0)
        _link_sample(self.matrix, s_ma03_t2, 'tumor', 1)
        _link_sample(self.matrix, s_ma03_n, 'normal', 2)
        _link_sample(self.matrix, s_ma04_t, 'tumor', 3)
        _link_sample(self.matrix, s_ma04_n, 'normal', 4)

        service = SamplePairingService(self.matrix)
        report = service.run(dry_run=False)

        # Só MA04 vira par
        self.assertEqual(OmicSamplePairing.objects.filter(matrix=self.matrix).count(), 1)
        self.assertTrue(
            OmicSamplePairing.objects.filter(matrix=self.matrix, case_id='MA04').exists()
        )
        self.assertEqual(report['n_pairs_created'], 1)
        self.assertIn('MA03', report['multi_aliquota_tumor'])
        self.assertNotIn('MA04', report['multi_aliquota_tumor'])


# =============================================================================
# 7. Isolamento (Regra #3) — resolve_matrix
# =============================================================================

class SamplePairingIsolationTests(TestCase):
    """
    Garante que resolve_matrix não cruza projetos nem usuários.
    """

    def setUp(self):
        super().setUp()
        # Usuário A com projeto A e dataset A
        self.user_a = _make_user('iso_user_a')
        self.project_a = _make_project(self.user_a, 'Iso Project A')
        self.dataset_a = _make_dataset('CPTAC-ISO-A')
        _link_dataset(self.project_a, self.dataset_a)
        self.matrix_a = _make_matrix(self.dataset_a, loader_version='iso-v1')

        # Usuário B com projeto B e dataset B (matriz separada)
        self.user_b = _make_user('iso_user_b')
        self.project_b = _make_project(self.user_b, 'Iso Project B')
        self.dataset_b = _make_dataset('CPTAC-ISO-B')
        _link_dataset(self.project_b, self.dataset_b)
        self.matrix_b = _make_matrix(self.dataset_b, loader_version='iso-v1')

    def test_resolve_matrix_by_id_success(self):
        """resolve_matrix com matrix_id do próprio projeto retorna a matriz."""
        matrix = SamplePairingService.resolve_matrix(self.project_a, matrix_id=self.matrix_a.id)
        self.assertEqual(matrix.id, self.matrix_a.id)

    def test_resolve_matrix_cross_project_raises(self):
        """
        resolve_matrix com matrix_id de outro projeto levanta MatrixNotFoundError.
        Projeto A não pode resolver a matriz do Projeto B.
        """
        with self.assertRaises(MatrixNotFoundError):
            SamplePairingService.resolve_matrix(self.project_a, matrix_id=self.matrix_b.id)

    def test_resolve_matrix_cross_user_raises(self):
        """
        Projeto do usuário B não enxerga a matriz do usuário A.
        """
        with self.assertRaises(MatrixNotFoundError):
            SamplePairingService.resolve_matrix(self.project_b, matrix_id=self.matrix_a.id)

    def test_resolve_matrix_dataset_not_linked_raises(self):
        """
        Matriz de dataset NÃO vinculado ao projeto levanta MatrixNotFoundError,
        mesmo que a matriz exista no banco.
        """
        # Cria projeto C sem vincular dataset_a
        project_c = _make_project(self.user_a, 'Iso Project C')
        with self.assertRaises(MatrixNotFoundError):
            SamplePairingService.resolve_matrix(project_c, matrix_id=self.matrix_a.id)

    def test_resolve_matrix_none_when_no_ccrcc(self):
        """
        Auto-detecção (matrix_id=None) levanta MatrixNotFoundError quando não
        há matriz CCRCC proteome vinculada ao projeto.
        """
        with self.assertRaises(MatrixNotFoundError):
            SamplePairingService.resolve_matrix(self.project_a, matrix_id=None)

    def test_resolve_matrix_autodetect_ccrcc(self):
        """
        Auto-detecção encontra a matriz CCRCC proteome quando o dataset
        tem accession='CPTAC-CCRCC-PROTEOME' e omics_layer='proteomic'.
        """
        # Cria dataset com accession canônico CCRCC
        ds_ccrcc = _make_dataset('CPTAC-CCRCC-PROTEOME')
        project_ccrcc = _make_project(self.user_a, 'CCRCC Project')
        _link_dataset(project_ccrcc, ds_ccrcc)
        matrix_ccrcc = _make_matrix(ds_ccrcc, loader_version='ccrcc-v1')

        resolved = SamplePairingService.resolve_matrix(project_ccrcc, matrix_id=None)
        self.assertEqual(resolved.id, matrix_ccrcc.id)

    def test_resolve_matrix_excluded_dataset_raises(self):
        """
        Regressão A1: resolve_matrix NÃO resolve matrix de dataset com
        curation_status=EXCLUDED no ProjectDataset — datasets descartados
        pelo pesquisador não são elegíveis a pareamento.

        Contraste: INCLUDED é resolvido (happy path load→pair intacto).
        """
        # Dataset A está vinculado com INCLUDED (setUp), matrix_a é resolvível
        resolved = SamplePairingService.resolve_matrix(
            self.project_a, matrix_id=self.matrix_a.id
        )
        self.assertEqual(resolved.id, self.matrix_a.id)

        # Exclui o ProjectDataset do projeto A → dataset_a agora está EXCLUDED
        ProjectDataset.objects.filter(
            project=self.project_a,
            dataset=self.dataset_a,
        ).update(curation_status=ProjectDataset.CurationStatus.EXCLUDED)

        # Após exclusão, resolve_matrix deve levantar MatrixNotFoundError
        with self.assertRaises(MatrixNotFoundError):
            SamplePairingService.resolve_matrix(
                self.project_a, matrix_id=self.matrix_a.id
            )

    def test_pairing_only_within_own_matrix(self):
        """
        run() na matrix_a não cria pares para matrix_b, mesmo que as amostras
        sejam do mesmo dataset.
        """
        s_t = _make_sample(self.dataset_a, 'ISO_C1_tumor', 'C1')
        s_n = _make_sample(self.dataset_a, 'ISO_C1_normal', 'C1')
        _link_sample(self.matrix_a, s_t, 'tumor', 0)
        _link_sample(self.matrix_a, s_n, 'normal', 1)

        SamplePairingService(self.matrix_a).run(dry_run=False)

        # matrix_b não ganhou nenhum par
        self.assertEqual(OmicSamplePairing.objects.filter(matrix=self.matrix_b).count(), 0)
        # matrix_a tem exatamente 1 par
        self.assertEqual(OmicSamplePairing.objects.filter(matrix=self.matrix_a).count(), 1)


# =============================================================================
# 8. Helpers unitários — _extract_case_id e _derive_case_id_from_accession
# =============================================================================

class ExtractCaseIdHelperTests(TestCase):
    """Testes unitários dos helpers de extração de case_id."""

    def _make_sample_obj(self, accession: str, characteristics: dict) -> OmicSample:
        """
        Cria OmicSample mínimo para testar helpers.

        O campo characteristics é NOT NULL no schema (JSONField com default=dict),
        então recebe sempre um dict (vazio quando se quer simular "sem case_id").
        """
        ds = _make_dataset(f'DS-HELPER-{uuid.uuid4().hex[:6]}')
        return OmicSample.objects.create(
            dataset=ds,
            accession=accession,
            characteristics=characteristics,
        )

    def test_derive_from_tumor_suffix(self):
        self.assertEqual(_derive_case_id_from_accession('C3L-00004_tumor'), 'C3L-00004')

    def test_derive_from_normal_suffix(self):
        self.assertEqual(_derive_case_id_from_accession('C3L-00004_normal'), 'C3L-00004')

    def test_derive_no_suffix_returns_none(self):
        self.assertIsNone(_derive_case_id_from_accession('GSM123456'))
        self.assertIsNone(_derive_case_id_from_accession('C3L-00004'))

    def test_extract_from_characteristics_primary(self):
        """Fonte primária: characteristics['case_id']."""
        sample = self._make_sample_obj('C3L-TEST_tumor', {'case_id': 'C3L-TEST'})
        case_id, used_fallback, had_missing = _extract_case_id(sample)

        self.assertEqual(case_id, 'C3L-TEST')
        self.assertFalse(used_fallback)
        self.assertFalse(had_missing)

    def test_extract_fallback_when_no_case_id_in_characteristics(self):
        """Sem case_id em characteristics → fallback do accession."""
        sample = self._make_sample_obj('C3L-FB_tumor', {'other_key': 'val'})
        case_id, used_fallback, had_missing = _extract_case_id(sample)

        self.assertEqual(case_id, 'C3L-FB')
        self.assertTrue(used_fallback)
        self.assertTrue(had_missing)

    def test_extract_fallback_when_characteristics_empty(self):
        """characteristics={} → fallback via accession."""
        sample = self._make_sample_obj('C3L-EMPTY_normal', {})
        case_id, used_fallback, had_missing = _extract_case_id(sample)

        self.assertEqual(case_id, 'C3L-EMPTY')
        self.assertTrue(used_fallback)
        self.assertTrue(had_missing)

    def test_extract_fallback_when_characteristics_no_case_id_key(self):
        """
        characteristics sem chave 'case_id' (ex: apenas 'other_key') → fallback.
        O campo characteristics é NOT NULL no schema; testamos com dict sem 'case_id'.
        """
        sample = self._make_sample_obj('C3L-NONE_tumor', {'study': 'CCRCC'})
        case_id, used_fallback, had_missing = _extract_case_id(sample)

        self.assertEqual(case_id, 'C3L-NONE')
        self.assertTrue(used_fallback)
        self.assertTrue(had_missing)

    def test_extract_no_suffix_uses_accession_as_case_id(self):
        """Accession sem sufixo e sem case_id em characteristics → usa accession inteiro."""
        sample = self._make_sample_obj('GSM999888', {})
        case_id, used_fallback, had_missing = _extract_case_id(sample)

        # _derive_case_id_from_accession retorna None → usa accession diretamente
        self.assertEqual(case_id, 'GSM999888')
        self.assertTrue(used_fallback)
        self.assertTrue(had_missing)


# =============================================================================
# 9. Management command pair_samples
# =============================================================================

class PairSamplesCommandTests(TestCase):
    """
    Cobertura do command pair_samples.
    Usa call_command() — sem subprocess, sem internet.
    """

    def setUp(self):
        super().setUp()
        self.user = _make_user('cmd_user')
        self.project = _make_project(self.user, 'Cmd Project')
        self.dataset = _make_dataset('CPTAC-CMD-TEST')
        _link_dataset(self.project, self.dataset)
        self.matrix = _make_matrix(self.dataset, loader_version='cmd-v1')

        # Fixture: C1 pareável, C2 só tumor
        s_c1_t = _make_sample(self.dataset, 'CMD_C1_tumor', 'C1')
        s_c1_n = _make_sample(self.dataset, 'CMD_C1_normal', 'C1')
        s_c2_t = _make_sample(self.dataset, 'CMD_C2_tumor', 'C2')
        _link_sample(self.matrix, s_c1_t, 'tumor', 0)
        _link_sample(self.matrix, s_c1_n, 'normal', 1)
        _link_sample(self.matrix, s_c2_t, 'tumor', 2)

    def test_command_dry_run_writes_nothing(self):
        """--dry-run não grava OmicSamplePairing."""
        out = io.StringIO()
        call_command(
            'pair_samples',
            '--project', str(self.project.id),
            '--matrix', str(self.matrix.id),
            '--dry-run',
            stdout=out,
        )
        self.assertEqual(OmicSamplePairing.objects.filter(matrix=self.matrix).count(), 0)

    def test_command_dry_run_output_mentions_simulation(self):
        """--dry-run menciona 'simulação' ou 'dry' na saída."""
        out = io.StringIO()
        call_command(
            'pair_samples',
            '--project', str(self.project.id),
            '--matrix', str(self.matrix.id),
            '--dry-run',
            stdout=out,
        )
        output = out.getvalue().lower()
        self.assertTrue('dry' in output or 'simulaç' in output or 'simula' in output)

    def test_command_real_run_creates_pair(self):
        """Execução real grava 1 OmicSamplePairing."""
        out = io.StringIO()
        call_command(
            'pair_samples',
            '--project', str(self.project.id),
            '--matrix', str(self.matrix.id),
            stdout=out,
        )
        self.assertEqual(OmicSamplePairing.objects.filter(matrix=self.matrix).count(), 1)

    def test_command_second_run_idempotent(self):
        """2ª execução normal não duplica."""
        out = io.StringIO()
        call_command(
            'pair_samples',
            '--project', str(self.project.id),
            '--matrix', str(self.matrix.id),
            stdout=out,
        )
        call_command(
            'pair_samples',
            '--project', str(self.project.id),
            '--matrix', str(self.matrix.id),
            stdout=out,
        )
        self.assertEqual(OmicSamplePairing.objects.filter(matrix=self.matrix).count(), 1)

    def test_command_invalid_project_raises(self):
        """Projeto inexistente → CommandError."""
        with self.assertRaises(CommandError):
            call_command(
                'pair_samples',
                '--project', str(uuid.uuid4()),
                '--matrix', str(self.matrix.id),
                stdout=io.StringIO(),
            )

    def test_command_cross_project_matrix_raises(self):
        """
        --matrix de outro projeto → CommandError (via MatrixNotFoundError).
        """
        other_user = _make_user('cmd_other_user')
        other_project = _make_project(other_user, 'Other Cmd Project')
        other_dataset = _make_dataset('CPTAC-OTHER-CMD')
        _link_dataset(other_project, other_dataset)
        other_matrix = _make_matrix(other_dataset, loader_version='cmd-other-v1')

        with self.assertRaises(CommandError):
            call_command(
                'pair_samples',
                '--project', str(self.project.id),
                '--matrix', str(other_matrix.id),
                stdout=io.StringIO(),
            )

    def test_command_output_no_physical_path_leak(self):
        """
        Saída do command não vaza caminho físico absoluto (/tmp, /var, /home, etc.).
        """
        out = io.StringIO()
        call_command(
            'pair_samples',
            '--project', str(self.project.id),
            '--matrix', str(self.matrix.id),
            stdout=out,
        )
        output = out.getvalue()
        forbidden = ['/tmp/', '/var/', '/home/', '/Users/', '/root/']
        for path in forbidden:
            self.assertNotIn(path, output, f'Caminho físico vazou na saída: {path!r}')

    def test_command_with_specific_matrix_id(self):
        """--matrix <id> específico funciona corretamente."""
        out = io.StringIO()
        call_command(
            'pair_samples',
            '--project', str(self.project.id),
            '--matrix', str(self.matrix.id),
            stdout=out,
        )
        output = out.getvalue()
        # Verifica que o matrix id aparece na saída
        self.assertIn(str(self.matrix.id), output)


# =============================================================================
# 10. Regressão: matriz vazia não explode
# =============================================================================

class SamplePairingEdgeCasesTests(TestCase):
    """Casos de borda que não devem causar exceção."""

    def setUp(self):
        super().setUp()
        self.user = _make_user('edge_user')
        self.project = _make_project(self.user, 'Edge Project')
        self.dataset = _make_dataset('CPTAC-EDGE-TEST')
        _link_dataset(self.project, self.dataset)
        self.matrix = _make_matrix(self.dataset, loader_version='edge-v1')

    def test_empty_matrix_no_exception(self):
        """Matriz sem OmicMatrixSample → run() retorna 0 pares sem exceção."""
        service = SamplePairingService(self.matrix)
        report = service.run(dry_run=False)

        self.assertEqual(report['n_pairs_created'], 0)
        self.assertEqual(report['n_matrix_samples'], 0)
        self.assertEqual(report['n_case_ids_total'], 0)

    def test_all_tumor_no_normal(self):
        """Matriz só com tumores → 0 pares, todos em unpaired_tumor_only."""
        for i in range(3):
            s = _make_sample(self.dataset, f'ONLY_T{i}_tumor', f'T{i}')
            _link_sample(self.matrix, s, 'tumor', i)

        service = SamplePairingService(self.matrix)
        report = service.run(dry_run=False)

        self.assertEqual(report['n_pairs_created'], 0)
        self.assertEqual(report['n_unpaired_tumor_only'], 3)
        self.assertEqual(report['n_unpaired_normal_only'], 0)

    def test_all_normal_no_tumor(self):
        """Matriz só com normais → 0 pares, todos em unpaired_normal_only."""
        for i in range(2):
            s = _make_sample(self.dataset, f'ONLY_N{i}_normal', f'N{i}')
            _link_sample(self.matrix, s, 'normal', i)

        service = SamplePairingService(self.matrix)
        report = service.run(dry_run=False)

        self.assertEqual(report['n_pairs_created'], 0)
        self.assertEqual(report['n_unpaired_tumor_only'], 0)
        self.assertEqual(report['n_unpaired_normal_only'], 2)

    def test_unknown_role_samples_ignored(self):
        """
        Amostras com sample_role='unknown' são ignoradas no pareamento —
        não causam erro nem viram par.
        """
        s_t = _make_sample(self.dataset, 'UNK_C1_tumor', 'C1')
        s_n = _make_sample(self.dataset, 'UNK_C1_normal', 'C1')
        s_u = _make_sample(self.dataset, 'UNK_C1_unknown', 'C1')

        _link_sample(self.matrix, s_t, 'tumor', 0)
        _link_sample(self.matrix, s_n, 'normal', 1)
        _link_sample(self.matrix, s_u, 'unknown', 2)  # deve ser ignorado

        service = SamplePairingService(self.matrix)
        report = service.run(dry_run=False)

        # Par correto entre tumor e normal; amostra unknown não interfere
        self.assertEqual(report['n_pairs_created'], 1)
        self.assertEqual(report['n_matrix_samples'], 3)  # 3 amostras no total
        pair = OmicSamplePairing.objects.get(matrix=self.matrix, case_id='C1')
        self.assertEqual(pair.tumor_sample, s_t)
        self.assertEqual(pair.normal_sample, s_n)
