from celery import shared_task
from apps.core.models import DaVinciProject


@shared_task
def refresh_project_stats(project_id: str):
    """Recompute and save stats for a single project."""
    from apps.core.services.stats_service import StatsService
    try:
        project = DaVinciProject.objects.get(id=project_id)
        StatsService.compute_and_save(project)
    except DaVinciProject.DoesNotExist:
        pass


@shared_task
def refresh_all_project_stats():
    """Periodic task: recompute stats for every project."""
    from apps.core.services.stats_service import StatsService
    for project in DaVinciProject.objects.all():
        StatsService.compute_and_save(project)
