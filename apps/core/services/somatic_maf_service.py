"""
SomaticMafService — orquestração Django da carga do MAF somático GDC + seed SNV.

OmnisPathway Objetivo 2, Fase 2, Passo 2.6 (Slice 2D).

Fluxo único (matriz + seed na mesma execução):
  1. Pré-checks: GeneRole(source='oncokb') populado + VariantEffectResolved
     populado (senão erro claro com instrução ao operador).
  2. Resolver file_map via GDC API pública (sem credencial):
       POST https://api.gdc.cancer.gov/files
       filtro: project=CPTAC-3, data_type='Masked Somatic Mutation',
               access=open, primary_site=Kidney
       Monta file_map = [{file_id, sample_accession: f"{submitter_id}_tumor"}]
  3. Criar IngestionJob(job_type=SOMATIC_MAF_LOAD).
  4. Chamar rust_engine.load_somatic_maf(file_map, dest_dir, db_url,
     gene_allowlist, parquet_name) → SomaticMafManifest{parquet_path,
     checksum_md5, n_features, n_samples, sample_columns, occurrences_path,
     n_files_processed, n_occurrences, n_variants_skipped_synonymous,
     n_variants_skipped_offlist, errors}.
     O Rust NÃO toca PG além de receber db_url para OmicMatrixSample.
  5. Upload do Parquet burden via shared_omics_storage_key (dado OPEN).
  6. Criar OmicDataset + OmicMatrix + OmicSample (get_or_create) +
     OmicMatrixSample em transação atômica. Idempotente.
  7. SEED SNV (no mesmo fluxo, lendo occurrences_path antes do cleanup):
     Lê o TSV de ocorrências linha a linha (arquivo LOCAL pequeno — não Parquet).
     Para cada ocorrência (sample_accession, variant_key, gene_symbol,
     variant_classification):
       a. Resolve sample_id via OmicMatrixSample da matriz somática.
       b. Aplica regra de sinal SNV v1:
          - TRUNCANTE/LoF claro (Frame_Shift_Del, Frame_Shift_Ins,
            Nonsense_Mutation, Splice_Site, Translation_Start_Site,
            Nonstop_Mutation): LoF estrutural. direction=inactivator se
            gene_role=tsg; neutral caso contrário (truncar oncogene ≠ ativar).
            magnitude=0.9, confidence=0.9, effect_source='variant_classification'.
            NÃO busca VariantEffectResolved (truncante não depende de ClinVar/AM).
          - MISSENSE/In-frame (Missense_Mutation, In_Frame_Del, In_Frame_Ins):
            busca VariantEffectResolved(variant_key, gene_symbol,
            resolution_version mais recente). Se achado: usa direction/magnitude/
            confidence/effect_source/gene_role_used dele.
            Se NÃO achado: PULA (sem seed) — missense sem anotação não gera
            seed para não poluir com neutros sem sinal.
       c. Materializa VariantEffectSeed set-based: bulk_create(
          update_conflicts=True) na natural key.
  8. Cleanup do tempdir (automático via TemporaryDirectory).
  9. Marca job COMPLETED com o manifesto.

Justificativa do fluxo único (divergência do plano original de 2 commands):
  O TSV de ocorrências é arquivo LOCAL EFÊMERO produzido pelo Rust no mesmo
  tempdir do Parquet. Ler o TSV para derivar as seeds ANTES do cleanup evita
  a necessidade de persistir um artefato intermediário (seja como tabela PG
  ou como arquivo em object storage). O VariantEffectSeed JÁ É a materialização
  persistente da ocorrência-que-vira-seed. Isso elimina uma entidade extra sem
  consumidor além do próprio seeding (Regra #-1).

Fronteira de camadas (Regra #1 / django-rust-boundary):
  - Rust: fetch HTTP em lote dos 353 arquivos MAF do GDC, parse streaming,
    escrita do Parquet gene×amostra (burden) + TSV de ocorrências local.
    NÃO toca PG, NÃO faz upload.
  - Django: resolve file_map via GDC API (HTTP leve, 1 POST), upload Parquet,
    ORM set-based, leitura do TSV pequeno de ocorrências, seed set-based.
    NÃO abre o Parquet, NÃO parseia MAF.

Regra do sinal SNV v1 (truncante vs missense):
  TRUNCANTES/LoF claro → LoF estrutural direto (não precisa de ClinVar/AM):
    frame_shift_del, frame_shift_ins, nonsense_mutation, splice_site,
    translation_start_site, nonstop_mutation
    → direction=inactivator se gene_role=tsg; neutral se oncogene/outros.

  MISSENSE/In-frame → busca VariantEffectResolved:
    missense_mutation, in_frame_del, in_frame_ins
    → se encontrado: usa direction/magnitude/confidence/effect_source do catálogo.
    → se não encontrado: PULA (não gera seed neutral sem sinal claro).

Isolamento (firebase-auth-guard / Regra #3):
  - project deve pertencer ao request.user antes de chegar aqui.
  - OmicDataset é tabela compartilhada; isolamento via ProjectDataset.
  - VariantEffectSeed é catálogo derivado (global, ancorado em matrix/sample).

Sensitive-data-handling:
  - db_url NUNCA logada nem gravada em IngestionJob.parameters.
  - GDC API é pública (sem credencial, sem token) — não há dado sensível.
  - storage_key é chave LÓGICA — nunca caminho físico absoluto nos logs.
  - GDC API URL e filtros são registrados em parameters para auditoria.
"""

from __future__ import annotations

import csv
import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import requests
from django.conf import settings
from django.core.files import File
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

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
from apps.core.storage_utils import shared_omics_storage_key

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ─── Constantes do dataset ────────────────────────────────────────────────────

SOMATIC_ACCESSION = 'CPTAC-CCRCC-SOMATIC'
SOMATIC_PARQUET_NAME = 'cptac_ccrcc_somatic.parquet'
LOADER_VERSION = 'maf-v1'
METHOD_VERSION = 'fase2-snv-v1'

# GDC API pública (sem credencial, sem token)
GDC_FILES_URL = 'https://api.gdc.cancer.gov/files'
GDC_PROJECT = 'CPTAC-3'
GDC_DATA_TYPE = 'Masked Somatic Mutation'
GDC_ACCESS = 'open'
GDC_PRIMARY_SITE = 'Kidney'
GDC_MAX_FILES = 500  # margem sobre os 353 esperados

# Timeout para a chamada GDC (em segundos)
GDC_REQUEST_TIMEOUT = 60

# Variant classifications consideradas truncantes/LoF estrutural
_TRUNCATING_CLASSIFICATIONS = frozenset({
    'frame_shift_del',
    'frame_shift_ins',
    'nonsense_mutation',
    'splice_site',
    'translation_start_site',
    'nonstop_mutation',
})

# Variant classifications com anotação via VariantEffectResolved
_MISSENSE_CLASSIFICATIONS = frozenset({
    'missense_mutation',
    'in_frame_del',
    'in_frame_ins',
})

# Magnitude/confiança fixas para truncantes (LoF estrutural — não precisa de ClinVar/AM)
_TRUNCATING_MAGNITUDE = 0.9
_TRUNCATING_CONFIDENCE = 0.9


# ─── Erros específicos do serviço ────────────────────────────────────────────

class SomaticMafGeneRoleEmptyError(Exception):
    """GeneRole(source='oncokb') vazio — pré-check falhou."""
    def __init__(self):
        super().__init__(
            "GeneRole (source='oncokb') está vazio. "
            "Execute load_gene_roles antes de carregar o MAF somático. "
            "O serviço precisa do papel dos genes (oncogene/tsg) para "
            "resolver a direção das variantes truncantes."
        )


class SomaticMafVariantEffectResolvedEmptyError(Exception):
    """VariantEffectResolved vazio — pré-check falhou."""
    def __init__(self):
        super().__init__(
            "VariantEffectResolved está vazio. "
            "Execute resolve_variant_effects antes de carregar o MAF somático. "
            "As variantes missense requerem o catálogo de efeitos resolvidos "
            "(ClinVar>AlphaMissense>dbNSFP) para obter direção e magnitude."
        )


class SomaticMafAlreadyLoadedError(Exception):
    """OmicMatrix somática já existe para a natural key (idempotência)."""
    def __init__(self, matrix: OmicMatrix):
        self.matrix = matrix
        super().__init__(
            f"OmicMatrix somática já existe: dataset={matrix.dataset.accession}, "
            f"layer={matrix.omics_layer}, axis={matrix.feature_axis}, "
            f"loader_version={matrix.loader_version} (id={matrix.id})"
        )


class SomaticMafJobActiveError(Exception):
    """Já há um SOMATIC_MAF_LOAD pending/running para este projeto+dataset."""
    def __init__(self, job: IngestionJob):
        self.job = job
        super().__init__(
            f"SOMATIC_MAF_LOAD já ativo (job {job.id}, status={job.status}) "
            f"para projeto {job.project_id} — dispatch ignorado (idempotência)"
        )


class GdcApiError(Exception):
    """Erro ao consultar a GDC API."""


# ─── Dataclass do relatório ───────────────────────────────────────────────────

@dataclass
class SomaticMafReport:
    """Relatório produzido pelo SomaticMafService."""
    job_id: str = ''
    matrix_id: int = 0
    storage_key: str = ''
    n_files_processed: int = 0
    n_samples: int = 0
    n_occurrences: int = 0
    n_seeds_created: int = 0
    n_inactivator: int = 0
    n_activator: int = 0
    n_neutral: int = 0
    n_truncating_lof: int = 0
    n_missense_resolved: int = 0
    n_unannotated_skipped: int = 0
    n_variants_skipped_synonymous: int = 0
    n_variants_skipped_offlist: int = 0
    gdc_files_found: int = 0
    gdc_cases_found: int = 0
    errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'job_id': self.job_id,
            'matrix_id': self.matrix_id,
            'storage_key': self.storage_key,
            'n_files_processed': self.n_files_processed,
            'n_samples': self.n_samples,
            'n_occurrences': self.n_occurrences,
            'n_seeds_created': self.n_seeds_created,
            'n_inactivator': self.n_inactivator,
            'n_activator': self.n_activator,
            'n_neutral': self.n_neutral,
            'n_truncating_lof': self.n_truncating_lof,
            'n_missense_resolved': self.n_missense_resolved,
            'n_unannotated_skipped': self.n_unannotated_skipped,
            'n_variants_skipped_synonymous': self.n_variants_skipped_synonymous,
            'n_variants_skipped_offlist': self.n_variants_skipped_offlist,
            'gdc_files_found': self.gdc_files_found,
            'gdc_cases_found': self.gdc_cases_found,
            'errors': self.errors,
        }


# ─── Service ─────────────────────────────────────────────────────────────────

class SomaticMafService:
    """
    Serviço central de carga do MAF somático GDC + derivação de seed SNV.

    Fluxo único: resolve GDC file_map → Rust (Parquet + ocorrências TSV) →
    upload Parquet → ORM set-based (OmicMatrix/OmicSample/OmicMatrixSample) →
    seed SNV set-based (lê TSV de ocorrências × VariantEffectResolved) →
    VariantEffectSeed.

    Uso síncrono (management command / bancada de prova):
        service = SomaticMafService(project)
        report = service.run()

    Uso assíncrono (Celery):
        job = SomaticMafService.dispatch(project)
        # task run_somatic_maf_load(job.id, str(project.id)) prossegue em background
    """

    OMICS_LAYER = 'genomic'
    DATA_FORMAT_LEVEL = OmicMatrix.DataFormatLevel.COUNTS
    FEATURE_AXIS = OmicMatrix.FeatureAxis.GENE

    def __init__(self, project: DaVinciProject):
        self.project = project
        self.user = project.user

    # ─── API pública ─────────────────────────────────────────────────────────

    @classmethod
    def dispatch(cls, project: DaVinciProject) -> IngestionJob:
        """
        Gate de idempotência + criação do IngestionJob + enfileiramento Celery.

        Raises:
            SomaticMafGeneRoleEmptyError: GeneRole vazio.
            SomaticMafVariantEffectResolvedEmptyError: VariantEffectResolved vazio.
            SomaticMafAlreadyLoadedError: matriz já carregada.
            SomaticMafJobActiveError: job ativo encontrado.
        """
        from apps.core.tasks.ingestion_tasks import run_somatic_maf_load

        service = cls(project)
        service._check_preconditions()
        dataset = service._get_or_create_dataset()
        service._check_idempotency(dataset)

        job = IngestionJob.objects.create(
            project=project,
            job_type=IngestionJob.JobType.SOMATIC_MAF_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={
                'dataset_accession': dataset.accession,
                'dataset_id': dataset.id,
                'omics_layer': cls.OMICS_LAYER,
                'feature_axis': cls.FEATURE_AXIS,
                'loader_version': LOADER_VERSION,
                'method_version': METHOD_VERSION,
                'gdc_project': GDC_PROJECT,
                'gdc_data_type': GDC_DATA_TYPE,
                'gdc_access': GDC_ACCESS,
                'gdc_primary_site': GDC_PRIMARY_SITE,
                # db_url NÃO é armazenada (sensitive-data-handling)
            },
        )

        try:
            run_somatic_maf_load.delay(str(job.id), str(project.id))
            logger.info(
                'SOMATIC_MAF_LOAD disparado (job %s) para projeto %s / dataset %s',
                job.id,
                project.id,
                dataset.accession,
            )
        except Exception as exc:
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=f'Failed to dispatch Celery task: {exc}',
            )
            raise

        return job

    def run(self, job: IngestionJob | None = None) -> dict:
        """
        Executa a carga de forma síncrona (management command / bancada de prova).

        Fluxo:
          1. Pré-checks (GeneRole + VariantEffectResolved populados).
          2. Resolve/cria OmicDataset somático.
          3. Gate de idempotência.
          4. Cria/atualiza IngestionJob.
          5. Resolve file_map via GDC API.
          6. Chama Rust (load_somatic_maf).
          7. Upload do Parquet.
          8. Persiste ORM set-based (OmicMatrix + OmicSample + OmicMatrixSample).
          9. Deriva seeds SNV set-based (lê TSV de ocorrências).
         10. Cleanup do tmpdir. Marca job COMPLETED.

        Args:
            job: IngestionJob existente (criado pelo dispatch); se None,
                 cria internamente (modo síncrono direto do management command).

        Returns:
            dict com o relatório completo (ver SomaticMafReport.to_dict()).
        """
        import rust_engine

        self._check_preconditions()
        dataset = self._get_or_create_dataset()

        if job is None:
            self._check_idempotency(dataset)
            job = IngestionJob.objects.create(
                project=self.project,
                job_type=IngestionJob.JobType.SOMATIC_MAF_LOAD,
                status=IngestionJob.JobStatus.RUNNING,
                parameters={
                    'dataset_accession': dataset.accession,
                    'dataset_id': dataset.id,
                    'omics_layer': self.OMICS_LAYER,
                    'feature_axis': self.FEATURE_AXIS,
                    'loader_version': LOADER_VERSION,
                    'method_version': METHOD_VERSION,
                    'gdc_project': GDC_PROJECT,
                    'gdc_data_type': GDC_DATA_TYPE,
                    'gdc_access': GDC_ACCESS,
                    'gdc_primary_site': GDC_PRIMARY_SITE,
                },
            )
        else:
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.RUNNING,
                started_at=timezone.now(),
            )

        try:
            report = self._execute(dataset, job, rust_engine)
            return report.to_dict()
        except Exception as exc:
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=str(exc),
                completed_at=timezone.now(),
            )
            raise

    # ─── Pré-checks ──────────────────────────────────────────────────────────

    def _check_preconditions(self) -> None:
        """
        Verifica pré-condições obrigatórias antes de qualquer operação.

        Raises:
            SomaticMafGeneRoleEmptyError: GeneRole(source='oncokb') vazio.
            SomaticMafVariantEffectResolvedEmptyError: VariantEffectResolved vazio.
        """
        if not GeneRole.objects.filter(source='oncokb').exists():
            raise SomaticMafGeneRoleEmptyError()

        if not VariantEffectResolved.objects.exists():
            raise SomaticMafVariantEffectResolvedEmptyError()

        n_roles = GeneRole.objects.filter(source='oncokb').count()
        n_resolved = VariantEffectResolved.objects.count()
        logger.info(
            'SomaticMafService: pré-checks OK — %d gene roles, %d efeitos resolvidos',
            n_roles,
            n_resolved,
        )

    # ─── Dataset ─────────────────────────────────────────────────────────────

    def _get_or_create_dataset(self) -> OmicDataset:
        """
        Resolve ou cria o OmicDataset somático CPTAC-CCRCC.

        access_type='public': dado GDC OPEN → namespace compartilhado via
        shared_omics_storage_key. OmicDataset é tabela compartilhada;
        isolamento por projeto via ProjectDataset.
        """
        dataset, created = OmicDataset.objects.get_or_create(
            accession=SOMATIC_ACCESSION,
            defaults={
                'source_db': OmicDataset.SourceDB.CPTAC,
                'title': 'CPTAC CCRCC Discovery Study — Masked Somatic MAF (GDC OPEN)',
                'summary': (
                    'Clear cell renal cell carcinoma (CCRCC) masked somatic mutation '
                    'data (GDC Masked Somatic Mutation) from the Clinical Proteomic '
                    'Tumor Analysis Consortium (CPTAC) Discovery Study. '
                    'GDC project CPTAC-3, Kidney primary site. '
                    'VEP-annotated, GRCh38. OPEN access (no dbGaP token required). '
                    '353 files; burden gene×sample matrix + variant occurrences.'
                ),
                'omic_type': OmicDataset.OmicType.GENOMIC,
                'organism': 'Homo sapiens',
                'omics_layers': [self.OMICS_LAYER],
                'omics_count': 1,
                'has_control_group': OmicDataset.ControlGroup.NO,
                'access_type': OmicDataset.AccessType.PUBLIC,
                'is_single_cell': OmicDataset.SingleCell.BULK,
                'data_format': OmicDataset.DataFormat.PROCESSED,
                'extra_metadata': {
                    'cptac_study': 'CCRCC',
                    'data_source': 'GDC',
                    'gdc_project': GDC_PROJECT,
                    'gdc_data_type': GDC_DATA_TYPE,
                    'gdc_access': GDC_ACCESS,
                    'gdc_primary_site': GDC_PRIMARY_SITE,
                    'note': (
                        'MAF tumor-only (gene×amostra burden counts não-sinônimo). '
                        'Variante: variant_key = CHROM:POS:REF:ALT GRCh38.'
                    ),
                },
            },
        )

        if created:
            logger.info(
                'OmicDataset somático criado: accession=%s, source_db=cptac',
                SOMATIC_ACCESSION,
            )
        else:
            logger.info(
                'OmicDataset somático resolvido (existente): accession=%s',
                SOMATIC_ACCESSION,
            )

        # Garante vínculo ProjectDataset (isolamento por projeto)
        ProjectDataset.objects.get_or_create(
            project=self.project,
            dataset=dataset,
            defaults={'curation_status': ProjectDataset.CurationStatus.INCLUDED},
        )

        return dataset

    # ─── Idempotência ────────────────────────────────────────────────────────

    def _check_idempotency(self, dataset: OmicDataset) -> None:
        """
        Verifica se já existe OmicMatrix ou job ativo para esta natural key.

        Raises:
            SomaticMafAlreadyLoadedError: matriz já carregada.
            SomaticMafJobActiveError: job ativo encontrado.
        """
        existing_matrix = OmicMatrix.objects.filter(
            dataset=dataset,
            omics_layer=self.OMICS_LAYER,
            feature_axis=self.FEATURE_AXIS,
            loader_version=LOADER_VERSION,
        ).first()
        if existing_matrix:
            raise SomaticMafAlreadyLoadedError(existing_matrix)

        active_job = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.SOMATIC_MAF_LOAD,
            status__in=[
                IngestionJob.JobStatus.PENDING,
                IngestionJob.JobStatus.RUNNING,
            ],
            parameters__dataset_id=dataset.id,
        ).first()
        if active_job:
            raise SomaticMafJobActiveError(active_job)

    # ─── GDC API ─────────────────────────────────────────────────────────────

    @staticmethod
    def resolve_file_map() -> tuple[list[dict], int, int]:
        """
        Resolve o file_map via GDC API pública (sem credencial, sem token).

        POST https://api.gdc.cancer.gov/files
        Filtro: project=CPTAC-3, data_type='Masked Somatic Mutation',
                access=open, primary_site=Kidney

        Retorna (file_map, n_files, n_cases) onde:
          file_map = list[{file_id: str, sample_accession: str}]
          sample_accession = f"{submitter_id}_tumor"
            (ex: 'C3L-00004_tumor')

        Vários file_id podem ter o mesmo sample_accession (deduplicado pelo Rust).

        Raises:
            GdcApiError: falha de rede ou resposta inesperada.
        """
        payload = {
            'filters': {
                'op': 'and',
                'content': [
                    {
                        'op': '=',
                        'content': {'field': 'cases.project.project_id', 'value': GDC_PROJECT},
                    },
                    {
                        'op': '=',
                        'content': {'field': 'data_type', 'value': GDC_DATA_TYPE},
                    },
                    {
                        'op': '=',
                        'content': {'field': 'access', 'value': GDC_ACCESS},
                    },
                    {
                        'op': '=',
                        'content': {'field': 'cases.primary_site', 'value': GDC_PRIMARY_SITE},
                    },
                ],
            },
            'fields': 'file_id,cases.submitter_id',
            'size': GDC_MAX_FILES,
            'format': 'JSON',
        }

        logger.info(
            'SomaticMafService: consultando GDC API (project=%s, data_type=%s, '
            'access=%s, primary_site=%s, max=%d)',
            GDC_PROJECT,
            GDC_DATA_TYPE,
            GDC_ACCESS,
            GDC_PRIMARY_SITE,
            GDC_MAX_FILES,
        )

        try:
            response = requests.post(
                GDC_FILES_URL,
                json=payload,
                timeout=GDC_REQUEST_TIMEOUT,
                headers={'Content-Type': 'application/json'},
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise GdcApiError(
                f'Falha ao consultar GDC API ({GDC_FILES_URL}): {exc}'
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise GdcApiError(
                f'Resposta GDC não é JSON válido: {exc}'
            ) from exc

        hits = data.get('data', {}).get('hits', [])
        if not hits:
            raise GdcApiError(
                f'GDC API retornou 0 arquivos para filtro '
                f'project={GDC_PROJECT}, data_type={GDC_DATA_TYPE}. '
                'Verifique se os filtros estão corretos e a API está acessível.'
            )

        file_map = []
        cases_seen: set[str] = set()

        for hit in hits:
            file_id = hit.get('file_id', '').strip()
            if not file_id:
                logger.warning('SomaticMafService: hit sem file_id — ignorado')
                continue

            # Cada arquivo tem cases[0].submitter_id = C3L-NNNNN / C3N-NNNNN
            hit_cases = hit.get('cases', [])
            if not hit_cases:
                logger.warning(
                    'SomaticMafService: file_id=%s sem cases — ignorado',
                    file_id,
                )
                continue

            submitter_id = (hit_cases[0].get('submitter_id') or '').strip()
            if not submitter_id:
                logger.warning(
                    'SomaticMafService: file_id=%s sem submitter_id — ignorado',
                    file_id,
                )
                continue

            sample_accession = f'{submitter_id}_tumor'
            file_map.append({
                'file_id': file_id,
                'sample_accession': sample_accession,
            })
            cases_seen.add(submitter_id)

        n_files = len(file_map)
        n_cases = len(cases_seen)

        logger.info(
            'SomaticMafService: GDC API retornou %d arquivos / %d casos únicos',
            n_files,
            n_cases,
        )

        return file_map, n_files, n_cases

    # ─── Execução interna ────────────────────────────────────────────────────

    def _execute(
        self,
        dataset: OmicDataset,
        job: IngestionJob,
        rust_engine,
    ) -> SomaticMafReport:
        """
        Núcleo do serviço: GDC file_map → Rust → upload → ORM → seed SNV.
        """
        report = SomaticMafReport(job_id=str(job.id))

        # ── Passo 1: Resolver file_map via GDC API ────────────────────────────
        file_map, n_files, n_cases = self.resolve_file_map()
        report.gdc_files_found = n_files
        report.gdc_cases_found = n_cases

        # ── Passo 2: gene_allowlist = gene_symbol de GeneRole(source='oncokb') ─
        gene_allowlist = list(
            GeneRole.objects.filter(source='oncokb')
            .values_list('gene_symbol', flat=True)
        )
        logger.info(
            'SomaticMafService: gene_allowlist com %d genes (source=oncokb)',
            len(gene_allowlist),
        )

        # ── Passo 3: Rust carrega MAF → Parquet + TSV de ocorrências ──────────
        # db_url construída aqui — NUNCA logada, NUNCA gravada em parameters
        db = settings.DATABASES['default']
        db_url = (
            f"postgresql://{db['USER']}:{db['PASSWORD']}"
            f"@{db['HOST']}:{db['PORT']}/{db['NAME']}"
        )

        with tempfile.TemporaryDirectory(prefix='davinci_somatic_') as dest_dir:
            logger.info(
                'SomaticMafService: chamando rust_engine.load_somatic_maf '
                '(job %s, %d arquivos GDC, dest_dir omitido)',
                job.id,
                n_files,
            )

            manifest = rust_engine.load_somatic_maf(
                file_map=file_map,
                dest_dir=dest_dir,
                db_url=db_url,
                gene_allowlist=gene_allowlist,
                parquet_name=SOMATIC_PARQUET_NAME,
            )

            logger.info(
                'SomaticMafService: load_somatic_maf concluído (job %s): '
                'n_features=%d, n_samples=%d, n_files_processed=%d, '
                'n_occurrences=%d, n_skipped_synonymous=%d, n_skipped_offlist=%d',
                job.id,
                manifest.n_features,
                manifest.n_samples,
                manifest.n_files_processed,
                manifest.n_occurrences,
                manifest.n_variants_skipped_synonymous,
                manifest.n_variants_skipped_offlist,
            )

            if manifest.errors:
                logger.warning(
                    'SomaticMafService: load_somatic_maf reportou %d erros (job %s)',
                    len(manifest.errors),
                    job.id,
                )
                report.errors.extend(manifest.errors)

            report.n_files_processed = manifest.n_files_processed
            report.n_samples = manifest.n_samples
            report.n_occurrences = manifest.n_occurrences
            report.n_variants_skipped_synonymous = manifest.n_variants_skipped_synonymous
            report.n_variants_skipped_offlist = manifest.n_variants_skipped_offlist

            # ── Passo 4: Upload do Parquet via shared_omics_storage_key ──────────
            # Dado OPEN → namespace compartilhado (_shared)
            storage_key = self._upload_parquet(manifest.parquet_path, dataset, job)
            report.storage_key = storage_key

            # ── Passo 5: ORM set-based (OmicMatrix + OmicSample + OmicMatrixSample) ─
            matrix = self._persist_orm(
                dataset=dataset,
                manifest=manifest,
                storage_key=storage_key,
            )
            report.matrix_id = matrix.id

            # ── Passo 6: Seed SNV set-based (lê TSV antes do cleanup) ───────────
            n_seeds, seed_counts = self._derive_snv_seeds(
                occurrences_path=manifest.occurrences_path,
                matrix=matrix,
                job=job,
            )
            report.n_seeds_created = n_seeds
            report.n_inactivator = seed_counts.get('inactivator', 0)
            report.n_activator = seed_counts.get('activator', 0)
            report.n_neutral = seed_counts.get('neutral', 0)
            report.n_truncating_lof = seed_counts.get('truncating_lof', 0)
            report.n_missense_resolved = seed_counts.get('missense_resolved', 0)
            report.n_unannotated_skipped = seed_counts.get('unannotated_skipped', 0)

        # TemporaryDirectory limpo automaticamente ao sair do with-block

        # ── Passo 7: Marca job COMPLETED ─────────────────────────────────────
        IngestionJob.objects.filter(id=job.id).update(
            status=IngestionJob.JobStatus.COMPLETED,
            records_processed=manifest.n_occurrences,
            records_inserted=n_seeds,
            completed_at=timezone.now(),
        )

        logger.info(
            'SomaticMafService: SOMATIC_MAF_LOAD concluído (job %s): '
            'storage_key=%s, matrix_id=%d, n_occurrences=%d, '
            'n_seeds=%d, n_inactivator=%d, n_activator=%d, n_neutral=%d, '
            'n_truncating_lof=%d, n_missense_resolved=%d, '
            'n_unannotated_skipped=%d',
            job.id,
            storage_key,
            matrix.id,
            manifest.n_occurrences,
            n_seeds,
            report.n_inactivator,
            report.n_activator,
            report.n_neutral,
            report.n_truncating_lof,
            report.n_missense_resolved,
            report.n_unannotated_skipped,
        )

        return report

    def _upload_parquet(
        self,
        local_path: str,
        dataset: OmicDataset,
        job: IngestionJob,
    ) -> str:
        """
        Faz upload do Parquet burden para default_storage.

        Dado GDC OPEN → namespace compartilhado (_shared).
        Retorna storage_key lógica (nunca caminho físico).
        Remove o arquivo local após upload bem-sucedido.
        """
        filename = os.path.basename(local_path)
        object_key = shared_omics_storage_key(
            dataset_accession=dataset.accession,
            filename=filename,
        )

        logger.info(
            'SomaticMafService: fazendo upload do Parquet somático '
            '(storage_key lógica, job %s)',
            job.id,
        )

        with open(local_path, 'rb') as f:
            saved_key = default_storage.save(object_key, File(f))

        try:
            os.remove(local_path)
        except OSError as err:
            logger.warning(
                'SomaticMafService: falha ao remover Parquet local após upload '
                '(job %s): %s',
                job.id,
                err,
            )

        logger.info(
            'SomaticMafService: upload do Parquet somático concluído (job %s): '
            'storage_key=%s',
            job.id,
            saved_key,
        )
        return saved_key

    def _persist_orm(
        self,
        dataset: OmicDataset,
        manifest,
        storage_key: str,
    ) -> OmicMatrix:
        """
        Persiste OmicMatrix + OmicSample + OmicMatrixSample em transação atômica.

        OmicMatrix.omics_layer='genomic', data_format_level='counts',
        feature_axis='gene' — variante C1 do plano (sem migration nova;
        'genomic' = mutação/sequência, já nota no schema; 'counts' = burden
        inteiro de mutações não-sinônimas por gene×amostra).

        OmicSample: get_or_create por accession=f"{case_id}_tumor".
        Reusa amostras existentes da CNV/proteoma (mesma amostra biológica).
        """
        with transaction.atomic():
            # ── OmicMatrix ────────────────────────────────────────────────────
            matrix, matrix_created = OmicMatrix.objects.get_or_create(
                dataset=dataset,
                omics_layer=self.OMICS_LAYER,
                feature_axis=self.FEATURE_AXIS,
                loader_version=LOADER_VERSION,
                defaults={
                    'data_format_level': self.DATA_FORMAT_LEVEL,
                    'source_pointer': (
                        f'GDC:{GDC_PROJECT}:{GDC_DATA_TYPE}:{GDC_ACCESS}'
                    ),
                    'storage_key': storage_key,
                    'n_features': manifest.n_features,
                    'n_samples': manifest.n_samples,
                    'checksum_md5': manifest.checksum_md5,
                },
            )

            if not matrix_created:
                OmicMatrix.objects.filter(id=matrix.id).update(
                    storage_key=storage_key,
                    n_features=manifest.n_features,
                    n_samples=manifest.n_samples,
                    checksum_md5=manifest.checksum_md5,
                )
                matrix.refresh_from_db()

            # ── OmicSample: um por (case_id, 'tumor') ─────────────────────────
            # Loader somático retorna sample_role='tumor' para todas as amostras.
            # accession = f"{case_id}_tumor" — mesmo padrão da Fase 0 e CNV.
            # Se a amostra já existe (CNV/proteoma), get_or_create a resolve.
            col_map: dict[tuple[str, str], int] = {}
            for col in manifest.sample_columns:
                key = (col.case_id, col.sample_role)
                if key not in col_map:
                    col_map[key] = col.column_index

            sample_by_key: dict[tuple[str, str], OmicSample] = {}
            for (case_id, role), _col_idx in col_map.items():
                accession = f'{case_id}_{role}'
                sample, _ = OmicSample.objects.get_or_create(
                    accession=accession,
                    defaults={
                        'dataset': dataset,
                        'title': f'{case_id} — {role} (CPTAC CCRCC Somatic MAF)',
                        'organism': 'Homo sapiens',
                        'characteristics': {
                            'case_id': case_id,
                            'sample_role': role,
                            'study': 'CPTAC-CCRCC',
                            'omic_type': 'genomic',
                        },
                    },
                )
                sample_by_key[(case_id, role)] = sample

            # ── OmicMatrixSample: bulk_create com ignore_conflicts ─────────────
            oms_to_create = []
            for (case_id, role), col_idx in col_map.items():
                sample = sample_by_key[(case_id, role)]
                oms_to_create.append(
                    OmicMatrixSample(
                        matrix=matrix,
                        sample=sample,
                        column_index=col_idx,
                        sample_role=OmicMatrixSample.SampleRole.TUMOR,
                    )
                )

            if oms_to_create:
                OmicMatrixSample.objects.bulk_create(
                    oms_to_create,
                    ignore_conflicts=True,
                )
                logger.info(
                    'SomaticMafService: OmicMatrixSample bulk_create: '
                    '%d linhas (matrix %s)',
                    len(oms_to_create),
                    matrix.id,
                )

        return matrix

    # ─── Seed SNV set-based ───────────────────────────────────────────────────

    def _derive_snv_seeds(
        self,
        occurrences_path: str,
        matrix: OmicMatrix,
        job: IngestionJob,
    ) -> tuple[int, dict]:
        """
        Deriva VariantEffectSeed SNV a partir do TSV de ocorrências.

        Lê o arquivo local de ocorrências (sample_accession, variant_key,
        gene_symbol, variant_classification) produzido pelo Rust no mesmo
        tempdir.

        Algoritmo set-based:
          1. Lê todas as ocorrências do TSV.
          2. Constrói sample_map: accession → OmicSample (via OmicMatrixSample).
          3. Carrega GeneRole (source='oncokb') em memória.
          4. Para variantes missense/in-frame: busca VariantEffectResolved
             em UMA query (variant_key__in) — evita N queries.
          5. Aplica regra de sinal SNV v1 (truncante vs missense).
          6. bulk_create(update_conflicts=True) na natural key.

        Args:
            occurrences_path: Caminho local do TSV de ocorrências.
            matrix: OmicMatrix somática recém-criada.
            job: IngestionJob para logging.

        Returns:
            (n_seeds_criadas, {inactivator, activator, neutral,
             truncating_lof, missense_resolved, unannotated_skipped})
        """
        counts = {
            'inactivator': 0,
            'activator': 0,
            'neutral': 0,
            'truncating_lof': 0,
            'missense_resolved': 0,
            'unannotated_skipped': 0,
        }

        if not os.path.exists(occurrences_path):
            logger.warning(
                'SomaticMafService: occurrences_path não encontrado (job %s): '
                'caminho omitido. Nenhuma seed SNV derivada.',
                job.id,
            )
            return 0, counts

        # ── Passo 1: Lê as ocorrências do TSV ─────────────────────────────────
        # Formato: sample_accession\tvariant_key\tgene_symbol\tvariant_classification
        occurrences: list[tuple[str, str, str, str]] = []
        with open(occurrences_path, 'r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f, delimiter='\t')
            for row in reader:
                sample_acc = (row.get('sample_accession') or '').strip()
                variant_key = (row.get('variant_key') or '').strip()
                gene_symbol = (row.get('gene_symbol') or '').strip()
                variant_class = (row.get('variant_classification') or '').strip()
                if sample_acc and variant_key and gene_symbol and variant_class:
                    occurrences.append((sample_acc, variant_key, gene_symbol, variant_class))

        logger.info(
            'SomaticMafService: lidas %d ocorrências do TSV (job %s)',
            len(occurrences),
            job.id,
        )

        if not occurrences:
            logger.warning(
                'SomaticMafService: TSV de ocorrências está vazio (job %s)',
                job.id,
            )
            return 0, counts

        # ── Passo 2: sample_map: accession → OmicSample (via OmicMatrixSample) ─
        # Consulta OmicMatrixSample da matriz somática para resolver sample_id.
        oms_qs = (
            OmicMatrixSample.objects
            .filter(matrix=matrix)
            .select_related('sample')
            .values('sample__accession', 'sample_id', 'sample__id')
        )
        sample_map: dict[str, int] = {
            row['sample__accession']: row['sample_id']
            for row in oms_qs
        }
        # Para o bulk_create, precisamos do objeto OmicSample (para FK)
        sample_obj_map: dict[str, OmicSample] = {}
        for oms in OmicMatrixSample.objects.filter(matrix=matrix).select_related('sample'):
            sample_obj_map[oms.sample.accession] = oms.sample

        logger.info(
            'SomaticMafService: %d amostras na matriz somática (job %s)',
            len(sample_map),
            job.id,
        )

        # ── Passo 3: GeneRole em memória ──────────────────────────────────────
        gene_roles: dict[str, str] = {
            row['gene_symbol']: row['role']
            for row in GeneRole.objects.filter(source='oncokb').values('gene_symbol', 'role')
        }

        # ── Passo 4: Coleta variant_keys missense para query em lote ──────────
        # Separa ocorrências por tipo: truncante vs missense/in-frame.
        # Para missense: pré-carrega VariantEffectResolved em UMA query set-based.
        missense_vkeys: set[str] = set()
        for _, variant_key, _, variant_class in occurrences:
            vc_norm = variant_class.lower()
            if vc_norm in _MISSENSE_CLASSIFICATIONS:
                missense_vkeys.add(variant_key)

        # Query set-based: busca VariantEffectResolved para todos os variant_keys
        # missense presentes nas ocorrências. Uma única query.
        # Para cada (variant_key, gene_symbol): escolhe a resolução mais recente
        # (maior resolution_version — lexicográfico).
        resolved_map: dict[tuple[str, str], VariantEffectResolved] = {}
        if missense_vkeys:
            resolved_qs = (
                VariantEffectResolved.objects
                .filter(variant_key__in=missense_vkeys)
                .order_by('variant_key', 'gene_symbol', '-resolution_version')
            )
            for ver in resolved_qs:
                key = (ver.variant_key, ver.gene_symbol)
                if key not in resolved_map:
                    # Primeira entrada é a mais recente (ordenado por -resolution_version)
                    resolved_map[key] = ver

            logger.info(
                'SomaticMafService: %d variant_keys missense → %d resolvidos em '
                'VariantEffectResolved (job %s)',
                len(missense_vkeys),
                len(resolved_map),
                job.id,
            )

        # ── Passo 5: Aplica regra de sinal SNV v1 e monta seeds ───────────────
        seeds_to_create: list[VariantEffectSeed] = []
        seen_natural_keys: set[tuple[int, int, str, str]] = set()

        for sample_acc, variant_key, gene_symbol, variant_class in occurrences:
            # Resolve sample
            sample = sample_obj_map.get(sample_acc)
            if sample is None:
                logger.debug(
                    'SomaticMafService: amostra %s não encontrada na matriz '
                    'somática — ocorrência ignorada (job %s)',
                    sample_acc,
                    job.id,
                )
                continue

            gene_role = gene_roles.get(gene_symbol, GeneRole.Role.UNKNOWN)
            vc_norm = variant_class.lower()

            direction: str
            magnitude: float
            confidence: float
            effect_source: str

            if vc_norm in _TRUNCATING_CLASSIFICATIONS:
                # TRUNCANTE: LoF estrutural direto (não depende de ClinVar/AM)
                # direction=inactivator APENAS se gene_role=tsg.
                # Truncar oncogene ≠ ativar → neutral (heurística conservadora v1).
                if gene_role == GeneRole.Role.TSG:
                    direction = VariantEffectSeed.Direction.INACTIVATOR
                    counts['inactivator'] += 1
                else:
                    direction = VariantEffectSeed.Direction.NEUTRAL
                    counts['neutral'] += 1

                magnitude = _TRUNCATING_MAGNITUDE
                confidence = _TRUNCATING_CONFIDENCE
                effect_source = 'variant_classification'
                counts['truncating_lof'] += 1

            elif vc_norm in _MISSENSE_CLASSIFICATIONS:
                # MISSENSE/IN-FRAME: busca VariantEffectResolved
                resolved = resolved_map.get((variant_key, gene_symbol))
                if resolved is None:
                    # Missense sem anotação → PULA (não polui com neutral sem sinal)
                    counts['unannotated_skipped'] += 1
                    continue

                direction = resolved.direction
                magnitude = resolved.magnitude
                confidence = resolved.confidence
                effect_source = resolved.effect_source or 'alphamissense'
                gene_role = resolved.gene_role_used or gene_role
                counts['missense_resolved'] += 1

                if direction == VariantEffectSeed.Direction.INACTIVATOR:
                    counts['inactivator'] += 1
                elif direction == VariantEffectSeed.Direction.ACTIVATOR:
                    counts['activator'] += 1
                else:
                    counts['neutral'] += 1

            else:
                # Classificação desconhecida/não relevante → PULA
                counts['unannotated_skipped'] += 1
                continue

            # Natural key check (evitar duplicatas no lote)
            # UniqueConstraint (0034): (matrix, sample, gene_symbol, variant_key,
            # evidence_type, method_version). Aqui evidence_type='snv' e
            # method_version fixos no lote, então basta a tupla variável abaixo.
            natural_key = (matrix.id, sample.id, gene_symbol, variant_key)
            if natural_key in seen_natural_keys:
                continue
            seen_natural_keys.add(natural_key)

            seeds_to_create.append(
                VariantEffectSeed(
                    matrix=matrix,
                    sample=sample,
                    gene_symbol=gene_symbol,
                    variant_key=variant_key,
                    evidence_type=VariantEffectSeed.EvidenceType.SNV,
                    direction=direction,
                    magnitude=magnitude,
                    confidence=confidence,
                    gene_role=gene_role,
                    effect_source=effect_source,
                    method_version=METHOD_VERSION,
                )
            )

        logger.info(
            'SomaticMafService: %d seeds SNV montadas (job %s). '
            'Executando bulk_create (upsert).',
            len(seeds_to_create),
            job.id,
        )

        # ── Passo 6: bulk_create com update_conflicts (idempotente) ──────────
        if seeds_to_create:
            with transaction.atomic():
                VariantEffectSeed.objects.bulk_create(
                    seeds_to_create,
                    update_conflicts=True,
                    unique_fields=[
                        'matrix', 'sample', 'gene_symbol',
                        'variant_key', 'evidence_type', 'method_version',
                    ],
                    update_fields=[
                        'direction', 'magnitude', 'confidence',
                        'gene_role', 'effect_source',
                    ],
                )

        n_seeds = len(seeds_to_create)
        logger.info(
            'SomaticMafService: %d seeds SNV persistidas (job %s): '
            'inactivator=%d, activator=%d, neutral=%d, '
            'truncating_lof=%d, missense_resolved=%d, unannotated_skipped=%d',
            n_seeds,
            job.id,
            counts['inactivator'],
            counts['activator'],
            counts['neutral'],
            counts['truncating_lof'],
            counts['missense_resolved'],
            counts['unannotated_skipped'],
        )

        return n_seeds, counts
