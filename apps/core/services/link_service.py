"""
link_service.py — Materialização set-based de ProjectPaperDataset.

Responsabilidade única: projetar os links globais (DatasetPaperLink) para
dentro do escopo do projeto, criando registros ProjectPaperDataset com
confidence='auto' quando AMBAS as pontas já existem no projeto (Nível 1).

Nível 2 (orphans / sugestões) é read-only e não vive aqui — ver Fase B do plano.

Idempotência garantida via INSERT … ON CONFLICT DO NOTHING, alvo da
unique_together ['project', 'project_paper', 'project_dataset'] em ProjectPaperDataset.

A operação é 100% set-based dentro do banco — nenhuma linha é trazida para Python.
"""

import logging
import uuid

from django.db import connection

logger = logging.getLogger(__name__)

# Tabela alvo e nomes canônicos (Django: app_label + model em snake_case)
_TABLE_BRIDGE = 'core_projectpaperdataset'
_TABLE_DPL = 'core_datasetpaperlink'
_TABLE_PP = 'core_projectpaper'
_TABLE_PD = 'core_projectdataset'

# INSERT … SELECT set-based.
# Join direto por paper_id / dataset_id (PKs já materializadas — não passa por pmid/accession).
# Filtro duplo project_id garante escopo de projeto e bloqueia cross-project.
_SQL_MATERIALIZE = f"""
INSERT INTO {_TABLE_BRIDGE} (project_id, project_paper_id, project_dataset_id, confidence, created_at)
SELECT
    pp.project_id,
    pp.id   AS project_paper_id,
    pd.id   AS project_dataset_id,
    'auto'  AS confidence,
    NOW()   AS created_at
FROM {_TABLE_DPL}  dpl
JOIN {_TABLE_PP}   pp  ON pp.paper_id   = dpl.paper_id
JOIN {_TABLE_PD}   pd  ON pd.dataset_id = dpl.dataset_id
WHERE pp.project_id = %(project_id)s
  AND pd.project_id = %(project_id)s
ON CONFLICT (project_id, project_paper_id, project_dataset_id) DO NOTHING
"""

_SQL_MATERIALIZE_ALL = f"""
INSERT INTO {_TABLE_BRIDGE} (project_id, project_paper_id, project_dataset_id, confidence, created_at)
SELECT
    pp.project_id,
    pp.id   AS project_paper_id,
    pd.id   AS project_dataset_id,
    'auto'  AS confidence,
    NOW()   AS created_at
FROM {_TABLE_DPL}  dpl
JOIN {_TABLE_PP}   pp  ON pp.paper_id   = dpl.paper_id
JOIN {_TABLE_PD}   pd  ON pd.dataset_id = dpl.dataset_id
WHERE pp.project_id = pd.project_id
ON CONFLICT (project_id, project_paper_id, project_dataset_id) DO NOTHING
"""


def materialize_project_links(project_id: uuid.UUID | str) -> int:
    """
    Materializa todos os vínculos confirmados (Nível 1) para um projeto.

    Executa o INSERT … SELECT … ON CONFLICT DO NOTHING no banco.
    Retorna o número de linhas inseridas (0 se já existiam todas — idempotente).

    Não levanta exceção por si só — o chamador decide como tratar falhas.
    """
    project_id_str = str(project_id)
    with connection.cursor() as cursor:
        cursor.execute(_SQL_MATERIALIZE, {'project_id': project_id_str})
        inserted = cursor.rowcount  # rowcount = linhas realmente inseridas (ON CONFLICT ignora)

    logger.info(
        'materialize_project_links: projeto=%s → %d vínculos inseridos (ON CONFLICT ignora duplicatas)',
        project_id_str,
        inserted,
    )
    return inserted


def materialize_all_projects_links() -> int:
    """
    Backfill: materializa vínculos para TODOS os projetos existentes numa única query.

    Seguro para re-execução (ON CONFLICT DO NOTHING).
    Retorna o total de linhas inseridas.
    """
    with connection.cursor() as cursor:
        cursor.execute(_SQL_MATERIALIZE_ALL)
        inserted = cursor.rowcount

    logger.info(
        'materialize_all_projects_links: %d vínculos inseridos no total (backfill)',
        inserted,
    )
    return inserted


# =============================================================================
# Nível 2 — Sugestões de órfãos (READ-ONLY, nunca grava em ProjectPaperDataset)
# =============================================================================

# Caso A: paper já está no projeto como ProjectPaper, mas o dataset que o elink
# relaciona NÃO é ProjectDataset desse projeto → sugerir "adicionar este dataset".
#
# Retorna uma linha por (dpl.id, project_paper.id) com campos suficientes para
# renderizar no front e para a ação futura "adicionar ao projeto".
#
# O subquery NOT EXISTS descarta:
#   (a) datasets que já são ProjectDataset do mesmo projeto;
#   (b) datasets de outros projetos (o escopo é sempre project_pk passado).
_SQL_ORPHAN_CASE_A = f"""
SELECT
    'dataset_missing'          AS suggestion_type,
    dpl.id                     AS global_link_id,
    dpl.link_source,

    -- Ponta que JÁ está no projeto: o paper (ProjectPaper)
    pp.id                      AS project_paper_id,
    p.pmid                     AS paper_pmid,
    p.title                    AS paper_title,

    -- Ponta FALTANTE sugerida: o dataset global (não projetado)
    NULL::integer              AS project_dataset_id,
    d.id                       AS dataset_id,
    d.accession                AS dataset_accession,
    d.title                    AS dataset_title,
    d.omic_type

FROM {_TABLE_DPL}  dpl
JOIN {_TABLE_PP}   pp  ON pp.paper_id   = dpl.paper_id
JOIN core_paper    p   ON p.id          = pp.paper_id
JOIN core_omicdataset d ON d.id         = dpl.dataset_id
WHERE pp.project_id = %(project_id)s
  AND NOT EXISTS (
      SELECT 1 FROM {_TABLE_PD} pd2
      WHERE pd2.project_id  = %(project_id)s
        AND pd2.dataset_id  = dpl.dataset_id
  )
"""

# Caso B: dataset já está no projeto como ProjectDataset, mas o paper que o elink
# relaciona NÃO é ProjectPaper desse projeto → sugerir "adicionar este paper".
#
# Nota: não expõe p.id como coluna separada — o paper_pmid é suficiente para
# identificar e adicionar o paper ao projeto via endpoint existente.
# Mantém a mesma lista de 11 colunas do Caso A para o zip com `cursor.description`.
_SQL_ORPHAN_CASE_B = f"""
SELECT
    'paper_missing'            AS suggestion_type,
    dpl.id                     AS global_link_id,
    dpl.link_source,

    -- Ponta FALTANTE sugerida: o paper global (não projetado)
    NULL::integer              AS project_paper_id,
    p.pmid                     AS paper_pmid,
    p.title                    AS paper_title,

    -- Ponta que JÁ está no projeto: o dataset (ProjectDataset)
    pd.id                      AS project_dataset_id,
    d.id                       AS dataset_id,
    d.accession                AS dataset_accession,
    d.title                    AS dataset_title,
    d.omic_type

FROM {_TABLE_DPL}  dpl
JOIN {_TABLE_PD}   pd  ON pd.dataset_id = dpl.dataset_id
JOIN core_omicdataset d ON d.id         = dpl.dataset_id
JOIN core_paper    p   ON p.id          = dpl.paper_id
WHERE pd.project_id = %(project_id)s
  AND NOT EXISTS (
      SELECT 1 FROM {_TABLE_PP} pp2
      WHERE pp2.project_id = %(project_id)s
        AND pp2.paper_id   = dpl.paper_id
  )
"""

# Exclui também links que JÁ foram materializados como ProjectPaperDataset
# (Nível 1 confirmado) — para não re-sugerir o que o Nível 1 já resolveu.
# Isso é tratado pelos NOT EXISTS acima: se ambas as pontas existissem no projeto,
# a materialização teria criado o bridge e os NOT EXISTS não passariam — por design
# as queries já excluem a situação de "ambas no projeto".


def suggest_orphan_links(project_id: uuid.UUID | str) -> list[dict]:
    """
    Retorna sugestões de vínculos órfãos (Nível 2) para um projeto.

    Dois casos:
      - 'dataset_missing': paper já no projeto, dataset NÃO está → sugerir adicionar dataset.
      - 'paper_missing':   dataset já no projeto, paper NÃO está → sugerir adicionar paper.

    READ-ONLY: não escreve nada em ProjectPaperDataset.

    Retorna lista de dicts com shape:
      {
        'suggestion_type':   'dataset_missing' | 'paper_missing',
        'global_link_id':    <int>,
        'link_source':       <str>,
        'project_paper_id':  <int | None>,
        'paper_pmid':        <int>,
        'paper_title':       <str>,
        'project_dataset_id': <int | None>,
        'dataset_id':        <int>,
        'dataset_accession': <str>,
        'dataset_title':     <str>,
        'omic_type':         <str>,
      }

    O caller (viewset) é responsável por paginar e serializar.
    """
    project_id_str = str(project_id)

    results = []
    with connection.cursor() as cursor:
        for sql in (_SQL_ORPHAN_CASE_A, _SQL_ORPHAN_CASE_B):
            cursor.execute(sql, {'project_id': project_id_str})
            # Usa cursor.description para derivar os nomes de coluna dinamicamente —
            # evita desalinhamento se o SELECT for alterado no futuro.
            col_names = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            for row in rows:
                results.append(dict(zip(col_names, row)))

    logger.debug(
        'suggest_orphan_links: projeto=%s → %d sugestões de órfãos',
        project_id_str,
        len(results),
    )
    return results
