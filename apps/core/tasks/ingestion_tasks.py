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
        
        try:
            job = IngestionJob.objects.get(id=job_id)
        except IngestionJob.DoesNotExist:
            print(f"IngestionJob {job_id} not found. Task aborted.")
            return {'processed': 0, 'inserted': 0}
        
        # Build DB URL from settings
        db = settings.DATABASES['default']
        db_url = f"postgresql://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}"
        
        result = rust_engine.search_and_ingest_pubmed(
            job_id=str(job.id),
            query=job.parameters['query'],
            project_id=str(job.project_id),
            date_from=job.parameters.get('date_from'),
            date_to=job.parameters.get('date_to'),
            db_url=db_url,
            ncbi_api_key=job.parameters.get('ncbi_api_key') or getattr(settings, 'NCBI_API_KEY', None),
        )

        # Resolve any pending dataset-paper links from prior omics runs
        try:
            resolved = rust_engine.resolve_pending_links(db_url)
            if resolved > 0:
                print(f"Resolved {resolved} pending dataset-paper links after PubMed ingestion")
        except Exception as e:
            print(f"resolve_pending_links warning: {e}")

        return {
            'processed': result.records_processed,
            'inserted': result.records_inserted,
        }
    except ImportError:
        # Stub logic for Phase 1 MVP when rust logic is not yet compiled
        try:
            job = IngestionJob.objects.get(id=job_id)
            job.status = IngestionJob.JobStatus.COMPLETED
            job.records_processed = 0
            job.save(update_fields=['status', 'records_processed'])
        except IngestionJob.DoesNotExist:
            pass
        return {'processed': 0, 'inserted': 0}
    except Exception as exc:
        try:
            job = IngestionJob.objects.get(id=job_id)
            job.status = IngestionJob.JobStatus.FAILED
            job.error_message = str(exc)
            job.save(update_fields=['status', 'error_message'])
        except IngestionJob.DoesNotExist:
            print(f"Exception occurred but IngestionJob {job_id} was already deleted: {exc}")
        
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

        try:
            job = IngestionJob.objects.get(id=job_id)
        except IngestionJob.DoesNotExist:
            print(f"IngestionJob {job_id} not found. Omics task aborted.")
            return {'datasets_processed': 0, 'datasets_inserted': 0, 'links_inserted': 0, 'errors': []}

        db = settings.DATABASES['default']
        db_url = f"postgresql://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}"

        sources = job.parameters.get('sources', ['geo', 'sra', 'bioproject', 'gwas'])
        max_per_source = job.parameters.get('max_per_source', 500)

        result = rust_engine.search_and_ingest_omics(
            job_id=str(job.id),
            query=job.parameters['query'],
            db_url=db_url,
            project_id=str(job.project_id),
            sources=sources,
            max_per_source=max_per_source,
            ncbi_api_key=job.parameters.get('ncbi_api_key') or getattr(settings, 'NCBI_API_KEY', None),
            synonyms=job.parameters.get('synonyms') or [],
        )

        # Surface any non-fatal errors into the job record
        if result.errors:
            try:
                job = IngestionJob.objects.get(id=job_id)
                job.records_processed = result.datasets_processed
                job.error_message = '; '.join(result.errors)
                job.save(update_fields=['records_processed', 'error_message'])
            except IngestionJob.DoesNotExist:
                pass

        return {
            'datasets_processed': result.datasets_processed,
            'datasets_inserted': result.datasets_inserted,
            'links_inserted': result.links_inserted,
            'errors': result.errors,
        }
    except ImportError:
        # Fallback when Rust engine is not compiled
        try:
            job = IngestionJob.objects.get(id=job_id)
            job.status = IngestionJob.JobStatus.COMPLETED
            job.records_processed = 0
            job.error_message = 'rust_engine not installed — compile with maturin'
            job.save(update_fields=['status', 'records_processed', 'error_message'])
        except IngestionJob.DoesNotExist:
            pass
        return {'datasets_processed': 0, 'datasets_inserted': 0, 'links_inserted': 0, 'errors': []}
    except Exception as exc:
        try:
            job = IngestionJob.objects.get(id=job_id)
            job.status = IngestionJob.JobStatus.FAILED
            job.error_message = str(exc)
            job.save(update_fields=['status', 'error_message'])
        except IngestionJob.DoesNotExist:
            print(f"Exception in omics task but IngestionJob {job_id} was missing: {exc}")
        raise self.retry(exc=exc, countdown=60)
