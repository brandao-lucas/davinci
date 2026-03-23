from apps.core.models import DaVinciProject, IngestionJob
from apps.core.tasks.ingestion_tasks import run_pubmed_ingestion

class SearchService:
    """
    Service para despachar buscas. NUNCA processa dados diretamente.
    Cria um IngestionJob e despacha para o Celery.
    """

    @staticmethod
    def dispatch_pubmed_search(project: DaVinciProject) -> IngestionJob:
        job = IngestionJob.objects.create(
            project=project,
            job_type=IngestionJob.JobType.PUBMED_SEARCH,
            parameters={
                'query': project.query_term,
                'date_from': project.date_from,
                'date_to': project.date_to,
                'synonyms': project.query_synonyms,
            }
        )
        # Despacha para Celery (não bloqueia)
        run_pubmed_ingestion.delay(str(job.id))
        return job
