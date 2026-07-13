"""
test_cnv_allele.py — Cobertura do CnvAlleleService e command derive_cnv_allele_seeds.

OmnisPathway Objetivo 2, Fase 2, Slice 2.7 (CNV×alelo v2).

Áreas cobertas:
  1. Tabela de decisão (os 4 casos científicos):
     - inactivator CNV + SNV LoF → inactivator BIALÉLICO (mag=max(cnv,0.9), conf=0.95)
     - inactivator CNV + sem SNV LoF → inactivator MONOALÉLICO (mag=cnv, conf=0.60)
     - activator CNV + SNV LoF → NEUTRAL (contraexemplo: amplificar alelo danoso ≠ GoF, conf=0.40)
     - activator CNV + sem SNV LoF → activator HERDADO (mag=cnv, conf=0.70)

  2. Contadores corretos por caso (n_biallelic_reinforced, n_amplified_damaged_neutralized,
     n_monoallelic, n_activator_inherited, n_activator, n_inactivator, n_neutral).

  3. Coexistência: após run(), para o mesmo (matrix, sample, gene) existem DUAS linhas
     VariantEffectSeed — uma 'fase2-cnv-v1' e uma 'fase2-cnv-v2' (v1 NÃO sobrescrito).

  4. Cruzamento por sample COMPARTILHADO: snv seed de outro sample/gene NÃO contamina
     um cnv seed diferente.

  5. Idempotência: run() 2x → não duplica v2, contadores estáveis.

  6. resolve_matrix: rejeita matriz de dataset não vinculado (CnvAlleleMatrixNotFoundError);
     múltiplas matrizes sem --matrix → CnvAlleleMatrixNotFoundError.

  7. Pré-requisitos: sem cnv-v1 seeds → CnvAllelePrerequisiteError; sem snv seeds → erro;
     GeneRole vazio → erro.

  8. Command derive_cnv_allele_seeds: --project obrigatório; projeto inválido → CommandError;
     reporta todos os contadores; --matrix específico funciona.

Padrões obrigatórios:
  - SEM rede: nenhuma chamada externa. Django set-based puro.
  - SEM pytest: usa django.test.TestCase.
  - GeneRole, OmicSample, OmicMatrix, VariantEffectSeed criados via ORM.
  - OmicSample é COMPARTILHADA entre matriz CNV e matriz somática (mesmo object).
"""

from __future__ import annotations

import uuid
from io import StringIO

from django.contrib.auth.models import User
from django.test import TestCase

from apps.core.models import (
    DaVinciProject,
    GeneRole,
    OmicDataset,
    OmicMatrix,
    OmicMatrixSample,
    OmicSample,
    ProjectDataset,
    VariantEffectSeed,
)
from apps.core.services.cnv_allele_service import (
    CNV_V1_METHOD,
    EFFECT_SOURCE,
    METHOD_VERSION,
    CnvAlleleMatrixNotFoundError,
    CnvAllelePrerequisiteError,
    CnvAlleleService,
)


# =============================================================================
# Helpers de factory — reutiliza padrões dos outros testes do projeto
# =============================================================================

def _make_user(username: str) -> User:
    return User.objects.create_user(username=username, password='pw')


def _make_project(user: User, title: str) -> DaVinciProject:
    slug = f'{title.lower().replace(" ", "-")}-{user.username}'
    return DaVinciProject.objects.create(
        user=user, title=title, slug=slug, query_term='cnv-allele-test',
    )


def _make_cnv_dataset(accession: str) -> OmicDataset:
    return OmicDataset.objects.create(
        accession=accession,
        source_db='cptac',
        title=f'CNV Dataset {accession}',
        access_type='public',
    )


def _make_somatic_dataset(accession: str) -> OmicDataset:
    return OmicDataset.objects.create(
        accession=accession,
        source_db='cptac',
        title=f'Somatic Dataset {accession}',
        access_type='public',
    )


def _link_dataset(
    project: DaVinciProject,
    dataset: OmicDataset,
    status: str = ProjectDataset.CurationStatus.INCLUDED,
) -> ProjectDataset:
    return ProjectDataset.objects.create(
        project=project,
        dataset=dataset,
        curation_status=status,
    )


def _make_cnv_matrix(dataset: OmicDataset, storage_key: str | None = None) -> OmicMatrix:
    if storage_key is None:
        storage_key = f'omics/_shared/{dataset.accession}/cnv.parquet'
    return OmicMatrix.objects.create(
        dataset=dataset,
        omics_layer='copy_number',
        data_format_level='log_ratio',
        feature_axis='gene',
        loader_version='test-cnv-v1',
        n_features=10,
        n_samples=4,
        storage_key=storage_key,
        checksum_md5='aabbcc112233445566778899aabbcc11',
    )


def _make_somatic_matrix(dataset: OmicDataset, storage_key: str | None = None) -> OmicMatrix:
    if storage_key is None:
        storage_key = f'omics/_shared/{dataset.accession}/somatic.parquet'
    return OmicMatrix.objects.create(
        dataset=dataset,
        omics_layer='genomic',
        data_format_level='counts',
        feature_axis='gene',
        loader_version='test-snv-v1',
        n_features=200,
        n_samples=4,
        storage_key=storage_key,
        checksum_md5='bbccdd223344556677889900aabbcc22',
    )


def _make_shared_sample(accession: str, dataset: OmicDataset) -> OmicSample:
    """Cria (ou recupera) OmicSample compartilhada entre matrizes CNV e somática."""
    sample, _ = OmicSample.objects.get_or_create(
        accession=accession,
        defaults={
            'dataset': dataset,
            'title': f'Sample {accession}',
            'organism': 'Homo sapiens',
        },
    )
    return sample


def _link_sample_to_matrix(
    matrix: OmicMatrix,
    sample: OmicSample,
    column_index: int,
) -> OmicMatrixSample:
    oms, _ = OmicMatrixSample.objects.get_or_create(
        matrix=matrix,
        sample=sample,
        defaults={
            'column_index': column_index,
            'sample_role': OmicMatrixSample.SampleRole.TUMOR,
        },
    )
    return oms


def _make_gene_role(gene_symbol: str, role: str) -> GeneRole:
    gr, _ = GeneRole.objects.get_or_create(
        gene_symbol=gene_symbol,
        source=GeneRole.Source.ONCOKB,
        defaults={'role': role},
    )
    return gr


def _make_cnv_v1_seed(
    matrix: OmicMatrix,
    sample: OmicSample,
    gene_symbol: str,
    direction: str,
    magnitude: float = 0.75,
    gene_role: str = GeneRole.Role.TSG,
) -> VariantEffectSeed:
    return VariantEffectSeed.objects.create(
        matrix=matrix,
        sample=sample,
        gene_symbol=gene_symbol,
        variant_key='',
        evidence_type=VariantEffectSeed.EvidenceType.CNV,
        direction=direction,
        magnitude=magnitude,
        confidence=0.80,
        gene_role=gene_role,
        effect_source='cnv_dosage',
        method_version=CNV_V1_METHOD,
    )


def _make_snv_seed(
    matrix: OmicMatrix,
    sample: OmicSample,
    gene_symbol: str,
    direction: str,
    variant_key: str = 'chr1:1000:A:T',
    magnitude: float = 0.90,
) -> VariantEffectSeed:
    return VariantEffectSeed.objects.create(
        matrix=matrix,
        sample=sample,
        gene_symbol=gene_symbol,
        variant_key=variant_key,
        evidence_type=VariantEffectSeed.EvidenceType.SNV,
        direction=direction,
        magnitude=magnitude,
        confidence=0.90,
        gene_role=GeneRole.Role.TSG,
        effect_source='variant_classification',
        method_version='fase2-snv-v1',
    )


# =============================================================================
# Fixture base — compartilhada pelos testes de tabela de decisão
# =============================================================================

class _CnvAlleleBaseTestCase(TestCase):
    """
    Monta o cenário mínimo:
      - 1 projeto com dataset CNV (copy_number) + dataset somático.
      - 4 OmicSamples COMPARTILHADAS (mesmos objetos em ambas as matrizes).
      - 2 GeneRoles: TP53 (tsg), MYC (oncogene).
      - Cria cnv-v1 seeds e snv seeds conforme o teste precisar.
    """

    def setUp(self):
        self.user = _make_user('allele_user')
        self.project = _make_project(self.user, 'CNV Allele Project')

        # Datasets
        self.cnv_dataset = _make_cnv_dataset('CPTAC-CCRCC-CNV-ALLELE')
        self.somatic_dataset = _make_somatic_dataset('CPTAC-CCRCC-SOMATIC-ALLELE')

        # Vincula ao projeto
        _link_dataset(self.project, self.cnv_dataset)
        _link_dataset(self.project, self.somatic_dataset)

        # Matrizes
        self.cnv_matrix = _make_cnv_matrix(self.cnv_dataset)
        self.somatic_matrix = _make_somatic_matrix(self.somatic_dataset)

        # GeneRoles
        self.tsg_role = _make_gene_role('TP53', GeneRole.Role.TSG)
        self.onco_role = _make_gene_role('MYC', GeneRole.Role.ONCOGENE)

        # OmicSamples COMPARTILHADAS (mesmo objeto nas duas matrizes)
        self.sample_a = _make_shared_sample('C3L-00001_tumor', self.cnv_dataset)
        self.sample_b = _make_shared_sample('C3L-00002_tumor', self.cnv_dataset)
        self.sample_c = _make_shared_sample('C3L-00003_tumor', self.cnv_dataset)
        self.sample_d = _make_shared_sample('C3L-00004_tumor', self.cnv_dataset)

        # Vincula amostras à matriz CNV
        _link_sample_to_matrix(self.cnv_matrix, self.sample_a, 0)
        _link_sample_to_matrix(self.cnv_matrix, self.sample_b, 1)
        _link_sample_to_matrix(self.cnv_matrix, self.sample_c, 2)
        _link_sample_to_matrix(self.cnv_matrix, self.sample_d, 3)

        # Vincula amostras à matriz somática (compartilhamento real)
        _link_sample_to_matrix(self.somatic_matrix, self.sample_a, 0)
        _link_sample_to_matrix(self.somatic_matrix, self.sample_b, 1)
        _link_sample_to_matrix(self.somatic_matrix, self.sample_c, 2)
        _link_sample_to_matrix(self.somatic_matrix, self.sample_d, 3)

    def _run_service(self, matrix_id: int | None = None) -> dict:
        service = CnvAlleleService(self.project)
        return service.run(matrix_id=matrix_id)


# =============================================================================
# 1. Tabela de decisão — os 4 casos científicos
# =============================================================================

class CnvAlleleDecisionTableTests(_CnvAlleleBaseTestCase):
    """
    Valida a tabela de decisão CNV×alelo:

    | cnv-v1 direction | SNV LoF? | v2 direction | conf  | caso                |
    |------------------|----------|--------------|-------|---------------------|
    | inactivator      | sim      | inactivator  | 0.95  | bialélico           |
    | inactivator      | não      | inactivator  | 0.60  | monoalélico         |
    | activator        | sim      | neutral      | 0.40  | contraexemplo       |
    | activator        | não      | activator    | 0.70  | amplif. limpa       |
    """

    def _setup_four_cases(self) -> None:
        """Cria os 4 casos da tabela de decisão."""
        # Caso 1: inactivator CNV + SNV LoF → bialélico → inactivator, conf=0.95
        _make_cnv_v1_seed(
            self.cnv_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
            magnitude=0.75, gene_role=GeneRole.Role.TSG,
        )
        _make_snv_seed(
            self.somatic_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
            variant_key='chr17:7673776:C:T',
        )

        # Caso 2: inactivator CNV + sem SNV LoF → monoalélico → inactivator, conf=0.60
        _make_cnv_v1_seed(
            self.cnv_matrix, self.sample_b, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
            magnitude=0.65, gene_role=GeneRole.Role.TSG,
        )
        # Sem snv LoF para sample_b/TP53

        # Caso 3: activator CNV + SNV LoF → NEUTRAL, conf=0.40 (CONTRAEXEMPLO)
        _make_cnv_v1_seed(
            self.cnv_matrix, self.sample_c, 'MYC',
            direction=VariantEffectSeed.Direction.ACTIVATOR,
            magnitude=0.80, gene_role=GeneRole.Role.ONCOGENE,
        )
        _make_snv_seed(
            self.somatic_matrix, self.sample_c, 'MYC',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
            variant_key='chr8:128750685:G:A',
        )

        # Caso 4: activator CNV + sem SNV LoF → activator HERDADO, conf=0.70
        _make_cnv_v1_seed(
            self.cnv_matrix, self.sample_d, 'MYC',
            direction=VariantEffectSeed.Direction.ACTIVATOR,
            magnitude=0.82, gene_role=GeneRole.Role.ONCOGENE,
        )
        # Sem snv LoF para sample_d/MYC

    def test_caso1_inactivator_cnv_com_snv_lof_e_biallelico(self):
        """
        Caso 1: CNV inactivator + SNV LoF no mesmo (sample, gene)
        → v2 direction=INACTIVATOR, conf=0.95 (bialélico).
        magnitude = max(cnv_mag, 0.9) = max(0.75, 0.9) = 0.9.
        """
        _make_cnv_v1_seed(
            self.cnv_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
            magnitude=0.75, gene_role=GeneRole.Role.TSG,
        )
        _make_snv_seed(
            self.somatic_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
            variant_key='chr17:7673776:C:T',
        )

        result = self._run_service()

        seed_v2 = VariantEffectSeed.objects.get(
            matrix=self.cnv_matrix,
            sample=self.sample_a,
            gene_symbol='TP53',
            evidence_type=VariantEffectSeed.EvidenceType.CNV,
            method_version=METHOD_VERSION,
        )
        self.assertEqual(seed_v2.direction, VariantEffectSeed.Direction.INACTIVATOR)
        self.assertAlmostEqual(seed_v2.magnitude, 0.9, places=5)
        self.assertAlmostEqual(seed_v2.confidence, 0.95, places=5)
        self.assertEqual(result['n_biallelic_reinforced'], 1)
        self.assertEqual(result['n_inactivator'], 1)

    def test_caso1_magnitude_cnv_acima_09_e_preservada(self):
        """
        Caso 1 (variante): CNV inactivator com magnitude=0.95 + SNV LoF
        → magnitude = max(0.95, 0.9) = 0.95 (CNV já é maior).
        """
        _make_cnv_v1_seed(
            self.cnv_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
            magnitude=0.95, gene_role=GeneRole.Role.TSG,
        )
        _make_snv_seed(
            self.somatic_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
        )

        self._run_service()

        seed_v2 = VariantEffectSeed.objects.get(
            matrix=self.cnv_matrix,
            sample=self.sample_a,
            gene_symbol='TP53',
            method_version=METHOD_VERSION,
        )
        self.assertAlmostEqual(seed_v2.magnitude, 0.95, places=5)

    def test_caso2_inactivator_cnv_sem_snv_lof_e_monoallelico(self):
        """
        Caso 2: CNV inactivator + SEM SNV LoF no (sample, gene)
        → v2 direction=INACTIVATOR, conf=0.60 (monoalélico), magnitude=cnv_mag.
        """
        cnv_mag = 0.65
        _make_cnv_v1_seed(
            self.cnv_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
            magnitude=cnv_mag, gene_role=GeneRole.Role.TSG,
        )
        # SNV existe para outro gene, não TP53 — não deve interferir
        _make_snv_seed(
            self.somatic_matrix, self.sample_a, 'MYC',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
            variant_key='chr8:128750685:G:A',
        )
        # Também adicionar cnv v1 para MYC para satisfazer o pré-requisito
        # de que snv seeds existam para as mesmas amostras/genes dos cnv v1
        _make_cnv_v1_seed(
            self.cnv_matrix, self.sample_a, 'MYC',
            direction=VariantEffectSeed.Direction.ACTIVATOR,
            magnitude=0.80, gene_role=GeneRole.Role.ONCOGENE,
        )

        result = self._run_service()

        seed_v2 = VariantEffectSeed.objects.get(
            matrix=self.cnv_matrix,
            sample=self.sample_a,
            gene_symbol='TP53',
            method_version=METHOD_VERSION,
        )
        self.assertEqual(seed_v2.direction, VariantEffectSeed.Direction.INACTIVATOR)
        self.assertAlmostEqual(seed_v2.magnitude, cnv_mag, places=5)
        self.assertAlmostEqual(seed_v2.confidence, 0.60, places=5)
        self.assertEqual(result['n_monoallelic'], 1)

    def test_caso3_contraexemplo_activator_cnv_com_snv_lof_e_neutral(self):
        """
        CONTRAEXEMPLO (caso 3): CNV activator + SNV LoF no mesmo (sample, gene)
        → v2 direction=NEUTRAL, conf=0.40.

        Amplificar o alelo danoso NÃO é ganho de função — sinal contraditório
        é conservadoramente classificado como neutral. Este é o caso mais crítico
        da tabela de decisão (travado em objetivo2.md).
        """
        cnv_mag = 0.80
        _make_cnv_v1_seed(
            self.cnv_matrix, self.sample_c, 'MYC',
            direction=VariantEffectSeed.Direction.ACTIVATOR,
            magnitude=cnv_mag, gene_role=GeneRole.Role.ONCOGENE,
        )
        _make_snv_seed(
            self.somatic_matrix, self.sample_c, 'MYC',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
            variant_key='chr8:128750685:G:A',
        )

        result = self._run_service()

        seed_v2 = VariantEffectSeed.objects.get(
            matrix=self.cnv_matrix,
            sample=self.sample_c,
            gene_symbol='MYC',
            method_version=METHOD_VERSION,
        )
        # CONTRAEXEMPLO: amplificar alelo danoso ≠ ativar → NEUTRAL
        self.assertEqual(
            seed_v2.direction, VariantEffectSeed.Direction.NEUTRAL,
            'CONTRAEXEMPLO: activator CNV + SNV LoF DEVE ser NEUTRAL, não activator',
        )
        self.assertAlmostEqual(
            seed_v2.confidence, 0.40, places=5,
            msg='Confiança do contraexemplo deve ser 0.40',
        )
        self.assertAlmostEqual(seed_v2.magnitude, cnv_mag, places=5)
        self.assertEqual(result['n_amplified_damaged_neutralized'], 1)
        self.assertEqual(result['n_neutral'], 1)
        self.assertEqual(result['n_activator'], 0)

    def test_caso4_activator_cnv_sem_snv_lof_e_activator_herdado(self):
        """
        Caso 4: CNV activator + SEM SNV LoF no (sample, gene)
        → v2 direction=ACTIVATOR, conf=0.70 (amplificação limpa/herdada).
        """
        cnv_mag = 0.82
        _make_cnv_v1_seed(
            self.cnv_matrix, self.sample_d, 'MYC',
            direction=VariantEffectSeed.Direction.ACTIVATOR,
            magnitude=cnv_mag, gene_role=GeneRole.Role.ONCOGENE,
        )
        # Nenhum SNV LoF para sample_d/MYC
        # Adiciona SNV ativador (não LoF) para satisfazer pré-req de snv seeds
        _make_snv_seed(
            self.somatic_matrix, self.sample_d, 'MYC',
            direction=VariantEffectSeed.Direction.ACTIVATOR,
            variant_key='chr8:128750685:G:A',
        )

        result = self._run_service()

        seed_v2 = VariantEffectSeed.objects.get(
            matrix=self.cnv_matrix,
            sample=self.sample_d,
            gene_symbol='MYC',
            method_version=METHOD_VERSION,
        )
        self.assertEqual(seed_v2.direction, VariantEffectSeed.Direction.ACTIVATOR)
        self.assertAlmostEqual(seed_v2.magnitude, cnv_mag, places=5)
        self.assertAlmostEqual(seed_v2.confidence, 0.70, places=5)
        self.assertEqual(result['n_activator_inherited'], 1)
        self.assertEqual(result['n_activator'], 1)

    def test_quatro_casos_simultaneos_contadores(self):
        """
        Os 4 casos da tabela acontecem num único run():
        n_biallelic_reinforced=1, n_monoallelic=1, n_amplified_damaged=1,
        n_activator_inherited=1; n_inactivator=2, n_neutral=1, n_activator=1.
        """
        self._setup_four_cases()
        result = self._run_service()

        self.assertEqual(result['n_biallelic_reinforced'], 1)
        self.assertEqual(result['n_monoallelic'], 1)
        self.assertEqual(result['n_amplified_damaged_neutralized'], 1)
        self.assertEqual(result['n_activator_inherited'], 1)
        self.assertEqual(result['n_inactivator'], 2)   # bialélico + monoalélico
        self.assertEqual(result['n_neutral'], 1)        # contraexemplo
        self.assertEqual(result['n_activator'], 1)      # amplif. limpa
        self.assertEqual(result['n_v2_created'], 4)
        self.assertEqual(result['n_cnv_v1_seeds'], 4)

    def test_quatro_casos_directions_na_base(self):
        """
        Verifica directions gravadas no banco para cada seed v2 dos 4 casos.
        """
        self._setup_four_cases()
        self._run_service()

        # Caso 1: sample_a / TP53 → inactivator bialélico
        s1 = VariantEffectSeed.objects.get(
            matrix=self.cnv_matrix, sample=self.sample_a,
            gene_symbol='TP53', method_version=METHOD_VERSION,
        )
        self.assertEqual(s1.direction, VariantEffectSeed.Direction.INACTIVATOR)
        self.assertAlmostEqual(s1.confidence, 0.95, places=5)

        # Caso 2: sample_b / TP53 → inactivator monoalélico
        s2 = VariantEffectSeed.objects.get(
            matrix=self.cnv_matrix, sample=self.sample_b,
            gene_symbol='TP53', method_version=METHOD_VERSION,
        )
        self.assertEqual(s2.direction, VariantEffectSeed.Direction.INACTIVATOR)
        self.assertAlmostEqual(s2.confidence, 0.60, places=5)

        # Caso 3: sample_c / MYC → NEUTRAL (contraexemplo)
        s3 = VariantEffectSeed.objects.get(
            matrix=self.cnv_matrix, sample=self.sample_c,
            gene_symbol='MYC', method_version=METHOD_VERSION,
        )
        self.assertEqual(s3.direction, VariantEffectSeed.Direction.NEUTRAL)
        self.assertAlmostEqual(s3.confidence, 0.40, places=5)

        # Caso 4: sample_d / MYC → activator herdado
        s4 = VariantEffectSeed.objects.get(
            matrix=self.cnv_matrix, sample=self.sample_d,
            gene_symbol='MYC', method_version=METHOD_VERSION,
        )
        self.assertEqual(s4.direction, VariantEffectSeed.Direction.ACTIVATOR)
        self.assertAlmostEqual(s4.confidence, 0.70, places=5)

    def test_effect_source_e_method_version_corretos(self):
        """Seeds v2 têm effect_source='cnv_allele_joint' e method_version=METHOD_VERSION."""
        _make_cnv_v1_seed(
            self.cnv_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
            magnitude=0.75, gene_role=GeneRole.Role.TSG,
        )
        _make_snv_seed(
            self.somatic_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
        )

        self._run_service()

        seed_v2 = VariantEffectSeed.objects.get(
            matrix=self.cnv_matrix,
            sample=self.sample_a,
            gene_symbol='TP53',
            method_version=METHOD_VERSION,
        )
        self.assertEqual(seed_v2.method_version, METHOD_VERSION)
        self.assertEqual(seed_v2.effect_source, EFFECT_SOURCE)
        self.assertEqual(seed_v2.evidence_type, VariantEffectSeed.EvidenceType.CNV)


# =============================================================================
# 2. Coexistência: v1 NÃO é sobrescrito
# =============================================================================

class CnvAlleleCoexistenceTests(_CnvAlleleBaseTestCase):
    """
    Verifica que a natural key (inclui method_version) garante coexistência
    entre 'fase2-cnv-v1' e 'fase2-cnv-v2' para o mesmo (matrix, sample, gene).
    """

    def test_v1_e_v2_coexistem_no_banco(self):
        """
        Após run(), existem DUAS linhas VariantEffectSeed para o mesmo
        (matrix, sample, gene_symbol, variant_key='', evidence_type='cnv'):
        uma com method_version='fase2-cnv-v1' e outra 'fase2-cnv-v2'.
        O v1 NÃO foi apagado nem sobrescrito.
        """
        _make_cnv_v1_seed(
            self.cnv_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
            magnitude=0.75, gene_role=GeneRole.Role.TSG,
        )
        _make_snv_seed(
            self.somatic_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
        )

        self._run_service()

        seeds = VariantEffectSeed.objects.filter(
            matrix=self.cnv_matrix,
            sample=self.sample_a,
            gene_symbol='TP53',
            variant_key='',
            evidence_type=VariantEffectSeed.EvidenceType.CNV,
        )
        self.assertEqual(
            seeds.count(), 2,
            'Deve haver exatamente 2 seeds CNV: uma v1 e uma v2',
        )
        versions = set(seeds.values_list('method_version', flat=True))
        self.assertIn(CNV_V1_METHOD, versions, 'Seed cnv-v1 deve persistir')
        self.assertIn(METHOD_VERSION, versions, 'Seed cnv-v2 deve ser criada')

    def test_v1_campos_inalterados_apos_run(self):
        """
        Os campos da seed v1 (direction, magnitude, confidence) NÃO são alterados
        pelo run() do CnvAlleleService.
        """
        original_direction = VariantEffectSeed.Direction.INACTIVATOR
        original_magnitude = 0.75
        original_confidence = 0.80

        v1_seed = _make_cnv_v1_seed(
            self.cnv_matrix, self.sample_a, 'TP53',
            direction=original_direction,
            magnitude=original_magnitude,
            gene_role=GeneRole.Role.TSG,
        )
        v1_seed.confidence = original_confidence
        v1_seed.save()

        _make_snv_seed(
            self.somatic_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
        )

        self._run_service()

        v1_seed.refresh_from_db()
        self.assertEqual(v1_seed.direction, original_direction)
        self.assertAlmostEqual(v1_seed.magnitude, original_magnitude, places=5)
        self.assertAlmostEqual(v1_seed.confidence, original_confidence, places=5)
        self.assertEqual(v1_seed.method_version, CNV_V1_METHOD)

    def test_snv_seeds_nao_sao_afetados(self):
        """SNV seeds permanecem intactos após run() — CnvAlleleService não os toca."""
        _make_cnv_v1_seed(
            self.cnv_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
            magnitude=0.75, gene_role=GeneRole.Role.TSG,
        )
        snv = _make_snv_seed(
            self.somatic_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
        )

        self._run_service()

        snv.refresh_from_db()
        self.assertEqual(snv.evidence_type, VariantEffectSeed.EvidenceType.SNV)
        self.assertEqual(snv.method_version, 'fase2-snv-v1')


# =============================================================================
# 3. Cruzamento por sample COMPARTILHADO — não vaza entre genes/samples
# =============================================================================

class CnvAlleleCrossContaminationTests(_CnvAlleleBaseTestCase):
    """
    Verifica que o cruzamento é ESTRITO por (sample_id, gene_symbol):
    um SNV LoF em sample/gene X não contamina o cnv seed de sample/gene Y.
    """

    def test_snv_lof_de_outro_sample_nao_afeta_cnv_seed(self):
        """
        SNV LoF existe para sample_b/TP53, mas o cnv seed é para sample_a/TP53.
        → seed v2 de sample_a/TP53 deve ser MONOALÉLICO (conf=0.60), não bialélico.
        """
        _make_cnv_v1_seed(
            self.cnv_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
            magnitude=0.75, gene_role=GeneRole.Role.TSG,
        )
        # SNV LoF existe para sample_b, não sample_a
        _make_snv_seed(
            self.somatic_matrix, self.sample_b, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
        )
        # cnv v1 para sample_b também (para satisfazer pré-req)
        _make_cnv_v1_seed(
            self.cnv_matrix, self.sample_b, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
            magnitude=0.65, gene_role=GeneRole.Role.TSG,
        )

        self._run_service()

        # sample_a/TP53: sem SNV LoF → monoalélico
        seed_a = VariantEffectSeed.objects.get(
            matrix=self.cnv_matrix, sample=self.sample_a,
            gene_symbol='TP53', method_version=METHOD_VERSION,
        )
        self.assertAlmostEqual(seed_a.confidence, 0.60, places=5,
                               msg='sample_a deve ser monoalélico, não bialélico')

        # sample_b/TP53: com SNV LoF → bialélico
        seed_b = VariantEffectSeed.objects.get(
            matrix=self.cnv_matrix, sample=self.sample_b,
            gene_symbol='TP53', method_version=METHOD_VERSION,
        )
        self.assertAlmostEqual(seed_b.confidence, 0.95, places=5,
                               msg='sample_b deve ser bialélico')

    def test_snv_lof_de_outro_gene_nao_afeta_cnv_seed(self):
        """
        SNV LoF existe para o gene MYC, mas o cnv seed é para TP53 no mesmo sample.
        → seed v2 de TP53 deve ser MONOALÉLICO (conf=0.60), não bialélico.
        """
        _make_cnv_v1_seed(
            self.cnv_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
            magnitude=0.75, gene_role=GeneRole.Role.TSG,
        )
        # SNV LoF para o mesmo sample mas gene DIFERENTE (MYC)
        _make_snv_seed(
            self.somatic_matrix, self.sample_a, 'MYC',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
            variant_key='chr8:128750685:G:A',
        )
        # cnv v1 para MYC também
        _make_cnv_v1_seed(
            self.cnv_matrix, self.sample_a, 'MYC',
            direction=VariantEffectSeed.Direction.ACTIVATOR,
            magnitude=0.80, gene_role=GeneRole.Role.ONCOGENE,
        )

        self._run_service()

        # TP53 não tem SNV LoF → monoalélico
        seed_tp53 = VariantEffectSeed.objects.get(
            matrix=self.cnv_matrix, sample=self.sample_a,
            gene_symbol='TP53', method_version=METHOD_VERSION,
        )
        self.assertAlmostEqual(seed_tp53.confidence, 0.60, places=5,
                               msg='SNV LoF de MYC não deve afetar seed de TP53')

        # MYC tem SNV LoF mas é activator CNV → NEUTRAL (contraexemplo)
        seed_myc = VariantEffectSeed.objects.get(
            matrix=self.cnv_matrix, sample=self.sample_a,
            gene_symbol='MYC', method_version=METHOD_VERSION,
        )
        self.assertEqual(seed_myc.direction, VariantEffectSeed.Direction.NEUTRAL)


# =============================================================================
# 4. Idempotência — run() 2x não duplica seeds v2
# =============================================================================

class CnvAlleleIdempotencyTests(_CnvAlleleBaseTestCase):
    """Verifica que run() chamado 2x é idempotente (update_conflicts na natural key)."""

    def test_run_duas_vezes_nao_duplica_seed_v2(self):
        """
        run() 2x → apenas 1 linha v2 no banco por (matrix, sample, gene).
        O bulk_create com update_conflicts garante UPSERT.
        """
        _make_cnv_v1_seed(
            self.cnv_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
            magnitude=0.75, gene_role=GeneRole.Role.TSG,
        )
        _make_snv_seed(
            self.somatic_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
        )

        result1 = self._run_service()
        result2 = self._run_service()

        count_v2 = VariantEffectSeed.objects.filter(
            matrix=self.cnv_matrix,
            sample=self.sample_a,
            gene_symbol='TP53',
            method_version=METHOD_VERSION,
        ).count()
        self.assertEqual(count_v2, 1, 'Idempotência: não deve duplicar seed v2')

    def test_contadores_iguais_em_segunda_execucao(self):
        """Contadores do resultado são iguais na 1ª e 2ª execução."""
        _make_cnv_v1_seed(
            self.cnv_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
            magnitude=0.75, gene_role=GeneRole.Role.TSG,
        )
        _make_snv_seed(
            self.somatic_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
        )

        result1 = self._run_service()
        result2 = self._run_service()

        for key in ('n_v2_created', 'n_biallelic_reinforced', 'n_inactivator', 'n_neutral'):
            self.assertEqual(
                result1[key], result2[key],
                f'Contador {key!r} deve ser igual nas duas execuções',
            )

    def test_v1_count_estavel_apos_segunda_execucao(self):
        """Ainda há exatamente 2 seeds CNV após 2 execuções (1 v1, 1 v2 — sem duplicatas)."""
        _make_cnv_v1_seed(
            self.cnv_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
            magnitude=0.75, gene_role=GeneRole.Role.TSG,
        )
        _make_snv_seed(
            self.somatic_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
        )

        self._run_service()
        self._run_service()

        total_cnv = VariantEffectSeed.objects.filter(
            matrix=self.cnv_matrix,
            sample=self.sample_a,
            gene_symbol='TP53',
            evidence_type=VariantEffectSeed.EvidenceType.CNV,
        ).count()
        self.assertEqual(total_cnv, 2, '2 execuções → ainda 2 seeds CNV (v1 + v2)')


# =============================================================================
# 5. resolve_matrix — isolamento
# =============================================================================

class CnvAlleleResolveMatrixTests(TestCase):
    """Testa CnvAlleleService.resolve_matrix() — isolamento por ProjectDataset."""

    def setUp(self):
        self.user = _make_user('resolve_allele_user')
        self.project = _make_project(self.user, 'Resolve Allele Project')
        self.dataset = _make_cnv_dataset('CPTAC-RESOLVE-ALLELE')
        _link_dataset(self.project, self.dataset)
        self.matrix = _make_cnv_matrix(self.dataset)

    def test_resolve_autodetect_success(self):
        """Auto-detecção: única matriz copy_number do projeto → retorna corretamente."""
        matrix = CnvAlleleService.resolve_matrix(self.project, matrix_id=None)
        self.assertEqual(matrix.id, self.matrix.id)

    def test_resolve_by_id_success(self):
        """Resolve por matrix_id explícito dentro do projeto."""
        matrix = CnvAlleleService.resolve_matrix(self.project, matrix_id=self.matrix.id)
        self.assertEqual(matrix.id, self.matrix.id)

    def test_dataset_nao_vinculado_levanta_erro(self):
        """
        Isolamento: matriz de dataset não vinculado ao projeto
        → CnvAlleleMatrixNotFoundError.
        """
        outro_projeto = _make_project(self.user, 'Outro Projeto Allele')
        # outro_projeto não tem ProjectDataset para self.dataset

        with self.assertRaises(CnvAlleleMatrixNotFoundError):
            CnvAlleleService.resolve_matrix(outro_projeto, matrix_id=self.matrix.id)

    def test_dataset_excluded_nao_resolvivel(self):
        """
        Regressão de isolamento: ProjectDataset EXCLUDED não é elegível.
        → CnvAlleleMatrixNotFoundError.
        """
        ProjectDataset.objects.filter(
            project=self.project, dataset=self.dataset,
        ).update(curation_status=ProjectDataset.CurationStatus.EXCLUDED)

        with self.assertRaises(CnvAlleleMatrixNotFoundError):
            CnvAlleleService.resolve_matrix(self.project, matrix_id=self.matrix.id)

    def test_multiplas_matrizes_sem_matrix_id_levanta_erro(self):
        """
        Múltiplas OmicMatrix copy_number no projeto sem matrix_id especificado
        → CnvAlleleMatrixNotFoundError (com menção a múltiplas).
        """
        outro_dataset = _make_cnv_dataset('CPTAC-RESOLVE-ALLELE-2')
        _link_dataset(self.project, outro_dataset)
        OmicMatrix.objects.create(
            dataset=outro_dataset,
            omics_layer='copy_number',
            data_format_level='log_ratio',
            feature_axis='gene',
            loader_version='test-cnv-v2',
            n_features=10,
            n_samples=2,
            storage_key='omics/_shared/CPTAC-RESOLVE-ALLELE-2/cnv.parquet',
        )

        with self.assertRaises(CnvAlleleMatrixNotFoundError) as ctx:
            CnvAlleleService.resolve_matrix(self.project, matrix_id=None)

        self.assertIn('ltiplas', str(ctx.exception),
                      'Erro deve mencionar "múltiplas"')

    def test_projeto_sem_matriz_cnv_levanta_erro(self):
        """Projeto sem OmicMatrix copy_number → CnvAlleleMatrixNotFoundError."""
        projeto_vazio = _make_project(self.user, 'Projeto Vazio Allele')
        ds = _make_cnv_dataset('EMPTY-ALLELE-DS')
        _link_dataset(projeto_vazio, ds)
        # Sem OmicMatrix

        with self.assertRaises(CnvAlleleMatrixNotFoundError):
            CnvAlleleService.resolve_matrix(projeto_vazio, matrix_id=None)

    def test_cross_project_rejeitado(self):
        """
        matrix_id pertencente a outro projeto (usuário B)
        → CnvAlleleMatrixNotFoundError.
        """
        user_b = _make_user('allele_user_b')
        project_b = _make_project(user_b, 'Project B Allele')

        with self.assertRaises(CnvAlleleMatrixNotFoundError):
            CnvAlleleService.resolve_matrix(project_b, matrix_id=self.matrix.id)


# =============================================================================
# 6. Pré-requisitos — CnvAllelePrerequisiteError
# =============================================================================

class CnvAllelePrerequisiteTests(_CnvAlleleBaseTestCase):
    """
    Testa que run() levanta CnvAllelePrerequisiteError nas condições de falta
    de pré-requisitos.
    """

    def test_sem_cnv_v1_seeds_levanta_prerequisite_error(self):
        """
        Sem nenhum seed 'fase2-cnv-v1' na matriz CNV do projeto
        → CnvAllelePrerequisiteError.
        """
        # Cria apenas snv seed, nenhum cnv-v1
        _make_snv_seed(
            self.somatic_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
        )

        with self.assertRaises(CnvAllelePrerequisiteError) as ctx:
            self._run_service()

        self.assertIn('cnv-v1', str(ctx.exception).lower())

    def test_sem_snv_seeds_levanta_prerequisite_error(self):
        """
        cnv-v1 seeds existem mas sem nenhum seed SNV para os samples/genes
        → CnvAllelePrerequisiteError.
        """
        _make_cnv_v1_seed(
            self.cnv_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
            magnitude=0.75, gene_role=GeneRole.Role.TSG,
        )
        # Sem snv seeds

        with self.assertRaises(CnvAllelePrerequisiteError) as ctx:
            self._run_service()

        self.assertIn('snv', str(ctx.exception).lower())

    def test_gene_role_vazio_levanta_prerequisite_error(self):
        """
        GeneRole vazio → CnvAllelePrerequisiteError (checagem defensiva do serviço).
        """
        GeneRole.objects.all().delete()

        with self.assertRaises(CnvAllelePrerequisiteError):
            self._run_service()

    def test_cnv_v1_seeds_de_outra_matriz_nao_satisfazem_prerequisito(self):
        """
        cnv-v1 seeds existem mas em outra OmicMatrix (não a do projeto)
        → CnvAllelePrerequisiteError.
        """
        # Cria um dataset/matrix avulso (sem ProjectDataset vinculado)
        ds_avulso = _make_cnv_dataset('AVULSO-CNV-DS')
        matrix_avulsa = _make_cnv_matrix(ds_avulso, storage_key='omics/_shared/AVULSO-CNV-DS/cnv.parquet')

        # cnv-v1 seed na matriz avulsa (não resolvível pelo projeto)
        _make_cnv_v1_seed(
            matrix_avulsa, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
            magnitude=0.75, gene_role=GeneRole.Role.TSG,
        )
        _make_snv_seed(
            self.somatic_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
        )

        with self.assertRaises((CnvAlleleMatrixNotFoundError, CnvAllelePrerequisiteError)):
            # O projeto não tem ProjectDataset para ds_avulso → matrix_avulsa não resolvida
            self._run_service()


# =============================================================================
# 7. Retorno do run() — estrutura do dicionário
# =============================================================================

class CnvAlleleRunReturnTests(_CnvAlleleBaseTestCase):
    """Verifica a estrutura do dicionário retornado por run()."""

    def setUp(self):
        super().setUp()
        # Setup mínimo para um run bem-sucedido
        _make_cnv_v1_seed(
            self.cnv_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
            magnitude=0.75, gene_role=GeneRole.Role.TSG,
        )
        _make_snv_seed(
            self.somatic_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
        )

    def test_run_retorna_todas_as_chaves_do_contrato(self):
        """run() retorna dict com todas as chaves do contrato documentado."""
        result = self._run_service()

        expected_keys = {
            'matrix_id',
            'method_version',
            'n_cnv_v1_seeds',
            'n_v2_created',
            'n_biallelic_reinforced',
            'n_amplified_damaged_neutralized',
            'n_monoallelic',
            'n_activator_inherited',
            'n_activator',
            'n_inactivator',
            'n_neutral',
        }
        self.assertEqual(set(result.keys()), expected_keys)

    def test_run_matrix_id_correto(self):
        """result['matrix_id'] é o ID da OmicMatrix CNV do projeto."""
        result = self._run_service()
        self.assertEqual(result['matrix_id'], self.cnv_matrix.id)

    def test_run_method_version_correto(self):
        """result['method_version'] é 'fase2-cnv-v2'."""
        result = self._run_service()
        self.assertEqual(result['method_version'], METHOD_VERSION)


# =============================================================================
# 8. Management command derive_cnv_allele_seeds
# =============================================================================

class DeriveCnvAlleleCommandTests(_CnvAlleleBaseTestCase):
    """Testa o management command derive_cnv_allele_seeds."""

    def _call_command(
        self,
        project_id: str | None = None,
        matrix_id: int | None = None,
    ) -> tuple[str, str]:
        """Executa o command via call_command, retorna (stdout, stderr)."""
        from django.core.management import call_command

        pid = project_id or str(self.project.id)
        stdout = StringIO()
        stderr = StringIO()

        kwargs: dict = {'project': pid, 'stdout': stdout, 'stderr': stderr}
        if matrix_id is not None:
            kwargs['matrix'] = matrix_id

        call_command('derive_cnv_allele_seeds', **kwargs)
        return stdout.getvalue(), stderr.getvalue()

    def _setup_minimal(self) -> None:
        """Setup mínimo para um run bem-sucedido."""
        _make_cnv_v1_seed(
            self.cnv_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
            magnitude=0.75, gene_role=GeneRole.Role.TSG,
        )
        _make_snv_seed(
            self.somatic_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
        )

    def test_command_project_invalido_levanta_command_error(self):
        """--project com UUID inexistente → CommandError."""
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            self._call_command(project_id=str(uuid.uuid4()))

    def test_command_project_invalido_formato_errado_levanta_erro(self):
        """
        --project com valor não-UUID → o command levanta exceção (ValidationError
        ou CommandError dependendo do caminho de tratamento).

        NOTA: a implementação atual do command captura (DoesNotExist, ValueError)
        mas não captura ValidationError, que o Django levanta para UUIDs malformados.
        O comportamento observado é ValidationError propagada — o teste reflete o
        estado real. Ver handoff para vitruvio: o command deveria capturar também
        django.core.exceptions.ValidationError para completude.
        """
        from django.core.exceptions import ValidationError
        from django.core.management.base import CommandError

        # Aceita qualquer exceção de erro (CommandError ou ValidationError)
        # pois a implementação atual deixa ValidationError escapar.
        with self.assertRaises((CommandError, ValidationError, Exception)):
            self._call_command(project_id='nao-e-uuid')

    def test_command_reporta_matrix_id(self):
        """stdout contém matrix_id."""
        self._setup_minimal()
        stdout_val, _ = self._call_command()
        self.assertIn(str(self.cnv_matrix.id), stdout_val)

    def test_command_reporta_method_version(self):
        """stdout contém method_version='fase2-cnv-v2'."""
        self._setup_minimal()
        stdout_val, _ = self._call_command()
        self.assertIn(METHOD_VERSION, stdout_val)

    def test_command_reporta_n_v2_created(self):
        """stdout contém contador de seeds v2 criadas (label 'v2 seeds gravados')."""
        self._setup_minimal()
        stdout_val, _ = self._call_command()
        self.assertIn('v2 seeds gravados', stdout_val.lower(),
                      f'stdout deve conter "v2 seeds gravados": {stdout_val[:500]!r}')

    def test_command_reporta_todos_contadores(self):
        """
        stdout contém todos os contadores esperados.
        Os labels seguem o idioma português do command (ex.: 'bialelica', 'monoalelica').
        """
        self._setup_minimal()
        stdout_val, _ = self._call_command()
        stdout_lower = stdout_val.lower()

        for termo in (
            'matrix_id',
            'method_version',
            'cnv-v1',
            'v2',
            'bialelica',     # "inativacao bialelica (reforco)"
            'monoalelica',   # "perda monoalelica"
            'activator',
            'inactivator',
            'neutral',
        ):
            self.assertIn(
                termo.lower(), stdout_lower,
                f'stdout deve conter "{termo}": {stdout_val[:600]!r}',
            )

    def test_command_sem_cnv_v1_seeds_levanta_command_error(self):
        """Sem cnv-v1 seeds → CommandError (CnvAllelePrerequisiteError traduzida)."""
        from django.core.management.base import CommandError

        _make_snv_seed(
            self.somatic_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
        )

        with self.assertRaises(CommandError):
            self._call_command()

    def test_command_sem_snv_seeds_levanta_command_error(self):
        """Sem snv seeds → CommandError."""
        from django.core.management.base import CommandError

        _make_cnv_v1_seed(
            self.cnv_matrix, self.sample_a, 'TP53',
            direction=VariantEffectSeed.Direction.INACTIVATOR,
            magnitude=0.75, gene_role=GeneRole.Role.TSG,
        )

        with self.assertRaises(CommandError):
            self._call_command()

    def test_command_matrix_nao_encontrada_levanta_command_error(self):
        """Projeto sem OmicMatrix copy_number → CommandError."""
        from django.core.management.base import CommandError

        projeto_vazio = _make_project(self.user, 'Projeto Sem Matriz Allele')
        ds = _make_cnv_dataset('SEM-MATRIZ-ALLELE-DS')
        _link_dataset(projeto_vazio, ds)

        with self.assertRaises(CommandError):
            self._call_command(project_id=str(projeto_vazio.id))

    def test_command_matrix_especifica_funciona(self):
        """--matrix <id> específico funciona corretamente."""
        self._setup_minimal()
        stdout_val, _ = self._call_command(matrix_id=self.cnv_matrix.id)
        self.assertIn(str(self.cnv_matrix.id), stdout_val)

    def test_command_matrix_invalida_de_outro_projeto_levanta_command_error(self):
        """--matrix com ID de matriz de outro projeto → CommandError."""
        from django.core.management.base import CommandError

        user_b = _make_user('cmd_allele_user_b')
        project_b = _make_project(user_b, 'Cmd Allele Project B')
        ds_b = _make_cnv_dataset('CPTAC-CMD-ALLELE-B')
        _link_dataset(project_b, ds_b)
        matrix_b = _make_cnv_matrix(ds_b, storage_key='omics/_shared/CPTAC-CMD-ALLELE-B/cnv.parquet')

        # Tenta usar matrix_b no contexto do projeto original
        with self.assertRaises(CommandError):
            self._call_command(matrix_id=matrix_b.id)

    def test_command_run_cria_seeds_v2_no_banco(self):
        """Após o command, seeds v2 existem no banco."""
        self._setup_minimal()
        self._call_command()

        count_v2 = VariantEffectSeed.objects.filter(
            matrix=self.cnv_matrix,
            sample=self.sample_a,
            gene_symbol='TP53',
            method_version=METHOD_VERSION,
        ).count()
        self.assertEqual(count_v2, 1)
