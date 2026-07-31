"""
PathwayScoringService — orquestração Django do motor PFS v1.

OmnisPathway Objetivo 2, Fase 4 (RWR assinado + leitura por Regras + escore
contínuo por permutação). Ver
.claude/plans/2026-07-16-omnispathway-obj2-fase4-motor-pfs-v1.md (D1-D10 +
ADENDO) para o desenho completo.

Universo de vias (generalizado — deixou de ser 3 fixas):
  v1 (Fase 4 inicial) fixava DEFAULT_PATHWAY_KEGG_IDS em 3 vias KEGG (hoje
  alias de LEGACY_V1_PATHWAY_KEGG_IDS). Com a bancada expandida para as vias
  humanas completas do KEGG (372 vias, grafo com 49.322 nós / 5.520
  símbolos-gene distintos), fixar em 3 pontuaria só 3 das 372 vias
  silenciosamente (aparência de run completo, escala desatualizada) — o
  mesmo modo de falha já corrigido em regulon_load_service e
  readout_mapping_service. `dispatch()`/`run()` agora resolvem
  `pathway_kegg_ids=None` para TODAS as `Pathway.kegg_id` em PG no momento
  da execução (`_resolve_default_pathway_ids`), não para a constante.
  Lista explícita (via `--pathways` do command, ou chamada programática)
  continua sendo respeitada tal como passada — INCLUSIVE lista vazia (`[]`).
  `_resolve_default_pathway_ids()` é set-based (uma única query
  `Pathway.objects.values_list('kegg_id', flat=True)`) — não gera query por
  via; os pré-checks (`_check_graph_loaded`/`_check_readouts_mapped`) já
  usam `__in=pathway_ids`, também set-based.

Responsabilidade (D1 TRAVADO: núcleo 100% Rust, órbita 100% Django):
  1. Resolver AS DUAS matrizes de readout dentro do projeto (isolamento,
     Regra #3): fosfoproteoma (omics_layer='proteomic',
     feature_axis='phospho_site', loader_version='phospho-v1') e proteoma
     (omics_layer='proteomic', feature_axis='gene'), via ProjectDataset com
     _ACTIVE_STATUSES.
  2. Pré-checagens com mensagem acionável (falha alta, nunca silenciosa):
       (a) grafo carregado — Pathway das vias pedidas existe?
       (b) readouts mapeados — PathwayReadoutFeature do mapping_version pedido
           existe para essas vias?
       (c) features catalogadas — OmicMatrixFeature populado nas DUAS
           matrizes (backfill_matrix_features já rodou)?
       (d) pares existem NAS DUAS matrizes — OmicSamplePairing
           (validated_overlap=True). A natural key de OmicSamplePairing é
           (matrix, case_id): pares da matriz de proteoma NÃO valem para a
           matriz de fosfo (e vice-versa) — pareear só uma zera a Regra 1
           silenciosamente se não for checado.
     NÃO checa existência de seed (VariantEffectSeed) como gate — zero
     sementes é um estado VÁLIDO do motor (produz escore degenerado com
     contadores explicando `n_seeds_on_graph=0`, não crash). É exatamente o
     estado real da bancada em 2026-07-30 (Fase 2 seeds ainda não
     carregadas na ingestão live) — ver ADENDO do plano.
  3. Gate de reprodutibilidade (D3 do handoff do cartografo, mecanismo
     definido aqui): `method_version` é a NATURAL KEY inteira de
     PathwayActivityScore junto com (pathway, sample) — logo duas
     configurações diferentes rodadas sob a MESMA string de
     `method_version` colidiriam via `ON CONFLICT DO UPDATE` do COPY,
     SOBRESCREVENDO o benchmark anterior sem erro visível. Mecanismo:
       - Se NENHUM PathwayActivityScore existe ainda para este
         `method_version`: nenhum gate — este run estabelece a
         proveniência (primeira gravação).
       - Se JÁ existem escores para este `method_version`: busca o
         IngestionJob(PATHWAY_SCORE_RUN, COMPLETED, dry_run=False) MAIS
         RECENTE com esse `method_version` em `parameters` (busca GLOBAL,
         sem filtro de projeto — mesma decisão deliberada de
         `readout_mapping_service._resolve_regulon_path`: o recurso
         protegido, PathwayActivityScore, é catálogo global, não
         project-scoped) e compara os campos que AFETAM O RESULTADO
         NUMÉRICO (`GATE_FIELDS`: seed_method_versions, mapping_version,
         restart, unsigned_weight, n_permutations, rng_seed,
         min_regulon_targets, phospho_matrix_id, proteome_matrix_id) contra
         a configuração do run atual.
           - Se baterem: prossegue (é literalmente o mesmo experimento,
             UPSERT idempotente é o comportamento correto).
           - Se divergirem: ERRO (`PfsReproducibilityGateError`) exigindo
             um `--method-version` novo OU `--force` explícito.
           - Se não houver job de proveniência (dado inserido por outro
             caminho, ou histórico indisponível): trata como divergência
             não verificável — exige `--force` (postura conservadora:
             "não consigo provar que é seguro" é tratado como "não é
             seguro" nesta fronteira).
       - `--force` ignora o gate (loga warning) — é a saída explícita para
         quando o operador SABE que está sobrescrevendo de propósito
         (ex.: parâmetro corrigido, quer que a mesma versão reflita o
         parâmetro novo).
     `pathway_kegg_ids` e `dry_run` NÃO entram no gate: rodar um
     subconjunto de vias não corrompe as vias de fora do subconjunto
     (linhas são por `(pathway, sample)`), e `dry_run` nunca escreve.
  4. Gate de idempotência (job PATHWAY_SCORE_RUN PENDING/RUNNING para o
     mesmo `method_version`, busca GLOBAL) — não duplica execução
     concorrente.
  5. Cria IngestionJob(PATHWAY_SCORE_RUN) com `parameters` = configuração
     completa (vias, versões de seed, mapping_version, restart,
     unsigned_weight, n_permutations, rng_seed, min_regulon_targets, ids de
     matriz, method_version, force, dry_run) — NUNCA `db_url`, NUNCA
     `storage_key`.
  6. Baixa OS DOIS Parquets (fosfo + proteoma) via default_storage para um
     `tempfile.TemporaryDirectory` (streaming, `copyfileobj`), monta
     `db_url` (NUNCA logada), chama
     `rust_engine.run_pfs_scoring(...) -> PfsRunManifest`, cleanup
     automático do tmp.
  7. Marca job COMPLETED com os contadores do manifesto. Relatório expõe
     OS DOIS DENOMINADORES de readout (`n_readouts_mapped` vs
     `n_readouts_with_value`) e a razão `n_seeds_on_graph / (on + off)` —
     é o diagnóstico ordenado para o caso z≈0 em toda a coorte
     (anti-p-hacking: a resposta a "o escore deu zero" é olhar cobertura,
     nunca ajustar parâmetro para "dar sinal").

Fronteira de camadas (Regra #1 / django-rust-boundary):
  Django NÃO faz nenhuma conta numérica sobre os dados ômicos. NÃO abre os
  Parquets, NÃO calcula delta/z/média. `seed_coverage_ratio` e
  `readout_coverage_ratio` no relatório são DIVISÃO DE ESCALARES do
  manifesto (contadores agregados já computados pelo Rust) — não é
  processamento de dado bruto, é apresentação de um resumo já pronto.

Isolamento (firebase-auth-guard / Regra #3):
  - project deve pertencer ao request.user antes de chegar aqui.
  - As duas OmicMatrix são resolvidas via ProjectDataset do projeto
    (curation_status ativo) — sem acesso cross-project.
  - PathwayActivityScore é catálogo GLOBAL derivado, SEM FK de projeto
    (espelha VariantEffectSeed/OmicSamplePairing/Pathway). Isolamento de
    leitura futura vive em `sample__dataset__in_projects__project` — sem
    endpoint neste v1.
  - O gate de reprodutibilidade e o de idempotência de job buscam
    GLOBALMENTE (sem filtro de projeto) porque o recurso que protegem
    (PathwayActivityScore por method_version) é global — mesma decisão já
    tomada e documentada em `readout_mapping_service._resolve_regulon_path`.

Sensitive-data-handling:
  - db_url construída a partir de settings.DATABASES['default'] — NUNCA
    logada, NUNCA gravada em IngestionJob.parameters, NUNCA retornada.
  - storage_key das matrizes e caminhos locais do tmpdir não são logados.
  - Nenhuma credencial neste fluxo.

Catálogo derivado:
  PathwayActivityScore é derivado (Rust COPY UPSERT idempotente na NK
  pathway/sample/method_version). Regra #2 (curadoria) não se aplica.

Padrões reutilizados:
  - resolve_matrix: espelha CnvSeedService.resolve_matrix /
    ReadoutMappingService._resolve_phospho_matrix/_resolve_proteome_matrix.
  - IngestionJob dispatch/run: mesmo padrão de CnvSeedService.
  - tempfile.TemporaryDirectory + default_storage.open + copyfileobj:
    mesmo padrão de CnvSeedService, agora para DOIS arquivos.
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
    IngestionJob,
    OmicMatrix,
    OmicMatrixFeature,
    OmicSamplePairing,
    Pathway,
    PathwayActivityScore,
    PathwayReadoutFeature,
    ProjectDataset,
)
from apps.core.services.phospho_matrix_load_service import (
    LOADER_VERSION as PHOSPHO_LOADER_VERSION,
)
from apps.core.services.readout_mapping_service import (
    DEFAULT_MAPPING_VERSION,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Defaults do motor PFS v1 (D3/D5/D6/D7 do plano da Fase 4)
# ─────────────────────────────────────────────────────────────────────────────

# Preset legado (v1, Fase 4 inicial) — as 3 vias KEGG originais (PI3K-Akt,
# MAPK, p53). NÃO é mais o default de dispatch()/run() (ver "Universo de
# vias" na docstring do módulo) — mantido nomeado só para reprodutibilidade
# de execuções antigas / testes que queiram fixar o universo explicitamente.
LEGACY_V1_PATHWAY_KEGG_IDS = ['hsa04151', 'hsa04010', 'hsa04115']

# Alias retrocompatível (nome antigo) — mesmo conteúdo do preset acima.
# Não usar como default: dispatch()/run() resolvem `pathway_kegg_ids=None`
# para TODAS as `Pathway.kegg_id` em PG no momento da execução
# (`_resolve_default_pathway_ids`), não para esta constante.
DEFAULT_PATHWAY_KEGG_IDS = LEGACY_V1_PATHWAY_KEGG_IDS

# Precedência de seed por (sample, gene) TRAVADA em D3 (cnv-v2 > snv-v1 >
# cnv-v1) — a precedência é aplicada dentro do Rust; esta é a LISTA de
# versões consideradas, codificada como parte da proveniência de
# 'fase4-pfs-v1'. Mudar esta lista sem mudar --method-version é exatamente
# o que o gate de reprodutibilidade (D3) impede.
DEFAULT_SEED_METHOD_VERSIONS = ['fase2-cnv-v2', 'fase2-snv-v1', 'fase2-cnv-v1']

DEFAULT_METHOD_VERSION = 'fase4-pfs-v1'
DEFAULT_RESTART = 0.3
DEFAULT_UNSIGNED_WEIGHT = 0.3
DEFAULT_N_PERMUTATIONS = 1000
DEFAULT_RNG_SEED = 42
DEFAULT_MIN_REGULON_TARGETS = 5

# Statuses de ProjectDataset considerados ativos (consistência com
# CnvSeedService / SamplePairingService).
_ACTIVE_STATUSES = (
    ProjectDataset.CurationStatus.INCLUDED,
    ProjectDataset.CurationStatus.QUEUED_DOWNLOAD,
    ProjectDataset.CurationStatus.DOWNLOADED,
    ProjectDataset.CurationStatus.PENDING,
)

def _resolve_default_pathway_ids() -> list[str]:
    """
    Resolve o universo padrão de vias: TODAS as `Pathway.kegg_id` carregadas
    em PG neste momento — não a constante `DEFAULT_PATHWAY_KEGG_IDS` (preset
    legado de 3 vias).

    Chamada sob demanda (dentro de dispatch()/run(), quando
    `pathway_kegg_ids` não foi informado) para refletir o catálogo `Pathway`
    no momento da execução, não em tempo de import do módulo. Query única,
    set-based — não gera N queries para N vias.

    Retorna lista vazia se `Pathway` estiver vazio (grafo ainda não
    carregado) — `_check_graph_loaded` trata isso da mesma forma que hoje
    trata qualquer via ausente (`PfsGraphNotLoadedError`, instrui
    `load_pathway_topology`).
    """
    return list(
        Pathway.objects.order_by('kegg_id').values_list('kegg_id', flat=True)
    )


# Campos da configuração que AFETAM O RESULTADO NUMÉRICO — usados pelo gate
# de reprodutibilidade (D3). `pathway_kegg_ids` e `dry_run` ficam de fora
# deliberadamente (ver docstring do módulo).
GATE_FIELDS = (
    'seed_method_versions',
    'mapping_version',
    'restart',
    'unsigned_weight',
    'n_permutations',
    'rng_seed',
    'min_regulon_targets',
    'phospho_matrix_id',
    'proteome_matrix_id',
)


# ─────────────────────────────────────────────────────────────────────────────
# Exceções — cada uma diz qual command rodar (falhar alto, não silencioso)
# ─────────────────────────────────────────────────────────────────────────────

class PfsScoringError(Exception):
    """Classe base das exceções de pré-condição do PathwayScoringService."""


class PfsMatrixNotFoundError(PfsScoringError):
    """Levantado quando a matriz de fosfo ou de proteoma não é resolvível."""

    def __init__(self, label: str, project: DaVinciProject, detail: str):
        self.label = label
        hint = {
            'fosfoproteoma': 'load_phospho_matrix',
            'proteoma': 'load_cptac_matrix',
        }.get(label, '?')
        super().__init__(
            f"OmicMatrix de {label} não encontrada/ambígua para o projeto "
            f"{project.id}: {detail} Execute `manage.py {hint} "
            f"--project {project.id}` primeiro."
        )


class PfsGraphNotLoadedError(PfsScoringError):
    """Levantado quando alguma via pedida não tem grafo carregado."""

    def __init__(self, missing_kegg_ids: set[str]):
        self.missing_kegg_ids = missing_kegg_ids
        super().__init__(
            f"Grafo não carregado para as vias: {sorted(missing_kegg_ids)}. "
            f"Execute `manage.py load_pathway_topology --project <UUID> "
            f"--pathways {','.join(sorted(missing_kegg_ids))}` antes de "
            f"pontuar."
        )


class PfsReadoutsNotMappedError(PfsScoringError):
    """Levantado quando não há PathwayReadoutFeature para o mapping_version."""

    def __init__(self, mapping_version: str, pathway_ids: list[str]):
        self.mapping_version = mapping_version
        super().__init__(
            f"Nenhum PathwayReadoutFeature encontrado para "
            f"mapping_version={mapping_version!r} nas vias {pathway_ids}. "
            f"Execute `manage.py map_readouts --mapping-version "
            f"{mapping_version}` antes de pontuar."
        )


class PfsFeaturesNotCataloguedError(PfsScoringError):
    """Levantado quando OmicMatrixFeature está vazio para uma das matrizes."""

    def __init__(self, matrix: OmicMatrix, label: str):
        self.matrix = matrix
        self.label = label
        super().__init__(
            f"OmicMatrixFeature vazio para a matriz de {label} "
            f"(matrix_id={matrix.id}). Execute `manage.py "
            f"backfill_matrix_features --matrix-id {matrix.id}` antes de "
            f"pontuar — o motor precisa do catálogo de features para "
            f"resolver feature_key sem reabrir o Parquet no Django."
        )


class PfsPairingMissingError(PfsScoringError):
    """Levantado quando não há par validado na matriz de fosfo/proteoma."""

    def __init__(self, matrix: OmicMatrix, label: str):
        self.matrix = matrix
        self.label = label
        super().__init__(
            f"Nenhum OmicSamplePairing(validated_overlap=True) encontrado "
            f"para a matriz de {label} (matrix_id={matrix.id}). A natural "
            f"key de OmicSamplePairing é (matrix, case_id) — pares "
            f"validados na OUTRA matriz NÃO valem aqui (pareamento é POR "
            f"MATRIZ). Execute `manage.py pair_samples --project <UUID> "
            f"--matrix {matrix.id}` antes de pontuar."
        )


class PfsJobActiveError(PfsScoringError):
    """Levantado quando já há um PATHWAY_SCORE_RUN ativo para este method_version."""

    def __init__(self, job: IngestionJob):
        self.job = job
        super().__init__(
            f"PATHWAY_SCORE_RUN já ativo (job {job.id}, status={job.status}, "
            f"projeto {job.project_id}) para method_version="
            f"{job.parameters.get('method_version')!r} — dispatch ignorado "
            f"(idempotência). Aguarde o job concluir ou cancele-o "
            f"manualmente."
        )


class PfsReproducibilityGateError(PfsScoringError):
    """
    Levantado quando já existem PathwayActivityScore para este
    method_version e os parâmetros do run atual divergem (ou não podem ser
    verificados) contra os que produziram esse benchmark — ver mecanismo
    completo na docstring do módulo (item 3).
    """

    def __init__(
        self,
        method_version: str,
        reason: str,
        prior: dict | None = None,
        current: dict | None = None,
    ):
        self.method_version = method_version
        self.reason = reason
        self.prior = prior
        self.current = current

        base = (
            f"Gate de reprodutibilidade: já existem PathwayActivityScore "
            f"com method_version={method_version!r}. "
        )
        if reason == 'mismatch' and prior is not None and current is not None:
            diffs = [
                f"  {key}: gravado={prior.get(key)!r} vs solicitado="
                f"{current.get(key)!r}"
                for key in GATE_FIELDS
                if prior.get(key) != current.get(key)
            ]
            detail = (
                "Os parâmetros do run atual DIVERGEM dos que produziram "
                "esse benchmark:\n" + "\n".join(diffs) + "\n\n"
            )
        else:
            detail = (
                "Não foi encontrado nenhum IngestionJob(PATHWAY_SCORE_RUN, "
                "COMPLETED, dry_run=False) que registre a configuração que "
                "os produziu — não é possível verificar se o run atual usa "
                "os mesmos parâmetros.\n\n"
            )

        super().__init__(
            base + detail +
            "method_version é PARTE DA NATURAL KEY de PathwayActivityScore "
            "— reexecutar com parâmetros diferentes sob a MESMA string "
            "sobrescreveria o benchmark existente via ON CONFLICT DO "
            "UPDATE, sem erro visível (D3, Fase 4). Escolha um "
            "--method-version novo e distinto para esta configuração, ou "
            "passe --force para confirmar explicitamente a sobrescrita."
        )


class PathwayScoringService:
    """
    Serviço de orquestração do motor PFS v1 (score_pathways).

    Uso síncrono (management command / bancada de prova):
        service = PathwayScoringService(project)
        report = service.run(pathway_kegg_ids=[...], method_version='...')

    Uso assíncrono (Celery):
        job = PathwayScoringService.dispatch(project, ...)
        # task run_pathway_scoring(job.id, str(project.id)) prossegue em
        # background.

    Retorno de run() — ver `_execute` para a shape completa (contadores do
    PfsRunManifest + seed_coverage_ratio + readout_coverage_ratio +
    zero_seeds_on_graph).
    """

    def __init__(self, project: DaVinciProject):
        self.project = project

    # ─────────────────────────────────────────────────────────────────────
    # API pública
    # ─────────────────────────────────────────────────────────────────────

    @classmethod
    def dispatch(
        cls,
        project: DaVinciProject,
        *,
        pathway_kegg_ids: list[str] | None = None,
        method_version: str = DEFAULT_METHOD_VERSION,
        seed_method_versions: list[str] | None = None,
        mapping_version: str = DEFAULT_MAPPING_VERSION,
        restart: float = DEFAULT_RESTART,
        unsigned_weight: float = DEFAULT_UNSIGNED_WEIGHT,
        n_permutations: int = DEFAULT_N_PERMUTATIONS,
        rng_seed: int = DEFAULT_RNG_SEED,
        min_regulon_targets: int = DEFAULT_MIN_REGULON_TARGETS,
        force: bool = False,
        dry_run: bool = False,
    ) -> IngestionJob:
        """
        Pré-checks + gates + criação do IngestionJob. Não executa o motor —
        apenas valida pré-condições e enfileira a task Celery.

        Args:
            pathway_kegg_ids: IDs KEGG a pontuar. None → TODAS as `Pathway`
                               carregadas em PG neste momento
                               (`_resolve_default_pathway_ids`) — lista
                               explícita (incl. `[]`) é respeitada tal como
                               passada.

        Raises:
            PfsMatrixNotFoundError, PfsGraphNotLoadedError,
            PfsReadoutsNotMappedError, PfsFeaturesNotCataloguedError,
            PfsPairingMissingError, PfsJobActiveError,
            PfsReproducibilityGateError.
        """
        from apps.core.tasks.ingestion_tasks import run_pathway_scoring

        service = cls(project)
        pathway_ids = (
            list(pathway_kegg_ids)
            if pathway_kegg_ids is not None
            else _resolve_default_pathway_ids()
        )
        seed_versions = list(seed_method_versions or DEFAULT_SEED_METHOD_VERSIONS)

        phospho_matrix = service.resolve_phospho_matrix(project)
        proteome_matrix = service.resolve_proteome_matrix(project)

        service._check_graph_loaded(pathway_ids)
        service._check_readouts_mapped(pathway_ids, mapping_version)
        service._check_features_catalogued(phospho_matrix, proteome_matrix)
        service._check_pairing(phospho_matrix, proteome_matrix)

        config = service._build_config(
            pathway_kegg_ids=pathway_ids,
            phospho_matrix=phospho_matrix,
            proteome_matrix=proteome_matrix,
            seed_method_versions=seed_versions,
            mapping_version=mapping_version,
            restart=restart,
            unsigned_weight=unsigned_weight,
            n_permutations=n_permutations,
            rng_seed=rng_seed,
            min_regulon_targets=min_regulon_targets,
        )

        service._check_no_active_job(method_version)
        if not dry_run:
            service._check_reproducibility_gate(method_version, config, force)

        parameters = {
            **config,
            'pathway_kegg_ids': pathway_ids,
            'method_version': method_version,
            'force': force,
            'dry_run': dry_run,
            # db_url NÃO é armazenada (sensitive-data-handling)
        }

        job = IngestionJob.objects.create(
            project=project,
            job_type=IngestionJob.JobType.PATHWAY_SCORE_RUN,
            status=IngestionJob.JobStatus.PENDING,
            parameters=parameters,
        )

        try:
            run_pathway_scoring.delay(str(job.id), str(project.id))
            logger.info(
                'PATHWAY_SCORE_RUN disparado (job %s) para projeto %s / '
                'method_version=%s',
                job.id,
                project.id,
                method_version,
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
        *,
        pathway_kegg_ids: list[str] | None = None,
        method_version: str = DEFAULT_METHOD_VERSION,
        seed_method_versions: list[str] | None = None,
        mapping_version: str = DEFAULT_MAPPING_VERSION,
        restart: float = DEFAULT_RESTART,
        unsigned_weight: float = DEFAULT_UNSIGNED_WEIGHT,
        n_permutations: int = DEFAULT_N_PERMUTATIONS,
        rng_seed: int = DEFAULT_RNG_SEED,
        min_regulon_targets: int = DEFAULT_MIN_REGULON_TARGETS,
        force: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """
        Executa o run do motor PFS de forma síncrona.

        Fluxo:
          1. Resolve as duas OmicMatrix (isolamento via ProjectDataset).
          2. Pré-checks: grafo / readouts mapeados / features catalogadas /
             pares nas duas matrizes.
          3. Se `job` é None (chamada direta síncrona): gate de
             idempotência + gate de reprodutibilidade + cria IngestionJob.
             Se `job` foi passado (vindo de `dispatch` via Celery): os
             gates já foram checados no dispatch — os parâmetros efetivos
             são lidos de `job.parameters` (mesmo padrão de
             CnvSeedService.run).
          4. Download dos dois Parquets → tempfile local.
          5. Chama rust_engine.run_pfs_scoring(...).
          6. Cleanup automático do tmpdir. Marca job COMPLETED.

        Args:
            job: IngestionJob existente (criado por dispatch); se None, cria
                 internamente (modo síncrono direto do management command).
            pathway_kegg_ids: IDs KEGG a pontuar. None → usa
                               `job.parameters['pathway_kegg_ids']` (se
                               `job` foi informado — os pré-checks abaixo
                               precisam refletir o universo REAL do job, não
                               um default stale) ou TODAS as `Pathway`
                               carregadas em PG neste momento
                               (`_resolve_default_pathway_ids`). Lista
                               explícita (incl. `[]`) é respeitada tal como
                               passada.
            demais args: ver DEFAULT_* no topo do módulo.

        Returns:
            dict — ver `_execute` para a shape completa.
        """
        import rust_engine

        if pathway_kegg_ids is not None:
            pathway_ids = list(pathway_kegg_ids)
        elif job is not None and job.parameters.get('pathway_kegg_ids') is not None:
            pathway_ids = list(job.parameters['pathway_kegg_ids'])
        else:
            pathway_ids = _resolve_default_pathway_ids()
        seed_versions = list(seed_method_versions or DEFAULT_SEED_METHOD_VERSIONS)

        phospho_matrix = self.resolve_phospho_matrix(self.project)
        proteome_matrix = self.resolve_proteome_matrix(self.project)

        self._check_graph_loaded(pathway_ids)
        self._check_readouts_mapped(pathway_ids, mapping_version)
        self._check_features_catalogued(phospho_matrix, proteome_matrix)
        self._check_pairing(phospho_matrix, proteome_matrix)

        if job is None:
            self._check_no_active_job(method_version)

            config = self._build_config(
                pathway_kegg_ids=pathway_ids,
                phospho_matrix=phospho_matrix,
                proteome_matrix=proteome_matrix,
                seed_method_versions=seed_versions,
                mapping_version=mapping_version,
                restart=restart,
                unsigned_weight=unsigned_weight,
                n_permutations=n_permutations,
                rng_seed=rng_seed,
                min_regulon_targets=min_regulon_targets,
            )
            if not dry_run:
                self._check_reproducibility_gate(method_version, config, force)

            parameters = {
                **config,
                'pathway_kegg_ids': pathway_ids,
                'method_version': method_version,
                'force': force,
                'dry_run': dry_run,
            }
            job = IngestionJob.objects.create(
                project=self.project,
                job_type=IngestionJob.JobType.PATHWAY_SCORE_RUN,
                status=IngestionJob.JobStatus.RUNNING,
                parameters=parameters,
            )
        else:
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.RUNNING,
                started_at=timezone.now(),
            )
            # Parâmetros efetivos vêm do job pré-criado pelo dispatch (gates
            # já foram checados lá) — mesmo padrão de CnvSeedService.run.
            # pathway_ids já foi resolvido de job.parameters acima (antes
            # dos pré-checks, para que eles validem o universo REAL do job).
            p = job.parameters or {}
            method_version = p.get('method_version', method_version)
            seed_versions = p.get('seed_method_versions', seed_versions)
            mapping_version = p.get('mapping_version', mapping_version)
            restart = p.get('restart', restart)
            unsigned_weight = p.get('unsigned_weight', unsigned_weight)
            n_permutations = p.get('n_permutations', n_permutations)
            rng_seed = p.get('rng_seed', rng_seed)
            min_regulon_targets = p.get('min_regulon_targets', min_regulon_targets)
            dry_run = p.get('dry_run', dry_run)

        try:
            result = self._execute(
                job=job,
                pathway_ids=pathway_ids,
                phospho_matrix=phospho_matrix,
                proteome_matrix=proteome_matrix,
                seed_method_versions=seed_versions,
                mapping_version=mapping_version,
                method_version=method_version,
                restart=restart,
                unsigned_weight=unsigned_weight,
                n_permutations=n_permutations,
                rng_seed=rng_seed,
                min_regulon_targets=min_regulon_targets,
                dry_run=dry_run,
                rust_engine=rust_engine,
            )
            return result
        except Exception as exc:
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=str(exc),
                completed_at=timezone.now(),
            )
            raise

    # ─────────────────────────────────────────────────────────────────────
    # Resolução de matrizes (espelha CnvSeedService.resolve_matrix /
    # ReadoutMappingService._resolve_*_matrix)
    # ─────────────────────────────────────────────────────────────────────

    @classmethod
    def _resolve_matrix(
        cls,
        project: DaVinciProject,
        *,
        omics_layer: str,
        feature_axis: str,
        loader_version: str | None,
        label: str,
    ) -> OmicMatrix:
        dataset_ids = ProjectDataset.objects.filter(
            project=project,
            curation_status__in=_ACTIVE_STATUSES,
        ).values_list('dataset_id', flat=True)

        qs = OmicMatrix.objects.select_related('dataset').filter(
            dataset_id__in=dataset_ids,
            omics_layer=omics_layer,
            feature_axis=feature_axis,
        )
        if loader_version:
            qs = qs.filter(loader_version=loader_version)

        matrices = list(qs)
        if not matrices:
            raise PfsMatrixNotFoundError(label, project, 'nenhuma encontrada.')
        if len(matrices) > 1:
            ids = [m.id for m in matrices]
            raise PfsMatrixNotFoundError(
                label, project, f'múltiplas encontradas: {ids}.'
            )
        return matrices[0]

    @classmethod
    def resolve_phospho_matrix(cls, project: DaVinciProject) -> OmicMatrix:
        """Resolve a OmicMatrix de fosfoproteoma (Regra 1)."""
        return cls._resolve_matrix(
            project,
            omics_layer='proteomic',
            feature_axis=OmicMatrix.FeatureAxis.PHOSPHO_SITE,
            loader_version=PHOSPHO_LOADER_VERSION,
            label='fosfoproteoma',
        )

    @classmethod
    def resolve_proteome_matrix(cls, project: DaVinciProject) -> OmicMatrix:
        """Resolve a OmicMatrix de proteoma gene-level (Regra 2)."""
        return cls._resolve_matrix(
            project,
            omics_layer='proteomic',
            feature_axis=OmicMatrix.FeatureAxis.GENE,
            loader_version=None,
            label='proteoma',
        )

    # ─────────────────────────────────────────────────────────────────────
    # Pré-checks
    # ─────────────────────────────────────────────────────────────────────

    def _check_graph_loaded(self, pathway_ids: list[str]) -> None:
        found = set(
            Pathway.objects.filter(kegg_id__in=pathway_ids)
            .values_list('kegg_id', flat=True)
        )
        missing = set(pathway_ids) - found
        if missing:
            raise PfsGraphNotLoadedError(missing)

    def _check_readouts_mapped(
        self, pathway_ids: list[str], mapping_version: str
    ) -> None:
        exists = PathwayReadoutFeature.objects.filter(
            mapping_version=mapping_version,
            node__pathway__kegg_id__in=pathway_ids,
        ).exists()
        if not exists:
            raise PfsReadoutsNotMappedError(mapping_version, pathway_ids)

    def _check_features_catalogued(
        self, phospho_matrix: OmicMatrix, proteome_matrix: OmicMatrix
    ) -> None:
        for matrix, label in (
            (phospho_matrix, 'fosfoproteoma'),
            (proteome_matrix, 'proteoma'),
        ):
            if not OmicMatrixFeature.objects.filter(matrix=matrix).exists():
                raise PfsFeaturesNotCataloguedError(matrix, label)

    def _check_pairing(
        self, phospho_matrix: OmicMatrix, proteome_matrix: OmicMatrix
    ) -> None:
        for matrix, label in (
            (phospho_matrix, 'fosfoproteoma'),
            (proteome_matrix, 'proteoma'),
        ):
            exists = OmicSamplePairing.objects.filter(
                matrix=matrix, validated_overlap=True
            ).exists()
            if not exists:
                raise PfsPairingMissingError(matrix, label)

    def _check_no_active_job(self, method_version: str) -> None:
        """
        Verifica se já há um PATHWAY_SCORE_RUN ativo para este
        method_version. Busca GLOBAL (sem filtro de projeto) — o recurso
        protegido (PathwayActivityScore por method_version) é global.
        """
        active_job = IngestionJob.objects.filter(
            job_type=IngestionJob.JobType.PATHWAY_SCORE_RUN,
            status__in=[
                IngestionJob.JobStatus.PENDING,
                IngestionJob.JobStatus.RUNNING,
            ],
            parameters__method_version=method_version,
        ).first()
        if active_job:
            raise PfsJobActiveError(active_job)

    # ─────────────────────────────────────────────────────────────────────
    # Gate de reprodutibilidade (D3) — ver docstring do módulo, item 3
    # ─────────────────────────────────────────────────────────────────────

    def _build_config(
        self,
        *,
        pathway_kegg_ids: list[str],
        phospho_matrix: OmicMatrix,
        proteome_matrix: OmicMatrix,
        seed_method_versions: list[str],
        mapping_version: str,
        restart: float,
        unsigned_weight: float,
        n_permutations: int,
        rng_seed: int,
        min_regulon_targets: int,
    ) -> dict:
        """
        Configuração canônica do run — usada tanto para `parameters` do
        IngestionJob quanto para a comparação do gate de reprodutibilidade.
        """
        return {
            'pathway_kegg_ids': sorted(pathway_kegg_ids),
            'phospho_matrix_id': phospho_matrix.id,
            'proteome_matrix_id': proteome_matrix.id,
            'seed_method_versions': sorted(seed_method_versions),
            'mapping_version': mapping_version,
            'restart': restart,
            'unsigned_weight': unsigned_weight,
            'n_permutations': n_permutations,
            'rng_seed': rng_seed,
            'min_regulon_targets': min_regulon_targets,
        }

    def _check_reproducibility_gate(
        self, method_version: str, config: dict, force: bool
    ) -> None:
        has_existing_scores = PathwayActivityScore.objects.filter(
            method_version=method_version
        ).exists()
        if not has_existing_scores:
            # Primeiro run sob este method_version — estabelece a
            # proveniência, nenhum gate a checar.
            return

        prior_job = (
            IngestionJob.objects.filter(
                job_type=IngestionJob.JobType.PATHWAY_SCORE_RUN,
                status=IngestionJob.JobStatus.COMPLETED,
                parameters__method_version=method_version,
                parameters__dry_run=False,
            )
            .order_by('-completed_at')
            .first()
        )

        if prior_job is None:
            if force:
                logger.warning(
                    'Gate de reprodutibilidade ignorado via --force '
                    '(method_version=%s, sem job de proveniência '
                    'encontrado).',
                    method_version,
                )
                return
            raise PfsReproducibilityGateError(method_version, reason='no_provenance')

        prior_config = {key: prior_job.parameters.get(key) for key in GATE_FIELDS}
        current_config = {key: config.get(key) for key in GATE_FIELDS}

        if prior_config != current_config:
            if force:
                logger.warning(
                    'Gate de reprodutibilidade ignorado via --force '
                    '(method_version=%s, parâmetros divergem do job %s).',
                    method_version,
                    prior_job.id,
                )
                return
            raise PfsReproducibilityGateError(
                method_version,
                reason='mismatch',
                prior=prior_config,
                current=current_config,
            )

    # ─────────────────────────────────────────────────────────────────────
    # Execução interna
    # ─────────────────────────────────────────────────────────────────────

    def _execute(
        self,
        *,
        job: IngestionJob,
        pathway_ids: list[str],
        phospho_matrix: OmicMatrix,
        proteome_matrix: OmicMatrix,
        seed_method_versions: list[str],
        mapping_version: str,
        method_version: str,
        restart: float,
        unsigned_weight: float,
        n_permutations: int,
        rng_seed: int,
        min_regulon_targets: int,
        dry_run: bool,
        rust_engine,
    ) -> dict:
        """
        Núcleo do run: download dos 2 Parquets → Rust → manifesto → job
        COMPLETED → relatório de cobertura.

        rust_engine passado explicitamente para facilitar mock em testes.

        Regra #1: NÃO abre o conteúdo dos Parquets em Python. Apenas baixa
        (I/O de orquestração aceito) e passa os caminhos locais ao Rust.
        """
        for matrix, label in (
            (phospho_matrix, 'fosfoproteoma'),
            (proteome_matrix, 'proteoma'),
        ):
            if not matrix.storage_key:
                raise ValueError(
                    f'OmicMatrix id={matrix.id} ({label}) não tem '
                    f'storage_key definida — a matriz foi carregada '
                    f'corretamente?'
                )

        with tempfile.TemporaryDirectory(prefix='davinci_pfs_') as tmp_dir:
            local_phospho = os.path.join(tmp_dir, 'phospho_matrix.parquet')
            local_proteome = os.path.join(tmp_dir, 'proteome_matrix.parquet')

            logger.info(
                'PathwayScoringService: baixando 2 Parquets (job %s, '
                'caminhos locais omitidos)',
                job.id,
            )
            for matrix, local_path, label in (
                (phospho_matrix, local_phospho, 'fosfoproteoma'),
                (proteome_matrix, local_proteome, 'proteoma'),
            ):
                try:
                    with default_storage.open(matrix.storage_key, 'rb') as remote_f:
                        with open(local_path, 'wb') as local_f:
                            shutil.copyfileobj(remote_f, local_f)
                except Exception as exc:
                    raise RuntimeError(
                        f'Falha ao baixar Parquet da OmicMatrix id={matrix.id} '
                        f'({label}): {exc}'
                    ) from exc

            logger.info(
                'PathwayScoringService: Parquets baixados (job %s). '
                'Chamando rust_engine.run_pfs_scoring (dry_run=%s).',
                job.id,
                dry_run,
            )

            # ── db_url — NUNCA logar ──────────────────────────────────────
            db = settings.DATABASES['default']
            db_url = (
                f"postgresql://{db['USER']}:{db['PASSWORD']}"
                f"@{db['HOST']}:{db['PORT']}/{db['NAME']}"
            )

            manifest = rust_engine.run_pfs_scoring(
                pathway_kegg_ids=pathway_ids,
                phospho_parquet_path=local_phospho,
                proteome_parquet_path=local_proteome,
                phospho_matrix_id=phospho_matrix.id,
                proteome_matrix_id=proteome_matrix.id,
                seed_method_versions=seed_method_versions,
                mapping_version=mapping_version,
                method_version=method_version,
                db_url=db_url,
                restart=restart,
                unsigned_weight=unsigned_weight,
                n_permutations=n_permutations,
                rng_seed=rng_seed,
                min_regulon_targets=min_regulon_targets,
                dry_run=dry_run,
            )

        # ── Tmpdir limpo automaticamente ao sair do with-block ────────────

        IngestionJob.objects.filter(id=job.id).update(
            status=IngestionJob.JobStatus.COMPLETED,
            records_processed=manifest.n_cases_scored,
            records_inserted=manifest.n_rows_written,
            completed_at=timezone.now(),
        )

        # ── Diagnóstico ordenado anti-p-hacking (adendo do plano) ─────────
        # Razão on/(on+off) de cobertura de SEMENTE.
        seed_denominator = manifest.n_seeds_on_graph + manifest.n_seeds_off_graph
        seed_coverage_ratio = (
            manifest.n_seeds_on_graph / seed_denominator
            if seed_denominator > 0
            else None
        )
        # Razão with_value/mapped de cobertura de READOUT — o denominador
        # CORRETO é n_readouts_with_value (readouts efetivamente lidos),
        # nunca n_readouts_mapped (linhas de PathwayReadoutFeature, que
        # inclui readouts fantasma — ver docstring de PathwayActivityScore).
        readout_coverage_ratio = (
            manifest.n_readouts_with_value / manifest.n_readouts_mapped
            if manifest.n_readouts_mapped > 0
            else None
        )
        zero_seeds_on_graph = manifest.n_seeds_on_graph == 0

        logger.info(
            'PATHWAY_SCORE_RUN concluído (job %s): n_rows_written=%d, '
            'n_cases_scored=%d, n_seeds_on_graph=%d, n_seeds_off_graph=%d, '
            'n_readouts_mapped=%d, n_readouts_with_value=%d',
            job.id,
            manifest.n_rows_written,
            manifest.n_cases_scored,
            manifest.n_seeds_on_graph,
            manifest.n_seeds_off_graph,
            manifest.n_readouts_mapped,
            manifest.n_readouts_with_value,
        )
        if zero_seeds_on_graph:
            logger.warning(
                'PATHWAY_SCORE_RUN (job %s): n_seeds_on_graph=0 — todo '
                'z_score será degenerado (sintoma de cobertura de seed, '
                'NÃO bug do motor). Ver Fase 2 '
                '(derive_cnv_seeds/derive_cnv_allele_seeds/'
                'resolve_variant_effects).',
                job.id,
            )

        return {
            'job_id': str(job.id),
            'method_version': method_version,
            'pathway_kegg_ids': pathway_ids,
            'phospho_matrix_id': phospho_matrix.id,
            'proteome_matrix_id': proteome_matrix.id,
            'mapping_version': mapping_version,
            'dry_run': dry_run,
            # ── Manifesto (PfsRunManifest) ─────────────────────────────
            'n_pathways': manifest.n_pathways,
            'n_pathways_no_readout': manifest.n_pathways_no_readout,
            'n_cases_scored': manifest.n_cases_scored,
            'n_rows_written': manifest.n_rows_written,
            'n_seeds_total': manifest.n_seeds_total,
            'n_seeds_on_graph': manifest.n_seeds_on_graph,
            'n_seeds_off_graph': manifest.n_seeds_off_graph,
            'n_seeds_masked_by_neutral_v2': manifest.n_seeds_masked_by_neutral_v2,
            'n_readouts_mapped': manifest.n_readouts_mapped,
            'n_readouts_with_value': manifest.n_readouts_with_value,
            'n_readouts_missing': manifest.n_readouts_missing,
            'n_readouts_phospho': manifest.n_readouts_phospho,
            'n_readouts_tf': manifest.n_readouts_tf,
            'n_tf_below_min_targets': manifest.n_tf_below_min_targets,
            'n_tumor_unpaired_skipped': manifest.n_tumor_unpaired_skipped,
            'n_zero_sd_null': manifest.n_zero_sd_null,
            'n_unsigned_edges': manifest.n_unsigned_edges,
            'n_signed_edges': manifest.n_signed_edges,
            'elapsed_ms_load': manifest.elapsed_ms_load,
            'elapsed_ms_graph_precompute': manifest.elapsed_ms_graph_precompute,
            'elapsed_ms_readout_and_permutation': manifest.elapsed_ms_readout_and_permutation,
            'elapsed_ms_copy': manifest.elapsed_ms_copy,
            'elapsed_ms_total': manifest.elapsed_ms_total,
            # ── Diagnóstico derivado (divisão de escalares do manifesto) ─
            'seed_coverage_ratio': seed_coverage_ratio,
            'readout_coverage_ratio': readout_coverage_ratio,
            'zero_seeds_on_graph': zero_seeds_on_graph,
        }
