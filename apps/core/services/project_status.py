"""
Helpers de transição de status do pipeline DaVinciProject.

Regras de máquina de estados:
  draft → searching : start_searching()
  searching → draft  : revert_to_draft()   (cancela todos os jobs de busca ativos)
  searching → curating: advance_to_curating_if_done()  (quando não há mais jobs ativos)

Todos os helpers são idempotentes e usam update_fields mínimos para evitar
colisões com outros campos.
"""

import logging

from django.utils import timezone

from apps.core.models import DaVinciProject, IngestionJob

logger = logging.getLogger(__name__)

# Tipos de job que representam uma busca ativa no projeto.
# Quando existem jobs desses tipos em PENDING ou RUNNING, o projeto está "buscando".
SEARCH_JOB_TYPES = (
    IngestionJob.JobType.PUBMED_SEARCH,
    IngestionJob.JobType.GEO_SEARCH,
    IngestionJob.JobType.SRA_SEARCH,
    IngestionJob.JobType.GWAS_SEARCH,
    IngestionJob.JobType.PRIDE_SEARCH,
)


def start_searching(project: DaVinciProject) -> None:
    """
    Transição draft → searching. Idempotente: só promove draft; é no-op em todos
    os demais estados (searching, curating, analyzing, complete), evitando regressão.

    Chamada após criar e despachar um IngestionJob de busca com sucesso.
    """
    if project.status != DaVinciProject.PipelineStatus.DRAFT:
        return

    DaVinciProject.objects.filter(pk=project.pk).update(
        status=DaVinciProject.PipelineStatus.SEARCHING,
        updated_at=timezone.now(),
    )
    project.status = DaVinciProject.PipelineStatus.SEARCHING
    logger.info(
        'Projeto %s: status → searching (start_searching)',
        project.pk,
    )


def revert_to_draft(project: DaVinciProject) -> None:
    """
    Transição searching → draft.

    Cancela todos os IngestionJobs do projeto com status PENDING ou RUNNING,
    independente do tipo (inclui jobs de busca e também outros que possam estar
    ativos, como PUBMED_FETCH encadeado). Mantém o corpus existente — nenhum
    ProjectPaper/ProjectDataset é deletado (Regra #2 — rastreabilidade inegociável).

    Chamada quando os campos de busca do projeto são alterados enquanto está em
    searching.
    """
    cancelled = IngestionJob.objects.filter(
        project=project,
        status__in=[IngestionJob.JobStatus.PENDING, IngestionJob.JobStatus.RUNNING],
    ).update(status=IngestionJob.JobStatus.CANCELLED)

    DaVinciProject.objects.filter(pk=project.pk).update(
        status=DaVinciProject.PipelineStatus.DRAFT,
        updated_at=timezone.now(),
    )
    project.status = DaVinciProject.PipelineStatus.DRAFT
    logger.info(
        'Projeto %s: status → draft (revert_to_draft); %d job(s) cancelado(s)',
        project.pk,
        cancelled,
    )


def advance_to_curating_if_done(project: DaVinciProject) -> None:
    """
    Transição searching → curating, se não houver mais jobs de busca ativos.

    Guard: só avança se o projeto estiver em `searching`. Impede transições
    inválidas (ex.: draft → curating) mesmo que chamada por engano fora de
    contexto.

    Chamada ao fim de `run_pubmed_ingestion` e `run_omics_ingestion`, APÓS o
    eventual encadeamento (_dispatch_omics_after_pubmed), para que o GEO job já
    exista antes de verificar se ainda há jobs ativos.
    """
    # Recarrega do banco para garantir leitura do status atual (não do objeto em memória)
    project.refresh_from_db(fields=['status'])

    if project.status != DaVinciProject.PipelineStatus.SEARCHING:
        logger.debug(
            'advance_to_curating_if_done ignorado para projeto %s (status=%s)',
            project.pk,
            project.status,
        )
        return

    has_active = IngestionJob.objects.filter(
        project=project,
        job_type__in=SEARCH_JOB_TYPES,
        status__in=[IngestionJob.JobStatus.PENDING, IngestionJob.JobStatus.RUNNING],
    ).exists()

    if has_active:
        logger.debug(
            'Projeto %s ainda tem jobs de busca ativos — permanece em searching',
            project.pk,
        )
        return

    DaVinciProject.objects.filter(pk=project.pk).update(
        status=DaVinciProject.PipelineStatus.CURATING,
        updated_at=timezone.now(),
    )
    project.status = DaVinciProject.PipelineStatus.CURATING
    logger.info(
        'Projeto %s: status → curating (advance_to_curating_if_done)',
        project.pk,
    )
