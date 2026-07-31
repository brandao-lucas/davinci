"""
RegulonLoadService — orquestração Django da carga de regulons CollecTRI/OmniPath.

OmnisPathway Objetivo 2, Fase 3, Passo 3.3.

Responsabilidade:
  1. Derivar tf_allowlist dos nós gene das vias já carregadas em PG (default:
     TODAS as `Pathway` — ver "Universo de vias" abaixo):
     PathwayNode.objects.filter(pathway__kegg_id__in=[...], node_type='gene')
     .values_list('gene_symbol', flat=True).distinct() (exclui vazios).

Universo de vias (generalizado — deixou de ser 3 fixas):
  v1 (Fase 3 inicial) fixava DEFAULT_PATHWAY_IDS em 3 vias KEGG (hoje alias
  de LEGACY_V1_PATHWAY_IDS). Com a bancada expandida para as vias humanas
  completas do KEGG (372 vias, grafo com 49.322 nós / 5.520 símbolos-gene
  distintos), fixar em 3 deixaria o tf_allowlist artificialmente pequeno — a
  allowlist é 100% derivada do grafo carregado (`_derive_tf_allowlist`), não
  de uma lista hardcoded independente dele. `dispatch()`/`run()` agora
  resolvem `pathway_ids=None` para TODAS as `Pathway.kegg_id` em PG no
  momento da chamada (`_resolve_default_pathway_ids`), não para a constante.
  `pathway_ids` explícito (via `--pathway` no command, ou chamada
  programática) continua restringindo a execução — útil para debug ou para
  reproduzir o preset legado (LEGACY_V1_PATHWAY_IDS).
  2. Gate de idempotência: não duplicar job ativo de REGULON_LOAD.
  3. Criar IngestionJob(job_type=REGULON_LOAD).
  4. Chamar rust_engine.load_collectri_regulons(tf_allowlist, dest_dir)
     → RegulonLoadManifest{n_tfs, n_targets, n_edges, n_positive, n_negative,
       n_neutral, source_version, regulon_path (JSONL local)}.
     O Rust faz o fetch CollecTRI, filtra por tf_allowlist, e grava arquivo
     intermediário local (JSONL). NÃO cria tabela PG de regulons (Decisão 3.3
     do plano: sem consumidor adicional além do mapeamento 3.4 — não viola
     Regra #-1 com tabela persistente desnecessária).
  5. Guarda regulon_path no IngestionJob.parameters (caminho local do JSONL).
  6. Marcar IngestionJob COMPLETED com contadores do manifesto.

RISCO registrado (comentário de código):
  OmniPath estava retornando 502 na discovery (2026-07-15). O parser Rust
  segue o schema documentado (colunas: source_genesymbol, target_genesymbol,
  is_stimulation, is_inhibition). Confirmar colunas reais na 1ª ingestão live
  — o endpoint pode ser instável. Se o schema de resposta divergir do
  documentado, handoff para ferris antes de re-rodar.

Fronteira de camadas (Regra #1 / django-rust-boundary):
  - Rust: fetch OmniPath (omnipathdb.org), filtro por tf_allowlist em streaming,
    parse do TSV de regulons, escrita do arquivo JSONL local. NÃO toca PG.
  - Django: deriva tf_allowlist via queryset set-based (ORM puro), gate de
    idempotência, criação do IngestionJob, relatório de contadores. NÃO faz
    fetch HTTP, NÃO parseia regulons.

Arquivo intermediário vs tabela PG:
  O arquivo JSONL local (regulon_path) é o intermediário consumido pelo
  passo 3.4 (readout_mapping_service). Não cria tabela `Regulon` em PG porque
  só o mapeamento 3.4 a consome — criar tabela sem consumidor adicional viola
  Regra #-1. Se um endpoint de inspeção de regulon surgir no futuro, promover
  a tabela (Sugestão 4 do plano).

Catálogo global (sem FK de projeto):
  Mesmo padrão de GeneRole / PathwayTopologyLoadService. IngestionJob exige
  FK de projeto (constraint do modelo) — o caller fornece projeto de tracking.

Segurança (findings M2/M3 — allowlist de host):
  O fetcher Rust aplica allowlist de host (omnipathdb.org) e teto de bytes.
  Django não valida host — fronteira já protegida no Rust.

Licença CollecTRI/OmniPath (GPL/acadêmica):
  Cache local (dest_dir). O dado filtrado (regulon intermediário) é consumido
  apenas pelo mapeamento 3.4. Não redistribuir dado bruto.

Sensitive-data-handling:
  - Nenhum token/credencial (OmniPath público sem autenticação no v1).
  - db_url NÃO é necessária para esta pyfunction (sem COPY em PG).
  - regulon_path gravado em IngestionJob.parameters É EXPOSTO ao cliente:
    `IngestionJobSerializer` (apps/core/serializers/job.py) serializa o campo
    `parameters` por inteiro, sem allowlist de chaves, e
    `GET /projects/{project_pk}/jobs/` devolve isso ao dono do projeto.
    `IngestionJobViewSet.get_queryset()` já filtra por
    `DaVinciProject(user=request.user)`, então não é vazamento cross-usuário
    (Regra #3 preservada) — mas o dono do projeto vê o caminho absoluto do
    arquivo no filesystem do worker (inclui nome de usuário do SO).
    Consequência prática: NADA sensível (credencial, PII, segredo) pode ir
    para `parameters` de nenhum IngestionJob — é por isso que `db_url` e
    `ncbi_api_key`, por exemplo, corretamente nunca são gravados ali. Isso é
    padrão pré-existente do serializer (afeta todos os job_types, não só
    REGULON_LOAD); mitigação (allowlist de chaves ou redação de valores que
    pareçam caminho absoluto) avaliada e NÃO implementada — o frontend lê
    hoje chaves como `synonyms`/`date_from`/`date_to`/`query` de `parameters`
    de outros job_types (davinci-frontend/src/lib/utils/descriptor-diff.ts),
    então qualquer allowlist precisa ser desenhada por job_type para não
    quebrar consumidores existentes, e a severidade aqui é baixa (mesmo
    usuário, sem credencial) — não justifica o escopo maior agora. Se
    decidir revisitar, começar por apps/core/serializers/job.py.

Idempotência:
  Gate verifica REGULON_LOAD ativo (PENDING/RUNNING). O arquivo JSONL é
  regenerado em cada execução (sem estado em PG a deduplicar).

Pré-condição:
  Passo 3.2 (load_pathway_topology) deve ter sido executado para que os nós
  do grafo (PathwayNode) existam em PG — a tf_allowlist é derivada deles.
  Um gate de pré-condição verifica isso antes de criar o job.
"""

from __future__ import annotations

import logging
import tempfile

from django.conf import settings
from django.utils import timezone

from apps.core.models import DaVinciProject, IngestionJob, Pathway, PathwayNode

logger = logging.getLogger(__name__)

# Preset legado (v1, Fase 3 inicial) — as 3 vias KEGG originais (PI3K-Akt,
# MAPK, p53). NÃO é mais o default de dispatch()/run() (ver "Universo de
# vias" na docstring do módulo) — mantido nomeado só para reprodutibilidade
# de execuções antigas / testes que queiram fixar o universo explicitamente.
LEGACY_V1_PATHWAY_IDS = ['hsa04151', 'hsa04010', 'hsa04115']

# Alias retrocompatível (nome antigo) — mesmo conteúdo do preset acima.
# Não usar como default: ver dispatch()/run() (usam
# _resolve_default_pathway_ids() quando pathway_ids=None).
DEFAULT_PATHWAY_IDS = LEGACY_V1_PATHWAY_IDS

# Versão do loader: atualizar quando o contrato Rust mudar de forma incompatível.
LOADER_VERSION = 'collectri-v1'


def _resolve_default_pathway_ids() -> list[str]:
    """
    Resolve o universo padrão de vias: TODAS as `Pathway.kegg_id` carregadas
    em PG neste momento — não a constante `DEFAULT_PATHWAY_IDS` (preset
    legado de 3 vias).

    Chamada sob demanda (dentro de dispatch()/run(), quando `pathway_ids`
    não foi informado) para refletir o catálogo `Pathway` no momento da
    execução, não em tempo de import do módulo.

    Retorna lista vazia se `Pathway` estiver vazio (nenhuma via carregada
    ainda) — o chamador (`_derive_tf_allowlist`) trata isso como "nenhum
    PathwayNode encontrado" e levanta `RegulonGraphNotLoadedError`, o mesmo
    comportamento de antes para o caso "passo 3.2 não rodou".
    """
    return list(
        Pathway.objects.order_by('kegg_id').values_list('kegg_id', flat=True)
    )


class RegulonJobActiveError(Exception):
    """Levantado quando já há um REGULON_LOAD pending/running."""

    def __init__(self, job: IngestionJob):
        self.job = job
        super().__init__(
            f"REGULON_LOAD já ativo (job {job.id}, status={job.status}) "
            f"— dispatch ignorado (idempotência)"
        )


class RegulonGraphNotLoadedError(Exception):
    """
    Levantado quando PathwayNode não existe para as vias configuradas.

    Indica que o passo 3.2 (load_pathway_topology) não foi executado.
    """

    def __init__(self, pathway_ids: list[str]):
        super().__init__(
            f"Nenhum PathwayNode encontrado para as vias {pathway_ids}. "
            f"Execute load_pathway_topology antes de carregar regulons."
        )


class RegulonLoadService:
    """
    Serviço de carga de regulons CollecTRI (OmniPath) para os TFs das vias
    carregadas (default: TODAS as `Pathway` em PG — ver "Universo de vias"
    na docstring do módulo).

    Uso síncrono (management command / bancada de prova) — universo default
    (todas as vias carregadas):
        service = RegulonLoadService(project)
        result = service.run()

    Uso assíncrono (Celery):
        job = RegulonLoadService.dispatch(project)
        # task run_regulon_load(job.id, str(project.id)) em background

    Retorno de run():
        {
            'job_id': str,
            'n_tfs': int,
            'n_targets': int,
            'n_edges': int,
            'n_positive': int,
            'n_negative': int,
            'n_neutral': int,
            'source_version': str,
            'regulon_path': str,      # caminho local do JSONL intermediário
            'tf_allowlist_size': int, # nº de TFs derivados das vias
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
        a task Celery (via run_regulon_load).

        Args:
            project: DaVinciProject de tracking (isolamento de IngestionJob).
            pathway_ids: IDs KEGG cujos TFs compõem o allowlist. None →
                         TODAS as `Pathway` carregadas em PG neste momento
                         (`_resolve_default_pathway_ids`) — lista explícita
                         (incl. `[]`) é respeitada tal como passada.

        Returns:
            IngestionJob recém-criado (status=PENDING).

        Raises:
            RegulonJobActiveError: job ativo encontrado.
            RegulonGraphNotLoadedError: grafo não carregado (pré-condição 3.2).
        """
        from apps.core.tasks.ingestion_tasks import run_regulon_load

        ids = pathway_ids if pathway_ids is not None else _resolve_default_pathway_ids()
        service = cls(project)
        tf_allowlist = service._derive_tf_allowlist(ids)
        service._check_idempotency()

        job = IngestionJob.objects.create(
            project=project,
            job_type=IngestionJob.JobType.REGULON_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={
                'pathway_ids': ids,
                'tf_allowlist_size': len(tf_allowlist),
                'loader_version': LOADER_VERSION,
                # tf_allowlist completa não é salva para evitar parameters muito
                # grandes; o service a recalcula no run() via queryset.
                # db_url NÃO é armazenada (sensitive-data-handling).
            },
        )

        try:
            run_regulon_load.delay(str(job.id), str(project.id))
            logger.info(
                'REGULON_LOAD disparado (job %s) para projeto %s, '
                'tf_allowlist_size=%d, vias=%s',
                job.id,
                project.id,
                len(tf_allowlist),
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
        Executa a carga de regulons de forma síncrona.

        Fluxo:
          1. Deriva tf_allowlist dos PathwayNodes do grafo (ORM set-based).
          2. Gate de idempotência.
          3. Cria/atualiza IngestionJob para RUNNING.
          4. Chama rust_engine.load_collectri_regulons → manifest (JSONL local).
          5. Guarda regulon_path no IngestionJob. Marca COMPLETED.

        Args:
            job: IngestionJob existente (criado pelo dispatch); se None, cria
                 internamente.
            pathway_ids: IDs KEGG cujos TFs compõem o allowlist. None →
                         job.parameters['pathway_ids'] (se `job` foi
                         informado) ou TODAS as `Pathway` carregadas em PG
                         neste momento (`_resolve_default_pathway_ids`).

        Returns:
            dict com n_tfs, n_targets, n_edges, regulon_path, tf_allowlist_size.
        """
        import rust_engine

        # Resolve pathway_ids
        if pathway_ids is None:
            if job is not None and job.parameters.get('pathway_ids') is not None:
                pathway_ids = job.parameters['pathway_ids']
            else:
                pathway_ids = _resolve_default_pathway_ids()

        tf_allowlist = self._derive_tf_allowlist(pathway_ids)

        if job is None:
            try:
                self._check_idempotency()
            except RegulonJobActiveError:
                raise

        if job is None:
            job = IngestionJob.objects.create(
                project=self.project,
                job_type=IngestionJob.JobType.REGULON_LOAD,
                status=IngestionJob.JobStatus.RUNNING,
                parameters={
                    'pathway_ids': pathway_ids,
                    'tf_allowlist_size': len(tf_allowlist),
                    'loader_version': LOADER_VERSION,
                },
            )
        else:
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.RUNNING,
                started_at=timezone.now(),
            )

        try:
            result = self._execute(job, tf_allowlist, rust_engine)
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

    def _derive_tf_allowlist(self, pathway_ids: list[str]) -> list[str]:
        """
        Deriva a lista de TFs dos PathwayNodes do grafo já carregado.

        Queryset set-based: gene_symbol UPPERCASE não-vazio, distintos,
        filtrando por node_type='gene' nas vias especificadas.

        Pré-condição: PathwayNode deve existir (passo 3.2 executado).

        Raises:
            RegulonGraphNotLoadedError: nenhum nó encontrado para as vias.
        """
        # Filtra nós de gene das vias especificadas, excluindo símbolos vazios
        symbols = list(
            PathwayNode.objects.filter(
                pathway__kegg_id__in=pathway_ids,
                node_type=PathwayNode.NodeType.GENE,
            )
            .exclude(gene_symbol='')
            .values_list('gene_symbol', flat=True)
            .distinct()
            .order_by('gene_symbol')
        )

        if not symbols:
            raise RegulonGraphNotLoadedError(pathway_ids)

        logger.info(
            'tf_allowlist derivado: %d símbolos únicos nas vias %s',
            len(symbols),
            pathway_ids,
        )
        return symbols

    def _check_idempotency(self) -> None:
        """
        Verifica se há REGULON_LOAD ativo (PENDING/RUNNING).

        Raises:
            RegulonJobActiveError: job ativo encontrado.
        """
        active_job = IngestionJob.objects.filter(
            job_type=IngestionJob.JobType.REGULON_LOAD,
            status__in=[
                IngestionJob.JobStatus.PENDING,
                IngestionJob.JobStatus.RUNNING,
            ],
        ).first()
        if active_job:
            raise RegulonJobActiveError(active_job)

    def _execute(
        self,
        job: IngestionJob,
        tf_allowlist: list[str],
        rust_engine,
    ) -> dict:
        """
        Núcleo da carga: chama Rust com tf_allowlist → manifesto com JSONL local.

        RISCO: OmniPath estava com 502 na discovery (2026-07-15). O parser Rust
        segue o schema documentado (source_genesymbol / target_genesymbol /
        is_stimulation / is_inhibition). Confirmar colunas reais na 1ª ingestão
        live. Se divergir → handoff para ferris antes de re-rodar.

        O arquivo JSONL (regulon_path) fica em dest_dir — caminho local salvo
        em IngestionJob.parameters['regulon_path'] para uso pelo passo 3.4.
        ATENÇÃO: o caminho é local da máquina do worker Celery; se 3.3 e 3.4
        rodarem em workers distintos (sem volume compartilhado), serializar o
        JSONL para object storage ou rodar os dois no mesmo worker.
        """
        # dest_dir NÃO é TemporaryDirectory aqui — o arquivo JSONL precisa
        # sobreviver ao escopo deste método para ser lido pelo passo 3.4.
        # O caller (management command / task Celery) é responsável pelo cleanup.
        # A escolha de um caminho persistente (não /tmp volátil) é intencional:
        # o JSONL intermediário é efêmero de sessão, não de chamada.
        import os

        # Diretório de saída persistente para o intermediário de regulon
        # (sob diagnostics/cache — gitignored via diagnostics/cache/.gitignore.
        # O dado bruto (CollecTRI/OmniPath, licença acadêmica) NÃO pode ficar
        # em caminho commitável — usar settings.REPO_ROOT, não contar
        # os.path.dirname(__file__) manualmente: BASE_DIR aponta para config/,
        # não para a raiz do repo, e contagem de níveis quebra silenciosamente
        # se o arquivo mudar de lugar.)
        cache_dir = os.path.join(
            str(settings.REPO_ROOT), 'diagnostics', 'cache', 'omnipath',
        )
        os.makedirs(cache_dir, exist_ok=True)

        logger.info(
            'Chamando rust_engine.load_collectri_regulons '
            '(job %s, tf_allowlist_size=%d)',
            job.id,
            len(tf_allowlist),
        )

        manifest = rust_engine.load_collectri_regulons(
            tf_allowlist=tf_allowlist,
            dest_dir=cache_dir,
        )

        logger.info(
            'load_collectri_regulons concluído: n_tfs=%d, n_targets=%d, '
            'n_edges=%d, n_positive=%d, n_negative=%d, n_neutral=%d',
            manifest.n_tfs,
            manifest.n_targets,
            manifest.n_edges,
            manifest.n_positive,
            manifest.n_negative,
            manifest.n_neutral,
        )

        # Persiste regulon_path no job para que o passo 3.4 (map_readouts)
        # saiba onde ler o JSONL intermediário.
        # NOTA: caminho local do worker — não expor ao cliente.
        IngestionJob.objects.filter(id=job.id).update(
            status=IngestionJob.JobStatus.COMPLETED,
            records_processed=manifest.n_edges,
            records_inserted=manifest.n_tfs,
            completed_at=timezone.now(),
            parameters={
                **job.parameters,
                'regulon_path': manifest.regulon_path,
                'source_version': manifest.source_version,
            },
        )
        # Reload para ter parameters atualizado em memória
        job.refresh_from_db()

        logger.info(
            'REGULON_LOAD concluído (job %s): source_version=%s, '
            'regulon_path=<omitido>',
            job.id,
            manifest.source_version,
        )

        return {
            'job_id': str(job.id),
            'n_tfs': manifest.n_tfs,
            'n_targets': manifest.n_targets,
            'n_edges': manifest.n_edges,
            'n_positive': manifest.n_positive,
            'n_negative': manifest.n_negative,
            'n_neutral': manifest.n_neutral,
            'source_version': manifest.source_version,
            'regulon_path': manifest.regulon_path,
            'tf_allowlist_size': len(tf_allowlist),
        }
