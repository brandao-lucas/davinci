"""
GeneRoleLoadService — orquestração Django da carga de papéis de gene via OncoKB.

OmnisPathway Objetivo 2, Fase 2, Slice 2A.

Responsabilidade:
  1. Gate de idempotência: não duplicar job ativo de GENE_ROLE_LOAD.
  2. Criar IngestionJob(job_type=GENE_ROLE_LOAD).
  3. Chamar rust_engine.load_oncokb_gene_roles(url, dest_dir, db_url)
     → GeneRoleManifest{n_genes, n_oncogene, n_tsg, n_dual, n_neither,
       n_unknown, source_version, n_upserted}.
  4. O Rust FAZ o COPY/UPSERT direto em GeneRole — este service NÃO
     persiste GeneRole via ORM.
  5. Marcar IngestionJob COMPLETED com contadores do manifesto.

Fronteira de camadas (Regra #1 / django-rust-boundary):
  - Rust: fetch HTTP do cancerGeneList.txt (TOKEN-FREE), parse streaming
    do TSV, mapeamento de Gene Type → role, COPY/UPSERT na tabela GeneRole.
    NÃO é necessário nenhum token — fonte pública TOKEN-FREE.
  - Django: cria/monitora o job, constrói db_url sem logá-la, reporta
    contadores do manifesto. NÃO abre o arquivo, NÃO parseia TSV.

Isolamento (Regra #3):
  GeneRole é catálogo global de referência (SEM FK de projeto) — mesmo
  padrão de MendelianGene/DiseaseAxisReference. O IngestionJob ainda exige
  FK de projeto (constraint do modelo), portanto o caller DEVE fornecer um
  projeto de tracking. O job NÃO filtra/afeta apenas o projeto: a carga é
  global e idempotente (UPSERT). Usar project=sentinel project ou o projeto
  do pesquisador que disparou — o command usa --project como tracking.

Sensitive-data-handling:
  - Fonte OncoKB TOKEN-FREE: nenhuma credencial de API envolvida.
  - db_url construída de settings.DATABASES — NUNCA logada nem gravada
    em IngestionJob.parameters (mesmo padrão de run_pubmed_ingestion).
  - URL da fonte é pública — registrada em parameters para auditoria.

Idempotência:
  Gate verifica se há GENE_ROLE_LOAD ativo (PENDING/RUNNING) — não duplica
  job concorrente. O Rust usa UPSERT ON CONFLICT (gene_symbol, source) DO
  UPDATE — re-rodar atualiza os registros sem duplicar.
"""

from __future__ import annotations

import logging
import tempfile

from django.conf import settings
from django.utils import timezone

from apps.core.models import DaVinciProject, IngestionJob

logger = logging.getLogger(__name__)

# URL TOKEN-FREE do OncoKB cancerGeneList.txt (Campo Gene Type resolve papel
# sem chamar a API; traz CGC v99 e Vogelstein bundled).
ONCOKB_GENE_LIST_URL = (
    'https://www.oncokb.org/api/v1/utils/cancerGeneList.txt'
)

# Versão do loader: atualizar quando o contrato Rust mudar.
LOADER_VERSION = '0.1.0-oncokb-cancer-gene-list'


class GeneRoleJobActiveError(Exception):
    """Levantado quando já há um GENE_ROLE_LOAD pending/running."""

    def __init__(self, job: IngestionJob):
        self.job = job
        super().__init__(
            f"GENE_ROLE_LOAD já ativo (job {job.id}, status={job.status}) "
            f"— dispatch ignorado (idempotência)"
        )


class GeneRoleLoadService:
    """
    Serviço de carga do catálogo global GeneRole via OncoKB.

    Uso síncrono (management command / bancada de prova):
        service = GeneRoleLoadService(project)
        result = service.run()

    Uso assíncrono (Celery):
        job = GeneRoleLoadService.dispatch(project)
        # task run_gene_role_load(job.id, str(project.id)) prossegue em background
    """

    def __init__(self, project: DaVinciProject):
        self.project = project

    # ─────────────────────────────────────────────────────────────────────────
    # API pública
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def dispatch(cls, project: DaVinciProject) -> IngestionJob:
        """
        Gate de idempotência + criação do IngestionJob.

        Não executa a carga — apenas verifica pré-condições e enfileira
        a task Celery (via run_gene_role_load).

        Returns:
            IngestionJob recém-criado (status=PENDING).

        Raises:
            GeneRoleJobActiveError: job ativo encontrado.
        """
        from apps.core.tasks.ingestion_tasks import run_gene_role_load

        service = cls(project)
        service._check_idempotency()

        job = IngestionJob.objects.create(
            project=project,
            job_type=IngestionJob.JobType.GENE_ROLE_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={
                'source_url': ONCOKB_GENE_LIST_URL,
                'loader_version': LOADER_VERSION,
                # db_url NÃO é armazenada (sensitive-data-handling)
            },
        )

        try:
            run_gene_role_load.delay(str(job.id), str(project.id))
            logger.info(
                'GENE_ROLE_LOAD disparado (job %s) para projeto %s',
                job.id,
                project.id,
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
          1. Gate de idempotência (job ativo).
          2. Cria/atualiza IngestionJob para RUNNING.
          3. Constrói db_url sem logá-la.
          4. Chama rust_engine.load_oncokb_gene_roles → manifest.
          5. Marca job COMPLETED com contadores do manifesto.
             (O Rust fez UPSERT em GeneRole — Django não persiste via ORM.)

        Args:
            job: IngestionJob existente (criado pelo dispatch); se None,
                 cria internamente (modo síncrono direto do management command).

        Returns:
            dict com n_genes, n_oncogene, n_tsg, n_dual, n_neither, n_unknown,
            source_version, n_upserted, job_id.
        """
        import rust_engine

        # Gate de idempotência apenas no modo síncrono direto (sem job pre-criado)
        if job is None:
            self._check_idempotency()

        # Garante job existente
        if job is None:
            job = IngestionJob.objects.create(
                project=self.project,
                job_type=IngestionJob.JobType.GENE_ROLE_LOAD,
                status=IngestionJob.JobStatus.RUNNING,
                parameters={
                    'source_url': ONCOKB_GENE_LIST_URL,
                    'loader_version': LOADER_VERSION,
                },
            )
        else:
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.RUNNING,
                started_at=timezone.now(),
            )

        try:
            result = self._execute(job, rust_engine)
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

    def _check_idempotency(self) -> None:
        """
        Verifica se há um GENE_ROLE_LOAD ativo (pending ou running).

        GeneRole é catálogo global — a gate é apenas por job ativo, não por
        "registro já existe" (o UPSERT do Rust é idempotente; re-rodar após
        conclusão é válido para atualizar a versão).

        Raises:
            GeneRoleJobActiveError: job ativo encontrado.
        """
        active_job = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.GENE_ROLE_LOAD,
            status__in=[
                IngestionJob.JobStatus.PENDING,
                IngestionJob.JobStatus.RUNNING,
            ],
        ).first()
        if active_job:
            raise GeneRoleJobActiveError(active_job)

    def _build_db_url(self) -> str:
        """
        Constrói a db_url a partir de settings.DATABASES['default'].

        NUNCA logar o retorno desta função (contém credenciais).
        """
        db = settings.DATABASES['default']
        return (
            f"postgresql://{db['USER']}:{db['PASSWORD']}"
            f"@{db['HOST']}:{db['PORT']}/{db['NAME']}"
        )

    def _execute(self, job: IngestionJob, rust_engine) -> dict:
        """
        Núcleo da carga: chama Rust e atualiza o job.

        rust_engine passado explicitamente para facilitar mock em testes.
        """
        with tempfile.TemporaryDirectory(prefix='davinci_generole_') as dest_dir:
            # db_url construída aqui — NUNCA logada (sensitive-data-handling)
            db_url = self._build_db_url()

            logger.info(
                'Chamando rust_engine.load_oncokb_gene_roles (job %s, '
                'url=%s, dest_dir omitido)',
                job.id,
                ONCOKB_GENE_LIST_URL,
            )

            manifest = rust_engine.load_oncokb_gene_roles(
                url=ONCOKB_GENE_LIST_URL,
                dest_dir=dest_dir,
                db_url=db_url,
            )

            # db_url já saiu de escopo após a chamada — não é referenciada abaixo

            logger.info(
                'load_oncokb_gene_roles concluído (job %s): '
                'n_genes=%d, n_oncogene=%d, n_tsg=%d, n_upserted=%d, '
                'source_version=%s',
                job.id,
                manifest.n_genes,
                manifest.n_oncogene,
                manifest.n_tsg,
                manifest.n_upserted,
                manifest.source_version,
            )

        # Marca job COMPLETED com contadores do manifesto
        IngestionJob.objects.filter(id=job.id).update(
            status=IngestionJob.JobStatus.COMPLETED,
            records_processed=manifest.n_genes,
            records_inserted=manifest.n_upserted,
            completed_at=timezone.now(),
        )

        return {
            'job_id': str(job.id),
            'n_genes': manifest.n_genes,
            'n_oncogene': manifest.n_oncogene,
            'n_tsg': manifest.n_tsg,
            'n_dual': manifest.n_dual,
            'n_neither': manifest.n_neither,
            'n_unknown': manifest.n_unknown,
            'source_version': manifest.source_version,
            'n_upserted': manifest.n_upserted,
        }
