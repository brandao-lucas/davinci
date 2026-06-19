"""
Tasks Celery para derivação e persistência de contexto de medicamentos.

derive_drug_contexts(project_id, drug_name_lower):
    Popula EntityContext com sentenças do abstract que contêm o medicamento.
    Idempotente: pode ser chamada múltiplas vezes sem duplicar snippets.
    Disparada on-demand pelo endpoint de detalhe de medicamento quando o cache
    está frio ou stale (computed_at nulo ou < paper.updated_at).

Chave canônica:
    entity_name = drug_name_lower (consistente com o lookup em get_drug_detail).
    O match no abstract usa drug_name representativo (nome original do NER).

Invalidação de cache:
    Quando um paper é re-ingerido (abstract muda, updated_at avança),
    os snippets existentes ficam stale. O endpoint de detalhe detecta
    isso via comparação lazy (computed_at < paper.updated_at) e
    re-dispara esta task. Não há gancho automático pós-ingestão hoje;
    a comparação lazy é a estratégia adotada no MVP (vide plano
    2026-06-19-pagina-medicamentos-projeto.md, Passo 4).
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def derive_drug_contexts(self, project_id: str, drug_name_lower: str):
    """
    Deriva e persiste snippets de EntityContext para drug_name_lower no projeto.

    Parâmetros:
        project_id      — UUID (str) do DaVinciProject.
        drug_name_lower — chave canônica do medicamento (ex.: 'metformin').

    Idempotência: limpa e recria os contextos para cada paper com abstract.
    Race condition: ignore_conflicts=True no bulk_create protege contra
    execuções paralelas (unique_together no EntityContext).
    """
    from apps.core.models import DaVinciProject
    from apps.core.services.drug_service import DrugService

    try:
        project = DaVinciProject.objects.get(id=project_id)
    except DaVinciProject.DoesNotExist:
        logger.warning(
            'derive_drug_contexts: projeto %s não encontrado — task abortada',
            project_id,
        )
        return

    try:
        n = DrugService.derive_and_persist_contexts(project, drug_name_lower)
        logger.info(
            'derive_drug_contexts: concluído — projeto=%s drug=%s snippets=%d',
            project_id,
            drug_name_lower,
            n,
        )
    except Exception as exc:
        logger.exception(
            'derive_drug_contexts: erro — projeto=%s drug=%s',
            project_id,
            drug_name_lower,
        )
        raise self.retry(exc=exc)
