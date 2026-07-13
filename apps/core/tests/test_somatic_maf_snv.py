"""
test_somatic_maf_snv.py — Cobertura do SomaticMafService e command load_somatic_maf.

OmnisPathway Objetivo 2, Fase 2, Slice 2.6 (Slice 2D).

NÚCLEO CIENTÍFICO (seed SNV) — coberto extensivamente:

  Regra de sinal SNV v1:
    TRUNCANTE (Frame_Shift_Del, Frame_Shift_Ins, Nonsense_Mutation, Splice_Site,
    Translation_Start_Site, Nonstop_Mutation):
      - gene_role=TSG → inactivator (mag=0.9, conf=0.9, effect_source='variant_classification')
      - gene_role=oncogene → neutral (truncar oncogene ≠ ativar)
      - NÃO consulta VariantEffectResolved
    MISSENSE (Missense_Mutation, In_Frame_Del, In_Frame_Ins):
      - Com VariantEffectResolved: herda direction/magnitude/confidence/effect_source
      - Sem VariantEffectResolved: PULA (não gera seed neutral sem sinal)
    OUTRAS classificações → PULA (unannotated_skipped)

  GDC API:
    - POST /files retorna hits com cases.submitter_id → file_map [{file_id, sample_accession}]
    - Hits sem file_id ignorados
    - Hits sem cases ignorados
    - 0 hits → GdcApiError

  ORM set-based:
    - OmicMatrix 'genomic'/'counts'/'gene' criado
    - OmicSample get_or_create por accession (reuso cross-layer)
    - OmicMatrixSample bulk_create com ignore_conflicts
    - VariantEffectSeed bulk_create com update_conflicts (idempotente)

  Idempotência:
    - 2x run com mesmo dataset → SomaticMafAlreadyLoadedError no segundo
    - Job ativo → SomaticMafJobActiveError
    - Dedupe de seeds por natural key

  Sensitive-data-handling:
    - db_url NUNCA no resultado nem nos parâmetros do job
    - storage_key é chave lógica (_shared, sem caminho físico)

  Isolamento:
    - Task abortada quando job.project_id != project_id
    - project_id inexistente → erro sem processar

Padrões obrigatórios:
  - SEM internet: GDC API (requests.post) SEMPRE mockado.
  - SEM rust_engine real: load_somatic_maf SEMPRE mockado.
  - TSV de ocorrências criado em tempdir real para que o service possa ler.
  - default_storage mockado com FileSystemStorage em tmpdir.
  - SEM pytest — usa django.test.TestCase.
  - GeneRole, VariantEffectResolved populados via ORM (não via Rust).
"""

from __future__ import annotations

import csv
import os
import tempfile
import uuid
from io import StringIO
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.files.storage import FileSystemStorage
from django.test import TestCase

from apps.core.models import (
    DaVinciProject,
    GeneRole,
    IngestionJob,
    OmicDataset,
    OmicMatrix,
    OmicMatrixSample,
    OmicSample,
    ProjectDataset,
    VariantEffectResolved,
    VariantEffectSeed,
)
from apps.core.services.somatic_maf_service import (
    GDC_FILES_URL,
    GDC_PROJECT,
    LOADER_VERSION,
    METHOD_VERSION,
    SOMATIC_ACCESSION,
    GdcApiError,
    SomaticMafAlreadyLoadedError,
    SomaticMafGeneRoleEmptyError,
    SomaticMafJobActiveError,
    SomaticMafService,
    SomaticMafVariantEffectResolvedEmptyError,
)


# =============================================================================
# Helpers de fixture
# =============================================================================

def _make_user(username: str = 'somatic_user') -> User:
    return User.objects.create_user(username=username, password='pw')


def _make_project(user: User, title: str = 'Somatic MAF Project') -> DaVinciProject:
    slug = f'{title.lower().replace(" ", "-")}-{user.username}'
    return DaVinciProject.objects.create(
        user=user, title=title, slug=slug, query_term='somatic-maf',
    )


def _populate_gene_role(genes: dict[str, str]) -> None:
    """genes: {symbol: role}. source='oncokb'."""
    for sym, role in genes.items():
        GeneRole.objects.get_or_create(
            gene_symbol=sym, source='oncokb',
            defaults={'role': role},
        )


def _create_resolved_effect(
    variant_key: str,
    gene_symbol: str,
    direction: str = VariantEffectResolved.Direction.ACTIVATOR,
    magnitude: float = 0.9,
    confidence: float = 0.9,
    effect_source: str = 'clinvar',
    gene_role_used: str = GeneRole.Role.ONCOGENE,
    resolution_version: str = 'fase2-snv-v1',
) -> VariantEffectResolved:
    return VariantEffectResolved.objects.create(
        variant_key=variant_key,
        gene_symbol=gene_symbol,
        magnitude=magnitude,
        effect_class='damaging',
        direction=direction,
        confidence=confidence,
        effect_source=effect_source,
        gene_role_used=gene_role_used,
        resolution_version=resolution_version,
    )


def _write_occurrences_tsv(
    path: str,
    rows: list[tuple[str, str, str, str]],
) -> None:
    """
    Escreve TSV de ocorrências em path.
    rows: [(sample_accession, variant_key, gene_symbol, variant_classification)]
    """
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                'sample_accession', 'variant_key',
                'gene_symbol', 'variant_classification',
            ],
            delimiter='\t',
        )
        writer.writeheader()
        for sample_acc, vk, gene, vc in rows:
            writer.writerow({
                'sample_accession': sample_acc,
                'variant_key': vk,
                'gene_symbol': gene,
                'variant_classification': vc,
            })


def _make_fake_sample_column(case_id: str, sample_role: str = 'tumor', column_index: int = 0):
    col = MagicMock()
    col.case_id = case_id
    col.sample_role = sample_role
    col.column_index = column_index
    return col


def _make_fake_manifest(
    parquet_path: str,
    occurrences_path: str,
    sample_case_ids: list[str] | None = None,
    n_files: int = 3,
    n_occurrences: int = 5,
) -> MagicMock:
    """Manifesto fake de SomaticMafManifest do Rust."""
    case_ids = sample_case_ids or ['C3L-00004', 'C3L-00005']
    m = MagicMock()
    m.parquet_path = parquet_path
    m.occurrences_path = occurrences_path
    m.checksum_md5 = 'abcdef1234567890abcdef1234567890'
    m.n_features = 200
    m.n_samples = len(case_ids)
    m.n_files_processed = n_files
    m.n_occurrences = n_occurrences
    m.n_variants_skipped_synonymous = 2
    m.n_variants_skipped_offlist = 8
    m.errors = []
    m.sample_columns = [
        _make_fake_sample_column(cid, 'tumor', idx)
        for idx, cid in enumerate(case_ids)
    ]
    return m


def _make_gdc_response(submitter_ids: list[str], file_ids: list[str] | None = None) -> dict:
    """Resposta JSON mockada da GDC API."""
    if file_ids is None:
        file_ids = [f'file-{i:04d}' for i in range(len(submitter_ids))]
    hits = []
    for file_id, sub_id in zip(file_ids, submitter_ids):
        hits.append({
            'file_id': file_id,
            'cases': [{'submitter_id': sub_id}],
        })
    return {'data': {'hits': hits}}


# =============================================================================
# Classe base com storage e tmpdir
# =============================================================================

class _SomaticStorageTestCase(TestCase):
    """Substitui default_storage por FileSystemStorage local."""

    def setUp(self):
        super().setUp()
        self._storage_tmpdir = tempfile.mkdtemp(prefix='davinci_somatic_test_')
        self._storage = FileSystemStorage(location=self._storage_tmpdir)
        self._storage_patcher = patch(
            'apps.core.services.somatic_maf_service.default_storage',
            new=self._storage,
        )
        self._storage_patcher.start()

    def _put_parquet_in_tmpdir(self, name: str = 'cptac_ccrcc_somatic.parquet') -> str:
        """Cria arquivo Parquet dummy no tmpdir e retorna o caminho."""
        path = os.path.join(self._storage_tmpdir, name)
        with open(path, 'wb') as f:
            f.write(b'PAR1' + b'\x00' * 64 + b'PAR1')
        return path

    def tearDown(self):
        self._storage_patcher.stop()
        import shutil
        shutil.rmtree(self._storage_tmpdir, ignore_errors=True)
        super().tearDown()


# =============================================================================
# 1. resolve_file_map — GDC API (requests mockado)
# =============================================================================

class SomaticMafResolveFileMapTests(TestCase):
    """Testa SomaticMafService.resolve_file_map()."""

    def _mock_response(self, data: dict, status_code: int = 200) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = data
        resp.raise_for_status.return_value = None
        return resp

    def test_resolve_file_map_returns_correct_structure(self):
        """
        POST /files retornando 2 hits com submitter_ids C3L-00001, C3L-00002 →
        file_map = [{file_id, sample_accession='C3L-00001_tumor'}, ...]
        """
        gdc_data = _make_gdc_response(
            submitter_ids=['C3L-00001', 'C3L-00002'],
            file_ids=['file-aaaa', 'file-bbbb'],
        )

        with patch('requests.post', return_value=self._mock_response(gdc_data)):
            file_map, n_files, n_cases = SomaticMafService.resolve_file_map()

        self.assertEqual(n_files, 2)
        self.assertEqual(n_cases, 2)

        accessions = {e['sample_accession'] for e in file_map}
        self.assertIn('C3L-00001_tumor', accessions)
        self.assertIn('C3L-00002_tumor', accessions)

        file_ids_returned = {e['file_id'] for e in file_map}
        self.assertIn('file-aaaa', file_ids_returned)

    def test_resolve_file_map_hit_without_file_id_ignored(self):
        """Hit sem file_id é ignorado."""
        gdc_data = {
            'data': {
                'hits': [
                    {'file_id': '', 'cases': [{'submitter_id': 'C3L-00001'}]},
                    {'file_id': 'file-bbbb', 'cases': [{'submitter_id': 'C3L-00002'}]},
                ]
            }
        }
        with patch('requests.post', return_value=self._mock_response(gdc_data)):
            file_map, n_files, n_cases = SomaticMafService.resolve_file_map()

        self.assertEqual(n_files, 1)
        self.assertEqual(file_map[0]['file_id'], 'file-bbbb')

    def test_resolve_file_map_hit_without_cases_ignored(self):
        """Hit sem cases (lista vazia) é ignorado."""
        gdc_data = {
            'data': {
                'hits': [
                    {'file_id': 'file-aaaa', 'cases': []},
                    {'file_id': 'file-bbbb', 'cases': [{'submitter_id': 'C3L-00002'}]},
                ]
            }
        }
        with patch('requests.post', return_value=self._mock_response(gdc_data)):
            file_map, n_files, _ = SomaticMafService.resolve_file_map()

        self.assertEqual(n_files, 1)

    def test_resolve_file_map_zero_hits_raises_gdc_api_error(self):
        """0 hits → GdcApiError."""
        gdc_data = {'data': {'hits': []}}
        with patch('requests.post', return_value=self._mock_response(gdc_data)):
            with self.assertRaises(GdcApiError):
                SomaticMafService.resolve_file_map()

    def test_resolve_file_map_network_error_raises_gdc_api_error(self):
        """Erro de rede (RequestException) → GdcApiError."""
        import requests as requests_lib
        with patch('requests.post',
                   side_effect=requests_lib.RequestException('Network error')):
            with self.assertRaises(GdcApiError):
                SomaticMafService.resolve_file_map()

    def test_resolve_file_map_same_submitter_multiple_files(self):
        """Mesmo submitter_id em 2 arquivos → n_cases=1, n_files=2."""
        gdc_data = {
            'data': {
                'hits': [
                    {'file_id': 'file-aaaa', 'cases': [{'submitter_id': 'C3L-00001'}]},
                    {'file_id': 'file-bbbb', 'cases': [{'submitter_id': 'C3L-00001'}]},
                ]
            }
        }
        with patch('requests.post', return_value=self._mock_response(gdc_data)):
            file_map, n_files, n_cases = SomaticMafService.resolve_file_map()

        self.assertEqual(n_files, 2)
        self.assertEqual(n_cases, 1)  # mesmo caso, 2 arquivos


# =============================================================================
# 2. Pré-checks
# =============================================================================

class SomaticMafPrecheckTests(TestCase):
    """Testa _check_preconditions()."""

    def setUp(self):
        self.user = _make_user('somatic_precheck_user')
        self.project = _make_project(self.user, 'Precheck Project')

    def test_gene_role_empty_raises(self):
        """GeneRole vazio → SomaticMafGeneRoleEmptyError."""
        GeneRole.objects.all().delete()
        VariantEffectResolved.objects.create(
            variant_key='chr1:1:A:T', gene_symbol='TP53',
            magnitude=1.0, effect_class='damaging',
            direction='inactivator', confidence=0.9,
            effect_source='clinvar', gene_role_used='tsg',
            resolution_version='fase2-snv-v1',
        )

        service = SomaticMafService(self.project)
        with self.assertRaises(SomaticMafGeneRoleEmptyError):
            service._check_preconditions()

    def test_variant_effect_resolved_empty_raises(self):
        """VariantEffectResolved vazio → SomaticMafVariantEffectResolvedEmptyError."""
        _populate_gene_role({'TP53': GeneRole.Role.TSG})
        VariantEffectResolved.objects.all().delete()

        service = SomaticMafService(self.project)
        with self.assertRaises(SomaticMafVariantEffectResolvedEmptyError):
            service._check_preconditions()

    def test_both_populated_passes(self):
        """GeneRole + VariantEffectResolved populados → sem exceção."""
        _populate_gene_role({'TP53': GeneRole.Role.TSG})
        VariantEffectResolved.objects.create(
            variant_key='chr1:1:A:T', gene_symbol='TP53',
            magnitude=1.0, effect_class='damaging',
            direction='inactivator', confidence=0.9,
            effect_source='clinvar', gene_role_used='tsg',
            resolution_version='fase2-snv-v1',
        )

        service = SomaticMafService(self.project)
        try:
            service._check_preconditions()
        except (SomaticMafGeneRoleEmptyError, SomaticMafVariantEffectResolvedEmptyError) as e:
            self.fail(f'Pré-check falhou inesperadamente: {e}')


# =============================================================================
# 3. Seed SNV — _derive_snv_seeds (NÚCLEO CIENTÍFICO)
# =============================================================================

class SomaticMafDeriveSeedsTests(_SomaticStorageTestCase):
    """
    Testa _derive_snv_seeds diretamente com TSV de ocorrências real em tmpdir.

    Cobre:
      - Truncante em TSG → inactivator (mag=0.9, conf=0.9, effect_source='variant_classification')
      - Truncante em oncogene → neutral
      - Missense com VariantEffectResolved → herda direction/magnitude/effect_source
      - Missense sem VariantEffectResolved → PULA (não gera seed)
      - Gene sem papel → uses GeneRole.UNKNOWN → neutral (para truncante)
      - Dedupe por natural key (mesma ocorrência 2x no TSV → 1 seed)
    """

    def setUp(self):
        super().setUp()
        self.user = _make_user('somatic_seed_user')
        self.project = _make_project(self.user, 'Seed SNV Project')

        # Pré-checks: GeneRole + VariantEffectResolved
        _populate_gene_role({
            'TP53': GeneRole.Role.TSG,
            'PIK3CA': GeneRole.Role.ONCOGENE,
            'BRAF': GeneRole.Role.ONCOGENE,
        })

        # VariantEffectResolved para missense
        self.resolved_braf = _create_resolved_effect(
            variant_key='chr7:140453136:A:T',
            gene_symbol='BRAF',
            direction=VariantEffectResolved.Direction.ACTIVATOR,
            magnitude=0.92,
            confidence=0.9,
            effect_source='clinvar',
            gene_role_used=GeneRole.Role.ONCOGENE,
        )

        # OmicDataset + OmicMatrix somática
        self.dataset = OmicDataset.objects.create(
            accession='CPTAC-CCRCC-SOMATIC',
            source_db='cptac',
            title='CPTAC CCRCC Somatic (Test)',
            access_type='public',
        )
        self.matrix = OmicMatrix.objects.create(
            dataset=self.dataset,
            omics_layer='genomic',
            data_format_level='counts',
            feature_axis='gene',
            loader_version=LOADER_VERSION,
            n_features=200,
            n_samples=3,
            storage_key='omics/_shared/CPTAC-CCRCC-SOMATIC/cptac_ccrcc_somatic.parquet',
            checksum_md5='aabbccddeeff11223344556677889900',
        )
        ProjectDataset.objects.create(
            project=self.project,
            dataset=self.dataset,
            curation_status=ProjectDataset.CurationStatus.INCLUDED,
        )

        # OmicSamples
        self.sample_tp53 = OmicSample.objects.create(
            accession='C3L-00001_tumor', dataset=self.dataset,
            title='C3L-00001 tumor', organism='Homo sapiens',
        )
        self.sample_pik3ca = OmicSample.objects.create(
            accession='C3L-00002_tumor', dataset=self.dataset,
            title='C3L-00002 tumor', organism='Homo sapiens',
        )
        self.sample_braf = OmicSample.objects.create(
            accession='C3L-00003_tumor', dataset=self.dataset,
            title='C3L-00003 tumor', organism='Homo sapiens',
        )

        # OmicMatrixSamples
        OmicMatrixSample.objects.create(
            matrix=self.matrix, sample=self.sample_tp53,
            column_index=0, sample_role=OmicMatrixSample.SampleRole.TUMOR,
        )
        OmicMatrixSample.objects.create(
            matrix=self.matrix, sample=self.sample_pik3ca,
            column_index=1, sample_role=OmicMatrixSample.SampleRole.TUMOR,
        )
        OmicMatrixSample.objects.create(
            matrix=self.matrix, sample=self.sample_braf,
            column_index=2, sample_role=OmicMatrixSample.SampleRole.TUMOR,
        )

    def _write_and_derive(self, rows: list[tuple[str, str, str, str]]) -> tuple[int, dict]:
        """Escreve TSV e chama _derive_snv_seeds."""
        occurrences_path = os.path.join(self._storage_tmpdir, 'test_occurrences.tsv')
        _write_occurrences_tsv(occurrences_path, rows)

        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.SOMATIC_MAF_LOAD,
            status=IngestionJob.JobStatus.RUNNING,
            parameters={},
        )

        service = SomaticMafService(self.project)
        n_seeds, counts = service._derive_snv_seeds(
            occurrences_path=occurrences_path,
            matrix=self.matrix,
            job=job,
        )
        return n_seeds, counts

    def test_truncating_in_tsg_becomes_inactivator(self):
        """
        NÚCLEO: Nonsense_Mutation em TP53 (TSG) → inactivator.
        magnitude=0.9, confidence=0.9, effect_source='variant_classification'.
        """
        n, counts = self._write_and_derive([
            ('C3L-00001_tumor', 'chr17:7673776:C:T', 'TP53', 'Nonsense_Mutation'),
        ])

        self.assertEqual(n, 1)
        self.assertEqual(counts['inactivator'], 1)
        self.assertEqual(counts['truncating_lof'], 1)

        seed = VariantEffectSeed.objects.get(
            matrix=self.matrix, sample=self.sample_tp53,
            gene_symbol='TP53', variant_key='chr17:7673776:C:T',
            evidence_type=VariantEffectSeed.EvidenceType.SNV,
        )
        self.assertEqual(seed.direction, VariantEffectSeed.Direction.INACTIVATOR)
        self.assertAlmostEqual(seed.magnitude, 0.9)
        self.assertAlmostEqual(seed.confidence, 0.9)
        self.assertEqual(seed.effect_source, 'variant_classification')
        self.assertEqual(seed.gene_role, GeneRole.Role.TSG)

    def test_frame_shift_del_in_tsg_is_inactivator(self):
        """Frame_Shift_Del em TSG → inactivator."""
        n, counts = self._write_and_derive([
            ('C3L-00001_tumor', 'chr17:7673800:CA:C', 'TP53', 'Frame_Shift_Del'),
        ])
        self.assertEqual(counts['inactivator'], 1)

    def test_truncating_in_oncogene_is_neutral(self):
        """
        NÚCLEO: Nonsense_Mutation em PIK3CA (oncogene) → neutral.
        Truncar oncogene NÃO é ativação (heurística conservadora v1).
        """
        n, counts = self._write_and_derive([
            ('C3L-00002_tumor', 'chr3:179234297:G:T', 'PIK3CA', 'Nonsense_Mutation'),
        ])

        self.assertEqual(n, 1)
        self.assertEqual(counts['neutral'], 1)
        self.assertEqual(counts['truncating_lof'], 1)
        self.assertEqual(counts['inactivator'], 0)

        seed = VariantEffectSeed.objects.get(
            matrix=self.matrix, sample=self.sample_pik3ca,
            gene_symbol='PIK3CA', evidence_type=VariantEffectSeed.EvidenceType.SNV,
        )
        self.assertEqual(seed.direction, VariantEffectSeed.Direction.NEUTRAL)
        self.assertAlmostEqual(seed.magnitude, 0.9)

    def test_splice_site_in_tsg_is_inactivator(self):
        """Splice_Site em TSG → inactivator."""
        n, counts = self._write_and_derive([
            ('C3L-00001_tumor', 'chr17:7673900:G:A', 'TP53', 'Splice_Site'),
        ])
        self.assertEqual(counts['inactivator'], 1)

    def test_missense_with_resolved_effect_inherits_direction(self):
        """
        NÚCLEO: Missense_Mutation em BRAF com VariantEffectResolved → herda
        direction=activator, magnitude=0.92, effect_source='clinvar'.
        """
        n, counts = self._write_and_derive([
            ('C3L-00003_tumor', 'chr7:140453136:A:T', 'BRAF', 'Missense_Mutation'),
        ])

        self.assertEqual(n, 1)
        self.assertEqual(counts['missense_resolved'], 1)
        self.assertEqual(counts['activator'], 1)

        seed = VariantEffectSeed.objects.get(
            matrix=self.matrix, sample=self.sample_braf,
            gene_symbol='BRAF', variant_key='chr7:140453136:A:T',
        )
        self.assertEqual(seed.direction, VariantEffectSeed.Direction.ACTIVATOR)
        self.assertAlmostEqual(seed.magnitude, 0.92)
        self.assertEqual(seed.effect_source, 'clinvar')

    def test_missense_without_resolved_effect_is_skipped(self):
        """
        NÚCLEO: Missense_Mutation sem VariantEffectResolved → PULA.
        Não gera seed neutral sem sinal claro.
        """
        n, counts = self._write_and_derive([
            ('C3L-00001_tumor', 'chr1:999999:A:C', 'TP53', 'Missense_Mutation'),
        ])

        self.assertEqual(n, 0)
        self.assertEqual(counts['unannotated_skipped'], 1)
        self.assertEqual(counts['missense_resolved'], 0)
        self.assertFalse(
            VariantEffectSeed.objects.filter(
                variant_key='chr1:999999:A:C'
            ).exists()
        )

    def test_in_frame_del_with_resolved_inherits_direction(self):
        """In_Frame_Del com VariantEffectResolved → herda direção."""
        n, counts = self._write_and_derive([
            ('C3L-00003_tumor', 'chr7:140453136:A:T', 'BRAF', 'In_Frame_Del'),
        ])
        self.assertEqual(counts['missense_resolved'], 1)

    def test_synonymous_mutation_is_skipped(self):
        """
        Synonymous_Mutation (classificação não relevante) → PULA.
        Conta em unannotated_skipped.
        """
        n, counts = self._write_and_derive([
            ('C3L-00001_tumor', 'chr17:7673776:C:T', 'TP53', 'Synonymous_Mutation'),
        ])

        self.assertEqual(n, 0)
        self.assertEqual(counts['unannotated_skipped'], 1)

    def test_mixed_occurrences_correct_counts(self):
        """
        TSV com 4 ocorrências de tipos diferentes:
          1. Truncante TSG → inactivator
          2. Truncante oncogene → neutral
          3. Missense com resolved → activator
          4. Missense sem resolved → skip
        Verifica n_seeds=3 e contagens corretas.
        """
        n, counts = self._write_and_derive([
            ('C3L-00001_tumor', 'chr17:7673776:C:T', 'TP53', 'Nonsense_Mutation'),   # 1
            ('C3L-00002_tumor', 'chr3:179234297:G:T', 'PIK3CA', 'Frame_Shift_Ins'),  # 2
            ('C3L-00003_tumor', 'chr7:140453136:A:T', 'BRAF', 'Missense_Mutation'),  # 3
            ('C3L-00001_tumor', 'chr1:999999:A:C', 'TP53', 'Missense_Mutation'),     # 4 — sem resolved
        ])

        self.assertEqual(n, 3)
        self.assertEqual(counts['inactivator'], 1)
        self.assertEqual(counts['neutral'], 1)
        self.assertEqual(counts['activator'], 1)
        self.assertEqual(counts['truncating_lof'], 2)
        self.assertEqual(counts['missense_resolved'], 1)
        self.assertEqual(counts['unannotated_skipped'], 1)

    def test_duplicate_occurrence_in_tsv_deduped(self):
        """
        Mesma ocorrência 2x no TSV → apenas 1 seed criada (dedupe por natural key).
        """
        n, counts = self._write_and_derive([
            ('C3L-00001_tumor', 'chr17:7673776:C:T', 'TP53', 'Nonsense_Mutation'),
            ('C3L-00001_tumor', 'chr17:7673776:C:T', 'TP53', 'Nonsense_Mutation'),
        ])

        self.assertEqual(n, 1)
        count_in_db = VariantEffectSeed.objects.filter(
            matrix=self.matrix,
            sample=self.sample_tp53,
            gene_symbol='TP53',
            variant_key='chr17:7673776:C:T',
            evidence_type=VariantEffectSeed.EvidenceType.SNV,
        ).count()
        self.assertEqual(count_in_db, 1)

    def test_method_version_set_on_seeds(self):
        """Seeds têm method_version=METHOD_VERSION."""
        self._write_and_derive([
            ('C3L-00001_tumor', 'chr17:7673776:C:T', 'TP53', 'Nonsense_Mutation'),
        ])

        seed = VariantEffectSeed.objects.get(
            matrix=self.matrix, sample=self.sample_tp53,
            gene_symbol='TP53',
        )
        self.assertEqual(seed.method_version, METHOD_VERSION)

    def test_evidence_type_is_snv(self):
        """Todas as seeds têm evidence_type=SNV."""
        self._write_and_derive([
            ('C3L-00001_tumor', 'chr17:7673776:C:T', 'TP53', 'Nonsense_Mutation'),
        ])

        seed = VariantEffectSeed.objects.get(
            matrix=self.matrix, sample=self.sample_tp53,
        )
        self.assertEqual(seed.evidence_type, VariantEffectSeed.EvidenceType.SNV)

    def test_empty_occurrences_file_returns_zero_seeds(self):
        """TSV vazio (apenas header) → 0 seeds."""
        occurrences_path = os.path.join(self._storage_tmpdir, 'empty_occ.tsv')
        _write_occurrences_tsv(occurrences_path, [])

        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.SOMATIC_MAF_LOAD,
            status=IngestionJob.JobStatus.RUNNING,
            parameters={},
        )
        service = SomaticMafService(self.project)
        n, counts = service._derive_snv_seeds(
            occurrences_path=occurrences_path,
            matrix=self.matrix,
            job=job,
        )

        self.assertEqual(n, 0)

    def test_missing_occurrences_file_returns_zero_seeds(self):
        """Arquivo de ocorrências inexistente → 0 seeds sem erro."""
        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.SOMATIC_MAF_LOAD,
            status=IngestionJob.JobStatus.RUNNING,
            parameters={},
        )
        service = SomaticMafService(self.project)
        n, counts = service._derive_snv_seeds(
            occurrences_path='/nonexistent/path/occurrences.tsv',
            matrix=self.matrix,
            job=job,
        )

        self.assertEqual(n, 0)

    def test_idempotent_second_seed_derivation(self):
        """
        _derive_snv_seeds chamado 2x com as mesmas ocorrências → mesmo resultado,
        sem duplicatas no DB (update_conflicts).
        """
        rows = [
            ('C3L-00001_tumor', 'chr17:7673776:C:T', 'TP53', 'Nonsense_Mutation'),
        ]

        n1, _ = self._write_and_derive(rows)
        n2, _ = self._write_and_derive(rows)

        self.assertEqual(n1, 1)
        self.assertEqual(n2, 1)  # second run still "creates" 1 (upsert)

        count_in_db = VariantEffectSeed.objects.filter(
            matrix=self.matrix, sample=self.sample_tp53,
            gene_symbol='TP53', evidence_type=VariantEffectSeed.EvidenceType.SNV,
        ).count()
        self.assertEqual(count_in_db, 1, 'Idempotência: não deve duplicar seed')


# =============================================================================
# 4. run() completo — mock GDC + Rust + default_storage
# =============================================================================

class SomaticMafRunTests(_SomaticStorageTestCase):
    """
    Testa SomaticMafService.run() de ponta a ponta:
    - GDC API mockada (requests.post).
    - rust_engine.load_somatic_maf mockado.
    - default_storage com FileSystemStorage em tmpdir.
    - GeneRole + VariantEffectResolved populados via ORM.
    """

    def setUp(self):
        super().setUp()
        self.user = _make_user('somatic_run_user')
        self.project = _make_project(self.user, 'Somatic Run Project')

        _populate_gene_role({
            'TP53': GeneRole.Role.TSG,
            'PIK3CA': GeneRole.Role.ONCOGENE,
        })

        # VariantEffectResolved para missense test
        _create_resolved_effect(
            variant_key='chr7:140453136:A:T',
            gene_symbol='PIK3CA',
            direction=VariantEffectResolved.Direction.ACTIVATOR,
            magnitude=0.9,
            confidence=0.9,
            effect_source='clinvar',
            gene_role_used=GeneRole.Role.ONCOGENE,
        )

    def _run_mocked(self, occurrences_rows: list[tuple] | None = None) -> dict:
        """
        Executa service.run() com mocks completos.
        Cria Parquet dummy e TSV de ocorrências no tmpdir.
        """
        # Criar arquivos no tmpdir
        parquet_path = self._put_parquet_in_tmpdir('cptac_ccrcc_somatic.parquet')

        occurrences_path = os.path.join(
            self._storage_tmpdir, 'test_occurrences_run.tsv'
        )
        rows = occurrences_rows or [
            ('C3L-00001_tumor', 'chr17:7673776:C:T', 'TP53', 'Nonsense_Mutation'),
            ('C3L-00001_tumor', 'chr7:140453136:A:T', 'PIK3CA', 'Missense_Mutation'),
        ]
        _write_occurrences_tsv(occurrences_path, rows)

        fake_manifest = _make_fake_manifest(
            parquet_path=parquet_path,
            occurrences_path=occurrences_path,
            sample_case_ids=['C3L-00001'],
            n_files=3,
            n_occurrences=len(rows),
        )

        gdc_data = _make_gdc_response(['C3L-00001'], ['file-test-0001'])
        gdc_mock_resp = MagicMock()
        gdc_mock_resp.json.return_value = gdc_data
        gdc_mock_resp.raise_for_status.return_value = None

        fake_engine = MagicMock()
        fake_engine.load_somatic_maf.return_value = fake_manifest

        service = SomaticMafService(self.project)

        with patch.dict('sys.modules', {'rust_engine': fake_engine}), \
             patch('requests.post', return_value=gdc_mock_resp):
            result = service.run()

        return result

    def test_run_returns_all_report_keys(self):
        """run() retorna dict com todas as chaves do contrato do relatório."""
        result = self._run_mocked()

        expected_keys = {
            'job_id', 'matrix_id', 'storage_key',
            'n_files_processed', 'n_samples', 'n_occurrences',
            'n_seeds_created', 'n_inactivator', 'n_activator', 'n_neutral',
            'n_truncating_lof', 'n_missense_resolved', 'n_unannotated_skipped',
            'n_variants_skipped_synonymous', 'n_variants_skipped_offlist',
            'gdc_files_found', 'gdc_cases_found', 'errors',
        }
        self.assertEqual(set(result.keys()), expected_keys)

    def test_run_creates_omic_matrix_genomic(self):
        """
        run() cria OmicMatrix com omics_layer='genomic', data_format_level='counts',
        feature_axis='gene', loader_version=LOADER_VERSION.
        """
        result = self._run_mocked()

        matrix = OmicMatrix.objects.get(id=result['matrix_id'])
        self.assertEqual(matrix.omics_layer, 'genomic')
        self.assertEqual(matrix.data_format_level, OmicMatrix.DataFormatLevel.COUNTS)
        self.assertEqual(matrix.feature_axis, OmicMatrix.FeatureAxis.GENE)
        self.assertEqual(matrix.loader_version, LOADER_VERSION)

    def test_run_creates_omic_dataset_somatic(self):
        """run() cria OmicDataset com accession=SOMATIC_ACCESSION, source_db='cptac'."""
        self._run_mocked()

        dataset = OmicDataset.objects.get(accession=SOMATIC_ACCESSION)
        self.assertEqual(dataset.source_db, OmicDataset.SourceDB.CPTAC)
        self.assertEqual(dataset.access_type, OmicDataset.AccessType.PUBLIC)

    def test_run_creates_omic_sample_with_tumor_accession(self):
        """run() cria OmicSample com accession='C3L-00001_tumor'."""
        self._run_mocked()

        self.assertTrue(
            OmicSample.objects.filter(accession='C3L-00001_tumor').exists()
        )

    def test_run_creates_omic_matrix_sample(self):
        """run() cria OmicMatrixSample vinculando amostra à matriz."""
        result = self._run_mocked()

        matrix = OmicMatrix.objects.get(id=result['matrix_id'])
        oms = OmicMatrixSample.objects.filter(matrix=matrix).first()
        self.assertIsNotNone(oms)

    def test_run_job_completed_after_success(self):
        """run() cria IngestionJob e o marca COMPLETED."""
        result = self._run_mocked()

        job = IngestionJob.objects.get(id=result['job_id'])
        self.assertEqual(job.status, IngestionJob.JobStatus.COMPLETED)
        self.assertEqual(job.job_type, IngestionJob.JobType.SOMATIC_MAF_LOAD)

    def test_run_result_does_not_leak_db_url(self):
        """Resultado de run() NUNCA contém postgresql://, db_url, password."""
        result = self._run_mocked()
        result_str = str(result)
        self.assertNotIn('postgresql://', result_str)
        self.assertNotIn('db_url', result_str)
        self.assertNotIn('password', result_str)

    def test_run_storage_key_uses_shared_namespace(self):
        """storage_key tem namespace _shared (dado público GDC OPEN)."""
        result = self._run_mocked()
        self.assertIn('_shared', result['storage_key'])

    def test_run_storage_key_no_absolute_path(self):
        """storage_key não contém caminho físico absoluto."""
        result = self._run_mocked()
        for prefix in ('/tmp', '/var', '/home', '/Users', '/private'):
            self.assertNotIn(prefix, result['storage_key'],
                             f'storage_key vaza caminho físico: {result["storage_key"]}')

    def test_run_rust_called_with_correct_args(self):
        """load_somatic_maf chamado com file_map, dest_dir, db_url, gene_allowlist, parquet_name."""
        parquet_path = self._put_parquet_in_tmpdir()
        occurrences_path = os.path.join(self._storage_tmpdir, 'occ.tsv')
        _write_occurrences_tsv(occurrences_path, [])

        fake_manifest = _make_fake_manifest(
            parquet_path=parquet_path,
            occurrences_path=occurrences_path,
            sample_case_ids=[],
            n_occurrences=0,
        )
        fake_engine = MagicMock()
        fake_engine.load_somatic_maf.return_value = fake_manifest

        gdc_resp = MagicMock()
        gdc_resp.json.return_value = _make_gdc_response(['C3L-00001'])
        gdc_resp.raise_for_status.return_value = None

        service = SomaticMafService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}), \
             patch('requests.post', return_value=gdc_resp):
            service.run()

        call_kwargs = fake_engine.load_somatic_maf.call_args[1]
        self.assertIn('file_map', call_kwargs)
        self.assertIn('dest_dir', call_kwargs)
        self.assertIn('db_url', call_kwargs)
        self.assertIn('gene_allowlist', call_kwargs)
        self.assertIn('parquet_name', call_kwargs)

    def test_run_already_loaded_raises(self):
        """
        Segundo run() com OmicMatrix já existente → SomaticMafAlreadyLoadedError.
        """
        self._run_mocked()

        with self.assertRaises(SomaticMafAlreadyLoadedError):
            self._run_mocked()

    def test_run_seed_counts_in_report(self):
        """Relatório contém contagens de seeds corretas para as ocorrências."""
        result = self._run_mocked()
        # 1 truncante TSG → inactivator
        # 1 missense com resolved → activator
        self.assertEqual(result['n_inactivator'], 1)
        self.assertEqual(result['n_activator'], 1)
        self.assertEqual(result['n_seeds_created'], 2)


# =============================================================================
# 5. Idempotência — OmicMatrix e Job gate
# =============================================================================

class SomaticMafIdempotencyTests(TestCase):
    """Testa idempotência de OmicMatrix e gate de job ativo."""

    def setUp(self):
        self.user = _make_user('somatic_idem_user')
        self.project = _make_project(self.user, 'Somatic Idempotency Project')
        _populate_gene_role({'TP53': GeneRole.Role.TSG})
        VariantEffectResolved.objects.create(
            variant_key='chr1:1:A:T', gene_symbol='TP53',
            magnitude=1.0, effect_class='damaging',
            direction='inactivator', confidence=0.9,
            effect_source='clinvar', gene_role_used='tsg',
            resolution_version='fase2-snv-v1',
        )

    def _get_or_create_dataset(self) -> OmicDataset:
        service = SomaticMafService(self.project)
        return service._get_or_create_dataset()

    def test_already_loaded_matrix_blocks_run(self):
        """OmicMatrix com mesma natural key já existe → SomaticMafAlreadyLoadedError."""
        dataset = self._get_or_create_dataset()
        OmicMatrix.objects.create(
            dataset=dataset,
            omics_layer='genomic',
            data_format_level='counts',
            feature_axis='gene',
            loader_version=LOADER_VERSION,
            n_features=100,
            n_samples=5,
            storage_key='omics/_shared/CPTAC-CCRCC-SOMATIC/test.parquet',
        )

        service = SomaticMafService(self.project)
        with self.assertRaises(SomaticMafAlreadyLoadedError):
            service._check_idempotency(dataset)

    def test_active_job_blocks(self):
        """Job SOMATIC_MAF_LOAD PENDING ativo → SomaticMafJobActiveError."""
        dataset = self._get_or_create_dataset()
        IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.SOMATIC_MAF_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={'dataset_id': dataset.id},
        )

        service = SomaticMafService(self.project)
        with self.assertRaises(SomaticMafJobActiveError):
            service._check_idempotency(dataset)

    def test_completed_job_does_not_block_idempotency_check(self):
        """
        Job COMPLETED não bloqueia _check_idempotency (UPSERT do Rust é idempotente).
        O bloqueio é na matriz (SomaticMafAlreadyLoadedError), não no job completado.
        """
        dataset = self._get_or_create_dataset()
        IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.SOMATIC_MAF_LOAD,
            status=IngestionJob.JobStatus.COMPLETED,
            parameters={'dataset_id': dataset.id},
        )

        service = SomaticMafService(self.project)
        # Sem matriz existente → deve passar (sem SomaticMafJobActiveError)
        try:
            service._check_idempotency(dataset)
        except SomaticMafJobActiveError as e:
            self.fail(f'Job COMPLETED não deve bloquear: {e}')

    def test_get_or_create_dataset_creates_project_dataset_link(self):
        """_get_or_create_dataset cria ProjectDataset vinculando projeto ao dataset."""
        service = SomaticMafService(self.project)
        dataset = service._get_or_create_dataset()

        self.assertTrue(
            ProjectDataset.objects.filter(
                project=self.project, dataset=dataset
            ).exists()
        )

    def test_get_or_create_dataset_second_call_same_dataset(self):
        """_get_or_create_dataset 2x retorna o mesmo OmicDataset (get_or_create)."""
        service = SomaticMafService(self.project)
        d1 = service._get_or_create_dataset()
        d2 = service._get_or_create_dataset()
        self.assertEqual(d1.id, d2.id)


# =============================================================================
# 6. dispatch()
# =============================================================================

class SomaticMafDispatchTests(TestCase):
    """Testa SomaticMafService.dispatch()."""

    def setUp(self):
        self.user = _make_user('somatic_disp_user')
        self.project = _make_project(self.user, 'Somatic Dispatch Project')
        _populate_gene_role({'TP53': GeneRole.Role.TSG})
        VariantEffectResolved.objects.create(
            variant_key='chr1:1:A:T', gene_symbol='TP53',
            magnitude=1.0, effect_class='damaging',
            direction='inactivator', confidence=0.9,
            effect_source='clinvar', gene_role_used='tsg',
            resolution_version='fase2-snv-v1',
        )

    @patch('apps.core.tasks.ingestion_tasks.run_somatic_maf_load.delay', return_value=None)
    def test_dispatch_creates_pending_job(self, mock_delay):
        """dispatch() cria job PENDING com job_type SOMATIC_MAF_LOAD."""
        job = SomaticMafService.dispatch(self.project)
        self.assertEqual(job.status, IngestionJob.JobStatus.PENDING)
        self.assertEqual(job.job_type, IngestionJob.JobType.SOMATIC_MAF_LOAD)

    @patch('apps.core.tasks.ingestion_tasks.run_somatic_maf_load.delay', return_value=None)
    def test_dispatch_parameters_no_db_url(self, mock_delay):
        """IngestionJob.parameters NUNCA contém db_url."""
        job = SomaticMafService.dispatch(self.project)
        params = job.parameters or {}
        self.assertNotIn('db_url', params)
        self.assertNotIn('postgresql://', str(params))

    @patch('apps.core.tasks.ingestion_tasks.run_somatic_maf_load.delay', return_value=None)
    def test_dispatch_calls_celery_delay(self, mock_delay):
        """dispatch() chama run_somatic_maf_load.delay com (job_id, project_id)."""
        job = SomaticMafService.dispatch(self.project)
        mock_delay.assert_called_once_with(str(job.id), str(self.project.id))

    def test_dispatch_gene_role_empty_raises(self):
        """GeneRole vazio → SomaticMafGeneRoleEmptyError sem criar job."""
        GeneRole.objects.all().delete()

        with self.assertRaises(SomaticMafGeneRoleEmptyError):
            with patch('apps.core.tasks.ingestion_tasks.run_somatic_maf_load.delay'):
                SomaticMafService.dispatch(self.project)

        self.assertEqual(
            IngestionJob.objects.filter(
                project=self.project,
                job_type=IngestionJob.JobType.SOMATIC_MAF_LOAD,
            ).count(),
            0,
        )

    def test_dispatch_resolved_empty_raises(self):
        """VariantEffectResolved vazio → SomaticMafVariantEffectResolvedEmptyError."""
        VariantEffectResolved.objects.all().delete()

        with self.assertRaises(SomaticMafVariantEffectResolvedEmptyError):
            with patch('apps.core.tasks.ingestion_tasks.run_somatic_maf_load.delay'):
                SomaticMafService.dispatch(self.project)


# =============================================================================
# 7. Management command load_somatic_maf
# =============================================================================

class LoadSomaticMafCommandTests(_SomaticStorageTestCase):
    """Testa o management command load_somatic_maf."""

    def setUp(self):
        super().setUp()
        self.user = _make_user('somatic_cmd_user')
        self.project = _make_project(self.user, 'Somatic Cmd Project')

        _populate_gene_role({
            'TP53': GeneRole.Role.TSG,
            'PIK3CA': GeneRole.Role.ONCOGENE,
        })
        VariantEffectResolved.objects.create(
            variant_key='chr1:1:A:T', gene_symbol='TP53',
            magnitude=1.0, effect_class='damaging',
            direction='inactivator', confidence=0.9,
            effect_source='clinvar', gene_role_used='tsg',
            resolution_version='fase2-snv-v1',
        )

    def _call_sync(self, project_id: str | None = None) -> tuple[str, str]:
        from django.core.management import call_command

        pid = project_id or str(self.project.id)
        stdout = StringIO()
        stderr = StringIO()

        parquet_path = self._put_parquet_in_tmpdir()
        occurrences_path = os.path.join(self._storage_tmpdir, 'cmd_occ.tsv')
        _write_occurrences_tsv(occurrences_path, [
            ('C3L-00001_tumor', 'chr17:7673776:C:T', 'TP53', 'Nonsense_Mutation'),
        ])

        fake_manifest = _make_fake_manifest(
            parquet_path=parquet_path,
            occurrences_path=occurrences_path,
            sample_case_ids=['C3L-00001'],
            n_occurrences=1,
        )
        fake_engine = MagicMock()
        fake_engine.load_somatic_maf.return_value = fake_manifest

        gdc_resp = MagicMock()
        gdc_resp.json.return_value = _make_gdc_response(['C3L-00001'])
        gdc_resp.raise_for_status.return_value = None

        with patch.dict('sys.modules', {'rust_engine': fake_engine}), \
             patch('requests.post', return_value=gdc_resp):
            call_command('load_somatic_maf',
                         project=pid, stdout=stdout, stderr=stderr)

        return stdout.getvalue(), stderr.getvalue()

    def test_command_sync_reports_counters(self):
        """load_somatic_maf (síncrono) reporta contadores esperados."""
        stdout_val, _ = self._call_sync()
        for field in ('n_seeds_created', 'n_samples', 'matrix_id', 'storage_key'):
            self.assertIn(field, stdout_val.lower(),
                          f'stdout deve conter "{field}": {stdout_val[:500]!r}')

    def test_command_sync_does_not_leak_db_url(self):
        """stdout/stderr não vaza postgresql://, db_url, password."""
        stdout_val, stderr_val = self._call_sync()
        combined = stdout_val + stderr_val
        for token in ('postgresql://', 'db_url', 'password'):
            self.assertNotIn(token, combined,
                             f'Token sensível "{token}" vazou na saída')

    def test_command_invalid_project_raises_command_error(self):
        """--project com UUID inexistente → CommandError."""
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command('load_somatic_maf',
                         project=str(uuid.uuid4()),
                         stdout=StringIO())

    def test_command_gene_role_empty_raises_command_error(self):
        """GeneRole vazio → CommandError."""
        from django.core.management import call_command
        from django.core.management.base import CommandError
        GeneRole.objects.all().delete()

        with self.assertRaises(CommandError):
            call_command('load_somatic_maf',
                         project=str(self.project.id),
                         stdout=StringIO())

    def test_command_resolved_empty_raises_command_error(self):
        """VariantEffectResolved vazio → CommandError."""
        from django.core.management import call_command
        from django.core.management.base import CommandError
        VariantEffectResolved.objects.all().delete()

        with self.assertRaises(CommandError):
            call_command('load_somatic_maf',
                         project=str(self.project.id),
                         stdout=StringIO())

    def test_command_async_creates_pending_job(self):
        """load_somatic_maf --async cria job PENDING sem executar a carga."""
        from django.core.management import call_command
        stdout = StringIO()

        with patch('apps.core.tasks.ingestion_tasks.run_somatic_maf_load.delay',
                   return_value=None):
            call_command('load_somatic_maf',
                         project=str(self.project.id),
                         async_mode=True,
                         stdout=stdout)

        job = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.SOMATIC_MAF_LOAD,
            status=IngestionJob.JobStatus.PENDING,
        ).first()
        self.assertIsNotNone(job, 'Modo --async deve criar job PENDING')

    def test_command_already_loaded_reports_warning(self):
        """OmicMatrix já carregada → aviso de idempotência sem erro."""
        from django.core.management import call_command

        # Primeiro run (cria a matriz)
        self._call_sync()

        # Segundo run (deve avisar, não falhar)
        stdout = StringIO()
        gdc_resp = MagicMock()
        gdc_resp.json.return_value = _make_gdc_response(['C3L-00001'])
        gdc_resp.raise_for_status.return_value = None

        with patch('requests.post', return_value=gdc_resp), \
             patch.dict('sys.modules', {'rust_engine': MagicMock()}):
            call_command('load_somatic_maf',
                         project=str(self.project.id),
                         stdout=stdout)

        output = stdout.getvalue().lower()
        self.assertTrue(
            any(kw in output for kw in ['idempot', 'already', 'já existe']),
            f'stdout deve mencionar idempotência: {output[:300]!r}',
        )


# =============================================================================
# 8. Isolamento na task run_somatic_maf_load
# =============================================================================

class SomaticMafTaskIsolationTests(TestCase):
    """Testa isolamento cross-project na task run_somatic_maf_load."""

    def setUp(self):
        self.user_a = _make_user('somatic_iso_a')
        self.user_b = _make_user('somatic_iso_b')
        self.project_a = _make_project(self.user_a, 'Somatic Iso A')
        self.project_b = _make_project(self.user_b, 'Somatic Iso B')

    def test_task_aborts_cross_project(self):
        """Job de A com project_id de B → job FAILED."""
        from apps.core.tasks.ingestion_tasks import run_somatic_maf_load

        job_a = IngestionJob.objects.create(
            project=self.project_a,
            job_type=IngestionJob.JobType.SOMATIC_MAF_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={},
        )

        with patch.dict('sys.modules', {'rust_engine': MagicMock()}), \
             patch('apps.core.services.somatic_maf_service.SomaticMafService') as MockSvc:
            run_somatic_maf_load.run(str(job_a.id), str(self.project_b.id))

        job_a.refresh_from_db()
        self.assertEqual(job_a.status, IngestionJob.JobStatus.FAILED)
        MockSvc.assert_not_called()

    def test_task_returns_error_for_nonexistent_project(self):
        """run_somatic_maf_load com project_id inexistente retorna erro."""
        from apps.core.tasks.ingestion_tasks import run_somatic_maf_load

        with patch.dict('sys.modules', {'rust_engine': MagicMock()}):
            result = run_somatic_maf_load.run(
                str(uuid.uuid4()), str(uuid.uuid4())
            )

        self.assertIn('project not found', result.get('errors', []))

    def test_task_returns_error_for_nonexistent_job(self):
        """run_somatic_maf_load com job_id inexistente retorna erro."""
        from apps.core.tasks.ingestion_tasks import run_somatic_maf_load

        with patch.dict('sys.modules', {'rust_engine': MagicMock()}):
            result = run_somatic_maf_load.run(
                str(uuid.uuid4()), str(self.project_a.id)
            )

        self.assertIn('job not found', result.get('errors', []))

    def test_cross_project_seeds_not_visible(self):
        """
        Regressão de isolamento: seeds derivadas para projeto A não devem ser
        acessíveis via projeto B (via OmicMatrix/OmicMatrixSample).

        Verificação: OmicMatrix criada para A não pertence ao ProjectDataset de B.
        """
        _populate_gene_role({'TP53': GeneRole.Role.TSG})
        VariantEffectResolved.objects.create(
            variant_key='chr1:1:A:T', gene_symbol='TP53',
            magnitude=1.0, effect_class='damaging',
            direction='inactivator', confidence=0.9,
            effect_source='clinvar', gene_role_used='tsg',
            resolution_version='fase2-snv-v1',
        )

        service_a = SomaticMafService(self.project_a)
        dataset_a = service_a._get_or_create_dataset()

        # Projeto B não tem ProjectDataset para o dataset somático
        # (a menos que tenha feito seu próprio get_or_create)
        linked_to_b = ProjectDataset.objects.filter(
            project=self.project_b, dataset=dataset_a
        ).exists()
        # Inicialmente não vinculado
        self.assertFalse(linked_to_b)
