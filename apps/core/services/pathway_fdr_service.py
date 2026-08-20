"""
PathwayFdrService — aplica Benjamini-Hochberg (FDR) em DOIS ESCOPOS sobre
`PathwayActivityScore.method_version` — OmnisPathway Obj 2, Fase 4
(pós-processamento das migrations 0038/0039).

Por que isto vive em Django e não no motor Rust (ver docstring da 0039 em
`apps/core/models.py::PathwayActivityScore`): o BH é uma estatística DE
CONJUNTO — o q de uma linha depende do rank do seu p entre TODOS os p da
família. O motor Rust grava p_empirical por run e pode não ver a família
inteira; esta passada de pós-processamento roda quando a família está
completa, sobre o `method_version` já persistido.

Regra #1 (Django não processa dado bruto): o BH é SET-BASED, executado
inteiro dentro do Postgres via duas janelas (`ROW_NUMBER` + `COUNT` para o
rank/tamanho da família, `MIN(...) OVER (... ROWS BETWEEN UNBOUNDED
PRECEDING AND CURRENT ROW)` para o cummin decrescente que implementa o
"step-up" do BH) — nenhum laço Python sobre as linhas.

Dois escopos simultâneos (migration 0039 — o escopo está NO NOME da coluna,
não em um valor):

    population  partição = pathway_id  → q_value_across_samples / ...
                "esta via se destaca em mais pacientes que o acaso?"
    individual  partição = sample_id   → q_value_across_pathways / ...
                "neste paciente, quais vias se destacam?"

SEGURANÇA — `{part}`/`{suf}` são IDENTIFICADORES SQL interpolados na
string, não parâmetros de bind (Postgres não faz bind de nome de coluna).
Por isso NUNCA vêm de input do usuário: `SCOPE_MAP` é um mapa FECHADO
(`population`/`individual`) — qualquer valor fora dele levanta
`PathwayFdrScopeError` antes de qualquer SQL ser montado. `method_version`
É parâmetro de bind (`%s`), nunca interpolado.

Degenerados (`null_sd = 0`, ~74% das linhas na bancada de 372 vias) ficam
FORA do denominador do BH (WHERE null_sd <> 0 na primeira CTE) — inflar m
com eles encolheria o limiar por um fator de 3 a 5 e enterraria achados
reais (ver docstring da 0039). Um UPDATE irmão os RÓTULA sem lhes dar q:
`fdr_method_* = 'benjamini_hochberg'` (a linha FOI considerada por esta
passada) com `q_value_* = NULL` e `fdr_n_tests_* = 0` — distingue
"considerado e excluído do denominador" (fdr_method preenchido, q NULL) de
"esta passada nunca rodou nesta linha" (fdr_method == '').
"""

from __future__ import annotations

import logging

from django.db import connection, transaction

logger = logging.getLogger(__name__)

_TABLE = 'core_pathwayactivityscore'

# ─── Mapa FECHADO escopo → (coluna de partição, sufixo das colunas) ─────────
# Único ponto de entrada para `{part}`/`{suf}` no SQL abaixo. Adicionar um
# escopo novo (ex.: 'global') é editar SÓ este dicionário — nunca aceitar a
# coluna/sufixo como string livre vinda de fora.
SCOPE_MAP = {
    'population': {
        'partition_col': 'pathway_id',
        'suffix': 'across_samples',
        'label': 'Populacional (via através das amostras)',
    },
    'individual': {
        'partition_col': 'sample_id',
        'suffix': 'across_pathways',
        'label': 'Individual (amostra através das vias)',
    },
}

_SQL_BH_UPDATE = """
WITH elegiveis AS (
    SELECT id, {part}, p_empirical,
           ROW_NUMBER() OVER (PARTITION BY {part} ORDER BY p_empirical, id) AS rk,
           COUNT(*)     OVER (PARTITION BY {part}) AS m
    FROM {table}
    WHERE method_version = %s AND null_sd <> 0
), bh AS (
    SELECT id, m, MIN(LEAST(p_empirical * m / rk, 1.0))
             OVER (PARTITION BY {part} ORDER BY rk DESC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS q
    FROM elegiveis
)
UPDATE {table} s SET
    q_value_{suf} = bh.q, fdr_method_{suf} = 'benjamini_hochberg', fdr_n_tests_{suf} = bh.m
FROM bh WHERE s.id = bh.id
"""

_SQL_LABEL_DEGENERATE = """
UPDATE {table}
SET q_value_{suf} = NULL,
    fdr_method_{suf} = 'benjamini_hochberg',
    fdr_n_tests_{suf} = 0
WHERE method_version = %s AND null_sd = 0
"""

_SQL_COUNT_EVALUABLE = f"SELECT COUNT(*) FROM {_TABLE} WHERE method_version = %s AND null_sd <> 0"
_SQL_COUNT_DEGENERATE = f"SELECT COUNT(*) FROM {_TABLE} WHERE method_version = %s AND null_sd = 0"


class PathwayFdrScopeError(ValueError):
    """`--scope` fora do mapa fechado SCOPE_MAP (ou de 'both')."""


def resolve_scopes(scope: str) -> list[str]:
    """Resolve `--scope` ('both' | 'population' | 'individual') para a lista de escopos a aplicar."""
    if scope == 'both':
        return ['population', 'individual']
    if scope not in SCOPE_MAP:
        valid = sorted(SCOPE_MAP) + ['both']
        raise PathwayFdrScopeError(
            f'--scope inválido: {scope!r}. Valores aceitos: {valid}.'
        )
    return [scope]


def apply_fdr(method_version: str, scope: str = 'both', dry_run: bool = False) -> dict:
    """
    Aplica BH nos escopos pedidos para `method_version`. Idempotente: rodar
    de novo sobre os mesmos dados produz o mesmo resultado (UPDATE
    set-based, não incrementa nada).

    `dry_run=True` NÃO executa nenhum UPDATE — só conta quantas linhas
    seriam avaliáveis/degeneradas por escopo (mesmo predicado do BH real),
    para o operador conferir antes de gravar.

    Retorna um relatório dict: {method_version, dry_run, scopes: {escopo:
    {label, suffix, partition_col, n_evaluable, n_degenerate,
    rows_updated_bh, rows_labeled_degenerate}}}.
    """
    if not method_version:
        raise ValueError('method_version é obrigatório.')

    scopes = resolve_scopes(scope)
    report: dict = {
        'method_version': method_version,
        'dry_run': dry_run,
        'scopes': {},
    }

    def _counts(cursor) -> tuple[int, int]:
        cursor.execute(_SQL_COUNT_EVALUABLE, [method_version])
        n_evaluable = cursor.fetchone()[0]
        cursor.execute(_SQL_COUNT_DEGENERATE, [method_version])
        n_degenerate = cursor.fetchone()[0]
        return n_evaluable, n_degenerate

    if dry_run:
        with connection.cursor() as cursor:
            for scope_name in scopes:
                cfg = SCOPE_MAP[scope_name]
                n_evaluable, n_degenerate = _counts(cursor)
                report['scopes'][scope_name] = {
                    'label': cfg['label'],
                    'suffix': cfg['suffix'],
                    'partition_col': cfg['partition_col'],
                    'n_evaluable': n_evaluable,
                    'n_degenerate': n_degenerate,
                    'rows_updated_bh': 0,
                    'rows_labeled_degenerate': 0,
                }
        logger.info(
            'apply_pathway_fdr (dry-run): method_version=%s scope=%s report=%s',
            method_version, scope, report,
        )
        return report

    with transaction.atomic():
        with connection.cursor() as cursor:
            for scope_name in scopes:
                cfg = SCOPE_MAP[scope_name]
                part = cfg['partition_col']
                suf = cfg['suffix']
                n_evaluable, n_degenerate = _counts(cursor)

                sql_bh = _SQL_BH_UPDATE.format(table=_TABLE, part=part, suf=suf)
                cursor.execute(sql_bh, [method_version])
                rows_bh = cursor.rowcount

                sql_label = _SQL_LABEL_DEGENERATE.format(table=_TABLE, suf=suf)
                cursor.execute(sql_label, [method_version])
                rows_label = cursor.rowcount

                report['scopes'][scope_name] = {
                    'label': cfg['label'],
                    'suffix': suf,
                    'partition_col': part,
                    'n_evaluable': n_evaluable,
                    'n_degenerate': n_degenerate,
                    'rows_updated_bh': rows_bh,
                    'rows_labeled_degenerate': rows_label,
                }

    logger.info(
        'apply_pathway_fdr: method_version=%s scope=%s report=%s',
        method_version, scope, report,
    )
    return report
