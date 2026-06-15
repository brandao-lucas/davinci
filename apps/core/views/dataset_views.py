import logging

from django.contrib.postgres.search import SearchQuery
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.models import DaVinciProject, IngestionJob, OmicSample, ProjectDataset
from apps.core.serializers.dataset import (
    ProjectDatasetListSerializer,
    ProjectDatasetDetailSerializer,
    ProjectDatasetCurateSerializer,
)
from apps.core.tasks.ingestion_tasks import run_sample_ingestion

logger = logging.getLogger(__name__)


def _maybe_dispatch_sample_ingestion(project_dataset: ProjectDataset) -> None:
    """
    Dispara run_sample_ingestion sob demanda quando um dataset é curado como `included`.

    Guarda de idempotência dupla:
      1. Já existem OmicSamples para o dataset (Rust já ingeriu) — skip.
      2. Já há um SAMPLE_FETCH ativo (pending/running) para o dataset+projeto — skip.

    O try/except externo garante que falha no dispatch não derruba o flow de curadoria.
    """
    dataset = project_dataset.dataset
    project = project_dataset.project

    # Guarda 1: samples já ingeridos para o dataset
    if OmicSample.objects.filter(dataset=dataset).exists():
        logger.info(
            'Samples já ingeridos para dataset %s — disparo de SAMPLE_FETCH ignorado',
            dataset.accession,
        )
        return

    # Guarda 2: job SAMPLE_FETCH já ativo para este dataset+projeto
    already_active = IngestionJob.objects.filter(
        project=project,
        job_type=IngestionJob.JobType.SAMPLE_FETCH,
        status__in=[IngestionJob.JobStatus.PENDING, IngestionJob.JobStatus.RUNNING],
        parameters__dataset_id=dataset.id,
    ).exists()
    if already_active:
        logger.info(
            'SAMPLE_FETCH já ativo para projeto %s / dataset %s — disparo ignorado',
            project.id,
            dataset.accession,
        )
        return

    try:
        run_sample_ingestion.delay(str(project.id), dataset.id)
        logger.info(
            'SAMPLE_FETCH disparado para projeto %s / dataset %s',
            project.id,
            dataset.accession,
        )
    except Exception as exc:
        logger.error(
            'Falha ao disparar SAMPLE_FETCH para projeto %s / dataset %s: %s',
            project.id,
            dataset.accession,
            exc,
        )


class ProjectDatasetViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """
    Curadoria de datasets ômicos dentro de um projeto.

    list:   GET  /projects/{project_pk}/datasets/
    detail: GET  /projects/{project_pk}/datasets/{id}/
    patch:  PATCH /projects/{project_pk}/datasets/{id}/
    search: GET  /projects/{project_pk}/datasets/search/?q=term
    """
    http_method_names = ['get', 'patch', 'post', 'head', 'options']

    def _get_project(self):
        return get_object_or_404(
            DaVinciProject,
            pk=self.kwargs['project_pk'],
            user=self.request.user,
        )

    def get_queryset(self):
        project = self._get_project()
        qs = ProjectDataset.objects.filter(project=project).select_related('dataset')

        curation_status = self.request.query_params.get('curation_status')
        if curation_status:
            qs = qs.filter(curation_status=curation_status)

        omic_type = self.request.query_params.get('omic_type')
        if omic_type:
            qs = qs.filter(dataset__omic_type=omic_type)

        organism = self.request.query_params.get('organism')
        if organism:
            qs = qs.filter(dataset__organism__icontains=organism)

        source_db = self.request.query_params.get('source_db')
        if source_db:
            qs = qs.filter(dataset__source_db=source_db)

        if self.request.query_params.get('has_summary') == 'true':
            qs = qs.exclude(dataset__summary='')

        return qs.order_by('-added_at')

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ProjectDatasetDetailSerializer
        if self.action in ('update', 'partial_update'):
            return ProjectDatasetCurateSerializer
        return ProjectDatasetListSerializer

    def perform_update(self, serializer):
        instance = serializer.save(curated_at=timezone.now())
        # Trigger sob demanda: ingestão de samples ao incluir dataset (curation-audit-trail)
        if instance.curation_status == ProjectDataset.CurationStatus.INCLUDED:
            _maybe_dispatch_sample_ingestion(instance)

    @action(detail=False, methods=['post'], url_path='bulk_curate')
    def bulk_curate(self, request, project_pk=None):
        """
        Bulk-update curation_status for multiple project datasets.

        Body: {"dataset_ids": [int, ...], "curation_status": "included"}
        """
        dataset_ids = request.data.get('dataset_ids', [])
        new_status = request.data.get('curation_status')

        valid_statuses = [s.value for s in ProjectDataset.CurationStatus]
        if new_status not in valid_statuses:
            return Response(
                {'detail': f"Invalid status {new_status!r}. Choose from {valid_statuses}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not dataset_ids:
            return Response({'detail': 'dataset_ids is required.'}, status=400)

        project = self._get_project()
        exclusion_reason = request.data.get('exclusion_reason', '')
        updated = ProjectDataset.objects.filter(
            project=project, id__in=dataset_ids
        ).update(
            curation_status=new_status,
            exclusion_reason=exclusion_reason,
            curated_at=timezone.now(),
        )

        # Trigger sob demanda: dispara SAMPLE_FETCH para cada dataset incluído (curation-audit-trail)
        if new_status == ProjectDataset.CurationStatus.INCLUDED:
            for pd in ProjectDataset.objects.filter(project=project, id__in=dataset_ids).select_related('dataset'):
                _maybe_dispatch_sample_ingestion(pd)

        return Response({'updated': updated})

    @action(detail=False, methods=['get'], url_path='search')
    def search(self, request, project_pk=None):
        """FTS on project datasets via dataset.search_vector."""
        q = request.query_params.get('q', '').strip()
        if not q:
            return Response({'detail': 'Query parameter "q" is required.'}, status=400)

        project = self._get_project()
        search_query = SearchQuery(q)
        qs = (
            ProjectDataset.objects.filter(project=project)
            .filter(dataset__search_vector=search_query)
            .select_related('dataset')
            .order_by('-added_at')
        )
        serializer = ProjectDatasetListSerializer(qs, many=True)
        return Response(serializer.data)
