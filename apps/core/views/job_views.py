from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.models import DaVinciProject, IngestionJob
from apps.core.serializers.job import IngestionJobSerializer, JobCancelErrorSerializer


class IngestionJobViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """
    Read + cancel ingestion jobs for a project.

    list:   GET  /projects/{project_pk}/jobs/
    detail: GET  /projects/{project_pk}/jobs/{id}/
    cancel: POST /projects/{project_pk}/jobs/{id}/cancel/
    """
    serializer_class = IngestionJobSerializer
    # stub para drf-spectacular; get_queryset() prevalece em runtime
    queryset = IngestionJob.objects.none()

    def _get_project(self):
        return get_object_or_404(
            DaVinciProject,
            pk=self.kwargs['project_pk'],
            user=self.request.user,
        )

    def get_queryset(self):
        project = self._get_project()
        return IngestionJob.objects.filter(project=project).order_by('-created_at')

    @extend_schema(
        request=None,
        responses={
            200: IngestionJobSerializer,
            400: JobCancelErrorSerializer,
        },
        summary="Cancelar job de ingestão",
        description="Cancela um job que ainda não atingiu estado terminal (completed/failed).",
    )
    @action(detail=True, methods=['post'])
    def cancel(self, request, project_pk=None, pk=None):
        job = self.get_object()
        if job.status in (IngestionJob.JobStatus.COMPLETED, IngestionJob.JobStatus.FAILED):
            return Response(
                {'detail': 'Cannot cancel a job that has already finished.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        job.status = IngestionJob.JobStatus.CANCELLED
        job.save(update_fields=['status'])
        return Response(IngestionJobSerializer(job).data)
