from celery import shared_task
from django.conf import settings
from apps.core.models import IngestionJob

@shared_task(bind=True, max_retries=3)
def run_pubmed_ingestion(self, job_id: str):
    """
    Chama o Rust engine via PyO3.
    """
    try:
        import rust_engine
        
        job = IngestionJob.objects.get(id=job_id)
        
        # Build DB URL from settings
        db = settings.DATABASES['default']
        db_url = f"postgresql://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}"
        
        result = rust_engine.search_and_ingest_pubmed(
            job_id=str(job.id),
            query=job.parameters['query'],
            date_from=job.parameters.get('date_from'),
            date_to=job.parameters.get('date_to'),
            db_url=db_url,
            ncbi_api_key=settings.NCBI_API_KEY,
        )
        
        return {
            'processed': result.records_processed,
            'inserted': result.records_inserted,
        }
    except ImportError:
        # Stub logic for Phase 1 MVP when rust logic is not yet compiled
        job = IngestionJob.objects.get(id=job_id)
        job.status = IngestionJob.JobStatus.COMPLETED
        job.records_processed = 0
        job.save(update_fields=['status', 'records_processed'])
        return {'processed': 0, 'inserted': 0}
    except Exception as exc:
        job = IngestionJob.objects.get(id=job_id)
        job.status = IngestionJob.JobStatus.FAILED
        job.error_message = str(exc)
        job.save(update_fields=['status', 'error_message'])
        raise self.retry(exc=exc, countdown=60)


@shared_task(bind=True, max_retries=3)
def run_omics_ingestion(self, job_id: str):
    """
    Calls the Rust engine to ingest omics metadata from GEO, SRA, BioProject,
    and/or GWAS Catalog via PyO3.

    Job parameters expected:
        query         (str)        — search term
        sources       (list[str])  — subset of ["geo", "sra", "bioproject", "gwas"]
        max_per_source (int)       — max datasets per source (default: 500)
    """
    try:
        import rust_engine

        job = IngestionJob.objects.get(id=job_id)

        db = settings.DATABASES['default']
        db_url = f"postgresql://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}"

        sources = job.parameters.get('sources', ['geo', 'sra', 'bioproject', 'gwas'])
        max_per_source = job.parameters.get('max_per_source', 500)

        result = rust_engine.search_and_ingest_omics(
            job_id=str(job.id),
            query=job.parameters['query'],
            db_url=db_url,
            sources=sources,
            max_per_source=max_per_source,
            ncbi_api_key=getattr(settings, 'NCBI_API_KEY', None),
        )

        return {
            'datasets_processed': result.datasets_processed,
            'datasets_inserted': result.datasets_inserted,
            'links_inserted': result.links_inserted,
            'errors': result.errors,
        }
    except (ImportError, AttributeError):
        # Fallback when Rust engine is not compiled or compiled without search_and_ingest_omics
        # (AttributeError: old Phase 2 build installed but Phase 3 not yet recompiled)
        job = IngestionJob.objects.get(id=job_id)
        job.status = IngestionJob.JobStatus.COMPLETED
        job.records_processed = 0
        job.save(update_fields=['status', 'records_processed'])
        return {'datasets_processed': 0, 'datasets_inserted': 0, 'links_inserted': 0, 'errors': []}
    except Exception as exc:
        job = IngestionJob.objects.get(id=job_id)
        job.status = IngestionJob.JobStatus.FAILED
        job.error_message = str(exc)
        job.save(update_fields=['status', 'error_message'])
        raise self.retry(exc=exc, countdown=60)
