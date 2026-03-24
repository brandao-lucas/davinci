from apps.core.models import DaVinciProject, IngestionJob
from apps.core.tasks.ingestion_tasks import run_omics_ingestion, run_pubmed_ingestion

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

    @staticmethod
    def dispatch_omics_search(
        project: DaVinciProject,
        sources: list | None = None,
        max_per_source: int = 500,
    ) -> IngestionJob:
        """
        Creates a GEO_SEARCH IngestionJob and dispatches it to Celery.
        The Rust engine fetches GEO, SRA, BioProject, and/or GWAS Catalog.
        """
        if sources is None:
            sources = ['geo', 'sra', 'bioproject', 'gwas']
        job = IngestionJob.objects.create(
            project=project,
            job_type=IngestionJob.JobType.GEO_SEARCH,
            parameters={
                'query': project.query_term,
                'sources': sources,
                'max_per_source': max_per_source,
                'synonyms': project.query_synonyms,
            },
        )
        run_omics_ingestion.delay(str(job.id))
        return job
