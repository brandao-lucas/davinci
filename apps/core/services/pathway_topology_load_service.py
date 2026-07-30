"""
PathwayTopologyLoadService — orquestração Django da carga de topologia KEGG.

OmnisPathway Objetivo 2, Fase 3, Passo 3.2.

Responsabilidade:
  1. Gate de idempotência: não duplicar job ativo de PATHWAY_TOPOLOGY_LOAD.
  2. Criar IngestionJob(job_type=PATHWAY_TOPOLOGY_LOAD).
  3. Chamar rust_engine.load_kegg_topology(pathway_kegg_ids, dest_dir, db_url)
     → KeggTopologyManifest{n_pathways, n_nodes, n_edges, n_signed, n_unsigned,
       n_orphan_symbols, source_version}.
     O Rust faz o COPY de core_pathway/pathwaynode/pathwayedge diretamente.
     readout_role fica 'none' nesta etapa (marcado em 3.4 pelo mapeamento).
  4. Marcar IngestionJob COMPLETED com contadores do manifesto.

Fronteira de camadas (Regra #1 / django-rust-boundary):
  - Rust: fetch KGML (rest.kegg.jp), parse XML quick-xml, extração de sinal
    por subtype (§Sinal), COPY/UPSERT de Pathway/PathwayNode/PathwayEdge.
  - Django: gate de idempotência, criação/monitoramento do IngestionJob,
    construção da db_url (sem logá-la), relatório de contadores. NÃO abre
    KGML, NÃO parseia XML.

Catálogo global (sem FK de projeto):
  O grafo KEGG é catálogo global de referência — igual ao GeneRole/VariantEffectRaw.
  IngestionJob ainda exige FK de projeto (constraint do modelo); o caller fornece
  um projeto de tracking. A carga afeta toda a tabela global, não só o projeto.

Segurança (findings M2/M3 — allowlist de host):
  O fetcher Rust aplica allowlist de host (`rest.kegg.jp`) e teto de bytes.
  Django não valida host — fronteira já protegida no Rust.

Licença KEGG (acadêmica):
  KEGG via REST para uso acadêmico. Cache local em dest_dir (gitignored).
  O grafo derivado persiste em PG — o KGML bruto não é commitado.
  rate ≤3 req/s (irrelevante com 3 vias, mas o Rust respeita).

Sensitive-data-handling:
  - db_url construída de settings.DATABASES — NUNCA logada nem gravada em
    IngestionJob.parameters.
  - Nenhum token/credencial neste fluxo (KEGG público sem autenticação).

Idempotência:
  Gate verifica PATHWAY_TOPOLOGY_LOAD ativo (PENDING/RUNNING). O Rust usa
  COPY ON CONFLICT (kegg_id / pathway+kegg_entry_id / pathway+src+dst+interaction)
  DO UPDATE — re-rodar atualiza sem duplicar.

Via padrão (v1): PI3K-Akt (hsa04151), MAPK (hsa04010), p53 (hsa04115).
"""

from __future__ import annotations

import logging
import tempfile

from django.conf import settings
from django.utils import timezone

from apps.core.models import DaVinciProject, IngestionJob

logger = logging.getLogger(__name__)

# Vias padrão da Fase 3 (Decisão 1 — travada no plano)
DEFAULT_PATHWAY_IDS = ['hsa04151', 'hsa04010', 'hsa04115']

# Versão do loader: atualizar quando o contrato Rust mudar de forma incompatível.
LOADER_VERSION = 'kegg-topology-v1'


class PathwayTopologyJobActiveError(Exception):
    """Levantado quando já há um PATHWAY_TOPOLOGY_LOAD pending/running."""

    def __init__(self, job: IngestionJob):
        self.job = job
        super().__init__(
            f"PATHWAY_TOPOLOGY_LOAD já ativo (job {job.id}, "
            f"status={job.status}) — dispatch ignorado (idempotência)"
        )


class PathwayTopologyLoadService:
    """
    Serviço de carga de topologia KEGG para o catálogo global de vias.

    Uso síncrono (management command / bancada de prova):
        service = PathwayTopologyLoadService(project)
        result = service.run(pathway_ids=['hsa04151', 'hsa04010', 'hsa04115'])

    Uso assíncrono (Celery):
        job = PathwayTopologyLoadService.dispatch(project)
        # task run_pathway_topology_load(job.id, str(project.id)) em background

    Retorno de run():
        {
            'job_id': str,
            'n_pathways': int,
            'n_nodes': int,
            'n_edges': int,
            'n_signed': int,
            'n_unsigned': int,
            'n_orphan_symbols': int,
            'source_version': str,
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
        pathway_ids: list[str] | None = None,
    ) -> IngestionJob:
        """
        Gate de idempotência + criação do IngestionJob.

        Não executa a carga — apenas verifica pré-condições e enfileira
        a task Celery (via run_pathway_topology_load).

        Args:
            project: DaVinciProject de tracking (isolamento de IngestionJob).
            pathway_ids: IDs KEGG a carregar. None → DEFAULT_PATHWAY_IDS.

        Returns:
            IngestionJob recém-criado (status=PENDING).

        Raises:
            PathwayTopologyJobActiveError: job ativo encontrado.
        """
        from apps.core.tasks.ingestion_tasks import run_pathway_topology_load

        ids = pathway_ids or DEFAULT_PATHWAY_IDS
        service = cls(project)
        service._check_idempotency()

        job = IngestionJob.objects.create(
            project=project,
            job_type=IngestionJob.JobType.PATHWAY_TOPOLOGY_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={
                'pathway_ids': ids,
                'loader_version': LOADER_VERSION,
                # db_url NÃO é armazenada (sensitive-data-handling)
            },
        )

        try:
            run_pathway_topology_load.delay(str(job.id), str(project.id))
            logger.info(
                'PATHWAY_TOPOLOGY_LOAD disparado (job %s) para projeto %s, '
                'vias=%s',
                job.id,
                project.id,
                ids,
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
        pathway_ids: list[str] | None = None,
    ) -> dict:
        """
        Executa a carga de topologia KEGG de forma síncrona.

        Fluxo:
          1. Gate de idempotência.
          2. Cria/atualiza IngestionJob para RUNNING.
          3. Constrói db_url (sem logá-la).
          4. Chama rust_engine.load_kegg_topology → manifest.
          5. Marca job COMPLETED com contadores.

        Args:
            job: IngestionJob existente (criado pelo dispatch); se None, cria
                 internamente.
            pathway_ids: IDs KEGG a carregar. None → job.parameters ou
                         DEFAULT_PATHWAY_IDS.

        Returns:
            dict com n_pathways, n_nodes, n_edges, n_signed, source_version, job_id.
        """
        import rust_engine

        # Resolve pathway_ids: prioridade ao argumento, depois ao job.parameters
        if pathway_ids is None:
            if job is not None:
                pathway_ids = job.parameters.get('pathway_ids', DEFAULT_PATHWAY_IDS)
            else:
                pathway_ids = DEFAULT_PATHWAY_IDS

        if job is None:
            try:
                self._check_idempotency()
            except PathwayTopologyJobActiveError:
                raise

        if job is None:
            job = IngestionJob.objects.create(
                project=self.project,
                job_type=IngestionJob.JobType.PATHWAY_TOPOLOGY_LOAD,
                status=IngestionJob.JobStatus.RUNNING,
                parameters={
                    'pathway_ids': pathway_ids,
                    'loader_version': LOADER_VERSION,
                },
            )
        else:
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.RUNNING,
                started_at=timezone.now(),
            )

        try:
            result = self._execute(job, pathway_ids, rust_engine)
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
        Verifica se há PATHWAY_TOPOLOGY_LOAD ativo (PENDING/RUNNING).

        Raises:
            PathwayTopologyJobActiveError: job ativo encontrado.
        """
        active_job = IngestionJob.objects.filter(
            job_type=IngestionJob.JobType.PATHWAY_TOPOLOGY_LOAD,
            status__in=[
                IngestionJob.JobStatus.PENDING,
                IngestionJob.JobStatus.RUNNING,
            ],
        ).first()
        if active_job:
            raise PathwayTopologyJobActiveError(active_job)

    def _build_db_url(self) -> str:
        """
        Constrói a db_url a partir de settings.DATABASES['default'].

        NUNCA logada (sensitive-data-handling) — sai de escopo após a chamada
        ao Rust.
        """
        db = settings.DATABASES['default']
        return (
            f"postgresql://{db['USER']}:{db['PASSWORD']}"
            f"@{db['HOST']}:{db['PORT']}/{db['NAME']}"
        )

    def _execute(
        self,
        job: IngestionJob,
        pathway_ids: list[str],
        rust_engine,
    ) -> dict:
        """
        Núcleo da carga: constrói db_url → chama Rust → manifesto → COMPLETED.

        O Rust faz o COPY de Pathway/PathwayNode/PathwayEdge diretamente em PG.
        Django não persiste nada via ORM (catálogo global preenchido pelo Rust).
        db_url sai de escopo imediatamente após a chamada ao Rust.
        """
        with tempfile.TemporaryDirectory(prefix='davinci_kegg_') as dest_dir:
            logger.info(
                'Chamando rust_engine.load_kegg_topology (job %s, vias=%s)',
                job.id,
                pathway_ids,
            )

            # db_url construída aqui — NUNCA logada (sensitive-data-handling)
            db_url = self._build_db_url()
            try:
                manifest = rust_engine.load_kegg_topology(
                    pathway_kegg_ids=pathway_ids,
                    dest_dir=dest_dir,
                    db_url=db_url,
                )
            finally:
                # db_url sai de escopo; Python limpa a referência após o bloco
                del db_url

            logger.info(
                'load_kegg_topology concluído: n_pathways=%d, n_nodes=%d, '
                'n_edges=%d, n_signed=%d, n_unsigned=%d, n_orphan_symbols=%d',
                manifest.n_pathways,
                manifest.n_nodes,
                manifest.n_edges,
                manifest.n_signed,
                manifest.n_unsigned,
                manifest.n_orphan_symbols,
            )

        # Marca job COMPLETED com contadores do manifesto
        IngestionJob.objects.filter(id=job.id).update(
            status=IngestionJob.JobStatus.COMPLETED,
            records_processed=manifest.n_nodes,
            records_inserted=manifest.n_edges,
            completed_at=timezone.now(),
        )

        logger.info(
            'PATHWAY_TOPOLOGY_LOAD concluído (job %s): source_version=%s',
            job.id,
            manifest.source_version,
        )

        return {
            'job_id': str(job.id),
            'n_pathways': manifest.n_pathways,
            'n_nodes': manifest.n_nodes,
            'n_edges': manifest.n_edges,
            'n_signed': manifest.n_signed,
            'n_unsigned': manifest.n_unsigned,
            'n_orphan_symbols': manifest.n_orphan_symbols,
            'source_version': manifest.source_version,
        }
