"""
CnvSeedService — orquestração Django da derivação de seed CNV.

OmnisPathway Objetivo 2, Fase 2, Slice CNV-seed (derivação).

Responsabilidade:
  1. Resolver a OmicMatrix de copy_number dentro do projeto (isolamento,
     Regra #3): busca via ProjectDataset com curation_status ativo.
  2. Pré-checks com erro claro:
       (a) Existe OmicMatrix CNV para o projeto?
       (b) GeneRole está populado (ao menos 1 gene com role)?
           Se vazio → erro "rode load_gene_roles primeiro".
  3. Gate de idempotência: job CNV_SEED_LOAD ativo (PENDING/RUNNING) para
     a mesma matriz → não duplicar. Re-rodar após conclusão é OK (Rust UPSERT).
  4. Criar IngestionJob(job_type=CNV_SEED_LOAD).
  5. DOWNLOAD do Parquet de OmicMatrix.storage_key via default_storage para
     um tempfile local (tempfile.TemporaryDirectory). NÃO parseia o conteúdo
     em Python — passa apenas o caminho local para o Rust (Regra #1).
  6. Chamar rust_engine.seed_cnv_from_parquet(parquet_path=local, matrix_id,
     db_url, method_version, gain_threshold, loss_threshold).
  7. Cleanup do temp (automático via TemporaryDirectory).
  8. Marcar job COMPLETED com o manifesto (n_seeds_written, etc.).

Fronteira de camadas (Regra #1 / django-rust-boundary):
  - Rust: lê o Parquet LOCAL, consulta GeneRole + OmicMatrixSample via db_url,
    e faz COPY UPSERT em VariantEffectSeed. Idempotente (UPSERT na natural key).
  - Django: download via default_storage (I/O de orquestração aceito), criação
    do job, pré-checks set-based, cleanup do tmp. NÃO abre o Parquet, NÃO
    parseia o .cct, NÃO lê nenhum dado numérico de CNV em Python.

Isolamento (firebase-auth-guard / Regra #3):
  - project deve pertencer ao request.user antes de chegar aqui.
  - OmicMatrix é resolvida via ProjectDataset do projeto (dataset vinculado ao
    projeto com curation_status ativo). Não há acesso cross-project.
  - VariantEffectSeed é catálogo derivado (global, ancorado em matrix/sample,
    SEM FK de projeto) — mesmo padrão de OmicSamplePairing. Isolamento de
    leitura futura vive em ProjectDataset (sem endpoint agora).

Sensitive-data-handling:
  - db_url construída a partir de settings.DATABASES['default'] — NUNCA logada,
    NUNCA gravada em IngestionJob.parameters, NUNCA retornada ao caller.
  - storage_key (chave lógica de object storage) não é vazada em log nem no
    retorno. O caminho local do tempfile também não é logado.
  - Nenhuma credencial neste fluxo (LinkedOmics público, OncoKB token-free,
    dado CNV é dado público).

Catálogo derivado:
  VariantEffectSeed é derivado (Rust UPSERT idempotente). A Regra #2
  (rastreabilidade de curadoria) NÃO se aplica — delete/regeneração permitidos.

Versão do método:
  METHOD_VERSION = 'fase2-cnv-v1'
  Grava em VariantEffectSeed.method_version. Re-seed com method_version diferente
  coexiste via UPSERT (a natural key inclui matrix/sample/gene/variant_key/
  evidence_type, não method_version — o Rust faz DO UPDATE).

Padrões reutilizados:
  - resolve_matrix: espelha SamplePairingService.resolve_matrix (Fase 1):
    datasets via ProjectDataset com curation_status ativo, filtra por
    omics_layer='copy_number', levanta MatrixNotFoundError.
  - IngestionJob: mesmo padrão de MatrixLoadService / CnvMatrixLoadService.
  - build de db_url: mesmo padrão inline de ingestion_tasks.py (sem logar).
  - tempfile.TemporaryDirectory: cleanup automático ao sair do with-block.
  - default_storage.open + shutil.copyfileobj: download streaming (sem
    bufferizar inteiro em memória — o Parquet pode ter dezenas de MB).
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile

from django.conf import settings
from django.core.files.storage import default_storage
from django.utils import timezone

from apps.core.models import (
    DaVinciProject,
    GeneRole,
    IngestionJob,
    OmicMatrix,
    ProjectDataset,
)

logger = logging.getLogger(__name__)

# Versão do método de seeding — parte da proveniência de VariantEffectSeed.
METHOD_VERSION = 'fase2-cnv-v1'

# Camada ômica que identifica a matriz CNV.
_CNV_OMICS_LAYER = 'copy_number'

# Statuses de ProjectDataset considerados ativos (mesma lista de resolve_matrix
# do SamplePairingService — garante consistência entre os dois serviços).
_ACTIVE_STATUSES = (
    ProjectDataset.CurationStatus.INCLUDED,
    ProjectDataset.CurationStatus.QUEUED_DOWNLOAD,
    ProjectDataset.CurationStatus.DOWNLOADED,
    ProjectDataset.CurationStatus.PENDING,
)


class CnvSeedMatrixNotFoundError(Exception):
    """Levantado quando não é possível resolver a OmicMatrix CNV para o projeto."""


class CnvSeedJobActiveError(Exception):
    """Levantado quando já há um CNV_SEED_LOAD ativo para a mesma matriz."""

    def __init__(self, job: IngestionJob):
        self.job = job
        super().__init__(
            f"CNV_SEED_LOAD já ativo (job {job.id}, status={job.status}) "
            f"para matrix {job.parameters.get('matrix_id')} — "
            f"dispatch ignorado (idempotência)."
        )


class CnvSeedGeneRoleEmptyError(Exception):
    """Levantado quando GeneRole está vazio (pré-check 2A)."""

    def __init__(self):
        super().__init__(
            "GeneRole está vazio — execute load_gene_roles antes de derivar "
            "a seed CNV. O Rust precisa do papel do gene (oncogene/tsg) para "
            "resolver a direção (dosagem × papel)."
        )


class CnvSeedService:
    """
    Serviço de derivação de VariantEffectSeed a partir da matriz CNV.

    Uso síncrono (management command / bancada de prova):
        service = CnvSeedService(project)
        result = service.run()

    Uso assíncrono (Celery):
        job = CnvSeedService.dispatch(project)
        # task run_cnv_seed_load(job.id, str(project.id)) prossegue em background

    O relatório retornado tem a seguinte shape:
        {
            'job_id': str,              # UUID do IngestionJob
            'matrix_id': int,           # OmicMatrix.id da matriz CNV
            'method_version': str,      # METHOD_VERSION
            'n_seeds_written': int,     # seeds gravadas em VariantEffectSeed
            'n_activator': int,         # seeds de direção=activator
            'n_inactivator': int,       # seeds de direção=inactivator
            'n_neutral_skipped': int,   # células abaixo do threshold ou sem papel
            'n_genes_with_role': int,   # genes com GeneRole resolvido
            'n_genes_skipped_no_role': int,  # genes sem GeneRole → neutro/skip
            'n_cells_subthreshold': int,# células entre -loss e +gain (neutras)
            'n_cells_null': int,        # células nulas no Parquet
        }
    """

    def __init__(self, project: DaVinciProject):
        self.project = project

    # ─────────────────────────────────────────────────────────────────────────
    # API pública
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def dispatch(
        cls,
        project: DaVinciProject,
        matrix_id: int | None = None,
        gain_threshold: float = 0.3,
        loss_threshold: float = -0.3,
    ) -> IngestionJob:
        """
        Gate de pré-checks + idempotência + criação do IngestionJob.

        Não executa a derivação — apenas verifica pré-condições e enfileira
        a task Celery (via run_cnv_seed_load).

        Args:
            project: DaVinciProject do usuário (isolamento).
            matrix_id: ID específico de OmicMatrix CNV. Se None, auto-detecta.
            gain_threshold: Log-ratio mínimo para chamar amplificação (default 0.3).
            loss_threshold: Log-ratio máximo para chamar deleção (default -0.3).

        Returns:
            IngestionJob recém-criado (status=PENDING).

        Raises:
            CnvSeedMatrixNotFoundError: matriz CNV não encontrada ou cross-project.
            CnvSeedGeneRoleEmptyError: GeneRole vazio (pré-check 2A).
            CnvSeedJobActiveError: job ativo para a mesma matriz.
        """
        from apps.core.tasks.ingestion_tasks import run_cnv_seed_load

        service = cls(project)
        matrix = service.resolve_matrix(project, matrix_id)
        service._check_gene_role_populated()
        service._check_no_active_job(matrix)

        job = IngestionJob.objects.create(
            project=project,
            job_type=IngestionJob.JobType.CNV_SEED_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={
                'matrix_id': matrix.id,
                'method_version': METHOD_VERSION,
                'gain_threshold': gain_threshold,
                'loss_threshold': loss_threshold,
                # db_url NÃO é armazenada (sensitive-data-handling)
            },
        )

        try:
            run_cnv_seed_load.delay(str(job.id), str(project.id))
            logger.info(
                'CNV_SEED_LOAD disparado (job %s) para projeto %s / matrix %d',
                job.id,
                project.id,
                matrix.id,
            )
        except Exception as exc:
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=f'Failed to dispatch Celery task: {exc}',
            )
            raise

        return job

    def run(
        self,
        job: IngestionJob | None = None,
        matrix_id: int | None = None,
        gain_threshold: float = 0.3,
        loss_threshold: float = -0.3,
    ) -> dict:
        """
        Executa a derivação de seed CNV de forma síncrona.

        Fluxo:
          1. Resolve OmicMatrix CNV (isolamento via ProjectDataset).
          2. Pré-checks: GeneRole populado?
          3. Gate de idempotência (job ativo?).
          4. Cria/atualiza IngestionJob → RUNNING.
          5. Download do Parquet via default_storage para tempfile local.
             (NÃO parseia — apenas passa o caminho ao Rust.)
          6. Chama rust_engine.seed_cnv_from_parquet(parquet_path, matrix_id,
             db_url, method_version, gain_threshold, loss_threshold).
          7. Cleanup automático do tmpdir ao sair do with-block.
          8. Marca job COMPLETED com o manifesto.

        Args:
            job: IngestionJob existente (criado pelo dispatch); se None, cria
                 internamente (modo síncrono direto do management command).
            matrix_id: ID específico de OmicMatrix CNV. Se None, auto-detecta.
            gain_threshold: Log-ratio mínimo para chamar amplificação (default 0.3).
            loss_threshold: Log-ratio máximo para chamar deleção (default -0.3).

        Returns:
            dict com o manifesto do seeding (ver shape na docstring da classe).
        """
        import rust_engine

        matrix = self.resolve_matrix(self.project, matrix_id)
        self._check_gene_role_populated()

        # Gate de idempotência apenas no modo síncrono direto (sem job pré-criado)
        if job is None:
            self._check_no_active_job(matrix)

        # Garante job existente
        if job is None:
            job = IngestionJob.objects.create(
                project=self.project,
                job_type=IngestionJob.JobType.CNV_SEED_LOAD,
                status=IngestionJob.JobStatus.RUNNING,
                parameters={
                    'matrix_id': matrix.id,
                    'method_version': METHOD_VERSION,
                    'gain_threshold': gain_threshold,
                    'loss_threshold': loss_threshold,
                },
            )
        else:
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.RUNNING,
                started_at=timezone.now(),
            )
            # Extrai thresholds do job pré-criado (dispatch os gravou em parameters)
            gain_threshold = job.parameters.get('gain_threshold', gain_threshold)
            loss_threshold = job.parameters.get('loss_threshold', loss_threshold)

        try:
            result = self._execute(matrix, job, gain_threshold, loss_threshold, rust_engine)
            return result
        except Exception as exc:
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=str(exc),
                completed_at=timezone.now(),
            )
            raise

    # ─────────────────────────────────────────────────────────────────────────
    # Resolução de matriz (espelha SamplePairingService.resolve_matrix — Fase 1)
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def resolve_matrix(
        cls,
        project: DaVinciProject,
        matrix_id: int | None = None,
    ) -> OmicMatrix:
        """
        Resolve a OmicMatrix CNV para o projeto dado.

        Isolamento: só resolve matrizes de datasets vinculados ao projeto via
        ProjectDataset com curation_status ativo — garante que não há acesso
        cross-project.

        Args:
            project: DaVinciProject do usuário.
            matrix_id: ID específico. Se None, auto-detecta a única matriz
                       copy_number vinculada ao projeto.

        Returns:
            OmicMatrix de camada copy_number.

        Raises:
            CnvSeedMatrixNotFoundError: não encontrada ou não pertence ao projeto.
        """
        dataset_ids = ProjectDataset.objects.filter(
            project=project,
            curation_status__in=_ACTIVE_STATUSES,
        ).values_list('dataset_id', flat=True)

        qs = OmicMatrix.objects.select_related('dataset').filter(
            dataset_id__in=dataset_ids,
            omics_layer=_CNV_OMICS_LAYER,
        )

        if matrix_id is not None:
            matrix = qs.filter(id=matrix_id).first()
            if matrix is None:
                raise CnvSeedMatrixNotFoundError(
                    f'OmicMatrix id={matrix_id} com omics_layer=copy_number '
                    f'não encontrada ou não pertence ao projeto {project.id}.'
                )
            return matrix

        # Auto-detecção: única matriz copy_number do projeto
        matrices = list(qs)
        if not matrices:
            raise CnvSeedMatrixNotFoundError(
                f'Nenhuma OmicMatrix copy_number encontrada no projeto '
                f'{project.id}. Execute load_cnv_matrix primeiro.'
            )
        if len(matrices) > 1:
            ids = [m.id for m in matrices]
            raise CnvSeedMatrixNotFoundError(
                f'Múltiplas OmicMatrix copy_number encontradas no projeto '
                f'{project.id}: {ids}. Especifique --matrix para selecionar.'
            )
        return matrices[0]

    # ─────────────────────────────────────────────────────────────────────────
    # Pré-checks
    # ─────────────────────────────────────────────────────────────────────────

    def _check_gene_role_populated(self) -> None:
        """
        Verifica se GeneRole está populado.

        GeneRole é catálogo global (sem FK de projeto) — basta verificar que
        existe ao menos um registro. Se vazio, levanta CnvSeedGeneRoleEmptyError
        com instrução clara para o operador.

        Raises:
            CnvSeedGeneRoleEmptyError: tabela GeneRole vazia.
        """
        if not GeneRole.objects.exists():
            raise CnvSeedGeneRoleEmptyError()

        n_roles = GeneRole.objects.count()
        logger.debug(
            'CnvSeedService: pré-check GeneRole OK — %d genes com papel definido.',
            n_roles,
        )

    def _check_no_active_job(self, matrix: OmicMatrix) -> None:
        """
        Verifica se já há um CNV_SEED_LOAD ativo para a mesma matriz.

        Apenas bloqueia se PENDING ou RUNNING. Job COMPLETED ou FAILED não
        bloqueia — o Rust UPSERT permite re-seed após conclusão.

        Raises:
            CnvSeedJobActiveError: job ativo encontrado.
        """
        active_job = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.CNV_SEED_LOAD,
            status__in=[IngestionJob.JobStatus.PENDING, IngestionJob.JobStatus.RUNNING],
            parameters__matrix_id=matrix.id,
        ).first()
        if active_job:
            raise CnvSeedJobActiveError(active_job)

    # ─────────────────────────────────────────────────────────────────────────
    # Execução interna
    # ─────────────────────────────────────────────────────────────────────────

    def _execute(
        self,
        matrix: OmicMatrix,
        job: IngestionJob,
        gain_threshold: float,
        loss_threshold: float,
        rust_engine,
    ) -> dict:
        """
        Núcleo da derivação: download do Parquet → Rust → manifesto → job COMPLETED.

        rust_engine passado explicitamente para facilitar mock em testes.

        Fluxo:
          1. Obtém storage_key de OmicMatrix.storage_key.
          2. Abre o Parquet via default_storage.open (streaming, sem bufferizar).
          3. Copia para um arquivo temporário local (shutil.copyfileobj).
          4. Constrói db_url a partir de settings — NUNCA logada.
          5. Chama rust_engine.seed_cnv_from_parquet(parquet_path, matrix_id,
             db_url, method_version, gain_threshold, loss_threshold).
          6. O TemporaryDirectory é limpo automaticamente ao sair do with-block.
          7. Marca job COMPLETED; retorna manifesto.

        Regra #1: NÃO abre o conteúdo do Parquet em Python. Apenas baixa
        (I/O de orquestração aceito) e passa o caminho local ao Rust.
        """
        storage_key = matrix.storage_key
        if not storage_key:
            raise ValueError(
                f'OmicMatrix id={matrix.id} não tem storage_key definida — '
                'a matriz foi carregada corretamente?'
            )

        with tempfile.TemporaryDirectory(prefix='davinci_cnvseed_') as tmp_dir:
            # ── Passo 1: download do Parquet para tmpfile local ───────────────
            # Streaming via default_storage.open — não carrega tudo em memória.
            local_parquet = os.path.join(tmp_dir, 'cnv_matrix.parquet')

            logger.info(
                'CnvSeedService: downloading Parquet (job %s, matrix %d, '
                'caminho local omitido)',
                job.id,
                matrix.id,
            )
            try:
                with default_storage.open(storage_key, 'rb') as remote_f:
                    with open(local_parquet, 'wb') as local_f:
                        shutil.copyfileobj(remote_f, local_f)
            except Exception as exc:
                raise RuntimeError(
                    f'Falha ao baixar Parquet da OmicMatrix id={matrix.id}: {exc}'
                ) from exc

            logger.info(
                'CnvSeedService: Parquet baixado (job %s, matrix %d). '
                'Chamando rust_engine.seed_cnv_from_parquet.',
                job.id,
                matrix.id,
            )

            # ── Passo 2: construir db_url — NUNCA logar ───────────────────────
            db = settings.DATABASES['default']
            db_url = (
                f"postgresql://{db['USER']}:{db['PASSWORD']}"
                f"@{db['HOST']}:{db['PORT']}/{db['NAME']}"
            )

            # ── Passo 3: chamar Rust (processamento pesado) ───────────────────
            # O Rust lê o Parquet LOCAL, consulta GeneRole + OmicMatrixSample
            # via db_url e faz COPY UPSERT em VariantEffectSeed. Idempotente.
            manifest = rust_engine.seed_cnv_from_parquet(
                parquet_path=local_parquet,
                matrix_id=matrix.id,
                db_url=db_url,
                method_version=METHOD_VERSION,
                gain_threshold=gain_threshold,
                loss_threshold=loss_threshold,
            )

        # ── Tmpdir limpo automaticamente ao sair do with-block ────────────────

        # ── Passo 4: marcar job COMPLETED ────────────────────────────────────
        n_seeds = manifest.n_seeds_written
        IngestionJob.objects.filter(id=job.id).update(
            status=IngestionJob.JobStatus.COMPLETED,
            records_processed=n_seeds,
            records_inserted=n_seeds,
            completed_at=timezone.now(),
        )

        logger.info(
            'CNV_SEED_LOAD concluído (job %s, matrix %d): '
            'n_seeds_written=%d, n_activator=%d, n_inactivator=%d, '
            'n_genes_with_role=%d, n_genes_skipped_no_role=%d',
            job.id,
            matrix.id,
            manifest.n_seeds_written,
            manifest.n_activator,
            manifest.n_inactivator,
            manifest.n_genes_with_role,
            manifest.n_genes_skipped_no_role,
        )

        return {
            'job_id': str(job.id),
            'matrix_id': matrix.id,
            'method_version': METHOD_VERSION,
            'n_seeds_written': manifest.n_seeds_written,
            'n_activator': manifest.n_activator,
            'n_inactivator': manifest.n_inactivator,
            'n_neutral_skipped': manifest.n_neutral_skipped,
            'n_genes_with_role': manifest.n_genes_with_role,
            'n_genes_skipped_no_role': manifest.n_genes_skipped_no_role,
            'n_cells_subthreshold': manifest.n_cells_subthreshold,
            'n_cells_null': manifest.n_cells_null,
        }
