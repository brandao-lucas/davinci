"""
Tasks Celery para derivação e persistência de contexto de genes.

derive_gene_contexts(project_id, gene_symbol):
    Popula EntityContext com sentenças do abstract que contêm o gene.
    Idempotente: pode ser chamada múltiplas vezes sem duplicar snippets.
    Disparada on-demand pelo endpoint de detalhe de gene quando o cache
    está frio ou stale (computed_at nulo ou < paper.updated_at).

Invalidação de cache:
    Quando um paper é re-ingerido (abstract muda, updated_at avança),
    os snippets existentes ficam stale. O endpoint de detalhe detecta
    isso via comparação lazy (computed_at < paper.updated_at) e
    re-dispara esta task. Não há gancho automático pós-ingestão hoje;
    a comparação lazy é a estratégia adotada no MVP (vide plano
    2026-06-19-pagina-genes-projeto.md, Passo 4).
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def derive_gene_contexts(self, project_id: str, gene_symbol: str):
    """
    Deriva e persiste snippets de EntityContext para gene_symbol no projeto.

    Parâmetros:
        project_id  — UUID (str) do DaVinciProject.
        gene_symbol — símbolo do gene (ex.: 'TNF').

    Idempotência: limpa e recria os contextos para cada paper com abstract.
    Race condition: ignore_conflicts=True no bulk_create protege contra
    execuções paralelas (unique_together no EntityContext).
    """
    from apps.core.models import DaVinciProject
    from apps.core.services.gene_service import GeneService

    try:
        project = DaVinciProject.objects.get(id=project_id)
    except DaVinciProject.DoesNotExist:
        logger.warning(
            'derive_gene_contexts: projeto %s não encontrado — task abortada',
            project_id,
        )
        return

    try:
        n = GeneService.derive_and_persist_contexts(project, gene_symbol)
        logger.info(
            'derive_gene_contexts: concluído — projeto=%s gene=%s snippets=%d',
            project_id,
            gene_symbol,
            n,
        )
    except Exception as exc:
        logger.exception(
            'derive_gene_contexts: erro — projeto=%s gene=%s',
            project_id,
            gene_symbol,
        )
        raise self.retry(exc=exc)
