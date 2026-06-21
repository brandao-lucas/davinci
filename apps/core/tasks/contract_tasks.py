"""
Tasks Celery para classificação do contrato OmnisPathway (Fase 2).

classify_contract_axes(dataset_id|project_id):
    Roda os classificadores da Fase 2 em um dataset ou em todos os datasets
    de um projeto.

    Disparo on-demand (não há gancho automático pós-ingestão no MVP — Fase 2).
    O management command classify_contract é o disparo de backfill em massa.

Idempotência:
    Cada classificador re-escreve o campo + score em contract_confidence.
    Re-execução é segura (último resultado vence).
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def classify_contract_axes(self, *, dataset_id: int | None = None, project_id: str | None = None, axes: list[str] | None = None):
    """
    Classifica eixos do contrato OmnisPathway em um ou mais datasets.

    Parâmetros (exatamente um de dataset_id ou project_id deve ser fornecido):
      dataset_id  — PK de OmicDataset. Classifica apenas este dataset.
      project_id  — UUID (str) de DaVinciProject. Classifica todos os datasets
                    do projeto (via ProjectDataset).
      axes        — lista de eixos a classificar. None = todos.
                    Valores aceitos: 'has_control_group', 'is_single_cell',
                    'sample_join_key', 'data_format', 'access_type'.

    Retorna:
      dict com 'processed' (int), 'results' (dict por accession → dict por eixo).
    """
    from apps.core.models import OmicDataset, DaVinciProject, ProjectDataset
    from apps.core.services.contract_classifier_service import classify_all_axes

    if dataset_id is None and project_id is None:
        logger.error('classify_contract_axes: nenhum dataset_id nem project_id fornecido')
        return {'error': 'dataset_id or project_id required'}

    try:
        if dataset_id is not None:
            datasets = list(OmicDataset.objects.filter(pk=dataset_id))
            if not datasets:
                logger.warning(
                    'classify_contract_axes: dataset_id=%s não encontrado', dataset_id
                )
                return {'processed': 0}
        else:
            try:
                project = DaVinciProject.objects.get(id=project_id)
            except DaVinciProject.DoesNotExist:
                logger.warning(
                    'classify_contract_axes: projeto %s não encontrado', project_id
                )
                return {'processed': 0}

            datasets = list(
                OmicDataset.objects.filter(
                    in_projects__project=project
                ).distinct().iterator()
            )

        results = {}
        for dataset in datasets:
            try:
                result = classify_all_axes(dataset, axes=axes)
                results[dataset.accession] = result
            except Exception as exc:
                logger.exception(
                    'classify_contract_axes: erro ao classificar dataset=%s',
                    dataset.accession,
                )
                results[dataset.accession] = {'error': str(exc)}

        logger.info(
            'classify_contract_axes: concluído — %d datasets processados (project=%s, dataset_id=%s)',
            len(datasets),
            project_id,
            dataset_id,
        )
        return {'processed': len(datasets), 'results': results}

    except Exception as exc:
        logger.exception('classify_contract_axes: erro inesperado')
        raise self.retry(exc=exc)
