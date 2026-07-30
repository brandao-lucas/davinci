"""
PhosphoMatrixLoadService — orquestração Django da carga da matriz de
fosfoproteoma CPTAC CCRCC.

OmnisPathway Objetivo 2, Fase 3, Passo 3.1.

Responsabilidade:
  1. Resolver/criar OmicDataset para o fosfoproteoma CPTAC CCRCC.
  2. Gate de idempotência: não duplicar OmicMatrix nem job ativo de
     PHOSPHO_MATRIX_LOAD.
  3. Criar IngestionJob(job_type=PHOSPHO_MATRIX_LOAD).
  4. Chamar rust_engine.load_cptac_phospho_matrix(tumor_url, normal_url,
     dest_dir, parquet_name) → PhosphoMatrixManifest{parquet_path,
     checksum_md5, n_features, n_samples, sample_columns, genes_discarded}.
     O Rust NÃO toca PG, NÃO faz upload.
  5. Upload do Parquet via shared_omics_storage_key (dado público).
  6. Criar OmicMatrix + OmicSample (get_or_create — REUSA amostras CCRCC
     já carregadas) + OmicMatrixSample (bulk_create ignore_conflicts) em
     transação atômica.
  7. Limpeza do tmp local. Marcar IngestionJob COMPLETED.

Fronteira de camadas (Regra #1 / django-rust-boundary):
  - Rust: download HTTP dos arquivos .cct (gene-level phospho × amostra),
    parse streaming, escrita do Parquet local, MD5 incremental. NÃO toca
    PG, NÃO faz upload.
  - Django: upload via default_storage, ORM set-based, isolamento por
    projeto. NÃO abre o Parquet, NÃO parseia .cct.

Fosfoproteoma GENE-LEVEL (confirmado na discovery):
  Feature = símbolo de gene UPPERCASE (não fosfo-sítio `TP53_S15`).
  OmicMatrix.feature_axis = 'phospho_site' por convenção (distingue esta
  matriz da proteoma gene-level da Fase 0, mesmo que ambas sejam gene-level
  aqui). `phospho_site` já existe no CheckConstraint — nenhuma migration.

Reúso de amostras:
  OmicSample.accession = f"{case_id}_{role}" (mesmo padrão da Fase 0).
  get_or_create é idempotente; amostras CCRCC já carregadas (tumor/normal)
  são reutilizadas sem criar duplicatas.

OmicMatrix.feature_axis = 'phospho_site' (distingue da proteoma Fase 0
  que usa 'gene'). As duas matrizes são do mesmo OmicDataset base CCRCC
  mas têm accessions distintos (CPTAC-CCRCC-PHOSPHO vs CPTAC-CCRCC-PROTEOME).

Isolamento (firebase-auth-guard / Regra #3):
  - project deve pertencer ao request.user antes de chegar aqui.
  - OmicDataset é tabela compartilhada (uma linha por estudo). Isolamento
    via ProjectDataset.
  - OmicSample é tabela compartilhada (reúso por accession).

Sensitive-data-handling:
  - URLs são públicas (LinkedOmics, acesso aberto). Não há credencial.
  - db_url NUNCA logada nem gravada em IngestionJob.parameters.
  - storage_key é chave LÓGICA — nunca caminho físico absoluto nos logs.

Pré-condição do mapeamento (Fase 3, Passo 3.4, Regra 1):
  A existência desta OmicMatrix é pré-condição do mapeamento readout→feature
  (Passo 3.4). O conjunto de features desta matriz (símbolos de gene) é
  materializado em OmicMatrixSample.sample.accession — contudo o mapeamento
  3.4 valida existência via amostra, NÃO via features individuais (A→C
  adiada pela Decisão 5 do plano). Ver readout_mapping_service.py.

Padrão reutilizado de MatrixLoadService / CnvMatrixLoadService:
  - _get_or_create_dataset / _check_idempotency / _upload_parquet /
    _persist_orm: mesmos contratos; adaptados para fosfo.
  - shared_omics_storage_key: dado público → namespace _shared.
  - OmicSample.accession = f"{case_id}_{role}" (mesmo padrão das Fases 0/2).
  - OmicMatrixSample: bulk_create ignore_conflicts (idempotência).
  - OmicMatrix NK: (dataset, omics_layer, feature_axis, loader_version).
"""

from __future__ import annotations

import logging
import os
import tempfile

from django.core.files import File
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from apps.core.models import (
    DaVinciProject,
    IngestionJob,
    OmicDataset,
    OmicMatrix,
    OmicMatrixSample,
    OmicSample,
    ProjectDataset,
)
from apps.core.storage_utils import shared_omics_storage_key

logger = logging.getLogger(__name__)

# Constantes do fosfoproteoma CPTAC CCRCC (acesso público, gene-level)
PHOSPHO_CCRCC_ACCESSION = 'CPTAC-CCRCC-PHOSPHO'
PHOSPHO_TUMOR_URL = (
    'https://www.linkedomics.org/data_download/CPTAC-CCRCC/'
    'HS_CPTAC_CCRCC_phosphoproteome_gene_Tumor.cct'
)
PHOSPHO_NORMAL_URL = (
    'https://www.linkedomics.org/data_download/CPTAC-CCRCC/'
    'HS_CPTAC_CCRCC_phosphoproteome_gene_Normal.cct'
)
PHOSPHO_PARQUET_NAME = 'cptac_ccrcc_phospho.parquet'

# Versão do loader: atualizar quando o contrato Rust mudar de forma incompatível.
LOADER_VERSION = 'phospho-v1'

# Statuses de ProjectDataset considerados ativos (consistência com CnvSeedService).
_ACTIVE_STATUSES = (
    ProjectDataset.CurationStatus.INCLUDED,
    ProjectDataset.CurationStatus.QUEUED_DOWNLOAD,
    ProjectDataset.CurationStatus.DOWNLOADED,
    ProjectDataset.CurationStatus.PENDING,
)


class PhosphoMatrixAlreadyLoadedError(Exception):
    """Levantado quando OmicMatrix de fosfo já existe para a natural key."""

    def __init__(self, matrix: OmicMatrix):
        self.matrix = matrix
        super().__init__(
            f"OmicMatrix de fosfoproteoma já existe: dataset="
            f"{matrix.dataset.accession}, layer={matrix.omics_layer}, "
            f"axis={matrix.feature_axis}, loader_version={matrix.loader_version} "
            f"(id={matrix.id})"
        )


class PhosphoMatrixJobActiveError(Exception):
    """Levantado quando já há um PHOSPHO_MATRIX_LOAD pending/running."""

    def __init__(self, job: IngestionJob):
        self.job = job
        super().__init__(
            f"PHOSPHO_MATRIX_LOAD já ativo (job {job.id}, status={job.status}) "
            f"para projeto {job.project_id} — dispatch ignorado (idempotência)"
        )


class PhosphoMatrixLoadService:
    """
    Serviço de carga da matriz de fosfoproteoma CPTAC CCRCC.

    Uso síncrono (management command / bancada de prova):
        service = PhosphoMatrixLoadService(project)
        result = service.run()

    Uso assíncrono (Celery):
        job = PhosphoMatrixLoadService.dispatch(project)
        # task run_phospho_matrix_load(job.id, str(project.id)) em background

    Retorno de run():
        {
            'job_id': str,
            'storage_key': str,
            'n_features': int,
            'n_samples': int,
            'checksum_md5': str,
            'genes_discarded': int,
            'roles': {'tumor': N, 'normal': N},
        }
    """

    OMICS_LAYER = 'proteomic'
    FEATURE_AXIS = OmicMatrix.FeatureAxis.PHOSPHO_SITE
    DATA_FORMAT_LEVEL = OmicMatrix.DataFormatLevel.INTENSITIES

    def __init__(self, project: DaVinciProject):
        self.project = project
        self.user = project.user

    # ─────────────────────────────────────────────────────────────────────────
    # API pública
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def dispatch(cls, project: DaVinciProject) -> IngestionJob:
        """
        Gate de idempotência + criação do IngestionJob.

        Não executa a carga — apenas verifica pré-condições e enfileira
        a task Celery (via run_phospho_matrix_load).

        Returns:
            IngestionJob recém-criado (status=PENDING).

        Raises:
            PhosphoMatrixAlreadyLoadedError: matriz já carregada.
            PhosphoMatrixJobActiveError: job ativo encontrado.
        """
        from apps.core.tasks.ingestion_tasks import run_phospho_matrix_load

        service = cls(project)
        dataset = service._get_or_create_dataset()
        service._check_idempotency(dataset)

        job = IngestionJob.objects.create(
            project=project,
            job_type=IngestionJob.JobType.PHOSPHO_MATRIX_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={
                'dataset_accession': dataset.accession,
                'dataset_id': dataset.id,
                'omics_layer': cls.OMICS_LAYER,
                'feature_axis': cls.FEATURE_AXIS,
                'loader_version': LOADER_VERSION,
                # URLs públicas — não são credenciais; registradas para auditoria
                'tumor_url': PHOSPHO_TUMOR_URL,
                'normal_url': PHOSPHO_NORMAL_URL,
                # db_url NÃO é armazenada (sensitive-data-handling)
            },
        )

        try:
            run_phospho_matrix_load.delay(str(job.id), str(project.id))
            logger.info(
                'PHOSPHO_MATRIX_LOAD disparado (job %s) para projeto %s / '
                'dataset %s',
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
        Executa a carga de forma síncrona (bancada de prova / management command).

        Fluxo:
          1. Resolve/cria OmicDataset (idempotente).
          2. Gate de idempotência (matriz/job ativo).
          3. Cria/atualiza IngestionJob para RUNNING.
          4. Chama rust_engine.load_cptac_phospho_matrix → manifest.
          5. Upload do Parquet via default_storage.
          6. Cria OmicMatrix + OmicSample (get_or_create) + OmicMatrixSample.
          7. Limpa tmp local. Marca job COMPLETED.

        Args:
            job: IngestionJob existente (criado pelo dispatch); se None, cria
                 internamente (modo síncrono direto do management command).

        Returns:
            dict com n_features, n_samples, roles, storage_key, job_id.
        """
        import rust_engine

        dataset = self._get_or_create_dataset()

        if job is None:
            try:
                self._check_idempotency(dataset)
            except (PhosphoMatrixAlreadyLoadedError, PhosphoMatrixJobActiveError):
                raise

        if job is None:
            job = IngestionJob.objects.create(
                project=self.project,
                job_type=IngestionJob.JobType.PHOSPHO_MATRIX_LOAD,
                status=IngestionJob.JobStatus.RUNNING,
                parameters={
                    'dataset_accession': dataset.accession,
                    'dataset_id': dataset.id,
                    'omics_layer': self.OMICS_LAYER,
                    'feature_axis': self.FEATURE_AXIS,
                    'loader_version': LOADER_VERSION,
                    'tumor_url': PHOSPHO_TUMOR_URL,
                    'normal_url': PHOSPHO_NORMAL_URL,
                },
            )
        else:
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.RUNNING,
                started_at=timezone.now(),
            )

        try:
            result = self._execute(dataset, job, rust_engine)
            return result
        except Exception as exc:
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=str(exc),
                completed_at=timezone.now(),
            )
            raise

    # ─────────────────────────────────────────────────────────────────────────
    # Implementação interna
    # ─────────────────────────────────────────────────────────────────────────

    def _get_or_create_dataset(self) -> OmicDataset:
        """
        Resolve ou cria OmicDataset do fosfoproteoma CPTAC CCRCC.

        OmicDataset é tabela COMPARTILHADA — lookup key = accession.
        Isolamento por projeto via ProjectDataset.
        """
        dataset, created = OmicDataset.objects.get_or_create(
            accession=PHOSPHO_CCRCC_ACCESSION,
            defaults={
                'source_db': OmicDataset.SourceDB.CPTAC,
                'title': 'CPTAC CCRCC Discovery Study — Phosphoproteome gene-level (LinkedOmics)',
                'summary': (
                    'Clear cell renal cell carcinoma (CCRCC) phosphoproteome from '
                    'the Clinical Proteomic Tumor Analysis Consortium (CPTAC) '
                    'Discovery Study. Gene-level phosphopeptide intensities, tumor '
                    'vs. normal paired samples. Source: LinkedOmics / PDC.'
                ),
                'omic_type': OmicDataset.OmicType.PROTEOMIC,
                'organism': 'Homo sapiens',
                'omics_layers': [self.OMICS_LAYER],
                'omics_count': 1,
                'has_control_group': OmicDataset.ControlGroup.YES,
                'access_type': OmicDataset.AccessType.PUBLIC,
                'is_single_cell': OmicDataset.SingleCell.BULK,
                'data_format': OmicDataset.DataFormat.PROCESSED,
                'extra_metadata': {
                    'cptac_study': 'CCRCC',
                    'data_level': 'gene-level phosphoproteome',
                    'data_source': 'LinkedOmics',
                    'tumor_url': PHOSPHO_TUMOR_URL,
                    'normal_url': PHOSPHO_NORMAL_URL,
                },
            },
        )

        if created:
            logger.info(
                'OmicDataset fosfoproteoma criado: accession=%s',
                PHOSPHO_CCRCC_ACCESSION,
            )
        else:
            logger.info(
                'OmicDataset fosfoproteoma resolvido (existente): accession=%s',
                PHOSPHO_CCRCC_ACCESSION,
            )

        # Garante vínculo ProjectDataset (isolamento por projeto)
        ProjectDataset.objects.get_or_create(
            project=self.project,
            dataset=dataset,
            defaults={'curation_status': ProjectDataset.CurationStatus.INCLUDED},
        )

        return dataset

    def _check_idempotency(self, dataset: OmicDataset) -> None:
        """
        Verifica se já existe OmicMatrix ou job ativo para esta natural key.

        Raises:
            PhosphoMatrixAlreadyLoadedError: matriz já carregada.
            PhosphoMatrixJobActiveError: job ativo encontrado.
        """
        existing_matrix = OmicMatrix.objects.filter(
            dataset=dataset,
            omics_layer=self.OMICS_LAYER,
            feature_axis=self.FEATURE_AXIS,
            loader_version=LOADER_VERSION,
        ).first()
        if existing_matrix:
            raise PhosphoMatrixAlreadyLoadedError(existing_matrix)

        active_job = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.PHOSPHO_MATRIX_LOAD,
            status__in=[
                IngestionJob.JobStatus.PENDING,
                IngestionJob.JobStatus.RUNNING,
            ],
            parameters__dataset_id=dataset.id,
        ).first()
        if active_job:
            raise PhosphoMatrixJobActiveError(active_job)

    def _execute(
        self,
        dataset: OmicDataset,
        job: IngestionJob,
        rust_engine,
    ) -> dict:
        """
        Núcleo da carga: Rust → manifest → upload → ORM set-based.

        Parâmetro rust_engine passado explicitamente para facilitar mock em testes.
        """
        with tempfile.TemporaryDirectory(prefix='davinci_phospho_') as dest_dir:
            # ── Passo 1: Rust baixa e grava Parquet local ─────────────────────
            logger.info(
                'Chamando rust_engine.load_cptac_phospho_matrix (job %s)',
                job.id,
            )
            manifest = rust_engine.load_cptac_phospho_matrix(
                tumor_url=PHOSPHO_TUMOR_URL,
                normal_url=PHOSPHO_NORMAL_URL,
                dest_dir=dest_dir,
                parquet_name=PHOSPHO_PARQUET_NAME,
            )
            logger.info(
                'load_cptac_phospho_matrix concluído: n_features=%d, '
                'n_samples=%d, genes_discarded=%d',
                manifest.n_features,
                manifest.n_samples,
                manifest.genes_discarded,
            )

            # ── Passo 2: Upload do Parquet via default_storage ─────────────────
            storage_key = self._upload_parquet(manifest.parquet_path, dataset, job)

            # ── Passo 3: ORM set-based dentro de transação atômica ─────────────
            matrix, roles_summary = self._persist_orm(
                dataset=dataset,
                manifest=manifest,
                storage_key=storage_key,
            )

        # ── Passo 4: Marca job COMPLETED ──────────────────────────────────────
        IngestionJob.objects.filter(id=job.id).update(
            status=IngestionJob.JobStatus.COMPLETED,
            records_processed=manifest.n_features,
            records_inserted=manifest.n_samples,
            completed_at=timezone.now(),
        )

        logger.info(
            'PHOSPHO_MATRIX_LOAD concluído (job %s): storage_key=<omitida>, '
            'n_features=%d, n_samples=%d',
            job.id,
            manifest.n_features,
            manifest.n_samples,
        )

        return {
            'job_id': str(job.id),
            'storage_key': storage_key,
            'n_features': manifest.n_features,
            'n_samples': manifest.n_samples,
            'checksum_md5': manifest.checksum_md5,
            'genes_discarded': manifest.genes_discarded,
            'roles': roles_summary,
        }

    def _upload_parquet(
        self,
        local_path: str,
        dataset: OmicDataset,
        job: IngestionJob,
    ) -> str:
        """
        Faz upload do Parquet local para default_storage.

        Dado público → namespace compartilhado _shared.
        Retorna storage_key lógica (nunca caminho físico).
        """
        filename = os.path.basename(local_path)
        object_key = shared_omics_storage_key(
            dataset_accession=dataset.accession,
            filename=filename,
        )

        logger.info(
            'Fazendo upload do Parquet de fosfo para default_storage (job %s)',
            job.id,
        )

        with open(local_path, 'rb') as f:
            saved_key = default_storage.save(object_key, File(f))

        try:
            os.remove(local_path)
        except OSError as err:
            logger.warning(
                'Falha ao remover Parquet de fosfo local após upload (job %s): %s',
                job.id,
                err,
            )

        logger.info(
            'Upload do Parquet de fosfo concluído (job %s): storage_key=<omitida>',
            job.id,
        )
        return saved_key

    def _persist_orm(
        self,
        dataset: OmicDataset,
        manifest,
        storage_key: str,
    ) -> tuple[OmicMatrix, dict]:
        """
        Persiste OmicMatrix + OmicSample + OmicMatrixSample.

        Granularidade de OmicSample: UM por (case_id, role) —
        accession=f"{case_id}_{role}". Reúso de amostras CCRCC já carregadas
        pela Fase 0 (proteoma) — get_or_create idempotente pelo accession.
        """
        source_pointer = f"{PHOSPHO_TUMOR_URL} | {PHOSPHO_NORMAL_URL}"

        with transaction.atomic():
            # ── OmicMatrix ────────────────────────────────────────────────────
            matrix, matrix_created = OmicMatrix.objects.get_or_create(
                dataset=dataset,
                omics_layer=self.OMICS_LAYER,
                feature_axis=self.FEATURE_AXIS,
                loader_version=LOADER_VERSION,
                defaults={
                    'data_format_level': self.DATA_FORMAT_LEVEL,
                    'source_pointer': source_pointer,
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

            # ── OmicSample: UM por (case_id, role) ───────────────────────────
            sample_columns = manifest.sample_columns

            col_map: dict[tuple[str, str], int] = {}
            for col in sample_columns:
                key = (col.case_id, col.sample_role)
                if key not in col_map:
                    col_map[key] = col.column_index

            sample_by_key: dict[tuple[str, str], OmicSample] = {}
            roles_count: dict[str, int] = {'tumor': 0, 'normal': 0, 'unknown': 0}

            for (case_id, role), _col_idx in col_map.items():
                accession = f"{case_id}_{role}"
                # get_or_create: reutiliza amostras CCRCC já carregadas (Fase 0)
                sample, _ = OmicSample.objects.get_or_create(
                    accession=accession,
                    defaults={
                        'dataset': dataset,
                        'title': f'{case_id} — {role} (CPTAC CCRCC)',
                        'organism': 'Homo sapiens',
                        'characteristics': {
                            'case_id': case_id,
                            'sample_role': role,
                            'study': 'CPTAC-CCRCC',
                        },
                    },
                )
                sample_by_key[(case_id, role)] = sample
                roles_count[role] = roles_count.get(role, 0) + 1

            # ── OmicMatrixSample: bulk_create com ignore_conflicts ─────────────
            oms_to_create = []
            for (case_id, role), col_idx in col_map.items():
                sample = sample_by_key[(case_id, role)]
                role_value = _normalize_role(role)
                oms_to_create.append(
                    OmicMatrixSample(
                        matrix=matrix,
                        sample=sample,
                        column_index=col_idx,
                        sample_role=role_value,
                    )
                )

            if oms_to_create:
                OmicMatrixSample.objects.bulk_create(
                    oms_to_create,
                    ignore_conflicts=True,
                )
                logger.info(
                    'OmicMatrixSample fosfo bulk_create: %d linhas (matrix %s)',
                    len(oms_to_create),
                    matrix.id,
                )

        roles_summary = {k: v for k, v in roles_count.items() if v > 0}
        return matrix, roles_summary


def _normalize_role(role: str) -> str:
    """
    Mapeia role string do Rust para o vocabulário canônico de OmicMatrixSample.
    """
    if role == OmicMatrixSample.SampleRole.TUMOR:
        return OmicMatrixSample.SampleRole.TUMOR
    if role == OmicMatrixSample.SampleRole.NORMAL:
        return OmicMatrixSample.SampleRole.NORMAL
    return OmicMatrixSample.SampleRole.UNKNOWN
