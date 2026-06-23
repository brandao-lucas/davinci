"""
Tasks Celery para recompute de disease_axis + monogenic_gene_hit pós-ingestão.

Propósito
---------
Fecha o gap R4: re-ingestão de um dataset PRIDE (ou GEO/SRA/BioProject) via
COPY writer apaga `extra_metadata['contract']['monogenic_gene_hit']` porque o
jsonb merge (RHS vence) substitui a chave 'contract' inteira. Ao encadear esta
task *after* qualquer ingestão de datasets que conclui com sucesso, o campo é
recomputado imediatamente no mesmo job Celery, restaurando o valor correto.

Também torna o classificador disease_axis + monogenic_gene_hit um **gatilho
automático** — hoje a classificação só roda via `classify_disease_axis` manual
ou backfill. Após este encadeamento, toda ingestão nova dispara o recompute.

Design
------
- Task assíncrona (Celery), nunca inline no hot path de ingestão.
- Tolerante a falha: exception → log + return (não relança, não derruba o job
  de ingestão pai que já concluiu).
- Escopo: UNION de dois critérios (ver docstring da função).
- Idempotente por construção: `classify_disease_axis_for_dataset` e
  `compute_monogenic_gene_hit_for_dataset` usam read-modify-write; re-rodar
  não causa efeito espúrio. Over-classificar alguns datasets do projeto dentro
  da janela temporal é inofensivo.
- Sem max_retries: falha de classificação é silenciosa (logged). O job de
  ingestão já completou com sucesso — não faz sentido retentar a task pai.

Escopo de datasets (por que UNION)
------------------------------------
O cenário R4 é a re-ingestão: o dataset já existe, o Rust faz UPSERT via
ON CONFLICT DO UPDATE — incluindo `updated_at = NOW()` (copy_writer.rs:195)
— mas o vínculo `ProjectDataset.ingestion_job` aponta para o job ANTERIOR
(não há motivo para o Rust atualizar essa FK em UPSERT de OmicDataset).

Portanto `ProjectDataset.filter(ingestion_job=job)` captura apenas datasets
NOVOS (primeiro vínculo); não captura os RE-INGERIDOS (exatamente o caso R4).

Solução: UNION de dois critérios, bounded ao projeto:

  Critério A — datasets novos (vínculo criado pelo job):
    ProjectDataset.filter(ingestion_job=job)

  Critério B — datasets re-ingeridos (UPSERT bumpou updated_at):
    ProjectDataset.filter(
        project=job.project,
        dataset__updated_at__gte=job.created_at,
    )

  Critério B usa `job.created_at` (auto_now_add, sempre preenchido) como
  âncora temporal. O Rust seta `updated_at = NOW()` no ON CONFLICT SET para
  OmicDataset (confirmado em copy_writer.rs:195), então todo dataset tocado
  pelo COPY writer neste job terá `updated_at >= job.created_at`.

  Trade-off do critério B: pode over-classificar datasets do mesmo projeto
  que foram tocados por outro job concorrente dentro da mesma janela temporal.
  Esse over-classify é inofensivo (recompute é idempotente) e improvável na
  prática (jobs do mesmo projeto tendem a ser sequenciais). O que não podemos
  é deixar o cenário R4 descoberto.
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(
    bind=False,
    # Sem retry automático: falha de classificação é não-fatal e não requer
    # retentativa automática — o campo pode ser recomputado via backfill manual
    # (classify_disease_axis --dataset <accession>) ou na próxima ingestão.
    max_retries=0,
    ignore_result=True,
)
def recompute_disease_axis_for_job(job_id: str) -> dict:
    """
    Recomputa disease_axis e monogenic_gene_hit para os datasets tocados por
    um IngestionJob concluído.

    Escopo dos datasets (UNION de dois critérios, bounded ao projeto)
    ------------------------------------------------------------------
    Critério A — `ProjectDataset.ingestion_job == job`
        Datasets cujo vínculo projeto↔dataset foi criado por este job
        (primeira ingestão). Vínculo preciso.

    Critério B — `ProjectDataset.project == job.project
                  AND dataset.updated_at >= job.created_at`
        Datasets do projeto cujo `updated_at` foi bumpado a partir do
        `created_at` do job. Captura re-ingestões (UPSERT com ON CONFLICT
        DO UPDATE SET updated_at = NOW() — copy_writer.rs:195).

    A UNION garante cobertura de AMBOS os cenários sem duplicatas
    (set de dataset_ids). Bounded ao projeto: não reclassifica o banco inteiro.

    Parâmetros
    ----------
    job_id : str
        UUID (str) do IngestionJob que acabou de concluir.

    Retorna
    -------
    dict com 'datasets_classified', 'datasets_skipped', 'errors'.
    """
    from apps.core.models import IngestionJob, ProjectDataset

    try:
        job = IngestionJob.objects.select_related('project').get(id=job_id)
    except IngestionJob.DoesNotExist:
        logger.warning(
            'recompute_disease_axis_for_job: IngestionJob %s não encontrado — abortando',
            job_id,
        )
        return {'datasets_classified': 0, 'datasets_skipped': 0, 'errors': ['job not found']}

    # --- Critério A: datasets cujo ProjectDataset.ingestion_job aponta para este job ---
    ids_a = set(
        ProjectDataset.objects
        .filter(ingestion_job=job)
        .values_list('dataset_id', flat=True)
    )

    # --- Critério B: datasets do projeto com updated_at >= job.created_at ---
    # Cobre re-ingestões (UPSERT bumpou updated_at mas ingestion_job aponta pro job antigo).
    # job.created_at é auto_now_add — sempre preenchido, nunca NULL.
    ids_b = set(
        ProjectDataset.objects
        .filter(
            project=job.project,
            dataset__updated_at__gte=job.created_at,
        )
        .values_list('dataset_id', flat=True)
    )

    dataset_ids = ids_a | ids_b

    logger.info(
        'recompute_disease_axis_for_job: job %s — critério_A=%d critério_B=%d total_union=%d',
        job_id,
        len(ids_a),
        len(ids_b),
        len(dataset_ids),
    )

    if not dataset_ids:
        logger.info(
            'recompute_disease_axis_for_job: job %s sem datasets no escopo — nada a classificar',
            job_id,
        )
        return {'datasets_classified': 0, 'datasets_skipped': 0, 'errors': []}

    from apps.core.models import OmicDataset
    from apps.core.services.disease_axis_classifier_service import (
        classify_disease_axis_for_dataset,
        compute_monogenic_gene_hit_for_dataset,
    )

    classified = 0
    skipped = 0
    errors = []

    datasets = OmicDataset.objects.filter(id__in=dataset_ids).iterator(chunk_size=100)

    for dataset in datasets:
        try:
            # Parte 1 — disease_axis
            classify_disease_axis_for_dataset(dataset)

            # Parte 2 — monogenic_gene_hit (read-modify-write; restaura o campo
            # apagado pelo COPY writer sem substituir outras chaves do contract)
            compute_monogenic_gene_hit_for_dataset(dataset)

            classified += 1

        except Exception as exc:
            # Falha por dataset é isolada: loga e continua para os próximos.
            # Não propaga a exception — o job de ingestão pai já concluiu.
            logger.error(
                'recompute_disease_axis_for_job: erro ao classificar dataset %s (job %s): %s',
                dataset.accession,
                job_id,
                exc,
            )
            errors.append(f'{dataset.accession}: {exc}')
            skipped += 1

    logger.info(
        'recompute_disease_axis_for_job: job %s concluído — %d classificados, %d ignorados, %d erros',
        job_id,
        classified,
        skipped,
        len(errors),
    )

    return {
        'datasets_classified': classified,
        'datasets_skipped': skipped,
        'errors': errors,
    }
