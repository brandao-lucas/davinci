"""
ReadoutMappingService — mapeamento readout→feature (Fase 3, Passo 3.4).

OmnisPathway Objetivo 2, Fase 3, Passo 3.4.

Responsabilidade:
  Cruzar nós de readout do grafo KEGG × features das matrizes ômicas e
  materializar PathwayReadoutFeature + marcar PathwayNode.readout_role.
  Django set-based puro (ORM/SQL) — ZERO Rust, ZERO abertura de Parquet.

Universo de vias (generalizado — deixou de ser 3 fixas):
  v1 (Fase 3 inicial) fixava as regras em 3 vias KEGG via constante
  (PHOSPHO_PATHWAY_IDS/TF_PATHWAY_IDS, hoje aliases de
  LEGACY_V1_PHOSPHO_PATHWAY_IDS/LEGACY_V1_TF_PATHWAY_IDS). Com a bancada
  expandida para as vias humanas completas do KEGG (372 vias, grafo com
  49.322 nós / 5.520 símbolos-gene distintos), o default de AMBAS as regras
  passou a ser TODAS as `Pathway` carregadas em PG — resolvido em
  `run()`/`_resolve_pathway_universe`, não fixado em constante. As duas
  regras usam o MESMO universo por padrão (ver docstring de
  ReadoutMappingService para a justificativa de comparabilidade de z-score
  entre vias). `pathway_ids_phospho`/`pathway_ids_tf` continuam aceitando
  lista explícita para restringir uma execução (debug, reprodução do preset
  legado).

Regra 1 (fosfo):
  PathwayNode(node_type='gene') das vias do universo resolvido (default:
  todas) × features da matriz de fosfoproteoma (OmicMatrix
  omics_layer='proteomic', feature_axis='phospho_site',
  loader_version='phospho-v1').
  feature_key = gene_symbol do nó (UPPERCASE).
  Onde bate: PathwayNode.readout_role='phospho' + PathwayReadoutFeature(
    rule='phospho', confidence=1.0, mapping_version=<ver>).
  Vias cujo símbolo não está catalogado na matriz de fosfo simplesmente não
  geram readout de fosfo (n_features_not_found) — não há lista fixa que as
  exclua a priori.

Regra 2 (TF):
  Para cada nó cujo gene_symbol é TF (source) no JSONL de regulon (caminho
  do IngestionJob mais recente de REGULON_LOAD), dentre as vias do universo
  resolvido (default: todas), para cada alvo do regulon cruzar com features
  da matriz proteoma da Fase 0 (CPTAC-CCRCC proteome, feature_axis='gene').
  Onde bate (alvo ∈ features da matriz proteoma): nó do TF recebe
  readout_role='tf_target' + PathwayReadoutFeature(
  rule='tf_target', regulon_source='collectri', regulon_sign=±1/0 (lido do
  campo `mode` do JSONL — ver Regra 2 abaixo para o contrato real),
  confidence=CONFIDENCE_TF_TARGET (fixo, sem calibração no v1 — ver Regra 2),
  mapping_version=<ver>).

Validação de existência de feature (A→C concluída — migration 0036):
  NÃO abre Parquet no Django. A validação é feita em PG contra
  `OmicMatrixFeature` (catálogo de linhas do Parquet, populado pelo
  management command `backfill_matrix_features` — ver
  matrix_feature_catalog_service.py). O bug original (medido na ingestão
  live) era validar candidatos contra os símbolos do GRAFO KEGG
  (`PathwayNode.gene_symbol`) em vez de contra as features REAIS da matriz
  alvo — semanticamente errado, porque os alvos de TF são por definição
  genes FORA da via (validar contra o grafo descartava ~95% deles).

  - Regra 1 (fosfo): feature_key = gene_symbol do nó (UPPERCASE) validado
    contra `OmicMatrixFeature.objects.filter(matrix=phospho_matrix,
    feature_key__in=candidatos)`. Só materializa PathwayReadoutFeature para
    os símbolos que EXISTEM na matriz de fosfo. n_features_not_found agora é
    real (nós cujo símbolo não está catalogado na matriz).

  - Regra 2 (TF): target_symbol do regulon validado contra
    `OmicMatrixFeature.objects.filter(matrix=proteome_matrix,
    feature_key__in=candidatos)` — a matriz PROTEOMA (matriz alvo do
    readout), não mais contra o conjunto de símbolos do grafo. Os alvos de
    um TF são majoritariamente externos à via; validar contra a matriz real
    é o que os torna aceitáveis (medido: 70% dos alvos aceitos vs. 5% da
    validação intra-grafo antiga).

  Degradação graciosa (decisão deliberada): se `OmicMatrixFeature` estiver
  VAZIO para a matriz que precisa ser validada (backfill ainda não rodou),
  o service FALHA ALTO (`OmicMatrixFeatureNotCataloguedError`) em vez de
  cair de volta na validação intra-grafo antiga. Cair silenciosamente
  reintroduziria o mesmo bug sem avisar o operador — pior que falhar, porque
  o comando "funcionaria" e materializaria readouts fantasma de novo. O erro
  instrui a rodar `backfill_matrix_features` antes de (re)executar
  map_readouts. A checagem só dispara quando há candidatos reais a validar
  (matriz presente E há símbolos para checar) — matriz ausente já é tratada
  separadamente (regra pulada, não é o mesmo caminho de erro).

Idempotência:
  bulk_create(ignore_conflicts=True) — NK (node, matrix, feature_key, rule,
  mapping_version) → seguro re-rodar. PathwayNode.readout_role atualizado via
  .update() em queryset (sobrescreve).

Catálogo global:
  PathwayReadoutFeature e PathwayNode são catálogo global (sem FK de projeto).
  Isolamento futuro na leitura pela Fase 4 via matrix__dataset__in_projects__project.

  Isolamento (Regra #3) e `_resolve_regulon_path` — decisão deliberada:
  `_resolve_regulon_path` busca o IngestionJob(REGULON_LOAD, COMPLETED) MAIS
  RECENTE GLOBALMENTE, sem filtrar por `project`, mesmo o modelo IngestionJob
  tendo FK de projeto obrigatória. Avaliado e decidido MANTER global (não
  filtrar por projeto), pelos motivos abaixo — não é descuido:

  1. `project` em IngestionJob(REGULON_LOAD) é FK de TRACKING, não de escopo
     de dado. `load_regulons` (management command) documenta isso
     explicitamente: "--project ... obrigatório — FK do IngestionJob; NÃO
     restringe o regulon ao projeto". O dado carregado nunca foi
     project-scoped por design.
  2. O conteúdo do JSONL é determinístico a partir de (tf_allowlist, dado
     público CollecTRI/OmniPath). `tf_allowlist` é derivado exclusivamente do
     catálogo global `PathwayNode` (RegulonLoadService._derive_tf_allowlist),
     que também não tem FK de projeto — logo dois projetos que rodem
     `load_regulons` com o mesmo universo de vias (o caso normal, desde a
     generalização para todas as vias: nenhum `--pathway` informado ⇒ default
     é TODAS as `Pathway` carregadas em PG — mesmo catálogo global para
     qualquer projeto que o invoque) produzem JSONLs com conteúdo idêntico.
     Não há dado de usuário, credencial, nem recorte privado de projeto no
     arquivo — é público e catalogável.
  3. Consequência: pegar o job mais recente de OUTRO projeto não vaza nada
     sensível daquele projeto — o arquivo não carrega informação alguma sobre
     qual projeto o gerou, apenas o grafo KEGG (global) filtrado pelo dado
     público CollecTRI.
  4. Robustez (não isolamento): se o job mais recente tiver sido gerado com um
     `pathway_ids` diferente (custom, via chamada programática do service —
     não exposto no command), o pior caso é a Regra 2 mapear MENOS do que o
     possível (TFs fora do tf_allowlist original simplesmente não aparecem no
     JSONL) — nunca mais nem incorreto, porque `_apply_rule_tf` só materializa
     entradas cujo `tf_symbol` bate com um `PathwayNode` do conjunto de vias
     desta execução (`tf_nodes`); qualquer linha do JSONL fora desse conjunto
     é ignorada. Esse caso já é observável via `n_unmapped_nodes` /
     `regulon_path_found` no relatório — não é um risco silencioso de
     integridade, só de cobertura.
  5. Se no futuro o regulon deixar de ser 100% público (ex.: allowlist
     derivado de dado privado de projeto), esta decisão deve ser revisitada e
     o filtro por `project` precisa ser adicionado — o service teria que
     passar a receber o projeto como o `RegulonLoadService` já faz.

Sensitive-data-handling:
  Nenhuma credencial. regulon_path (lido aqui via IngestionJob.parameters do
  job REGULON_LOAD mais recente) É EXPOSTO ao dono do projeto: o campo
  `parameters` é serializado por inteiro por `IngestionJobSerializer`
  (apps/core/serializers/job.py, sem allowlist de chaves) e devolvido em
  `GET /projects/{project_pk}/jobs/` — não é vazamento cross-usuário
  (`IngestionJobViewSet.get_queryset()` já filtra por request.user, Regra #3
  preservada), mas o caminho absoluto do worker (com nome de usuário do SO)
  é visível para o dono do projeto. Ver justificativa completa e decisão de
  não mitigar agora em regulon_load_service.py (docstring de módulo, seção
  Sensitive-data-handling) — regra prática: nada sensível vai em
  `IngestionJob.parameters`.
"""

from __future__ import annotations

import json
import logging
import os

from django.db import transaction
from django.utils import timezone

from apps.core.models import (
    IngestionJob,
    OmicMatrix,
    OmicMatrixFeature,
    Pathway,
    PathwayNode,
    PathwayReadoutFeature,
)

logger = logging.getLogger(__name__)

# Preset legado (v1, Fase 3 inicial) — as 3 vias KEGG originais (PI3K-Akt,
# MAPK, p53). NÃO é mais o default do service (ver classe ReadoutMappingService
# e a Regra "ambas as regras se aplicam a todas as vias" abaixo) — mantido
# nomeado só para reprodutibilidade de execuções antigas / testes que queiram
# fixar o universo de vias explicitamente a esse conjunto.
LEGACY_V1_PHOSPHO_PATHWAY_IDS = ['hsa04151', 'hsa04010']
LEGACY_V1_TF_PATHWAY_IDS = ['hsa04151', 'hsa04010', 'hsa04115']

# Aliases retrocompatíveis (nomes antigos) — mesmo conteúdo dos presets acima.
# Não usar como default: ver ReadoutMappingService.__init__.
PHOSPHO_PATHWAY_IDS = LEGACY_V1_PHOSPHO_PATHWAY_IDS
TF_PATHWAY_IDS = LEGACY_V1_TF_PATHWAY_IDS

# loader_version da matriz de fosfoproteoma (passo 3.1)
PHOSPHO_LOADER_VERSION = 'phospho-v1'

# Versão padrão do mapeamento
DEFAULT_MAPPING_VERSION = 'fase3-readout-v1'

# Tamanho de lote do bulk_create de PathwayReadoutFeature — ver justificativa
# de escala na docstring de `_persist`. Não é obrigatório para correção (o
# Django já fatia sozinho respeitando o limite de params do backend), é para
# tornar o tamanho do lote previsível com o universo de 372 vias.
BULK_CREATE_BATCH_SIZE = 5000

# Confidence fixo para PathwayReadoutFeature(rule='tf_target') — Regra 2.
#
# O JSONL de regulon (produzido por rust_src/src/omics/regulon_loader.rs) NÃO
# tem campo de confiança/score contínuo — apenas `mode` (sinal) e
# `n_references` (contagem de referências de literatura, cru). O plano da
# Fase 4 (item S-7) já registra que no v1 a confiança do regulon é lida mas
# NÃO pondera o footprint (sem cálculo posterior que dependa de calibração
# fina). Diante disso, gravar confidence=0.0 seria enganoso — sugeriria
# confiança nula quando na verdade não há informação de confiança nenhuma
# capturada. DECISÃO: 1.0 fixo, sinalizando "par aceito, sem grau calculado"
# em vez de fabricar uma normalização de n_references sem calibração
# validada. Se uma Fase futura precisar ponderar por n_references, essa
# derivação deve ser decidida e documentada explicitamente ali (não aqui).
CONFIDENCE_TF_TARGET = 1.0


class ReadoutMappingError(Exception):
    """Levantado quando pré-condição do mapeamento não é satisfeita."""
    pass


class OmicMatrixFeatureNotCataloguedError(ReadoutMappingError):
    """
    Levantado quando `OmicMatrixFeature` está vazio para a matriz que precisa
    ser validada (o backfill `backfill_matrix_features` ainda não rodou para
    esta matriz).

    Decisão A→C (deliberada): falhar alto aqui em vez de degradar
    silenciosamente para a validação intra-grafo antiga. Essa validação
    antiga é exatamente o bug que motivou a promoção A→C — validar contra
    `PathwayNode.gene_symbol` (símbolos do grafo) descarta ~95% dos alvos
    reais de TF, que por definição ficam FORA da via. Cair de volta nela
    silenciosamente reintroduziria o bug sem qualquer aviso ao operador.
    """

    def __init__(self, matrix: OmicMatrix):
        self.matrix = matrix
        super().__init__(
            f"OmicMatrixFeature vazio para matrix_id={matrix.id} "
            f"(dataset_id={matrix.dataset_id}, omics_layer={matrix.omics_layer}, "
            f"feature_axis={matrix.feature_axis}). O catálogo de features desta "
            f"matriz ainda não foi populado. Rode "
            f"`manage.py backfill_matrix_features --matrix-id {matrix.id}` "
            f"antes de (re)executar map_readouts. Validar contra o grafo KEGG "
            f"(estratégia intra-grafo antiga) reintroduziria silenciosamente o "
            f"bug corrigido na promoção A→C — por isso o mapeamento é abortado "
            f"em vez de degradar."
        )


class ReadoutMappingService:
    """
    Serviço de mapeamento readout→feature (Django set-based puro).

    Default: TODAS as vias em `Pathway` (resolvido em run(), no momento da
    execução — reflete o catálogo carregado naquele instante), para AMBAS as
    regras (fosfo e TF). `pathway_ids_phospho`/`pathway_ids_tf` continuam
    aceitando uma lista explícita para restringir a execução (ex.: debug de
    uma via, ou reprodução de uma versão antiga via
    LEGACY_V1_PHOSPHO_PATHWAY_IDS/LEGACY_V1_TF_PATHWAY_IDS).

    Por que as duas regras aplicam ao MESMO universo de vias (não apenas o
    default — é uma invariante do service, documentada aqui porque motiva a
    assinatura do __init__): se a via X for pontuada com readout de fosfo+TF
    e a via Y só com TF, os z-scores de X e Y não são comparáveis entre si —
    a composição do readout (quantos/quais sinais entram no score) difere
    entre elas. Numa assinatura que ordena N vias (v1: 3; agora: as vias
    carregadas, ex. 372 do KEGG humano), comparabilidade entre todas é
    requisito, não opcional. Restringir uma regra a um subconjunto e a outra
    a um universo diferente reintroduz exatamente esse viés — por isso os
    dois parâmetros são independentes na assinatura (flexibilidade para
    debug/teste), mas o caso normal (nenhum dos dois informado) usa o MESMO
    conjunto — todas as vias — para ambas. Vias sem readout de fosfo
    catalogado na matriz simplesmente não geram PathwayReadoutFeature(rule=
    'phospho') — é o dado que decide a ausência, não uma lista fixa que
    exclui a via a priori.

    Uso (default — todas as vias carregadas, ambas as regras):
        service = ReadoutMappingService(
            mapping_version='fase3-readout-v1',
            dry_run=False,
        )
        report = service.run()

    Uso (restrito — reproduz o preset legado v1):
        service = ReadoutMappingService(
            pathway_ids_phospho=LEGACY_V1_PHOSPHO_PATHWAY_IDS,
            pathway_ids_tf=LEGACY_V1_TF_PATHWAY_IDS,
        )

    Retorno de run():
        {
            'n_phospho_mapped': int,
            'n_tf_mapped': int,
            'n_nodes_readout': int,
            'n_unmapped_nodes': int,
            'n_features_not_found': int,
            'n_pathways_total': int,        # nº de vias no universo desta execução
            'n_pathways_with_phospho': int, # vias com >=1 PathwayReadoutFeature(phospho)
            'n_pathways_with_tf': int,      # vias com >=1 PathwayReadoutFeature(tf_target)
            'n_pathways_without_readout': int,  # sem nenhum dos dois — escore degenerado
            'validation_strategy': str,   # documentação da estratégia usada
            'mapping_version': str,
            'dry_run': bool,
            'duration_s': float,
        }
    """

    def __init__(
        self,
        pathway_ids_phospho: list[str] | None = None,
        pathway_ids_tf: list[str] | None = None,
        mapping_version: str = DEFAULT_MAPPING_VERSION,
        dry_run: bool = False,
    ):
        # None = "todas as vias carregadas" — resolvido em run() (não aqui)
        # para refletir o catálogo `Pathway` no momento da execução, não no
        # momento da construção do service. Lista explícita (incl. []) é
        # respeitada tal como passada — [] restringe a execução a NENHUMA
        # via para aquela regra (não cai de volta no default).
        self._pathway_ids_phospho_arg = pathway_ids_phospho
        self._pathway_ids_tf_arg = pathway_ids_tf
        self.pathway_ids_phospho: list[str] = (
            pathway_ids_phospho if pathway_ids_phospho is not None else []
        )
        self.pathway_ids_tf: list[str] = (
            pathway_ids_tf if pathway_ids_tf is not None else []
        )
        self.mapping_version = mapping_version
        self.dry_run = dry_run
        self._start = None

    # ─────────────────────────────────────────────────────────────────────────
    # API pública
    # ─────────────────────────────────────────────────────────────────────────

    def run(self) -> dict:
        """
        Executa o mapeamento de readouts.

        Fluxo:
          0. Resolve o universo de vias (se `pathway_ids_phospho`/`pathway_ids_tf`
             não foram informados no construtor): TODAS as `Pathway` em PG no
             momento desta chamada — não no momento da construção do service.
          1. Resolve matrizes (fosfoproteoma + proteoma).
          2. Resolve caminho do JSONL de regulon (IngestionJob mais recente).
          3. Regra 1: cruza nós de fosfo × matriz fosfoproteoma.
          4. Regra 2: cruza TFs regulon × matriz proteoma.
          5. bulk_create PathwayReadoutFeature + update readout_role.
          6. Retorna relatório (agregado + distribuição por via).

        Returns:
            dict com contadores e estratégia de validação.

        Raises:
            ReadoutMappingError: pré-condição não satisfeita.
            OmicMatrixFeatureNotCataloguedError: a matriz a validar não tem
                nenhuma OmicMatrixFeature catalogada (backfill não rodou).
        """
        import time
        self._start = time.monotonic()

        self._resolve_pathway_universe()

        phospho_matrix = self._resolve_phospho_matrix()
        proteome_matrix = self._resolve_proteome_matrix()
        regulon_path = self._resolve_regulon_path()

        phospho_features, n_phospho_not_found = self._apply_rule_phospho(
            phospho_matrix
        )
        tf_features, n_tf_not_found = self._apply_rule_tf(
            proteome_matrix, regulon_path
        )

        all_features = phospho_features + tf_features
        n_features_not_found = n_phospho_not_found + n_tf_not_found

        if not self.dry_run:
            self._persist(phospho_features, tf_features)

        duration_s = time.monotonic() - self._start

        n_phospho_mapped = len(phospho_features)
        n_tf_mapped = len(tf_features)
        n_nodes_readout = len({f.node_id for f in all_features})

        # Nós de gene das vias sem nenhum readout mapeado
        total_gene_nodes = PathwayNode.objects.filter(
            pathway__kegg_id__in=self.pathway_ids_phospho + self.pathway_ids_tf,
            node_type=PathwayNode.NodeType.GENE,
        ).exclude(gene_symbol='').count()
        n_unmapped_nodes = max(0, total_gene_nodes - n_nodes_readout)

        # ── Distribuição por via (relatório item 5) ────────────────────────
        # Set-based: uma única query mapeando kegg_id -> pk para o universo
        # desta execução; os conjuntos "com phospho"/"com tf" vêm direto dos
        # nós já carregados em memória (select_related('pathway') nas duas
        # regras) — sem query adicional por via.
        universe_kegg_ids = set(self.pathway_ids_phospho) | set(self.pathway_ids_tf)
        universe_pathway_ids = set(
            Pathway.objects.filter(kegg_id__in=universe_kegg_ids).values_list(
                'id', flat=True
            )
        ) if universe_kegg_ids else set()

        pathway_ids_with_phospho = {f.node.pathway_id for f in phospho_features}
        pathway_ids_with_tf = {f.node.pathway_id for f in tf_features}
        n_pathways_with_phospho = len(pathway_ids_with_phospho)
        n_pathways_with_tf = len(pathway_ids_with_tf)
        n_pathways_without_readout = len(
            universe_pathway_ids - pathway_ids_with_phospho - pathway_ids_with_tf
        )

        report = {
            'n_phospho_mapped': n_phospho_mapped,
            'n_tf_mapped': n_tf_mapped,
            'n_nodes_readout': n_nodes_readout,
            'n_unmapped_nodes': n_unmapped_nodes,
            'n_features_not_found': n_features_not_found,
            'n_pathways_total': len(universe_pathway_ids),
            'n_pathways_with_phospho': n_pathways_with_phospho,
            'n_pathways_with_tf': n_pathways_with_tf,
            'n_pathways_without_readout': n_pathways_without_readout,
            'mapping_version': self.mapping_version,
            'dry_run': self.dry_run,
            'duration_s': round(duration_s, 2),
            # Estratégia de validação (A→C concluída — migration 0036)
            'validation_strategy': (
                'OmicMatrixFeature: feature_key validada contra o catalogo de '
                'features da MATRIZ ALVO (Regra 1: matriz de fosfo; Regra 2: '
                'matriz de proteoma) via '
                'OmicMatrixFeature.objects.filter(matrix=..., '
                'feature_key__in=candidatos). Substitui a validacao intra-grafo '
                'antiga (contra PathwayNode.gene_symbol), que descartava ~95% '
                'dos alvos de TF por validar contra o grafo em vez da matriz. '
                'Se a matriz nao tem OmicMatrixFeature catalogada, o mapeamento '
                'falha alto (OmicMatrixFeatureNotCataloguedError) em vez de '
                'degradar silenciosamente para a estrategia antiga.'
            ),
            'phospho_matrix_id': phospho_matrix.id if phospho_matrix else None,
            'proteome_matrix_id': proteome_matrix.id if proteome_matrix else None,
            'regulon_path_found': bool(regulon_path),
        }

        logger.info(
            'ReadoutMappingService concluido (dry_run=%s, version=%s): '
            'n_phospho=%d, n_tf=%d, n_nodes_readout=%d, n_unmapped=%d, '
            'n_not_found=%d, duracao=%.1fs',
            self.dry_run,
            self.mapping_version,
            n_phospho_mapped,
            n_tf_mapped,
            n_nodes_readout,
            n_unmapped_nodes,
            n_features_not_found,
            duration_s,
        )
        logger.info(
            'ReadoutMappingService distribuicao por via: n_pathways_total=%d, '
            'com_phospho=%d, com_tf=%d, sem_nenhum_readout=%d '
            '(essas produzirao escore degenerado na Fase 4).',
            len(universe_pathway_ids),
            n_pathways_with_phospho,
            n_pathways_with_tf,
            n_pathways_without_readout,
        )
        return report

    # ─────────────────────────────────────────────────────────────────────────
    # Resolução do universo de vias
    # ─────────────────────────────────────────────────────────────────────────

    def _resolve_pathway_universe(self) -> None:
        """
        Resolve `self.pathway_ids_phospho`/`self.pathway_ids_tf` quando NÃO
        foram informados explicitamente no construtor (None) — default:
        TODAS as `Pathway.kegg_id` carregadas em PG neste momento (query
        única, reaproveitada para as duas regras quando ambas usam default —
        garante que as regras enxergam o MESMO universo por construção,
        reforçando a comparabilidade de z-score entre vias documentada na
        docstring da classe).

        Lista explícita passada ao construtor (incl. `[]`) nunca é
        sobrescrita aqui — só o sentinel `None` aciona a resolução.
        """
        if self._pathway_ids_phospho_arg is not None and self._pathway_ids_tf_arg is not None:
            return  # nada a resolver — ambas explícitas

        all_pathway_ids = list(
            Pathway.objects.order_by('kegg_id').values_list('kegg_id', flat=True)
        )
        logger.info(
            'ReadoutMappingService: universo de vias resolvido via default '
            '(Pathway em PG): %d via(s).',
            len(all_pathway_ids),
        )

        if self._pathway_ids_phospho_arg is None:
            self.pathway_ids_phospho = all_pathway_ids
        if self._pathway_ids_tf_arg is None:
            self.pathway_ids_tf = all_pathway_ids

    # ─────────────────────────────────────────────────────────────────────────
    # Resolução de pré-condições
    # ─────────────────────────────────────────────────────────────────────────

    def _resolve_phospho_matrix(self) -> OmicMatrix | None:
        """
        Resolve a OmicMatrix de fosfoproteoma (pré-condição da Regra 1).

        Retorna None se não encontrada (e loga aviso — Regra 1 será pulada).
        """
        matrix = OmicMatrix.objects.filter(
            omics_layer='proteomic',
            feature_axis=OmicMatrix.FeatureAxis.PHOSPHO_SITE,
            loader_version=PHOSPHO_LOADER_VERSION,
        ).first()
        if matrix is None:
            logger.warning(
                'ReadoutMappingService: OmicMatrix de fosfoproteoma nao '
                'encontrada (loader_version=%s) — Regra 1 (fosfo) sera pulada. '
                'Execute load_phospho_matrix primeiro.',
                PHOSPHO_LOADER_VERSION,
            )
        else:
            logger.info(
                'ReadoutMappingService: OmicMatrix fosfoproteoma resolvida '
                '(id=%d, n_features=%s)',
                matrix.id,
                matrix.n_features,
            )
        return matrix

    def _resolve_proteome_matrix(self) -> OmicMatrix | None:
        """
        Resolve a OmicMatrix de proteoma CPTAC CCRCC da Fase 0 (Regra 2).

        feature_axis='gene', omics_layer='proteomic', loader_version='v1'.
        Retorna None se não encontrada (Regra 2 será pulada).
        """
        matrix = OmicMatrix.objects.filter(
            omics_layer='proteomic',
            feature_axis=OmicMatrix.FeatureAxis.GENE,
        ).first()
        if matrix is None:
            logger.warning(
                'ReadoutMappingService: OmicMatrix de proteoma gene-level nao '
                'encontrada — Regra 2 (TF) sera pulada. '
                'Execute load_cptac_matrix primeiro.'
            )
        else:
            logger.info(
                'ReadoutMappingService: OmicMatrix proteoma resolvida '
                '(id=%d, n_features=%s)',
                matrix.id,
                matrix.n_features,
            )
        return matrix

    def _resolve_regulon_path(self) -> str | None:
        """
        Resolve o caminho do JSONL de regulon (Regra 2).

        Busca o IngestionJob mais recente de REGULON_LOAD completado e extrai
        parameters['regulon_path']. Retorna None se não encontrado (Regra 2
        será pulada).

        Busca GLOBAL (sem filtro por `project`) — decisão deliberada, ver
        "Isolamento (Regra #3) e `_resolve_regulon_path`" na docstring do
        módulo para a justificativa completa.

        O caminho é local do worker — não exposto ao cliente.
        """
        job = (
            IngestionJob.objects.filter(
                job_type=IngestionJob.JobType.REGULON_LOAD,
                status=IngestionJob.JobStatus.COMPLETED,
            )
            .order_by('-completed_at')
            .first()
        )
        if job is None:
            logger.warning(
                'ReadoutMappingService: nenhum REGULON_LOAD completado '
                'encontrado — Regra 2 (TF) sera pulada. '
                'Execute load_regulons primeiro.'
            )
            return None

        regulon_path = (job.parameters or {}).get('regulon_path')
        if not regulon_path:
            logger.warning(
                'ReadoutMappingService: IngestionJob REGULON_LOAD (id=%d) '
                'sem regulon_path em parameters — Regra 2 sera pulada.',
                job.id,
            )
            return None

        if not os.path.isfile(regulon_path):
            logger.warning(
                'ReadoutMappingService: regulon_path do job %d nao existe '
                'no filesystem local — Regra 2 sera pulada. '
                'O arquivo JSONL e local do worker; se 3.3 e 3.4 rodaram '
                'em workers distintos sem volume compartilhado, o arquivo '
                'nao esta disponivel neste worker.',
                job.id,
            )
            return None

        logger.info(
            'ReadoutMappingService: regulon_path resolvido (job %d)',
            job.id,
        )
        return regulon_path

    def _get_existing_feature_keys(
        self,
        matrix: OmicMatrix,
        candidate_keys: set[str],
    ) -> set[str]:
        """
        Retorna o subconjunto de `candidate_keys` que EXISTE em
        `OmicMatrixFeature` para a `matrix` dada — validação real de
        existência no Parquet (via catálogo A→C), sem abrir o arquivo.

        `candidate_keys` já deve estar normalizado (UPPERCASE) — a NK de
        OmicMatrixFeature (matrix, feature_key) é case-sensitive.

        Levanta OmicMatrixFeatureNotCataloguedError se a matriz não tem
        NENHUMA feature catalogada (backfill_matrix_features não rodou para
        ela). Ver docstring do módulo / da exceção para a justificativa de
        falhar alto em vez de degradar para a validação intra-grafo antiga.
        """
        if not candidate_keys:
            return set()

        if not OmicMatrixFeature.objects.filter(matrix=matrix).exists():
            raise OmicMatrixFeatureNotCataloguedError(matrix)

        existing = set(
            OmicMatrixFeature.objects.filter(
                matrix=matrix,
                feature_key__in=candidate_keys,
            ).values_list('feature_key', flat=True)
        )
        logger.info(
            'ReadoutMappingService: %d/%d candidatos existem em '
            'OmicMatrixFeature (matrix_id=%d).',
            len(existing),
            len(candidate_keys),
            matrix.id,
        )
        return existing

    # ─────────────────────────────────────────────────────────────────────────
    # Regra 1 — fosfo
    # ─────────────────────────────────────────────────────────────────────────

    def _apply_rule_phospho(
        self,
        phospho_matrix: OmicMatrix | None,
    ) -> tuple[list[PathwayReadoutFeature], int]:
        """
        Regra 1: nós gene das vias de fosfo × matriz de fosfoproteoma.

        feature_key = gene_symbol do nó (UPPERCASE).
        Confidence = 1.0 (gene-level — nó está na via e a feature é o símbolo).

        Estratégia de validação (A→C concluída):
          feature_key validada contra `OmicMatrixFeature.objects.filter(
          matrix=phospho_matrix, feature_key__in=candidatos)` — a matriz de
          fosfo é a matriz ALVO deste readout, então validar contra ela (em
          vez do grafo) é o correto por construção. Só materializa
          PathwayReadoutFeature para os símbolos que EXISTEM na matriz.
          n_not_found é real: nós cujo gene_symbol não está catalogado.

        Returns:
            (lista de PathwayReadoutFeature não persistidos, n_not_found)

        Raises:
            OmicMatrixFeatureNotCataloguedError: matriz de fosfo sem nenhuma
                OmicMatrixFeature catalogada (backfill não rodou).
        """
        if phospho_matrix is None:
            logger.info(
                'ReadoutMappingService: Regra 1 pulada (matriz fosfo ausente).'
            )
            return [], 0

        nodes = list(
            PathwayNode.objects.filter(
                pathway__kegg_id__in=self.pathway_ids_phospho,
                node_type=PathwayNode.NodeType.GENE,
            )
            .exclude(gene_symbol='')
            .select_related('pathway')
        )

        candidate_symbols = {node.gene_symbol.upper() for node in nodes}
        existing_symbols = self._get_existing_feature_keys(
            phospho_matrix, candidate_symbols
        )

        features = []
        seen: set[tuple[int, int, str, str]] = set()
        n_not_found = 0

        for node in nodes:
            symbol = node.gene_symbol.upper()

            if symbol not in existing_symbols:
                n_not_found += 1
                continue

            key = (node.id, phospho_matrix.id, symbol, 'phospho')
            if key in seen:
                continue
            seen.add(key)
            features.append(
                PathwayReadoutFeature(
                    node=node,
                    matrix=phospho_matrix,
                    feature_key=symbol,
                    rule=PathwayReadoutFeature.Rule.PHOSPHO,
                    regulon_source='',
                    regulon_sign=None,
                    confidence=1.0,
                    mapping_version=self.mapping_version,
                )
            )

        logger.info(
            'ReadoutMappingService Regra 1 (fosfo): %d PathwayReadoutFeature '
            'prontos para materializacao, %d nao encontrados na matriz '
            '(vias=%s, matrix_id=%d).',
            len(features),
            n_not_found,
            self.pathway_ids_phospho,
            phospho_matrix.id,
        )
        return features, n_not_found

    # ─────────────────────────────────────────────────────────────────────────
    # Regra 2 — TF
    # ─────────────────────────────────────────────────────────────────────────

    def _apply_rule_tf(
        self,
        proteome_matrix: OmicMatrix | None,
        regulon_path: str | None,
    ) -> tuple[list[PathwayReadoutFeature], int]:
        """
        Regra 2: TFs das vias × regulons CollecTRI × features da matriz proteoma.

        Para cada TF (gene_symbol de nó no grafo) que aparece como source no
        JSONL de regulon, para cada alvo (target_symbol) do regulon: se o alvo
        EXISTE na matriz proteoma (validação real via OmicMatrixFeature),
        materializa PathwayReadoutFeature(
          node=<nó do TF>, matrix=<proteoma>, feature_key=<target_symbol>,
          rule='tf_target', regulon_source='collectri', regulon_sign=<mode>,
          confidence=CONFIDENCE_TF_TARGET).

        Estratégia de validação (A→C concluída):
          alvo validado contra `OmicMatrixFeature.objects.filter(
          matrix=proteome_matrix, feature_key__in=candidatos)` — a matriz de
          proteoma é a matriz ALVO deste readout. Os alvos de um TF são, por
          definição, majoritariamente genes FORA da via — validar contra o
          grafo (estratégia antiga) descartava ~95% deles; validar contra a
          matriz real aceita ~70% (medido na ingestão live).
          n_features_not_found = nº de alvos do regulon que NÃO existem na
          matriz proteoma.

        JSONL format REAL (produzido por
        rust_src/src/omics/regulon_loader.rs — é lá que se confere o
        contrato, não em exemplo de prompt/planejamento):
          Cada linha: {"tf":"MYC","target":"TERT","mode":1,
                       "sources":"TRRUST_CollecTRI2;NTNU...;CollecTRI",
                       "n_references":178}
          - `tf` (str), `target` (str): símbolos de gene.
          - `mode` (i8): +1 estimulação / -1 inibição / 0 conflito-ou-neutro.
            Mapeia 1:1 para PathwayReadoutFeature.regulon_sign — NÃO existe
            campo `sign` no JSONL (nome anterior era um exemplo fictício).
          - `sources` (str crua, `;`-delimitada): não consumida aqui.
          - `n_references` (int): não consumida aqui — não existe campo
            `confidence` no JSONL. Ver CONFIDENCE_TF_TARGET para a decisão
            sobre o que gravar em PathwayReadoutFeature.confidence.

        Returns:
            (lista de PathwayReadoutFeature não persistidos, n_not_found)

        Raises:
            OmicMatrixFeatureNotCataloguedError: matriz proteoma sem nenhuma
                OmicMatrixFeature catalogada (backfill não rodou).
        """
        if proteome_matrix is None or regulon_path is None:
            logger.info(
                'ReadoutMappingService: Regra 2 pulada '
                '(matriz proteoma ausente=%s, regulon_path ausente=%s).',
                proteome_matrix is None,
                regulon_path is None,
            )
            return [], 0

        # Mapa gene_symbol → PathwayNode nas vias de TF (mais recente por símbolo)
        tf_nodes: dict[str, PathwayNode] = {}
        for node in (
            PathwayNode.objects.filter(
                pathway__kegg_id__in=self.pathway_ids_tf,
                node_type=PathwayNode.NodeType.GENE,
            )
            .exclude(gene_symbol='')
            .select_related('pathway')
        ):
            # Um símbolo pode aparecer em múltiplas vias; usa o primeiro
            # encontrado (qualquer um serve como âncora para o PathwayReadoutFeature)
            if node.gene_symbol not in tf_nodes:
                tf_nodes[node.gene_symbol] = node

        # ── Passo 1: lê o JSONL inteiro, filtrando por TF conhecido na via ────
        # (duas passadas: a 1ª coleta os alvos candidatos para uma única
        # consulta batch a OmicMatrixFeature; a 2ª materializa.)
        parsed_entries: list[tuple[PathwayNode, str, int, float]] = []
        n_regulon_lines = 0

        try:
            with open(regulon_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    n_regulon_lines += 1
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError as jex:
                        logger.warning(
                            'ReadoutMappingService Regra 2: linha JSONL '
                            'invalida (ignorada): %s — erro: %s',
                            line[:120],
                            jex,
                        )
                        continue

                    tf_symbol = (entry.get('tf') or '').strip().upper()
                    target_symbol = (entry.get('target') or '').strip().upper()
                    # `mode` (não `sign`) é o campo real do JSONL — ver
                    # docstring acima / rust_src/src/omics/regulon_loader.rs.
                    sign = entry.get('mode', 0)
                    # Não há campo de confiança no JSONL (`n_references` é
                    # cru, não normalizado) — usa o fixo declarado.
                    confidence = CONFIDENCE_TF_TARGET

                    if not tf_symbol or not target_symbol:
                        continue

                    # TF deve ser nó da via
                    tf_node = tf_nodes.get(tf_symbol)
                    if tf_node is None:
                        # TF não está nas vias (filtro tf_allowlist do loader)
                        continue

                    parsed_entries.append((tf_node, target_symbol, int(sign), confidence))

        except OSError as exc:
            logger.error(
                'ReadoutMappingService Regra 2: nao foi possivel ler '
                'regulon_path=%s — erro: %s. Regra 2 abortada.',
                regulon_path,
                exc,
            )
            return [], 0

        # ── Passo 2: valida os alvos candidatos contra a matriz proteoma ──────
        candidate_targets = {target for _tf_node, target, _sign, _conf in parsed_entries}
        existing_targets = self._get_existing_feature_keys(
            proteome_matrix, candidate_targets
        )

        # ── Passo 3: materializa apenas os alvos que existem na matriz ────────
        features: list[PathwayReadoutFeature] = []
        seen: set[tuple[int, int, str, str]] = set()
        n_not_found = 0

        for tf_node, target_symbol, sign, confidence in parsed_entries:
            if target_symbol not in existing_targets:
                n_not_found += 1
                continue

            # Dedup por (node, matrix, feature_key, rule)
            key = (tf_node.id, proteome_matrix.id, target_symbol, 'tf_target')
            if key in seen:
                continue
            seen.add(key)

            features.append(
                PathwayReadoutFeature(
                    node=tf_node,
                    matrix=proteome_matrix,
                    feature_key=target_symbol,
                    rule=PathwayReadoutFeature.Rule.TF_TARGET,
                    regulon_source='collectri',
                    regulon_sign=sign,
                    confidence=confidence,
                    mapping_version=self.mapping_version,
                )
            )

        logger.info(
            'ReadoutMappingService Regra 2 (TF): %d linhas JSONL lidas, '
            '%d PathwayReadoutFeature prontos, %d alvos nao encontrados na '
            'matriz proteoma (vias=%s, matrix_id=%d).',
            n_regulon_lines,
            len(features),
            n_not_found,
            self.pathway_ids_tf,
            proteome_matrix.id,
        )
        return features, n_not_found

    # ─────────────────────────────────────────────────────────────────────────
    # Persistência
    # ─────────────────────────────────────────────────────────────────────────

    def _persist(
        self,
        phospho_features: list[PathwayReadoutFeature],
        tf_features: list[PathwayReadoutFeature],
    ) -> None:
        """
        Persiste PathwayReadoutFeature (bulk_create ignore_conflicts) e atualiza
        readout_role dos PathwayNode mapeados.

        Transação atômica — idempotente (NK na bulk_create).

        Escala (generalização p/ 372 vias — verificado, não é conjectura):
          `bulk_create` SEM `batch_size` explícito já não emite uma única
          INSERT gigante — o Django calcula internamente
          (`connection.ops.bulk_batch_size`) o nº máximo de linhas por
          statement que cabe no limite de parâmetros do backend (Postgres:
          65535 params/query) e fatia sozinho. Para os 9 campos de
          PathwayReadoutFeature isso já dá ~7281 linhas/lote automaticamente.
          `batch_size=BULK_CREATE_BATCH_SIZE` abaixo é mais conservador só
          para tornar o tamanho do lote PREVISÍVEL (não depende de contar
          campos do model) e limitar o tamanho de cada round-trip — não é
          uma correção de um bug de N+1 (não havia: nenhuma query roda dentro
          de laço por via/nó em nenhuma das duas regras — ver
          `_apply_rule_phospho`/`_apply_rule_tf`, ambas set-based com no
          máximo 1 query de validação cada, chamada uma única vez para o
          batch inteiro de candidatos).
        """
        all_features = phospho_features + tf_features

        with transaction.atomic():
            # bulk_create: idempotente pela NK (node, matrix, feature_key,
            # rule, mapping_version) → ignore_conflicts. batch_size explícito
            # (ver docstring acima) — não é obrigatório p/ correção, é p/
            # previsibilidade em escala (centenas de milhares de linhas
            # esperadas com o universo de 372 vias).
            if all_features:
                PathwayReadoutFeature.objects.bulk_create(
                    all_features,
                    ignore_conflicts=True,
                    batch_size=BULK_CREATE_BATCH_SIZE,
                )
                logger.info(
                    'PathwayReadoutFeature bulk_create: %d objetos enviados '
                    'em lotes de %d (ignore_conflicts=True; novos reais '
                    'podem ser menos).',
                    len(all_features),
                    BULK_CREATE_BATCH_SIZE,
                )

            # Atualiza readout_role nos PathwayNode mapeados como fosfo
            if phospho_features:
                phospho_node_ids = {f.node_id for f in phospho_features}
                updated_phospho = PathwayNode.objects.filter(
                    id__in=phospho_node_ids,
                ).update(readout_role=PathwayNode.ReadoutRole.PHOSPHO)
                logger.info(
                    'PathwayNode readout_role=phospho atualizado: %d nos.',
                    updated_phospho,
                )

            # Atualiza readout_role nos PathwayNode mapeados como tf_target
            if tf_features:
                tf_node_ids = {f.node_id for f in tf_features}
                updated_tf = PathwayNode.objects.filter(
                    id__in=tf_node_ids,
                ).update(readout_role=PathwayNode.ReadoutRole.TF_TARGET)
                logger.info(
                    'PathwayNode readout_role=tf_target atualizado: %d nos.',
                    updated_tf,
                )
