"""
MatrixLoadService — orquestração Django do carregamento de matriz CPTAC.

OmnisPathway Objetivo 2, Fase 0, passo 0.3.

Responsabilidade:
  1. Resolver/criar a casca OmicDataset para o estudo CPTAC dentro do projeto.
  2. Gate de idempotência: não duplicar job ou OmicMatrix ativa.
  3. Criar IngestionJob(job_type=MATRIX_LOAD).
  4. Chamar rust_engine.load_cptac_matrix → CptacMatrixManifest.
  5. Upload do Parquet via default_storage; gravar storage_key lógica.
  6. Criar OmicMatrix + OmicSample (get_or_create) + OmicMatrixSample (bulk)
     dentro de uma transação set-based.
  7. Limpeza do tmp local. Marcar IngestionJob COMPLETED.

Fronteira de camadas (Regra #1 / django-rust-boundary):
  - Rust: download HTTP das duas URLs CCT, parse streaming, escrita do Parquet
    local, MD5 incremental.  NÃO toca Postgres, NÃO faz upload.
  - Django: upload via default_storage, ORM set-based, isolamento por projeto.

Isolamento (firebase-auth-guard / Regra #3):
  - project deve pertencer ao request.user antes de chegar aqui (verificado
    na view / management command antes do dispatch).
  - OmicDataset é tabela compartilhada (uma linha por estudo); o isolamento
    é via ProjectDataset (vínculo projeto↔dataset).  Nenhum queryset cruza
    projetos distintos.

Sensitive-data-handling:
  - Nenhuma credencial relevante neste fluxo: as URLs de origem são públicas
    (LinkedOmics, acesso aberto).  Mesmo assim, db_url nunca é logada.
  - storage_key é chave LÓGICA — nunca caminho físico absoluto nos logs.

Decisão de granularidade de OmicSample:
  UM OmicSample por (case_id, sample_role).
  Justificativa: OmicSample.accession é UNIQUE no banco (constraint única, não
  por dataset).  O mesmo case_id ("C3L-00004") aparece com papéis distintos
  (tumor e normal) — são amostras biologicamente diferentes (tecido diferente,
  aliquota diferente).  Como OmicSample não tem campo de sample_role próprio
  (esse campo vive em OmicMatrixSample, que é a tabela-ponte), a forma mais
  limpa de evitar colisão de accession é usar accession = f"{case_id}_{role}"
  (ex: "C3L-00004_tumor").  Assim:
    - Cada OmicSample mapeia exatamente uma aliquota (tumor OU normal);
    - OmicMatrixSample liga a OmicSample ao índice de coluna + role (redundante
      mas explícito — facilita consultas "amostras tumor desta matriz");
    - Idempotência por get_or_create(accession=f"{case_id}_{role}") é direta.
  Alternativa descartada: um OmicSample por case_id → colidiria na constraint
  UNIQUE de accession se o mesmo case_id entrar com dois papéis distintos.

source_db:
  OmicDataset.SourceDB.CPTAC = 'cptac' foi adicionado pelo cartografo via
  migration 0028.  O service usa OmicDataset.SourceDB.CPTAC diretamente.
  Linhas placeholder antigas com source_db='tcga' (se existirem) são corrigidas
  para 'cptac' no _get_or_create_dataset quando resolvidas pelo accession.
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
from apps.core.storage_utils import omics_storage_key

logger = logging.getLogger(__name__)

# Constantes do estudo alvo (CPTAC CCRCC Discovery Proteome, acesso público)
CPTAC_CCRCC_ACCESSION = 'CPTAC-CCRCC-PROTEOME'
CPTAC_TUMOR_URL = (
    'https://www.linkedomics.org/data_download/CPTAC-CCRCC/'
    'HS_CPTAC_CCRCC_proteome_Tumor.cct'
)
CPTAC_NORMAL_URL = (
    'https://www.linkedomics.org/data_download/CPTAC-CCRCC/'
    'HS_CPTAC_CCRCC_proteome_Normal.cct'
)
CPTAC_PARQUET_NAME = 'cptac_ccrcc_proteome.parquet'

# Versão do loader: deve ser atualizada quando o loader Rust mudar o contrato
# ou o formato de parse de forma incompatível.
LOADER_VERSION = '0.1.0-cptac-ccrcc-proteome'


class MatrixAlreadyLoadedError(Exception):
    """Levantado quando OmicMatrix já existe para a natural key (idempotência)."""

    def __init__(self, matrix: OmicMatrix):
        self.matrix = matrix
        super().__init__(
            f"OmicMatrix já existe para dataset={matrix.dataset.accession}, "
            f"layer={matrix.omics_layer}, axis={matrix.feature_axis}, "
            f"loader_version={matrix.loader_version} (id={matrix.id})"
        )


class MatrixJobActiveError(Exception):
    """Levantado quando já há um MATRIX_LOAD pending/running para o dataset+projeto."""

    def __init__(self, job: IngestionJob):
        self.job = job
        super().__init__(
            f"MATRIX_LOAD já ativo (job {job.id}, status={job.status}) "
            f"para projeto {job.project_id} — dispatch ignorado (idempotência)"
        )


class MatrixLoadService:
    """
    Serviço central de carga de matriz CPTAC.

    Uso síncrono (management command / bancada de prova):
        service = MatrixLoadService(project)
        result = service.run()

    Uso assíncrono (Celery):
        job = MatrixLoadService.dispatch(project)
        # task run_matrix_load(job.id, str(project.id)) prossegue em background
    """

    OMICS_LAYER = 'proteomic'
    DATA_FORMAT_LEVEL = OmicMatrix.DataFormatLevel.INTENSITIES
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
        a task Celery (via run_matrix_load).

        Returns:
            IngestionJob recém-criado (status=PENDING).

        Raises:
            MatrixAlreadyLoadedError: matriz já existe para esta natural key.
            MatrixJobActiveError: job ativo para este dataset+projeto.
        """
        from apps.core.tasks.ingestion_tasks import run_matrix_load

        service = cls(project)
        dataset = service._get_or_create_dataset()
        service._check_idempotency(dataset)

        job = IngestionJob.objects.create(
            project=project,
            job_type=IngestionJob.JobType.MATRIX_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={
                'dataset_accession': dataset.accession,
                'dataset_id': dataset.id,
                'omics_layer': cls.OMICS_LAYER,
                'feature_axis': cls.FEATURE_AXIS,
                'loader_version': LOADER_VERSION,
                # URLs públicas — não são credenciais; registradas para auditoria
                'tumor_url': CPTAC_TUMOR_URL,
                'normal_url': CPTAC_NORMAL_URL,
                # db_url NÃO é armazenada aqui (sensitive-data-handling)
            },
        )

        try:
            run_matrix_load.delay(str(job.id), str(project.id))
            logger.info(
                'MATRIX_LOAD disparado (job %s) para projeto %s / dataset %s',
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
          6. Cria OmicMatrix + OmicSample + OmicMatrixSample (transação set-based).
          7. Limpa tmp local. Marca job COMPLETED.

        Args:
            job: IngestionJob existente (criado pelo dispatch); se None, cria
                 internamente (modo síncrono direto do management command).

        Returns:
            dict com n_features, n_samples, roles, storage_key, job_id.
        """
        import rust_engine

        dataset = self._get_or_create_dataset()

        # Gate de idempotência (só se não tivermos job já criado pelo dispatch)
        if job is None:
            try:
                self._check_idempotency(dataset)
            except (MatrixAlreadyLoadedError, MatrixJobActiveError):
                raise

        # Garante job existente
        if job is None:
            job = IngestionJob.objects.create(
                project=self.project,
                job_type=IngestionJob.JobType.MATRIX_LOAD,
                status=IngestionJob.JobStatus.RUNNING,
                parameters={
                    'dataset_accession': dataset.accession,
                    'dataset_id': dataset.id,
                    'omics_layer': self.OMICS_LAYER,
                    'feature_axis': self.FEATURE_AXIS,
                    'loader_version': LOADER_VERSION,
                    'tumor_url': CPTAC_TUMOR_URL,
                    'normal_url': CPTAC_NORMAL_URL,
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
        Resolve ou cria a casca OmicDataset para o CPTAC CCRCC dentro do projeto.

        OmicDataset é tabela COMPARTILHADA — o get_or_create usa o accession como
        natural key.  O isolamento por projeto é via ProjectDataset (criado logo
        após), não por filtro no OmicDataset.

        source_db usa OmicDataset.SourceDB.CPTAC ('cptac'), adicionado pelo
        cartografo na migration 0028.  A lookup key do get_or_create é
        accession (não source_db), garantindo que linhas placeholder antigas
        com source_db='tcga' sejam resolvidas pelo accession e corrigidas
        para 'cptac' via update() logo após.
        """
        dataset, created = OmicDataset.objects.get_or_create(
            accession=CPTAC_CCRCC_ACCESSION,
            defaults={
                'source_db': OmicDataset.SourceDB.CPTAC,
                'title': 'CPTAC CCRCC Discovery Study — Proteome (LinkedOmics)',
                'summary': (
                    'Clear cell renal cell carcinoma (CCRCC) proteome from the '
                    'Clinical Proteomic Tumor Analysis Consortium (CPTAC) '
                    'Discovery Study. Tumor vs. normal paired samples. '
                    'Source: LinkedOmics / PDC.'
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
                    'data_source': 'LinkedOmics',
                    'tumor_url': CPTAC_TUMOR_URL,
                    'normal_url': CPTAC_NORMAL_URL,
                },
            },
        )

        if created:
            logger.info(
                'OmicDataset criado: accession=%s, source_db=cptac',
                CPTAC_CCRCC_ACCESSION,
            )
        else:
            # Corrige placeholder antigo com source_db='tcga' se necessário.
            # A natural key de resolução é accession (não source_db), portanto
            # rodar 2x não cria linha duplicada independente do valor anterior.
            if dataset.source_db != OmicDataset.SourceDB.CPTAC:
                old_source_db = dataset.source_db
                OmicDataset.objects.filter(pk=dataset.pk).update(
                    source_db=OmicDataset.SourceDB.CPTAC,
                )
                dataset.source_db = OmicDataset.SourceDB.CPTAC
                logger.info(
                    'OmicDataset %s: source_db corrigido de %r para cptac',
                    CPTAC_CCRCC_ACCESSION,
                    old_source_db,
                )
            else:
                logger.info(
                    'OmicDataset resolvido (existente): accession=%s, source_db=cptac',
                    CPTAC_CCRCC_ACCESSION,
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
            MatrixAlreadyLoadedError: matriz já carregada.
            MatrixJobActiveError: job ativo encontrado.
        """
        # Gate 1: matriz já existe para esta natural key?
        existing_matrix = OmicMatrix.objects.filter(
            dataset=dataset,
            omics_layer=self.OMICS_LAYER,
            feature_axis=self.FEATURE_AXIS,
            loader_version=LOADER_VERSION,
        ).first()
        if existing_matrix:
            raise MatrixAlreadyLoadedError(existing_matrix)

        # Gate 2: job ativo para este dataset+projeto?
        active_job = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.MATRIX_LOAD,
            status__in=[IngestionJob.JobStatus.PENDING, IngestionJob.JobStatus.RUNNING],
            parameters__dataset_id=dataset.id,
        ).first()
        if active_job:
            raise MatrixJobActiveError(active_job)

    def _execute(self, dataset: OmicDataset, job: IngestionJob, rust_engine) -> dict:
        """
        Núcleo da carga: Rust → manifest → upload → ORM set-based.

        Parâmetro rust_engine passado explicitamente para facilitar mock em testes.
        """
        with tempfile.TemporaryDirectory(prefix='davinci_matrix_') as dest_dir:
            # ── Passo 1: Rust carrega e escreve Parquet local ─────────────────
            logger.info(
                'Chamando rust_engine.load_cptac_matrix (job %s, dest_dir omitido)',
                job.id,
            )
            manifest = rust_engine.load_cptac_matrix(
                tumor_url=CPTAC_TUMOR_URL,
                normal_url=CPTAC_NORMAL_URL,
                dest_dir=dest_dir,
                parquet_name=CPTAC_PARQUET_NAME,
            )
            logger.info(
                'load_cptac_matrix concluído: n_features=%d, n_samples=%d, '
                'genes_discarded=%d',
                manifest.n_features,
                manifest.n_samples,
                manifest.genes_discarded,
            )

            # ── Passo 2: Upload do Parquet via default_storage ────────────────
            storage_key = self._upload_parquet(manifest.parquet_path, dataset, job)

            # ── Passo 3: ORM set-based dentro de transação atômica ────────────
            matrix, roles_summary = self._persist_orm(
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
            'MATRIX_LOAD concluído (job %s): storage_key=%s, '
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

        Retorna a storage_key lógica (nunca caminho físico).
        Remove o arquivo local após upload bem-sucedido.
        """
        filename = os.path.basename(local_path)

        # Convenção de path: omics/{user_id}/{project_id}/{dataset_accession}/{filename}
        # Reutiliza omics_storage_key — mesmo namespace dos DatasetFile.
        object_key = omics_storage_key(
            user_id=self.user.id,
            project_id=self.project.id,
            dataset_accession=dataset.accession,
            filename=filename,
        )

        logger.info(
            'Fazendo upload do Parquet para default_storage '
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
                'Falha ao remover arquivo Parquet local após upload (job %s): %s',
                job.id,
                err,
            )

        logger.info(
            'Upload do Parquet concluído (job %s): storage_key=%s',
            job.id,
            saved_key,
        )
        return saved_key

    def _persist_orm(
        self,
        dataset: OmicDataset,
        manifest,
        storage_key: str,
    ) -> tuple[OmicMatrix, dict]:
        """
        Persiste OmicMatrix + OmicSample + OmicMatrixSample dentro de transação.

        Granularidade de OmicSample: UM por (case_id, role) — accession=f"{case_id}_{role}".
        Ver justificativa no docstring do módulo.

        Returns:
            (OmicMatrix, roles_summary) onde roles_summary = {'tumor': N, 'normal': N}.
        """
        source_pointer = f"{CPTAC_TUMOR_URL} | {CPTAC_NORMAL_URL}"

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
                # Atualiza storage_key e dimensões caso tenha mudado
                OmicMatrix.objects.filter(id=matrix.id).update(
                    storage_key=storage_key,
                    n_features=manifest.n_features,
                    n_samples=manifest.n_samples,
                    checksum_md5=manifest.checksum_md5,
                )
                matrix.refresh_from_db()

            # ── OmicSample: um por (case_id, role) ───────────────────────────
            # Coleta case_ids únicos por role para get_or_create eficiente
            sample_columns = manifest.sample_columns  # list[CptacSampleColumn]

            # Mapeia (case_id, role) → column_index
            # case_id+role é a chave natural; pode haver múltiplas aliquotas,
            # mas o loader garante unicidade de (case_id, role) no Parquet.
            col_map: dict[tuple[str, str], int] = {}
            for col in sample_columns:
                key = (col.case_id, col.sample_role)
                # Em caso de duplicata (não esperada), mantém o primeiro
                if key not in col_map:
                    col_map[key] = col.column_index

            # get_or_create OmicSample para cada (case_id, role)
            sample_by_key: dict[tuple[str, str], OmicSample] = {}
            roles_count: dict[str, int] = {'tumor': 0, 'normal': 0, 'unknown': 0}

            for (case_id, role), _col_idx in col_map.items():
                accession = f"{case_id}_{role}"
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
            # Respeita as unique constraints (matrix, sample) e (matrix, column_index).
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
                    ignore_conflicts=True,  # idempotência: re-rodar não duplica
                )
                logger.info(
                    'OmicMatrixSample bulk_create: %d linhas (matrix %s)',
                    len(oms_to_create),
                    matrix.id,
                )

        roles_summary = {k: v for k, v in roles_count.items() if v > 0}
        return matrix, roles_summary


def _normalize_role(role: str) -> str:
    """
    Mapeia role string do Rust para o vocabulário canônico de OmicMatrixSample.

    O loader Rust retorna 'tumor' | 'normal' como definidos no contrato.
    Qualquer outro valor mapeia para 'unknown'.
    """
    if role == OmicMatrixSample.SampleRole.TUMOR:
        return OmicMatrixSample.SampleRole.TUMOR
    if role == OmicMatrixSample.SampleRole.NORMAL:
        return OmicMatrixSample.SampleRole.NORMAL
    return OmicMatrixSample.SampleRole.UNKNOWN
