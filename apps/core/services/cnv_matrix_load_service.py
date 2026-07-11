"""
CnvMatrixLoadService — orquestração Django da carga da matriz CNV CPTAC CCRCC.

OmnisPathway Objetivo 2, Fase 2, Slice CNV-seed (materialização da matriz).

Responsabilidade:
  1. Resolver/criar OmicDataset CNV (source_db='cptac', accession='CPTAC-CCRCC-CNV',
     access_type='public', omics_layers=['copy_number']).
  2. Gate de idempotência: não duplicar OmicMatrix nem job ativo de CNV_MATRIX_LOAD.
  3. Criar IngestionJob(job_type=CNV_MATRIX_LOAD).
  4. Chamar rust_engine.load_cnv_matrix(url, dest_dir, parquet_name)
     → CnvMatrixManifest{parquet_path, checksum_md5, n_features, n_samples,
       sample_columns:[{case_id, sample_role='tumor', column_index}]}.
     O Rust NÃO toca PG, NÃO faz upload.
  5. Upload do Parquet via shared_omics_storage_key (dado público).
  6. Criar OmicMatrix + OmicSample (get_or_create por accession) +
     OmicMatrixSample (bulk_create ignore_conflicts) em transação atômica.
  7. Limpeza do tmp local. Marcar IngestionJob COMPLETED.

Fronteira de camadas (Regra #1 / django-rust-boundary):
  - Rust: fetch HTTP do .cct (gene×amostra log-ratio), parse streaming,
    escrita do Parquet local, MD5 incremental. NÃO toca PG, NÃO faz upload.
  - Django: upload via default_storage, ORM set-based, isolamento por projeto.
    NÃO abre o Parquet, NÃO parseia o .cct.

Padrão reutilizado de MatrixLoadService (Fase 0):
  - _get_or_create_dataset / _check_idempotency / _upload_parquet / _persist_orm:
    mesmos contratos; adaptados para CNV (camada=copy_number, formato=log_ratio,
    amostras tumor-only, dataset accession CNV, source_db=cptac).
  - shared_omics_storage_key: dado público → namespace _shared.
  - OmicSample.accession = f"{case_id}_{role}" (mesmo padrão da Fase 0).
  - OmicMatrixSample: bulk_create ignore_conflicts (idempotência).
  - OmicMatrix natural key: (dataset, omics_layer, feature_axis, loader_version).

Isolamento (firebase-auth-guard / Regra #3):
  - project deve pertencer ao request.user antes de chegar aqui.
  - OmicDataset é tabela compartilhada — isolamento via ProjectDataset.

Sensitive-data-handling:
  - URL é pública (LinkedOmics, acesso aberto) — registrada em parameters para
    auditoria, mas NÃO é credencial.
  - db_url NUNCA logada nem gravada em IngestionJob.parameters.
  - storage_key é chave LÓGICA — nunca caminho físico absoluto nos logs.

Decisão de granularidade de OmicSample (CNV):
  UM OmicSample por (case_id, sample_role='tumor').
  O loader CNV retorna apenas amostras tumor (conforme o contrato do loader
  Rust). accession = f"{case_id}_tumor" — garante unicidade sem colidir com
  samples do proteoma (accession="C3L-00004_tumor" é o mesmo para ambas as
  matrizes, o que é CORRETO: o OmicSample representa a amostra biológica, não
  a medição). get_or_create idempotente pelo accession.
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

# Constantes do dataset CNV CPTAC CCRCC (acesso público, sem credencial)
CNV_CCRCC_ACCESSION = 'CPTAC-CCRCC-CNV'
CNV_URL = (
    'https://www.linkedomics.org/data_download/CPTAC-CCRCC/'
    'HS_CPTAC_CCRCC_CNV_gene_Tumor.cct'
)
CNV_PARQUET_NAME = 'cptac_ccrcc_cnv.parquet'

# Versão do loader: atualizar quando o contrato Rust mudar.
LOADER_VERSION = '0.1.0-cptac-ccrcc-cnv'


class CnvMatrixAlreadyLoadedError(Exception):
    """Levantado quando OmicMatrix já existe para a natural key (idempotência)."""

    def __init__(self, matrix: OmicMatrix):
        self.matrix = matrix
        super().__init__(
            f"OmicMatrix CNV já existe para dataset={matrix.dataset.accession}, "
            f"layer={matrix.omics_layer}, axis={matrix.feature_axis}, "
            f"loader_version={matrix.loader_version} (id={matrix.id})"
        )


class CnvMatrixJobActiveError(Exception):
    """Levantado quando já há um CNV_MATRIX_LOAD pending/running para o dataset+projeto."""

    def __init__(self, job: IngestionJob):
        self.job = job
        super().__init__(
            f"CNV_MATRIX_LOAD já ativo (job {job.id}, status={job.status}) "
            f"para projeto {job.project_id} — dispatch ignorado (idempotência)"
        )


class CnvMatrixLoadService:
    """
    Serviço central de carga da matriz CNV CPTAC CCRCC.

    Reutiliza o padrão estabelecido por MatrixLoadService (Fase 0):
    criação de OmicDataset/OmicSample/OmicMatrix/OmicMatrixSample,
    upload Parquet via default_storage, shared_omics_storage_key para dado
    público, idempotência, isolamento por projeto, IngestionJob.

    Diferenças vs MatrixLoadService:
      - omics_layer='copy_number' (vs 'proteomic')
      - data_format_level='log_ratio' (vs 'intensities')
      - Amostras todas tumor (sample_role='tumor' — CNV tumor-only)
      - Uma URL de origem (vs duas URLs tumor/normal da Fase 0)
      - job_type=CNV_MATRIX_LOAD (vs MATRIX_LOAD)

    Uso síncrono (management command / bancada de prova):
        service = CnvMatrixLoadService(project)
        result = service.run()

    Uso assíncrono (Celery):
        job = CnvMatrixLoadService.dispatch(project)
        # task run_cnv_matrix_load(job.id, str(project.id)) prossegue em background
    """

    OMICS_LAYER = 'copy_number'
    DATA_FORMAT_LEVEL = OmicMatrix.DataFormatLevel.LOG_RATIO
    FEATURE_AXIS = OmicMatrix.FeatureAxis.GENE

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
        a task Celery (via run_cnv_matrix_load).

        Returns:
            IngestionJob recém-criado (status=PENDING).

        Raises:
            CnvMatrixAlreadyLoadedError: matriz já existe para esta natural key.
            CnvMatrixJobActiveError: job ativo para este dataset+projeto.
        """
        from apps.core.tasks.ingestion_tasks import run_cnv_matrix_load

        service = cls(project)
        dataset = service._get_or_create_dataset()
        service._check_idempotency(dataset)

        job = IngestionJob.objects.create(
            project=project,
            job_type=IngestionJob.JobType.CNV_MATRIX_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={
                'dataset_accession': dataset.accession,
                'dataset_id': dataset.id,
                'omics_layer': cls.OMICS_LAYER,
                'feature_axis': cls.FEATURE_AXIS,
                'loader_version': LOADER_VERSION,
                # URL pública — registrada para auditoria, não é credencial
                'cnv_url': CNV_URL,
                # db_url NÃO é armazenada (sensitive-data-handling)
            },
        )

        try:
            run_cnv_matrix_load.delay(str(job.id), str(project.id))
            logger.info(
                'CNV_MATRIX_LOAD disparado (job %s) para projeto %s / dataset %s',
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
          1. Resolve/cria OmicDataset CNV (idempotente).
          2. Gate de idempotência (matriz/job ativo).
          3. Cria/atualiza IngestionJob para RUNNING.
          4. Chama rust_engine.load_cnv_matrix → manifest.
          5. Upload do Parquet via shared_omics_storage_key.
          6. Cria OmicMatrix + OmicSample + OmicMatrixSample (transação).
          7. Limpa tmp local. Marca job COMPLETED.

        Args:
            job: IngestionJob existente (criado pelo dispatch); se None,
                 cria internamente (modo síncrono direto do management command).

        Returns:
            dict com n_features, n_samples, storage_key, job_id, checksum_md5.
        """
        import rust_engine

        dataset = self._get_or_create_dataset()

        # Gate de idempotência apenas no modo síncrono direto (sem job pre-criado)
        if job is None:
            try:
                self._check_idempotency(dataset)
            except (CnvMatrixAlreadyLoadedError, CnvMatrixJobActiveError):
                raise

        # Garante job existente
        if job is None:
            job = IngestionJob.objects.create(
                project=self.project,
                job_type=IngestionJob.JobType.CNV_MATRIX_LOAD,
                status=IngestionJob.JobStatus.RUNNING,
                parameters={
                    'dataset_accession': dataset.accession,
                    'dataset_id': dataset.id,
                    'omics_layer': self.OMICS_LAYER,
                    'feature_axis': self.FEATURE_AXIS,
                    'loader_version': LOADER_VERSION,
                    'cnv_url': CNV_URL,
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
        Resolve ou cria a casca OmicDataset para o CNV CPTAC CCRCC.

        OmicDataset é tabela COMPARTILHADA — o get_or_create usa o accession
        como natural key. O isolamento por projeto é via ProjectDataset.

        access_type='public': dado LinkedOmics público — Parquet vai para
        namespace compartilhado via shared_omics_storage_key.
        """
        dataset, created = OmicDataset.objects.get_or_create(
            accession=CNV_CCRCC_ACCESSION,
            defaults={
                'source_db': OmicDataset.SourceDB.CPTAC,
                'title': 'CPTAC CCRCC Discovery Study — CNV Gene-Level (LinkedOmics)',
                'summary': (
                    'Clear cell renal cell carcinoma (CCRCC) copy number variation '
                    '(CNV) at gene level from the Clinical Proteomic Tumor Analysis '
                    'Consortium (CPTAC) Discovery Study. Tumor samples only. '
                    'Log-ratio format. Source: LinkedOmics / PDC.'
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
                    'data_source': 'LinkedOmics',
                    'cnv_url': CNV_URL,
                    'note': (
                        'CNV tumor-only (gene×amostra log-ratio). '
                        'Paired normal não disponível para CNV neste dataset.'
                    ),
                },
            },
        )

        if created:
            logger.info(
                'OmicDataset CNV criado: accession=%s, source_db=cptac',
                CNV_CCRCC_ACCESSION,
            )
        else:
            logger.info(
                'OmicDataset CNV resolvido (existente): accession=%s',
                CNV_CCRCC_ACCESSION,
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
            CnvMatrixAlreadyLoadedError: matriz já carregada.
            CnvMatrixJobActiveError: job ativo encontrado.
        """
        # Gate 1: matriz já existe para esta natural key?
        existing_matrix = OmicMatrix.objects.filter(
            dataset=dataset,
            omics_layer=self.OMICS_LAYER,
            feature_axis=self.FEATURE_AXIS,
            loader_version=LOADER_VERSION,
        ).first()
        if existing_matrix:
            raise CnvMatrixAlreadyLoadedError(existing_matrix)

        # Gate 2: job ativo para este dataset+projeto?
        active_job = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.CNV_MATRIX_LOAD,
            status__in=[
                IngestionJob.JobStatus.PENDING,
                IngestionJob.JobStatus.RUNNING,
            ],
            parameters__dataset_id=dataset.id,
        ).first()
        if active_job:
            raise CnvMatrixJobActiveError(active_job)

    def _execute(self, dataset: OmicDataset, job: IngestionJob, rust_engine) -> dict:
        """
        Núcleo da carga: Rust → manifest → upload → ORM set-based.

        rust_engine passado explicitamente para facilitar mock em testes.
        """
        with tempfile.TemporaryDirectory(prefix='davinci_cnv_') as dest_dir:
            # ── Passo 1: Rust carrega e escreve Parquet local ─────────────────
            logger.info(
                'Chamando rust_engine.load_cnv_matrix (job %s, dest_dir omitido)',
                job.id,
            )
            manifest = rust_engine.load_cnv_matrix(
                url=CNV_URL,
                dest_dir=dest_dir,
                parquet_name=CNV_PARQUET_NAME,
            )
            logger.info(
                'load_cnv_matrix concluído (job %s): n_features=%d, n_samples=%d',
                job.id,
                manifest.n_features,
                manifest.n_samples,
            )

            # ── Passo 2: Upload do Parquet via shared_omics_storage_key ───────
            # Dado público → namespace compartilhado (_shared), não por projeto.
            storage_key = self._upload_parquet(manifest.parquet_path, dataset, job)

            # ── Passo 3: ORM set-based dentro de transação atômica ────────────
            matrix = self._persist_orm(
                dataset=dataset,
                manifest=manifest,
                storage_key=storage_key,
            )

        # Limpeza do tmp já foi feita ao sair do TemporaryDirectory

        # ── Passo 4: Marca job COMPLETED ──────────────────────────────────────
        IngestionJob.objects.filter(id=job.id).update(
            status=IngestionJob.JobStatus.COMPLETED,
            records_processed=manifest.n_features,
            records_inserted=manifest.n_samples,
            completed_at=timezone.now(),
        )

        logger.info(
            'CNV_MATRIX_LOAD concluído (job %s): storage_key=%s, '
            'n_features=%d, n_samples=%d',
            job.id,
            storage_key,
            manifest.n_features,
            manifest.n_samples,
        )

        return {
            'job_id': str(job.id),
            'storage_key': storage_key,
            'n_features': manifest.n_features,
            'n_samples': manifest.n_samples,
            'checksum_md5': manifest.checksum_md5,
        }

    def _upload_parquet(
        self,
        local_path: str,
        dataset: OmicDataset,
        job: IngestionJob,
    ) -> str:
        """
        Faz upload do Parquet local para default_storage.

        CNV é dado público (access_type='public') → namespace compartilhado:
          omics/_shared/{dataset_accession}/{filename}

        Retorna a storage_key lógica (nunca caminho físico).
        Remove o arquivo local após upload bem-sucedido.
        """
        filename = os.path.basename(local_path)

        # CNV é sempre público — shared namespace (mesmo padrão da Fase 0).
        # Se o acesso mudar no futuro, use omics_storage_key com user/project.
        object_key = shared_omics_storage_key(
            dataset_accession=dataset.accession,
            filename=filename,
        )

        logger.info(
            'Fazendo upload do Parquet CNV para default_storage '
            '(storage_key lógica, job %s)',
            job.id,
        )

        with open(local_path, 'rb') as f:
            saved_key = default_storage.save(object_key, File(f))

        # Remove local apenas após upload bem-sucedido
        try:
            os.remove(local_path)
        except OSError as err:
            logger.warning(
                'Falha ao remover Parquet CNV local após upload (job %s): %s',
                job.id,
                err,
            )

        logger.info(
            'Upload do Parquet CNV concluído (job %s): storage_key=%s',
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

        Granularidade de OmicSample: UM por (case_id, 'tumor').
        O loader CNV é tumor-only — sample_role='tumor' para todas as amostras.
        accession = f"{case_id}_tumor" — mesmo padrão da Fase 0.

        OmicMatrix.omics_layer='copy_number', data_format_level='log_ratio',
        feature_axis='gene' — conforme contrato do schema CNV (cartografo 0030).

        Returns:
            OmicMatrix criada/resolvida.
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
                    'source_pointer': CNV_URL,
                    'storage_key': storage_key,
                    'n_features': manifest.n_features,
                    'n_samples': manifest.n_samples,
                    'checksum_md5': manifest.checksum_md5,
                },
            )

            if not matrix_created:
                # Atualiza storage_key e dimensões caso tenha mudado (re-carga)
                OmicMatrix.objects.filter(id=matrix.id).update(
                    storage_key=storage_key,
                    n_features=manifest.n_features,
                    n_samples=manifest.n_samples,
                    checksum_md5=manifest.checksum_md5,
                )
                matrix.refresh_from_db()

            # ── OmicSample: um por (case_id, 'tumor') ─────────────────────────
            # O loader CNV retorna sample_role='tumor' para todas as amostras.
            # Mapeia (case_id, role) → column_index para get_or_create eficiente.
            sample_columns = manifest.sample_columns

            col_map: dict[tuple[str, str], int] = {}
            for col in sample_columns:
                key = (col.case_id, col.sample_role)
                if key not in col_map:
                    col_map[key] = col.column_index

            # get_or_create OmicSample para cada (case_id, 'tumor')
            # accession = f"{case_id}_tumor" — idêntico ao padrão da Fase 0.
            # Se o OmicSample já foi criado pelo proteoma, get_or_create resolve
            # o mesmo accession sem duplicar (biologicamente é a mesma amostra).
            sample_by_key: dict[tuple[str, str], OmicSample] = {}

            for (case_id, role), _col_idx in col_map.items():
                accession = f"{case_id}_{role}"
                sample, _ = OmicSample.objects.get_or_create(
                    accession=accession,
                    defaults={
                        'dataset': dataset,
                        'title': f'{case_id} — {role} (CPTAC CCRCC CNV)',
                        'organism': 'Homo sapiens',
                        'characteristics': {
                            'case_id': case_id,
                            'sample_role': role,
                            'study': 'CPTAC-CCRCC',
                            'omic_type': 'copy_number',
                        },
                    },
                )
                sample_by_key[(case_id, role)] = sample

            # ── OmicMatrixSample: bulk_create com ignore_conflicts ─────────────
            oms_to_create = []
            for (case_id, role), col_idx in col_map.items():
                sample = sample_by_key[(case_id, role)]
                role_value = OmicMatrixSample.SampleRole.TUMOR  # CNV tumor-only
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
                    ignore_conflicts=True,  # idempotência: re-rodar não duplica
                )
                logger.info(
                    'OmicMatrixSample CNV bulk_create: %d linhas (matrix %s)',
                    len(oms_to_create),
                    matrix.id,
                )

        return matrix
