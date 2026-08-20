"""
PathwayReportService — números reprodutíveis do PFS (OmnisPathway Obj 2,
Fase 4), para substituir consultas ad-hoc digitadas no shell.

Nomenclatura (deliberada — ver plano da Fase 4): o relatório NÃO usa
"ativa/inativa". O escore não mede se a via está biologicamente ligada —
mede se a alteração genômica do paciente EXPLICA a mudança proteômica
melhor que o acaso (permutação da posição da semente). Por isso os rótulos
são "atribuível" / "não atribuível": uma via "atribuível" tem pelo menos um
paciente em que o q do escopo POPULACIONAL (`q_value_across_samples`) cruza
o limiar — a alteração genômica do paciente é uma explicação estatisticamente
defensável para a leitura de via observada. "Não atribuível" é uma via
testada (tem pelo menos um par avaliável) que nunca cruzou o limiar.

Taxonomia das 372 vias (soma = total do catálogo `Pathway`):
    bloqueada-sem-readout  via SEM NENHUMA linha em PathwayActivityScore
                            para este method_version — o motor a pulou
                            inteira (n_pathways_no_readout do manifesto):
                            zero PathwayReadoutFeature, nunca chegou a rodar
                            RWR nela.
    bloqueada-sem-semente   via COM linhas, mas TODAS degeneradas
                            (null_sd == 0 em 100% dos pares desta via) — o
                            teste rodou mas nunca teve substrato (semente
                            e/ou readout) para produzir uma nula não-trivial
                            em NENHUM paciente.
    atribuível              via com pelo menos um par avaliável E pelo
                            menos um q_value_across_samples < limiar.
    não atribuível          via com pelo menos um par avaliável, mas NUNCA
                            cruzou o limiar.

Estas quatro categorias são MUTUAMENTE EXCLUSIVAS e cobrem o catálogo
inteiro por construção (cada via cai em exatamente uma, verificado no
teste de aceitação: 22/237/97/16 = 372 na bancada `fase4-pfs-v2-372vias`).

Regra #1: os números vêm de queries agregadas set-based (Django ORM com
`Count(..., filter=Q(...))`, ou SQL agregado quando o ORM não expressa —
mediana via `PERCENTILE_CONT`). Nenhum loop Python soma/filtra linha por
linha de PathwayActivityScore.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.db import connection
from django.db.models import Count, Q

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 0.05
_SENSITIVITY_THRESHOLDS = (0.05, 0.01, 0.001)


class PathwayReportEmptyError(ValueError):
    """`method_version` sem nenhuma linha em PathwayActivityScore."""


# =============================================================================
# Construção do relatório
# =============================================================================

def build_report(method_version: str, threshold: float = DEFAULT_THRESHOLD) -> dict:
    """
    Monta o relatório completo do PFS para `method_version`. Levanta
    `PathwayReportEmptyError` se não houver nenhuma linha — sinal de que o
    motor (`score_pathways`) ainda não rodou sob esta versão.
    """
    from apps.core.models import Pathway, PathwayActivityScore

    if not method_version:
        raise ValueError('method_version é obrigatório.')

    base_qs = PathwayActivityScore.objects.filter(method_version=method_version)
    total_pairs = base_qs.count()
    if total_pairs == 0:
        raise PathwayReportEmptyError(
            f'Nenhuma linha em PathwayActivityScore para method_version='
            f'{method_version!r}. O motor (score_pathways) já rodou sob '
            f'esta versão?'
        )

    generated_at = datetime.now(dt_timezone.utc)

    summary = _build_summary(base_qs, total_pairs)
    taxonomy = _build_taxonomy(Pathway, method_version, threshold)
    population = _build_population(method_version, threshold)
    individual = _build_individual(method_version, threshold)
    sensitivity = _build_sensitivity(base_qs)
    grouping = _build_grouping(method_version, threshold)

    return {
        'method_version': method_version,
        'threshold': threshold,
        'generated_at': generated_at.isoformat(),
        'summary': summary,
        'taxonomy': taxonomy,
        'population': population,
        'individual': individual,
        'sensitivity': sensitivity,
        'grouping': grouping,
    }


def _build_summary(base_qs, total_pairs: int) -> dict:
    agg = base_qs.aggregate(
        evaluable=Count('id', filter=~Q(null_sd=0)),
        degenerate=Count('id', filter=Q(null_sd=0)),
    )
    b_values = sorted(set(
        base_qs.exclude(n_permutations=0).values_list('n_permutations', flat=True).distinct()
    ))
    return {
        'total_pairs': total_pairs,
        'evaluable': agg['evaluable'],
        'degenerate': agg['degenerate'],
        # B efetivo: n_permutations != 0 é o valor REALMENTE usado quando a
        # permutação rodou. 0 é o marcador do motor para "não rodou nenhuma
        # permutação" (skip precoce em par sem semente no grafo) — nunca é B.
        'b_values': b_values,
        'b': b_values[0] if len(b_values) == 1 else None,
    }


def _build_taxonomy(pathway_model, method_version: str, threshold: float) -> dict:
    """
    Uma query agregada por via (LEFT JOIN de Pathway com
    PathwayActivityScore restrito a este method_version) — cobre inclusive
    vias SEM nenhuma linha (bloqueada-sem-readout).
    """
    qs = pathway_model.objects.annotate(
        n_rows=Count(
            'activity_scores',
            filter=Q(activity_scores__method_version=method_version),
        ),
        n_evaluable=Count(
            'activity_scores',
            filter=Q(activity_scores__method_version=method_version)
            & ~Q(activity_scores__null_sd=0),
        ),
        n_significant=Count(
            'activity_scores',
            filter=Q(
                activity_scores__method_version=method_version,
                activity_scores__q_value_across_samples__lt=threshold,
            ),
        ),
    ).values('n_rows', 'n_evaluable', 'n_significant')

    counts = {
        'bloqueada_sem_readout': 0,
        'bloqueada_sem_semente': 0,
        'atribuivel': 0,
        'nao_atribuivel': 0,
    }
    for row in qs:
        if row['n_rows'] == 0:
            counts['bloqueada_sem_readout'] += 1
        elif row['n_evaluable'] == 0:
            counts['bloqueada_sem_semente'] += 1
        elif row['n_significant'] > 0:
            counts['atribuivel'] += 1
        else:
            counts['nao_atribuivel'] += 1

    counts['total'] = sum(
        counts[k] for k in
        ('bloqueada_sem_readout', 'bloqueada_sem_semente', 'atribuivel', 'nao_atribuivel')
    )
    return counts


_SQL_POPULATION_BY_PATHWAY = """
SELECT
    p.kegg_id,
    p.name,
    COUNT(s.id) FILTER (WHERE s.q_value_across_samples < %(threshold)s) AS n_attributable_samples,
    COUNT(s.id) FILTER (WHERE s.null_sd <> 0) AS n_evaluable,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY s.z_score)
        FILTER (WHERE s.null_sd <> 0) AS z_median,
    MIN(s.q_value_across_samples) AS q_min,
    e.unsigned_edge_fraction
FROM core_pathway p
JOIN core_pathwayactivityscore s
    ON s.pathway_id = p.id AND s.method_version = %(mv)s
LEFT JOIN (
    SELECT pathway_id, AVG(CASE WHEN sign = 0 THEN 1.0 ELSE 0.0 END) AS unsigned_edge_fraction
    FROM core_pathwayedge
    GROUP BY pathway_id
) e ON e.pathway_id = p.id
GROUP BY p.id, p.kegg_id, p.name, e.unsigned_edge_fraction
HAVING COUNT(s.id) FILTER (WHERE s.q_value_across_samples < %(threshold)s) > 0
ORDER BY q_min ASC NULLS LAST, p.kegg_id
"""


def _build_population(method_version: str, threshold: float) -> dict:
    """
    Escopo populacional (q_value_across_samples): por via ATRIBUÍVEL —
    pacientes atribuíveis, avaliáveis, z mediano, q mínimo, fração de
    arestas sem sinal (PathwayEdge.sign == 0 nesta via).
    """
    with connection.cursor() as cursor:
        cursor.execute(_SQL_POPULATION_BY_PATHWAY, {'threshold': threshold, 'mv': method_version})
        cols = [c[0] for c in cursor.description]
        rows = [dict(zip(cols, r)) for r in cursor.fetchall()]

    for row in rows:
        row['z_median'] = float(row['z_median']) if row['z_median'] is not None else None
        row['q_min'] = float(row['q_min']) if row['q_min'] is not None else None
        row['unsigned_edge_fraction'] = (
            float(row['unsigned_edge_fraction'])
            if row['unsigned_edge_fraction'] is not None else None
        )

    distinct_pathways = len(rows)

    # n_samples do escopo populacional = distinct sample_id entre os pares
    # significativos (não é soma de n_attributable_samples — um paciente
    # pode ser atribuível em mais de uma via).
    from apps.core.models import PathwayActivityScore
    n_samples = (
        PathwayActivityScore.objects
        .filter(method_version=method_version, q_value_across_samples__lt=threshold)
        .values('sample_id').distinct().count()
    )
    n_significant_pairs = (
        PathwayActivityScore.objects
        .filter(method_version=method_version, q_value_across_samples__lt=threshold)
        .count()
    )

    return {
        'threshold': threshold,
        'n_significant_pairs': n_significant_pairs,
        'n_pathways': distinct_pathways,
        'n_samples': n_samples,
        'by_pathway': rows,
    }


_SQL_INDIVIDUAL_BY_SAMPLE = """
SELECT
    os.accession,
    os.characteristics ->> 'case_id' AS case_id,
    COUNT(s.id) AS n_pathways_below_threshold,
    MIN(s.q_value_across_pathways) AS q_min
FROM core_omicsample os
JOIN core_pathwayactivityscore s
    ON s.sample_id = os.id AND s.method_version = %(mv)s
WHERE s.q_value_across_pathways < %(threshold)s
GROUP BY os.id, os.accession, case_id
ORDER BY n_pathways_below_threshold DESC, os.accession
"""


def _build_individual(method_version: str, threshold: float) -> dict:
    """Escopo individual (q_value_across_pathways): por paciente — vias abaixo do limiar."""
    with connection.cursor() as cursor:
        cursor.execute(_SQL_INDIVIDUAL_BY_SAMPLE, {'threshold': threshold, 'mv': method_version})
        cols = [c[0] for c in cursor.description]
        rows = [dict(zip(cols, r)) for r in cursor.fetchall()]

    for row in rows:
        row['q_min'] = float(row['q_min']) if row['q_min'] is not None else None

    n_significant_pairs = sum(row['n_pathways_below_threshold'] for row in rows)

    return {
        'threshold': threshold,
        'n_significant_pairs': n_significant_pairs,
        'n_samples': len(rows),
        'by_sample': rows,
    }


def _build_sensitivity(base_qs) -> dict:
    """
    Contagem de pares significativos no escopo POPULACIONAL
    (q_value_across_samples) em três limiares fixos — mede o quão sensível
    a lista de achados é à escolha do limiar de publicação.
    """
    out = {}
    for q in _SENSITIVITY_THRESHOLDS:
        out[f'q<{q}'] = base_qs.filter(q_value_across_samples__lt=q).count()
    return out


_SQL_CO_OCCURRING_PAIRS = """
WITH sig AS (
    SELECT s.sample_id, p.kegg_id
    FROM core_pathwayactivityscore s
    JOIN core_pathway p ON p.id = s.pathway_id
    WHERE s.method_version = %(mv)s AND s.q_value_across_pathways < %(threshold)s
)
SELECT a.kegg_id AS pathway_a, b.kegg_id AS pathway_b, COUNT(DISTINCT a.sample_id) AS n_patients
FROM sig a
JOIN sig b ON a.sample_id = b.sample_id AND a.kegg_id < b.kegg_id
GROUP BY a.kegg_id, b.kegg_id
ORDER BY n_patients DESC, pathway_a, pathway_b
"""

_SQL_MULTI_ATTRIBUTABLE_PATIENTS = """
SELECT
    os.accession,
    os.characteristics ->> 'case_id' AS case_id,
    ARRAY_AGG(p.kegg_id ORDER BY p.kegg_id) AS pathways
FROM core_pathwayactivityscore s
JOIN core_omicsample os ON os.id = s.sample_id
JOIN core_pathway p ON p.id = s.pathway_id
WHERE s.method_version = %(mv)s AND s.q_value_across_pathways < %(threshold)s
GROUP BY os.id, os.accession, case_id
HAVING COUNT(*) > 1
ORDER BY os.accession
"""


def _build_grouping(method_version: str, threshold: float) -> dict:
    """
    Agrupamento (escopo individual): pacientes com MAIS DE UMA via
    atribuível, e pares de vias co-ocorrentes (mesma leitura em UM
    paciente).
    """
    with connection.cursor() as cursor:
        cursor.execute(
            _SQL_MULTI_ATTRIBUTABLE_PATIENTS, {'threshold': threshold, 'mv': method_version},
        )
        cols = [c[0] for c in cursor.description]
        multi_patients = [dict(zip(cols, r)) for r in cursor.fetchall()]

        cursor.execute(_SQL_CO_OCCURRING_PAIRS, {'threshold': threshold, 'mv': method_version})
        cols = [c[0] for c in cursor.description]
        co_occurring = [dict(zip(cols, r)) for r in cursor.fetchall()]

    return {
        'patients_multi_attributable': multi_patients,
        'co_occurring_pairs': co_occurring,
    }


# =============================================================================
# Formatação / persistência
# =============================================================================

def render_text(report: dict) -> str:
    lines: list[str] = []
    w = lines.append
    sep = '=' * 78

    w(sep)
    w(f'Relatório PFS — method_version={report["method_version"]!r}')
    w(f'limiar de significância (q): {report["threshold"]}')
    w(f'gerado em: {report["generated_at"]}')
    w(sep)

    s = report['summary']
    w('')
    w('── Resumo ─────────────────────────────────────────────────────────────')
    w(f'  total de pares:  {s["total_pairs"]}')
    w(f'  avaliáveis:      {s["evaluable"]}')
    w(f'  degenerados:     {s["degenerate"]}')
    if s['b'] is not None:
        w(f'  B (permutações): {s["b"]}')
    else:
        w(f'  B (permutações): valores distintos {s["b_values"]!r} — verifique consistência do run')

    t = report['taxonomy']
    w('')
    w('── Taxonomia das vias (atribuível / não atribuível, NÃO "ativa/inativa") ─')
    w('  O escore mede se a alteração genômica do paciente EXPLICA a mudança')
    w('  proteômica melhor que o acaso — não se a via está biologicamente ligada.')
    w(f'  atribuível              (>=1 q_across_samples < limiar): {t["atribuivel"]}')
    w(f'  não atribuível          (testada, nunca cruzou o limiar): {t["nao_atribuivel"]}')
    w(f'  bloqueada-sem-semente   (100% degenerada):                {t["bloqueada_sem_semente"]}')
    w(f'  bloqueada-sem-readout   (nunca pontuada, sem readout):    {t["bloqueada_sem_readout"]}')
    w(f'  total:                                                    {t["total"]}')

    p = report['population']
    w('')
    w('── Populacional (q_value_across_samples — via através da coorte) ────────')
    w(f'  pares significativos: {p["n_significant_pairs"]}')
    w(f'  vias atribuíveis:     {p["n_pathways"]}')
    w(f'  pacientes envolvidos: {p["n_samples"]}')
    w('')
    w(f'  {"via":10s} {"nome":38s} {"pac.atrib":>9s} {"avaliáveis":>10s} {"z mediano":>10s} {"q mín":>10s} {"frac.sem sinal":>14s}')
    for row in p['by_pathway']:
        name = (row['name'] or '')[:38]
        z_med = f'{row["z_median"]:.3f}' if row['z_median'] is not None else 'N/A'
        q_min = f'{row["q_min"]:.5f}' if row['q_min'] is not None else 'N/A'
        frac = f'{row["unsigned_edge_fraction"]:.1%}' if row['unsigned_edge_fraction'] is not None else 'N/A'
        w(f'  {row["kegg_id"]:10s} {name:38s} {row["n_attributable_samples"]:>9d} '
          f'{row["n_evaluable"]:>10d} {z_med:>10s} {q_min:>10s} {frac:>14s}')

    ind = report['individual']
    w('')
    w('── Individual (q_value_across_pathways — via através das vias do paciente) ─')
    w(f'  pares significativos: {ind["n_significant_pairs"]}')
    w(f'  pacientes:            {ind["n_samples"]}')
    w('')
    w(f'  {"paciente":16s} {"case_id":14s} {"vias abaixo do limiar":>22s} {"q mín":>10s}')
    for row in ind['by_sample']:
        q_min = f'{row["q_min"]:.5f}' if row['q_min'] is not None else 'N/A'
        w(f'  {row["accession"]:16s} {row["case_id"] or "":14s} '
          f'{row["n_pathways_below_threshold"]:>22d} {q_min:>10s}')

    sens = report['sensitivity']
    w('')
    w('── Sensibilidade (escopo populacional) ───────────────────────────────────')
    for k, v in sens.items():
        w(f'  {k:10s}: {v}')

    g = report['grouping']
    w('')
    w('── Agrupamento (escopo individual) ────────────────────────────────────────')
    w(f'  pacientes com mais de uma via atribuível: {len(g["patients_multi_attributable"])}')
    for row in g['patients_multi_attributable']:
        w(f'    {row["accession"]} ({row["case_id"]}): {", ".join(row["pathways"])}')
    w(f'  pares de vias co-ocorrentes: {len(g["co_occurring_pairs"])}')
    for row in g['co_occurring_pairs']:
        w(f'    {row["pathway_a"]} + {row["pathway_b"]}: {row["n_patients"]} paciente(s)')

    w('')
    w(sep)
    return '\n'.join(lines)


def write_json(report: dict, method_version: str) -> str:
    """
    Grava o relatório em JSON sob `settings.REPO_ROOT / 'diagnostics' /
    'exports'` (gitignored — nunca em davinci-frontend/public nem em
    caminho commitável). Retorna o caminho absoluto gravado.
    """
    export_dir = os.path.join(str(settings.REPO_ROOT), 'diagnostics', 'exports')
    os.makedirs(export_dir, exist_ok=True)

    timestamp = datetime.now(dt_timezone.utc).strftime('%Y%m%d_%H%M%S')
    safe_mv = ''.join(c if c.isalnum() or c in '-_' else '_' for c in method_version)
    filename = f'pathway_activity_report_{safe_mv}_{timestamp}.json'
    path = os.path.join(export_dir, filename)

    with open(path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=False)

    return path
