from apps.core.models import DaVinciProject, IngestionJob
from apps.core.tasks.ingestion_tasks import run_omics_ingestion, run_pubmed_ingestion

class SearchService:
    """
    Service para despachar buscas. NUNCA processa dados diretamente.
    Cria um IngestionJob e despacha para o Celery.
    """

    @staticmethod
    def dispatch_pubmed_search(project: DaVinciProject, user=None) -> IngestionJob:
        ncbi_key = ''
        if user is not None:
            try:
                ncbi_key = user.profile.ncbi_api_key or ''
            except Exception:
                pass

        # Build NCBI-compatible query by combining the main query term with synonyms using OR
        combined_query = project.query_term
        if project.query_synonyms:
            combined_query = f"{project.query_term} OR {' OR '.join(project.query_synonyms)}"

        job = IngestionJob.objects.create(
            project=project,
            job_type=IngestionJob.JobType.PUBMED_SEARCH,
            parameters={
                'query': combined_query,
                'date_from': project.date_from,
                'date_to': project.date_to,
                'synonyms': project.query_synonyms,
                'ncbi_api_key': ncbi_key or None,
            }
        )
        # Despacha para Celery (não bloqueia)
        run_pubmed_ingestion.delay(str(job.id))
        return job

    @staticmethod
    def dispatch_omics_search(
        project: DaVinciProject,
        sources: list | None = None,
        max_per_source: int = 10_000,
        user=None,
    ) -> IngestionJob:
        """
        Creates a GEO_SEARCH IngestionJob and dispatches it to Celery.
        The Rust engine fetches GEO, SRA, BioProject, and/or GWAS Catalog.
        max_per_source defaults to 10,000 (no practical limit).
        """
        if sources is None:
            sources = ['geo', 'sra', 'bioproject', 'gwas']
        ncbi_key = ''
        if user is not None:
            try:
                ncbi_key = user.profile.ncbi_api_key or ''
            except Exception:
                pass

        # Build NCBI-compatible query by combining the main query term with synonyms using OR
        combined_query = project.query_term
        if project.query_synonyms:
            combined_query = f"{project.query_term} OR {' OR '.join(project.query_synonyms)}"

        job = IngestionJob.objects.create(
            project=project,
            job_type=IngestionJob.JobType.GEO_SEARCH,
            parameters={
                'query': combined_query,
                'sources': sources,
                'max_per_source': max_per_source,
                'synonyms': project.query_synonyms,
                'ncbi_api_key': ncbi_key or None,
            },
        )
        run_omics_ingestion.delay(str(job.id))
        return job

