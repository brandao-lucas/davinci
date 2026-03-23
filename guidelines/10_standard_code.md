## 10. Padrões de Código

### 10.1 Django Services

```python
# apps/core/services/search_service.py

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
```

### 10.2 Celery Tasks

```python
# apps/core/tasks/ingestion_tasks.py

from celery import shared_task
from django.conf import settings
from apps.core.models import IngestionJob

@shared_task(bind=True, max_retries=3)
def run_pubmed_ingestion(self, job_id: str):
    """
    Chama o Rust engine via PyO3.
    O Rust faz todo o trabalho pesado e atualiza o IngestionJob.
    """
    import rust_engine  # Módulo compilado via maturin

    job = IngestionJob.objects.get(id=job_id)
    try:
        result = rust_engine.search_and_ingest_pubmed(
            job_id=str(job.id),
            query=job.parameters['query'],
            date_from=job.parameters.get('date_from'),
            date_to=job.parameters.get('date_to'),
            db_url=settings.DATABASES['default']['URL'],
            ncbi_api_key=settings.NCBI_API_KEY,
        )
        # Rust já atualizou o job no banco, mas podemos logar
        return {
            'processed': result.records_processed,
            'inserted': result.records_inserted,
        }
    except Exception as exc:
        job.status = IngestionJob.JobStatus.FAILED
        job.error_message = str(exc)
        job.save(update_fields=['status', 'error_message'])
        raise self.retry(exc=exc, countdown=60)
```

### 10.3 ViewSets (Finos)

```python
# apps/core/views/project_views.py

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from apps.core.models import DaVinciProject
from apps.core.serializers.project import DaVinciProjectSerializer
from apps.core.services.search_service import SearchService

class DaVinciProjectViewSet(viewsets.ModelViewSet):
    serializer_class = DaVinciProjectSerializer

    def get_queryset(self):
        return DaVinciProject.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @action(detail=True, methods=['post'])
    def search(self, request, pk=None):
        """Dispara busca no PubMed + bases ômicas."""
        project = self.get_object()
        job = SearchService.dispatch_pubmed_search(project)
        return Response(
            {'job_id': str(job.id), 'status': job.status},
            status=status.HTTP_202_ACCEPTED
        )
```

---


---