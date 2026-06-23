"""
Tasks Celery para populacao de DatasetGene (OmnisPathway Fase 3, P2).

populate_dataset_genes_task(dataset_id, sources):
    Popula DatasetGene para um OmicDataset específico via paper_link e/ou
    dataset_metadata. Task fina: delega ao DatasetGeneService.

Idempotencia:
    UPSERT via bulk_create com update_conflicts=True.
    Re-execucao atualiza mention_count sem duplicar.
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def populate_dataset_genes_task(
    self,
    dataset_id: int,
    sources: list[str] | None = None,
):
    """
    Popula DatasetGene para um OmicDataset.

    Parametros:
        dataset_id — PK de OmicDataset.
        sources    — lista de fontes a processar: ['paper_link', 'dataset_metadata'].
                     None = ambas.

    Retorna dict com contagens por fonte.
    """
    from apps.core.models import OmicDataset
    from apps.core.services.dataset_gene_service import (
        ALL_SOURCES,
        DatasetGeneService,
    )

    resolved_sources = tuple(sources) if sources else ALL_SOURCES

    try:
        dataset = OmicDataset.objects.get(pk=dataset_id)
    except OmicDataset.DoesNotExist:
        logger.warning(
            'populate_dataset_genes_task: dataset_id=%s nao encontrado — task abortada',
            dataset_id,
        )
        return {'error': 'dataset_not_found', 'dataset_id': dataset_id}

    try:
        result = DatasetGeneService.populate_dataset(dataset, sources=resolved_sources)
        logger.info(
            'populate_dataset_genes_task: concluido — dataset=%s result=%s',
            dataset.accession,
            result,
        )
        return result
    except Exception as exc:
        logger.exception(
            'populate_dataset_genes_task: erro — dataset=%s sources=%s',
            dataset_id,
            resolved_sources,
        )
        raise self.retry(exc=exc)
