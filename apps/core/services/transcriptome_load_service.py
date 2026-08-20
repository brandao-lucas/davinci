"""
TranscriptomeLoadService — orquestração Django da carga da matriz de
transcriptoma (RNA-seq FPKM log2) CPTAC CCRCC.

OmnisPathway Objetivo 2 — camada ômica faltante (mRNA). Insumo de duas
frentes: Regra 2 do motor PFS (footprint de fator de transcrição, hoje lido
sobre proteína) e análise comparativa mRNA×proteína por gene.

Responsabilidade:
  1. Resolver/criar OmicDataset para o transcriptoma CPTAC CCRCC.
  2. Gate de idempotência: não duplicar OmicMatrix nem job ativo de
     TRANSCRIPTOME_MATRIX_LOAD.
  3. Criar IngestionJob(job_type=TRANSCRIPTOME_MATRIX_LOAD_JOB_TYPE).
  4. Chamar rust_engine.load_cptac_matrix(tumor_url, normal_url, dest_dir,
     parquet_name) → CptacMatrixManifest{parquet_path, checksum_md5,
     n_features, n_samples, sample_columns, genes_discarded}. Reúso direto:
     o loader Rust já é totalmente parametrizado por URL — nenhuma mudança
     em rust_src/ foi necessária (confirmado na discovery desta tarefa).
     O Rust NÃO toca PG, NÃO faz upload.
  5. Upload do Parquet via shared_omics_storage_key (dado público).
  6. Criar OmicMatrix + OmicSample (get_or_create — REÚSA amostras CCRCC
     já carregadas pelas cargas de proteoma/fosfo/CNV/MAF) + OmicMatrixSample
     (bulk_create ignore_conflicts) em transação atômica.
  7. Limpeza do tmp local. Marcar IngestionJob COMPLETED.

Fronteira de camadas (Regra #1 / django-rust-boundary):
  - Rust: download HTTP dos arquivos .cct (gene × amostra), parse streaming,
    escrita do Parquet local, MD5 incremental. NÃO toca PG, NÃO faz upload.
  - Django: upload via default_storage, ORM set-based, isolamento por
    projeto. NÃO abre o Parquet, NÃO parseia .cct.

Reúso de amostras:
  OmicSample.accession = f"{case_id}_{role}" (mesmo padrão das demais
  camadas CCRCC). get_or_create é idempotente; amostras CCRCC já carregadas
  (tumor/normal) são reutilizadas sem criar duplicatas. `case_id` gravado em
  `characteristics` é o valor nu (ex.: `C3L-00004`) — via primária usada por
  `pair_samples` (NK de OmicSamplePairing é (matrix, case_id); pareamento
  desta matriz é por matriz e NÃO é feito por este service).

Isolamento (firebase-auth-guard / Regra #3):
  - project deve pertencer ao request.user antes de chegar aqui.
  - OmicDataset é tabela compartilhada (uma linha por estudo). Isolamento
    via ProjectDataset.
  - OmicSample é tabela compartilhada (reúso por accession).

Sensitive-data-handling:
  - URLs são públicas (LinkedOmics, acesso aberto). Não há credencial.
  - db_url NUNCA logada nem gravada em IngestionJob.parameters.
  - storage_key é chave LÓGICA — nunca caminho físico absoluto nos logs.

job_type (pendência de schema — NÃO resolvida neste módulo):
  IngestionJob.JobType ainda não tem um valor dedicado para carga de
  transcriptoma (verificado nesta tarefa: só existem MATRIX_LOAD,
  CNV_MATRIX_LOAD, PHOSPHO_MATRIX_LOAD, SOMATIC_MAF_LOAD, etc.). Seguindo o
  padrão de um job_type por camada ômica já estabelecido nesta base
  (migrations 0025/0032/0033 alteraram job_type a cada camada nova), este
  módulo usa a string literal TRANSCRIPTOME_MATRIX_LOAD_JOB_TYPE em vez de
  IngestionJob.JobType.TRANSCRIPTOME_MATRIX_LOAD (que não existe ainda).
  Não há CheckConstraint de banco sobre job_type — a string funciona em
  runtime sem migration. cartografo deve adicionar
  `TRANSCRIPTOME_MATRIX_LOAD = 'transcriptome_matrix_load'` a
  IngestionJob.JobType (migration alter_ingestionjob_job_type) para que o
  valor apareça no admin/choices; depois, trocar a string literal aqui pela
  referência ao enum.

data_format_level (pendência de schema — em paralelo, cartografo migration
  0040): este módulo assume o valor `'fpkm_log2'` em
  OmicMatrix.DataFormatLevel, adicionado em paralelo pelo cartografo. Se o
  nome final escolhido for outro, ajustar DATA_FORMAT_LEVEL abaixo.

Padrão reutilizado de MatrixLoadService / PhosphoMatrixLoadService:
  - _get_or_create_dataset / _check_idempotency / _upload_parquet /
    _persist_orm: mesmos contratos; adaptados para transcriptoma.
  - shared_omics_storage_key: dado público → namespace _shared.
  - OmicSample.accession = f"{case_id}_{role}" (mesmo padrão das demais fases).
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

# Constantes do transcriptoma CPTAC CCRCC (acesso público, gene-level RNA-seq)
TRANSCRIPTOME_CCRCC_ACCESSION = 'CPTAC-CCRCC-TRANSCRIPTOME'
TRANSCRIPTOME_TUMOR_URL = (
    'https://www.linkedomics.org/data_download/CPTAC-CCRCC/'
    'HS_CPTAC_CCRCC_RNAseq_fpkm_log2_Tumor.cct'
)
TRANSCRIPTOME_NORMAL_URL = (
    'https://www.linkedomics.org/data_download/CPTAC-CCRCC/'
    'HS_CPTAC_CCRCC_RNAseq_fpkm_log2_Normal.cct'
)
TRANSCRIPTOME_PARQUET_NAME = 'cptac_ccrcc_transcriptome.parquet'

# Versão do loader: atualizar quando o contrato Rust mudar de forma incompatível.
LOADER_VERSION = 'rnaseq-v1'

# job_type — string literal até cartografo adicionar o choice dedicado
# (ver docstring do módulo). NÃO referenciar IngestionJob.JobType aqui.
TRANSCRIPTOME_MATRIX_LOAD_JOB_TYPE = 'transcriptome_matrix_load'

# data_format_level — string literal até cartografo confirmar/migrar o
# choice `fpkm_log2` em OmicMatrix.DataFormatLevel (migration 0040, em
# paralelo — ver docstring do módulo).
TRANSCRIPTOME_DATA_FORMAT_LEVEL = 'fpkm_log2'

# Statuses de ProjectDataset considerados ativos (consistência com as demais
# camadas CCRCC).
_ACTIVE_STATUSES = (
    ProjectDataset.CurationStatus.INCLUDED,
    ProjectDataset.CurationStatus.QUEUED_DOWNLOAD,
    ProjectDataset.CurationStatus.DOWNLOADED,
    ProjectDataset.CurationStatus.PENDING,
)


class TranscriptomeMatrixAlreadyLoadedError(Exception):
    """Levantado quando OmicMatrix de transcriptoma já existe para a natural key."""

    def __init__(self, matrix: OmicMatrix):
        self.matrix = matrix
        super().__init__(
            f"OmicMatrix de transcriptoma já existe: dataset="
            f"{matrix.dataset.accession}, layer={matrix.omics_layer}, "
            f"axis={matrix.feature_axis}, loader_version={matrix.loader_version} "
            f"(id={matrix.id})"
        )


class TranscriptomeMatrixJobActiveError(Exception):
    """Levantado quando já há um TRANSCRIPTOME_MATRIX_LOAD pending/running."""

    def __init__(self, job: IngestionJob):
        self.job = job
        super().__init__(
            f"TRANSCRIPTOME_MATRIX_LOAD já ativo (job {job.id}, status={job.status}) "
            f"para projeto {job.project_id} — dispatch ignorado (idempotência)"
        )


class TranscriptomeLoadService:
    """
    Serviço de carga da matriz de transcriptoma (RNA-seq FPKM log2) CPTAC CCRCC.

    Uso síncrono (management command / bancada de prova):
        service = TranscriptomeLoadService(project)
        result = service.run()

    Uso assíncrono (Celery):
        job = TranscriptomeLoadService.dispatch(project)
        # task run_transcriptome_load(job.id, str(project.id)) em background

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

    OMICS_LAYER = 'transcriptomic'
    FEATURE_AXIS = OmicMatrix.FeatureAxis.GENE
    DATA_FORMAT_LEVEL = TRANSCRIPTOME_DATA_FORMAT_LEVEL

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
        a task Celery (via run_transcriptome_load).

        Returns:
            IngestionJob recém-criado (status=PENDING).

        Raises:
            TranscriptomeMatrixAlreadyLoadedError: matriz já carregada.
            TranscriptomeMatrixJobActiveError: job ativo encontrado.
        """
        from apps.core.tasks.ingestion_tasks import run_transcriptome_load

        service = cls(project)
        dataset = service._get_or_create_dataset()
        service._check_idempotency(dataset)

        job = IngestionJob.objects.create(
            project=project,
            job_type=TRANSCRIPTOME_MATRIX_LOAD_JOB_TYPE,
            status=IngestionJob.JobStatus.PENDING,
            parameters={
                'dataset_accession': dataset.accession,
                'dataset_id': dataset.id,
                'omics_layer': cls.OMICS_LAYER,
                'feature_axis': cls.FEATURE_AXIS,
                'loader_version': LOADER_VERSION,
                # URLs públicas — não são credenciais; registradas para auditoria
                'tumor_url': TRANSCRIPTOME_TUMOR_URL,
                'normal_url': TRANSCRIPTOME_NORMAL_URL,
                # db_url NÃO é armazenada (sensitive-data-handling)
            },
        )

        try:
            run_transcriptome_load.delay(str(job.id), str(project.id))
            logger.info(
                'TRANSCRIPTOME_MATRIX_LOAD disparado (job %s) para projeto %s / '
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
          4. Chama rust_engine.load_cptac_matrix → manifest.
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
            except (
                TranscriptomeMatrixAlreadyLoadedError,
                TranscriptomeMatrixJobActiveError,
            ):
                raise

        if job is None:
            job = IngestionJob.objects.create(
                project=self.project,
                job_type=TRANSCRIPTOME_MATRIX_LOAD_JOB_TYPE,
                status=IngestionJob.JobStatus.RUNNING,
                parameters={
                    'dataset_accession': dataset.accession,
                    'dataset_id': dataset.id,
                    'omics_layer': self.OMICS_LAYER,
                    'feature_axis': self.FEATURE_AXIS,
                    'loader_version': LOADER_VERSION,
                    'tumor_url': TRANSCRIPTOME_TUMOR_URL,
                    'normal_url': TRANSCRIPTOME_NORMAL_URL,
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
        Resolve ou cria OmicDataset do transcriptoma CPTAC CCRCC.

        OmicDataset é tabela COMPARTILHADA — lookup key = accession.
        Isolamento por projeto via ProjectDataset.
        """
        dataset, created = OmicDataset.objects.get_or_create(
            accession=TRANSCRIPTOME_CCRCC_ACCESSION,
            defaults={
                'source_db': OmicDataset.SourceDB.CPTAC,
                'title': 'CPTAC CCRCC Discovery Study — Transcriptome RNA-seq FPKM log2 (LinkedOmics)',
                'summary': (
                    'Clear cell renal cell carcinoma (CCRCC) transcriptome '
                    '(RNA-seq, FPKM log2) from the Clinical Proteomic Tumor '
                    'Analysis Consortium (CPTAC) Discovery Study. Gene-level '
                    'expression, tumor vs. normal paired samples. Source: '
                    'LinkedOmics / PDC.'
                ),
                'omic_type': OmicDataset.OmicType.TRANSCRIPTOMIC,
                'organism': 'Homo sapiens',
                'omics_layers': [self.OMICS_LAYER],
                'omics_count': 1,
                'has_control_group': OmicDataset.ControlGroup.YES,
                'access_type': OmicDataset.AccessType.PUBLIC,
                'is_single_cell': OmicDataset.SingleCell.BULK,
                'data_format': OmicDataset.DataFormat.PROCESSED,
                'extra_metadata': {
                    'cptac_study': 'CCRCC',
                    'data_level': 'gene-level RNA-seq FPKM log2',
                    'data_source': 'LinkedOmics',
                    'tumor_url': TRANSCRIPTOME_TUMOR_URL,
                    'normal_url': TRANSCRIPTOME_NORMAL_URL,
                },
            },
        )

        if created:
            logger.info(
                'OmicDataset transcriptoma criado: accession=%s',
                TRANSCRIPTOME_CCRCC_ACCESSION,
            )
        else:
            logger.info(
                'OmicDataset transcriptoma resolvido (existente): accession=%s',
                TRANSCRIPTOME_CCRCC_ACCESSION,
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
            TranscriptomeMatrixAlreadyLoadedError: matriz já carregada.
            TranscriptomeMatrixJobActiveError: job ativo encontrado.
        """
        existing_matrix = OmicMatrix.objects.filter(
            dataset=dataset,
            omics_layer=self.OMICS_LAYER,
            feature_axis=self.FEATURE_AXIS,
            loader_version=LOADER_VERSION,
        ).first()
        if existing_matrix:
            raise TranscriptomeMatrixAlreadyLoadedError(existing_matrix)

        active_job = IngestionJob.objects.filter(
            project=self.project,
            job_type=TRANSCRIPTOME_MATRIX_LOAD_JOB_TYPE,
            status__in=[
                IngestionJob.JobStatus.PENDING,
                IngestionJob.JobStatus.RUNNING,
            ],
            parameters__dataset_id=dataset.id,
        ).first()
        if active_job:
            raise TranscriptomeMatrixJobActiveError(active_job)

    def _execute(
        self,
        dataset: OmicDataset,
        job: IngestionJob,
        rust_engine,
    ) -> dict:
        """
        Núcleo da carga: Rust → manifest → upload → ORM set-based.

        Parâmetro rust_engine passado explicitamente para facilitar mock em testes.

        Reúso direto do loader Rust do proteoma (Fase 0) — load_cptac_matrix
        é totalmente parametrizado por URL, sem qualquer mudança em rust_src/.
        """
        with tempfile.TemporaryDirectory(prefix='davinci_transcriptome_') as dest_dir:
            # ── Passo 1: Rust baixa e grava Parquet local ─────────────────────
            logger.info(
                'Chamando rust_engine.load_cptac_matrix para transcriptoma (job %s)',
                job.id,
            )
            manifest = rust_engine.load_cptac_matrix(
                tumor_url=TRANSCRIPTOME_TUMOR_URL,
                normal_url=TRANSCRIPTOME_NORMAL_URL,
                dest_dir=dest_dir,
                parquet_name=TRANSCRIPTOME_PARQUET_NAME,
            )
            logger.info(
                'load_cptac_matrix (transcriptoma) concluído: n_features=%d, '
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
            'TRANSCRIPTOME_MATRIX_LOAD concluído (job %s): storage_key=<omitida>, '
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
            'Fazendo upload do Parquet de transcriptoma para default_storage '
            '(job %s)',
            job.id,
        )

        with open(local_path, 'rb') as f:
            saved_key = default_storage.save(object_key, File(f))

        try:
            os.remove(local_path)
        except OSError as err:
            logger.warning(
                'Falha ao remover Parquet de transcriptoma local após upload '
                '(job %s): %s',
                job.id,
                err,
            )

        logger.info(
            'Upload do Parquet de transcriptoma concluído (job %s): '
            'storage_key=<omitida>',
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
        pelas demais camadas (proteoma/fosfo/CNV/MAF) — get_or_create
        idempotente pelo accession.
        """
        source_pointer = f"{TRANSCRIPTOME_TUMOR_URL} | {TRANSCRIPTOME_NORMAL_URL}"

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
                # get_or_create: reutiliza amostras CCRCC já carregadas
                # (proteoma/fosfo/CNV/MAF)
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
                    'OmicMatrixSample transcriptoma bulk_create: %d linhas '
                    '(matrix %s)',
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
