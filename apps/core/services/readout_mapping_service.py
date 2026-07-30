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
  feature_axis='gene'). Onde bate (alvo ∈ símbolos da matriz): nó do TF
  recebe readout_role='tf_target' + PathwayReadoutFeature(rule='tf_target',
  regulon_source='collectri', regulon_sign=±1, confidence=<conf>,
  mapping_version=<ver>).

Validação de existência de feature (Decisão 5 — A→C adiada):
  NÃO abre Parquet no Django. Estratégia por camada:

  - Regra 1: não há OmicMatrixFeature (A→C adiada). OmicMatrixSample guarda
    amostras, não features. O cruzamento gene_symbol é entre PG×PG — nós do
    grafo × símbolos derivados do próprio loader (o KGML traz gene_symbol que
    o Rust gravou em PathwayNode.gene_symbol). A validação "este símbolo existe
    no Parquet do fosfoproteoma" NÃO é possível sem abrir o Parquet.
    DECISÃO: materializa PathwayReadoutFeature para todos os nós cujo
    gene_symbol é não-vazio, registrando n_features_not_found=0 (sem
    validação) e sinalizando no relatório que a validação completa exige A→C.
    Gatilho A→C: se a Fase 4 precisar verificar que a feature realmente existe
    no Parquet antes de ler o valor → handoff para cartografo (OmicMatrixFeature).

  - Regra 2: os alvos dos regulons (target_symbol) são cruzados com os
    gene_symbol dos PathwayNode da mesma via (conjunto em PG). Se o alvo
    coincide com um gene_symbol de nó, considera-se "conhecido na via". Para
    cruzar com a matriz proteoma (features do Parquet), mesma limitação:
    sem OmicMatrixFeature não é possível verificar presença na linha do
    Parquet sem abrir o arquivo. DECISÃO: materializa PathwayReadoutFeature
    para todos os alvos cujos símbolos estão presentes nos PathwayNode das
    3 vias (validação intra-grafo, que é barata e em PG). Registra
    n_features_not_found e sinaliza limitação no relatório.

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


class ReadoutMappingError(Exception):
    """Levantado quando pré-condição do mapeamento não é satisfeita."""
    pass


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
            'gatilho_ac': bool,           # True se A→C é necessária p/ validação completa
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
        """
        import time
        self._start = time.monotonic()

        phospho_matrix = self._resolve_phospho_matrix()
        proteome_matrix = self._resolve_proteome_matrix()
        regulon_path = self._resolve_regulon_path()

        # Símbolos conhecidos na via (validação intra-grafo, barata)
        all_pathway_symbols = self._get_pathway_symbols(
            self.pathway_ids_phospho + [
                p for p in self.pathway_ids_tf
                if p not in self.pathway_ids_phospho
            ]
        )

        phospho_features, n_phospho_not_found = self._apply_rule_phospho(
            phospho_matrix, all_pathway_symbols
        )
        tf_features, n_tf_not_found = self._apply_rule_tf(
            proteome_matrix, regulon_path, all_pathway_symbols
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
            # Estratégia de validação e gatilho A→C (Decisão 5 do plano)
            'validation_strategy': (
                'intra-grafo: feature_key validada contra PathwayNode.gene_symbol '
                'em PG (conjunto de simbolos das vias). Validacao completa '
                '(existencia no Parquet da matriz) exige promocao A->C '
                '(OmicMatrixFeature). Gatilho A->C disparado: ver gatilho_ac.'
            ),
            'gatilho_ac': True,  # Sempre True para a Fase 3 (A→C adiada)
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

    def _get_pathway_symbols(self, pathway_ids: list[str]) -> set[str]:
        """
        Retorna o conjunto de gene_symbol dos PathwayNode das vias especificadas.

        Usado como "conjunto de features conhecidas em PG" para validação
        intra-grafo (Decisão 5 — A→C adiada). Excluindo símbolos vazios.
        """
        symbols = set(
            PathwayNode.objects.filter(
                pathway__kegg_id__in=pathway_ids,
                node_type=PathwayNode.NodeType.GENE,
            )
            .exclude(gene_symbol='')
            .values_list('gene_symbol', flat=True)
            .distinct()
        )
        logger.info(
            'ReadoutMappingService: %d simbolos distintos nas vias %s',
            len(symbols),
            pathway_ids,
        )
        return symbols

    # ─────────────────────────────────────────────────────────────────────────
    # Regra 1 — fosfo
    # ─────────────────────────────────────────────────────────────────────────

    def _apply_rule_phospho(
        self,
        phospho_matrix: OmicMatrix | None,
        all_pathway_symbols: set[str],
    ) -> tuple[list[PathwayReadoutFeature], int]:
        """
        Regra 1: nós gene das vias de fosfo × matriz de fosfoproteoma.

        feature_key = gene_symbol do nó (UPPERCASE).
        Confidence = 1.0 (gene-level — nó está na via e a feature é o símbolo).

        Estratégia de validação (Decisão 5 — A→C adiada):
          Não há OmicMatrixFeature nem índice de features em PG. O gene_symbol
          do nó é UPPERCASE (do KGML graphics name). A existência do símbolo
          no Parquet NÃO é verificável sem abrir o arquivo. Materializa para
          todos os nós com gene_symbol não-vazio nas vias de fosfo. Registra
          n_not_found=0 (sem validação) e sinaliza necessidade de A→C no
          relatório. Gatilho A→C: a Fase 4 precisará de OmicMatrixFeature para
          verificar valor real.

        Returns:
            (lista de PathwayReadoutFeature não persistidos, n_not_found)
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

        features = []
        seen: set[tuple[int, int, str, str]] = set()

        for node in nodes:
            symbol = node.gene_symbol.upper()
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

        # Validação intra-grafo (barata): conta quantos símbolos de fosfo NÃO
        # aparecem no conjunto global de símbolos das vias. Isso é apenas uma
        # verificação de consistência interna — não valida presença no Parquet.
        # n_not_found real (no Parquet) exige A→C.
        phospho_symbols = {f.feature_key for f in features}
        n_not_found_intra = len(phospho_symbols - all_pathway_symbols)
        # n_not_found reportado como 0 porque a validação completa não é feita.
        # A discrepância intra-grafo é registrada em log para diagnóstico.
        if n_not_found_intra > 0:
            logger.info(
                'ReadoutMappingService Regra 1: %d simbolos de fosfo nao '
                'encontrados no conjunto de simbolos das vias (inconsistencia '
                'intra-grafo) — nao afeta a materializacao.',
                n_not_found_intra,
            )

        logger.info(
            'ReadoutMappingService Regra 1 (fosfo): %d PathwayReadoutFeature '
            'prontos para materializacao (vias=%s, matrix_id=%d). '
            'NOTA: validacao de existencia no Parquet requer A->C (nao feita).',
            len(features),
            self.pathway_ids_phospho,
            phospho_matrix.id,
        )
        return features, 0  # 0 = validacao completa nao feita (ver gatilho_ac)

    # ─────────────────────────────────────────────────────────────────────────
    # Regra 2 — TF
    # ─────────────────────────────────────────────────────────────────────────

    def _apply_rule_tf(
        self,
        proteome_matrix: OmicMatrix | None,
        regulon_path: str | None,
        all_pathway_symbols: set[str],
    ) -> tuple[list[PathwayReadoutFeature], int]:
        """
        Regra 2: TFs das vias × regulons CollecTRI × features da matriz proteoma.

        Para cada TF (gene_symbol de nó no grafo) que aparece como source no
        JSONL de regulon, para cada alvo (target_symbol) do regulon: se o alvo
        está no conjunto de símbolos das vias (all_pathway_symbols, validação
        intra-grafo barata), materializa PathwayReadoutFeature(
          node=<nó do TF>, matrix=<proteoma>, feature_key=<target_symbol>,
          rule='tf_target', regulon_source='collectri', regulon_sign=±1,
          confidence=<conf>).

        Estratégia de validação (Decisão 5 — A→C adiada):
          Validação intra-grafo: alvo ∈ all_pathway_symbols (símbolos dos
          PathwayNode das 3 vias). Não verifica presença na linha do Parquet
          do proteoma (exigiria OmicMatrixFeature). n_features_not_found =
          nº de alvos do regulon ausentes do all_pathway_symbols (fora das vias).
          Gatilho A→C: validação completa (no Parquet) dispara na Fase 4.

        JSONL format esperado (produzido pelo Rust):
          Cada linha: {"tf": "SYMBOL", "target": "SYMBOL", "sign": 1, "confidence": 0.9}

        Returns:
            (lista de PathwayReadoutFeature não persistidos, n_not_found)
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

        features: list[PathwayReadoutFeature] = []
        seen: set[tuple[int, int, str, str]] = set()
        n_not_found = 0
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
                    sign = entry.get('sign', 0)
                    confidence = float(entry.get('confidence', 0.0))

                    if not tf_symbol or not target_symbol:
                        continue

                    # TF deve ser nó da via
                    tf_node = tf_nodes.get(tf_symbol)
                    if tf_node is None:
                        # TF não está nas vias (filtro tf_allowlist do loader)
                        continue

                    # Validação intra-grafo: alvo ∈ all_pathway_symbols
                    if target_symbol not in all_pathway_symbols:
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
                            regulon_sign=int(sign),
                            confidence=confidence,
                            mapping_version=self.mapping_version,
                        )
                    )

        except OSError as exc:
            logger.error(
                'ReadoutMappingService Regra 2: nao foi possivel ler '
                'regulon_path=%s — erro: %s. Regra 2 abortada.',
                regulon_path,
                exc,
            )
            return [], 0

        logger.info(
            'ReadoutMappingService Regra 2 (TF): %d linhas JSONL lidas, '
            '%d PathwayReadoutFeature prontos, %d alvos fora das vias '
            '(n_not_found intra-grafo, vias=%s, matrix_id=%d). '
            'NOTA: validacao no Parquet requer A->C (nao feita).',
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
