"""
ReadoutMappingService — mapeamento readout→feature (Fase 3, Passo 3.4).

OmnisPathway Objetivo 2, Fase 3, Passo 3.4.

Responsabilidade:
  Cruzar nós de readout do grafo KEGG × features das matrizes ômicas e
  materializar PathwayReadoutFeature + marcar PathwayNode.readout_role.
  Django set-based puro (ORM/SQL) — ZERO Rust, ZERO abertura de Parquet.

Regra 1 (fosfo):
  PathwayNode(node_type='gene') das vias hsa04151/hsa04010 (PI3K-Akt, MAPK)
  × features da matriz de fosfoproteoma (OmicMatrix omics_layer='proteomic',
  feature_axis='phospho_site', loader_version='phospho-v1').
  feature_key = gene_symbol do nó (UPPERCASE).
  Onde bate: PathwayNode.readout_role='phospho' + PathwayReadoutFeature(
    rule='phospho', confidence=1.0, mapping_version=<ver>).

Regra 2 (TF):
  Para cada nó cujo gene_symbol é TF (source) no JSONL de regulon (caminho
  do IngestionJob mais recente de REGULON_LOAD), para cada alvo do regulon
  cruzar com features da matriz proteoma da Fase 0 (CPTAC-CCRCC proteome,
  feature_axis='gene'). Onde bate (alvo ∈ features da matriz proteoma): nó
  do TF recebe readout_role='tf_target' + PathwayReadoutFeature(
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

Sensitive-data-handling:
  Nenhuma credencial. regulon_path é caminho local (nunca exposto ao cliente).
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
    PathwayNode,
    PathwayReadoutFeature,
)

logger = logging.getLogger(__name__)

# Vias da Regra 1 (fosfo): PI3K-Akt e MAPK (vias de sinalização com fosfo)
# p53 não entra na Regra 1 porque os readouts de fosfo do ciclo celular são
# menos bem definidos no fosfoproteoma gene-level. Extensível via parâmetro.
PHOSPHO_PATHWAY_IDS = ['hsa04151', 'hsa04010']

# Vias da Regra 2 (TF): todas as 3 (TFs aparecem nas 3 vias)
TF_PATHWAY_IDS = ['hsa04151', 'hsa04010', 'hsa04115']

# loader_version da matriz de fosfoproteoma (passo 3.1)
PHOSPHO_LOADER_VERSION = 'phospho-v1'

# Versão padrão do mapeamento
DEFAULT_MAPPING_VERSION = 'fase3-readout-v1'

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

    Uso:
        service = ReadoutMappingService(
            pathway_ids_phospho=PHOSPHO_PATHWAY_IDS,
            pathway_ids_tf=TF_PATHWAY_IDS,
            mapping_version='fase3-readout-v1',
            dry_run=False,
        )
        report = service.run()

    Retorno de run():
        {
            'n_phospho_mapped': int,
            'n_tf_mapped': int,
            'n_nodes_readout': int,
            'n_unmapped_nodes': int,
            'n_features_not_found': int,
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
        self.pathway_ids_phospho = pathway_ids_phospho or PHOSPHO_PATHWAY_IDS
        self.pathway_ids_tf = pathway_ids_tf or TF_PATHWAY_IDS
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
          1. Resolve matrizes (fosfoproteoma + proteoma).
          2. Resolve caminho do JSONL de regulon (IngestionJob mais recente).
          3. Regra 1: cruza nós de fosfo × matriz fosfoproteoma.
          4. Regra 2: cruza TFs regulon × matriz proteoma.
          5. bulk_create PathwayReadoutFeature + update readout_role.
          6. Retorna relatório.

        Returns:
            dict com contadores e estratégia de validação.

        Raises:
            ReadoutMappingError: pré-condição não satisfeita.
            OmicMatrixFeatureNotCataloguedError: a matriz a validar não tem
                nenhuma OmicMatrixFeature catalogada (backfill não rodou).
        """
        import time
        self._start = time.monotonic()

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

        report = {
            'n_phospho_mapped': n_phospho_mapped,
            'n_tf_mapped': n_tf_mapped,
            'n_nodes_readout': n_nodes_readout,
            'n_unmapped_nodes': n_unmapped_nodes,
            'n_features_not_found': n_features_not_found,
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
        return report

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
        """
        all_features = phospho_features + tf_features

        with transaction.atomic():
            # bulk_create: idempotente pela NK (node, matrix, feature_key,
            # rule, mapping_version) → ignore_conflicts.
            if all_features:
                created = PathwayReadoutFeature.objects.bulk_create(
                    all_features,
                    ignore_conflicts=True,
                )
                logger.info(
                    'PathwayReadoutFeature bulk_create: %d objetos enviados '
                    '(ignore_conflicts=True; novos reais podem ser menos).',
                    len(all_features),
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
