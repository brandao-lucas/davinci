"""
Tasks Celery para derivação e persistência de contexto de variantes genéticas.

derive_variant_contexts(project_id, rs_number):
    Popula EntityContext com sentenças do abstract que contêm a variante.
    Idempotente: pode ser chamada múltiplas vezes sem duplicar snippets.
    Disparada on-demand pelo endpoint de detalhe de variante quando o cache
    está frio ou stale (computed_at nulo ou < paper.updated_at).

Invalidação de cache:
    Quando um paper é re-ingerido (abstract muda, updated_at avança),
    os snippets existentes ficam stale. O endpoint de detalhe detecta
    isso via comparação lazy (computed_at < paper.updated_at) e
    re-dispara esta task. Não há gancho automático pós-ingestão hoje;
    a comparação lazy é a estratégia adotada no MVP (D3 do plano
    2026-06-19-pagina-variantes-projeto.md).
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def derive_variant_contexts(self, project_id: str, rs_number: str):
    """
    Deriva e persiste snippets de EntityContext para rs_number no projeto.

    Parâmetros:
        project_id — UUID (str) do DaVinciProject.
        rs_number  — RS Number da variante (ex.: 'rs1801133').

    Idempotência: limpa e recria os contextos para cada paper com abstract.
    Race condition: ignore_conflicts=True no bulk_create protege contra
    execuções paralelas (unique_together no EntityContext).
    """
    from apps.core.models import DaVinciProject
    from apps.core.services.variant_service import VariantService

    try:
        project = DaVinciProject.objects.get(id=project_id)
    except DaVinciProject.DoesNotExist:
        logger.warning(
            'derive_variant_contexts: projeto %s não encontrado — task abortada',
            project_id,
        )
        return

    try:
        n = VariantService.derive_and_persist_contexts(project, rs_number)
        logger.info(
            'derive_variant_contexts: concluído — projeto=%s rs_number=%s snippets=%d',
            project_id,
            rs_number,
            n,
        )
    except Exception as exc:
        logger.exception(
            'derive_variant_contexts: erro — projeto=%s rs_number=%s',
            project_id,
            rs_number,
        )
        raise self.retry(exc=exc)
