"""
Tasks Celery para derivação e persistência de contexto de termos MeSH.

derive_mesh_contexts(project_id, descriptor):
    Popula EntityContext com sentenças do abstract que contêm o descriptor MeSH.
    Idempotente: pode ser chamada múltiplas vezes sem duplicar snippets.
    Disparada on-demand pelo endpoint de detalhe de descriptor quando o cache
    está frio ou stale (computed_at nulo ou < paper.updated_at).

Nota sobre zero snippets:
    MeSH não garante que o descriptor apareça literalmente no abstract.
    Muitos papers são indexados com um termo MeSH sem mencioná-lo explicitamente.
    A task grava o marcador sentinela (sentence_position=-1) nesses casos,
    evitando loop infinito de context_status='computing' no endpoint de detalhe.

Invalidação de cache:
    Quando um paper é re-ingerido (abstract muda, updated_at avança),
    os snippets existentes ficam stale. O endpoint de detalhe detecta
    isso via comparação lazy (computed_at < paper.updated_at) e
    re-dispara esta task. Não há gancho automático pós-ingestão hoje;
    a comparação lazy é a estratégia adotada no MVP.
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def derive_mesh_contexts(self, project_id: str, descriptor: str):
    """
    Deriva e persiste snippets de EntityContext para descriptor MeSH no projeto.

    Parâmetros:
        project_id — UUID (str) do DaVinciProject.
        descriptor — descriptor MeSH (ex.: 'Diabetes Mellitus', 'Neoplasms').

    Idempotência: limpa e recria os contextos para cada paper com abstract.
    Race condition: ignore_conflicts=True no bulk_create protege contra
    execuções paralelas (unique_together no EntityContext).
    """
    from apps.core.models import DaVinciProject
    from apps.core.services.mesh_service import MeshService

    try:
        project = DaVinciProject.objects.get(id=project_id)
    except DaVinciProject.DoesNotExist:
        logger.warning(
            'derive_mesh_contexts: projeto %s não encontrado — task abortada',
            project_id,
        )
        return

    try:
        n = MeshService.derive_and_persist_contexts(project, descriptor)
        logger.info(
            'derive_mesh_contexts: concluído — projeto=%s descriptor=%s snippets=%d',
            project_id,
            descriptor,
            n,
        )
    except Exception as exc:
        logger.exception(
            'derive_mesh_contexts: erro — projeto=%s descriptor=%s',
            project_id,
            descriptor,
        )
        raise self.retry(exc=exc)
