"""
SeedConnectivityService — diagnóstico de conectividade de semente (checagem
PRÉ-EXECUÇÃO do PFS) — OmnisPathway Obj 2, Fase 4.

Motivação (registrada — não é hipotética): o motor PFS produziu resultado
fraco numa bancada real e a causa só apareceu no FIM da investigação — VHL
(driver de ~90% dos carcinomas renais, 219 sementes na bancada) é NÓ
ISOLADO no KEGG, sem nenhuma aresta. Uma semente sobre um nó sem saída não
propaga para lugar nenhum; o RWR assinado não tem substrato para produzir
sinal, e nenhum contador existente (nem `report_pathway_activity`, que só
enxerga o que já rodou) avisa disso ANTES do run. Medido sistematicamente,
o caso VHL revelou-se regra, não exceção: ~45% dos nós-gene semeados nas
372 vias não têm nenhuma aresta de saída.

A métrica que importa é o GRAU DE SAÍDA (`out_degree`), não o grau total —
a semente propaga PARA FORA do nó. Um nó com entradas mas zero saídas
(ex.: HIF1A em hsa04066) é tão inútil para semeadura quanto um nó
totalmente isolado; por isso este serviço NUNCA olha `in_edges`.

Três categorias, MUTUAMENTE EXCLUSIVAS, cobrindo todo nó-gene semeado:
    sem_saida   out_degree == 0                          — não propaga.
    cega        out_degree > 0, TODAS as arestas sign=0   — propaga sem
                                                             direção
                                                             ("biologicamente
                                                             cego").
    util        out_degree > 0, >=1 aresta com sign != 0  — útil.

Escopo do projeto: `VariantEffectSeed` não tem FK de projeto (mesmo padrão
de `OmicSamplePairing`/`PathwayScoringService`) — o isolamento por usuário
vem de `matrix -> dataset -> ProjectDataset(project=..., curation_status
ativo)`. Reusa a mesma lista `_ACTIVE_STATUSES` de
`pathway_scoring_service` para consistência com o motor que este
diagnóstico antecede.

Regra #1 (set-based): TODA agregação — global, por via, por gene — é uma
query SQL agregada própria (CTE reaproveitado literalmente em cada uma,
mesmo padrão de `pathway_report_service`). Nenhum loop Python soma/filtra
nó ou semente linha por linha; o resultado de cada query já vem pronto
para renderização.

Junção semente↔nó: por `gene_symbol` em UPPERCASE nos dois lados
(`UPPER(...)` explícito no SQL) — mesma convenção do resto do pipeline
(Decisão 6), mas sem confiar que o dado já está normalizado.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.db import connection

logger = logging.getLogger(__name__)

# Espelha pathway_scoring_service._ACTIVE_STATUSES — mesma noção de
# "dataset ativo no projeto" usada pelo motor que este diagnóstico antecede.
_ACTIVE_STATUSES = ('included', 'queued', 'downloaded', 'pending')

# "As direcionais atuais" — NÃO inclui fase2-cnv-v1 (legado), ao contrário
# de DEFAULT_SEED_METHOD_VERSIONS do PathwayScoringService (que existe para
# resolver precedência entre versões no motor, não para medir conectividade).
DEFAULT_SEED_METHOD_VERSIONS = ['fase2-cnv-v2', 'fase2-snv-v1']

DEFAULT_TOP = 20

# Fração "útil" abaixo da qual o veredito alerta execução tendendo a
# degenerada. Não é um limiar científico fino — é um sinal grosseiro de
# "isso provavelmente não vale a pena rodar sem investigar antes".
USEFUL_FRACTION_WARN_THRESHOLD = 0.60


class SeedConnectivityEmptyError(ValueError):
    """Nenhum nó-gene semeado encontrado para este projeto/escopo."""


# =============================================================================
# CTE compartilhado (literal em cada query — mesmo padrão de
# pathway_report_service._SQL_*)
# =============================================================================

_CTE = """
WITH qualifying_seeds AS (
    SELECT
        UPPER(s.gene_symbol) AS gene_upper,
        COUNT(*) AS n_seeds
    FROM core_varianteffectseed s
    JOIN core_omicmatrix m ON m.id = s.matrix_id
    JOIN core_projectdataset pd
        ON pd.dataset_id = m.dataset_id
        AND pd.project_id = %(project_id)s
        AND pd.curation_status = ANY(%(active_statuses)s)
    WHERE s.direction <> 'neutral'
      AND s.method_version = ANY(%(seed_method_versions)s)
    GROUP BY UPPER(s.gene_symbol)
),
seeded_nodes AS (
    SELECT
        n.id AS node_id,
        n.pathway_id,
        UPPER(n.gene_symbol) AS gene_upper
    FROM core_pathwaynode n
    JOIN core_pathway p ON p.id = n.pathway_id
    JOIN qualifying_seeds qs ON qs.gene_upper = UPPER(n.gene_symbol)
    WHERE n.node_type = 'gene'
      AND n.gene_symbol <> ''
      AND (%(pathway_ids)s IS NULL OR p.kegg_id = ANY(%(pathway_ids)s))
),
node_degree AS (
    SELECT
        sn.node_id,
        sn.pathway_id,
        sn.gene_upper,
        COUNT(e.id) AS out_total,
        COUNT(e.id) FILTER (WHERE e.sign <> 0) AS out_signed
    FROM seeded_nodes sn
    LEFT JOIN core_pathwayedge e ON e.source_node_id = sn.node_id
    GROUP BY sn.node_id, sn.pathway_id, sn.gene_upper
),
node_classified AS (
    SELECT
        nd.node_id,
        nd.pathway_id,
        nd.gene_upper,
        CASE
            WHEN nd.out_total = 0 THEN 'sem_saida'
            WHEN nd.out_signed = 0 THEN 'cega'
            ELSE 'util'
        END AS category
    FROM node_degree nd
)
"""


_SQL_GLOBAL = _CTE + """
SELECT
    COUNT(*) AS n_total,
    COUNT(*) FILTER (WHERE category = 'sem_saida') AS n_sem_saida,
    COUNT(*) FILTER (WHERE category = 'cega') AS n_cega,
    COUNT(*) FILTER (WHERE category = 'util') AS n_util
FROM node_classified
"""


_SQL_BY_PATHWAY = _CTE + """,
pathway_gene AS (
    SELECT DISTINCT pathway_id, gene_upper FROM node_classified
),
pathway_seeds AS (
    SELECT pg.pathway_id, SUM(qs.n_seeds) AS n_seeds_involved
    FROM pathway_gene pg
    JOIN qualifying_seeds qs ON qs.gene_upper = pg.gene_upper
    GROUP BY pg.pathway_id
)
SELECT
    p.kegg_id,
    p.name,
    COUNT(*) AS n_seeded_nodes,
    COUNT(*) FILTER (WHERE nc.category = 'sem_saida') AS n_sem_saida,
    COUNT(*) FILTER (WHERE nc.category = 'cega') AS n_cega,
    COUNT(*) FILTER (WHERE nc.category = 'util') AS n_util,
    COUNT(*) FILTER (WHERE nc.category = 'sem_saida')::float / COUNT(*) AS frac_sem_saida,
    ps.n_seeds_involved
FROM node_classified nc
JOIN core_pathway p ON p.id = nc.pathway_id
JOIN pathway_seeds ps ON ps.pathway_id = nc.pathway_id
GROUP BY p.id, p.kegg_id, p.name, ps.n_seeds_involved
ORDER BY frac_sem_saida DESC, n_sem_saida DESC, p.kegg_id
"""


_SQL_BY_GENE = _CTE + """,
pathway_gene_status AS (
    SELECT
        pathway_id,
        gene_upper,
        BOOL_AND(category = 'sem_saida') AS pathway_fully_isolated
    FROM node_classified
    GROUP BY pathway_id, gene_upper
)
SELECT
    qs.gene_upper AS gene_symbol,
    qs.n_seeds,
    COUNT(*) AS n_pathways_total,
    COUNT(*) FILTER (WHERE pgs.pathway_fully_isolated) AS n_pathways_isolated
FROM pathway_gene_status pgs
JOIN qualifying_seeds qs ON qs.gene_upper = pgs.gene_upper
GROUP BY qs.gene_upper, qs.n_seeds
HAVING COUNT(*) FILTER (WHERE pgs.pathway_fully_isolated) > 0
ORDER BY qs.n_seeds DESC, n_pathways_isolated DESC, gene_symbol
"""


def _params(project_id, seed_method_versions, pathway_ids):
    return {
        'project_id': str(project_id),
        'active_statuses': list(_ACTIVE_STATUSES),
        'seed_method_versions': list(seed_method_versions),
        'pathway_ids': list(pathway_ids) if pathway_ids else None,
    }


def _fetch_dicts(sql: str, params: dict) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        cols = [c[0] for c in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]


def build_report(
    project,
    *,
    seed_method_versions: list[str] | None = None,
    pathway_ids: list[str] | None = None,
    top: int = DEFAULT_TOP,
) -> dict:
    """
    Monta o diagnóstico completo de conectividade de semente para `project`
    (instância de `DaVinciProject`). Levanta `SeedConnectivityEmptyError` se
    nenhum nó-gene semeado for encontrado (zero interseção entre sementes
    direcionais do projeto e o grafo carregado) — sinal de que a bancada
    ainda não tem sementes ou o grafo/`--pathways` pedido não bate com
    nenhum gene semeado.
    """
    seed_versions = list(seed_method_versions or DEFAULT_SEED_METHOD_VERSIONS)
    params = _params(project.id, seed_versions, pathway_ids)

    global_rows = _fetch_dicts(_SQL_GLOBAL, params)
    global_row = global_rows[0] if global_rows else {
        'n_total': 0, 'n_sem_saida': 0, 'n_cega': 0, 'n_util': 0,
    }
    n_total = global_row['n_total'] or 0

    if n_total == 0:
        raise SeedConnectivityEmptyError(
            f'Nenhum nó-gene semeado para o projeto {project.id} com '
            f'seed_method_versions={seed_versions!r}'
            + (f', pathways={pathway_ids!r}' if pathway_ids else '')
            + '. Verifique se VariantEffectSeed foi derivado (derive_cnv_seeds '
              '/ derive_snv_seeds) e se load_pathway_topology já rodou.'
        )

    def _frac(n):
        return (n / n_total) if n_total else 0.0

    summary = {
        'n_total': n_total,
        'n_sem_saida': global_row['n_sem_saida'] or 0,
        'n_cega': global_row['n_cega'] or 0,
        'n_util': global_row['n_util'] or 0,
        'frac_sem_saida': _frac(global_row['n_sem_saida'] or 0),
        'frac_cega': _frac(global_row['n_cega'] or 0),
        'frac_util': _frac(global_row['n_util'] or 0),
    }

    by_pathway_rows = _fetch_dicts(_SQL_BY_PATHWAY, params)
    for row in by_pathway_rows:
        row['frac_sem_saida'] = float(row['frac_sem_saida'])
        row['n_seeds_involved'] = int(row['n_seeds_involved'] or 0)

    by_gene_rows = _fetch_dicts(_SQL_BY_GENE, params)

    verdict = _build_verdict(summary)

    generated_at = datetime.now(dt_timezone.utc)

    return {
        'project_id': str(project.id),
        'project_title': project.title,
        'seed_method_versions': seed_versions,
        'pathway_ids': list(pathway_ids) if pathway_ids else None,
        'top': top,
        'generated_at': generated_at.isoformat(),
        'summary': summary,
        'by_pathway_worst': by_pathway_rows[:top],
        'by_pathway_total': len(by_pathway_rows),
        'by_gene_isolated': by_gene_rows[:top],
        'by_gene_isolated_total': len(by_gene_rows),
        'verdict': verdict,
    }


def _build_verdict(summary: dict) -> dict:
    frac_util = summary['frac_util']
    degenerate = frac_util < USEFUL_FRACTION_WARN_THRESHOLD
    if degenerate:
        message = (
            f'ALERTA: apenas {frac_util:.1%} dos nós-gene semeados têm '
            f'saída assinada (útil). {summary["frac_sem_saida"]:.1%} não '
            f'propagam ({summary["n_sem_saida"]} nós sem nenhuma aresta de '
            f'saída) e {summary["frac_cega"]:.1%} propagam sem direção '
            f'({summary["n_cega"]} nós "cegos", só arestas sign=0). Rodar '
            f'o PFS agora tende a produzir resultado DEGENERADO — a maioria '
            f'das sementes não chega a lugar nenhum ou não carrega sinal. '
            f'Investigue os genes isolados listados abaixo (topologia KEGG '
            f'da via, ou se a semente deveria mirar outro nó) antes de '
            f'rodar `score_pathways`.'
        )
    else:
        message = (
            f'{frac_util:.1%} dos nós-gene semeados têm saída assinada '
            f'(útil) — substrato razoável para o PFS propagar sinal. '
            f'{summary["frac_sem_saida"]:.1%} sem saída e '
            f'{summary["frac_cega"]:.1%} cegos permanecem como ruído '
            f'esperado, não como bloqueio sistêmico.'
        )
    return {
        'degenerate_risk': degenerate,
        'threshold': USEFUL_FRACTION_WARN_THRESHOLD,
        'message': message,
    }


# =============================================================================
# Formatação / persistência
# =============================================================================

def render_text(report: dict) -> str:
    lines: list[str] = []
    w = lines.append
    sep = '=' * 78

    w(sep)
    w(f'Diagnóstico de conectividade de semente — projeto {report["project_title"]!r} '
      f'({report["project_id"]})')
    w(f'seed_method_versions: {report["seed_method_versions"]}')
    if report['pathway_ids']:
        w(f'vias restritas (--pathways): {report["pathway_ids"]}')
    w(f'gerado em: {report["generated_at"]}')
    w(sep)

    s = report['summary']
    w('')
    w('── Global (nós-gene semeados) ────────────────────────────────────────')
    w(f'  total de nós-gene semeados: {s["n_total"]}')
    w(f'  sem saída (não propaga):    {s["n_sem_saida"]:>6d}  ({s["frac_sem_saida"]:.1%})')
    w(f'  saída cega (sign=0 só):     {s["n_cega"]:>6d}  ({s["frac_cega"]:.1%})')
    w(f'  útil (saída assinada):      {s["n_util"]:>6d}  ({s["frac_util"]:.1%})')

    w('')
    w(f'── Por via — piores primeiro (maior fração sem saída), top {report["top"]} '
      f'de {report["by_pathway_total"]} ─────')
    w(f'  {"via":10s} {"nome":34s} {"nós":>5s} {"sem saída":>10s} {"cegos":>7s} '
      f'{"úteis":>7s} {"frac.sem saída":>15s} {"sementes":>9s}')
    for row in report['by_pathway_worst']:
        name = (row['name'] or '')[:34]
        w(f'  {row["kegg_id"]:10s} {name:34s} {row["n_seeded_nodes"]:>5d} '
          f'{row["n_sem_saida"]:>10d} {row["n_cega"]:>7d} {row["n_util"]:>7d} '
          f'{row["frac_sem_saida"]:>15.1%} {row["n_seeds_involved"]:>9d}')

    w('')
    w(f'── Por gene — genes semeados isolados (100% das vias onde aparecem), '
      f'top {report["top"]} de {report["by_gene_isolated_total"]} ─────')
    w(f'  {"gene":14s} {"sementes":>9s} {"vias isoladas / vias totais":>28s}')
    for row in report['by_gene_isolated']:
        frac = f'{row["n_pathways_isolated"]}/{row["n_pathways_total"]}'
        w(f'  {row["gene_symbol"]:14s} {row["n_seeds"]:>9d} {frac:>28s}')

    v = report['verdict']
    w('')
    w('── Veredito ─────────────────────────────────────────────────────────')
    w(f'  {v["message"]}')

    w('')
    w(sep)
    return '\n'.join(lines)


def write_json(report: dict, project_id) -> str:
    """
    Grava o relatório em JSON sob `settings.REPO_ROOT / 'diagnostics' /
    'exports'` (gitignored) — mesmo destino/convenção de
    `pathway_report_service.write_json`. Retorna o caminho absoluto gravado.
    """
    export_dir = os.path.join(str(settings.REPO_ROOT), 'diagnostics', 'exports')
    os.makedirs(export_dir, exist_ok=True)

    timestamp = datetime.now(dt_timezone.utc).strftime('%Y%m%d_%H%M%S')
    filename = f'seed_connectivity_diagnosis_{project_id}_{timestamp}.json'
    path = os.path.join(export_dir, filename)

    with open(path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=False)

    return path
