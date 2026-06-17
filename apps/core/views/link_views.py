from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.models import DaVinciProject, ProjectPaperDataset
from apps.core.serializers.link import ProjectPaperDatasetSerializer


class ProjectPaperDatasetViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    Literature ↔ Omics links within a project.

    list:    GET  /projects/{project_pk}/links/
    confirm: POST /projects/{project_pk}/links/{id}/confirm/
    reject:  POST /projects/{project_pk}/links/{id}/reject/
    """
    serializer_class = ProjectPaperDatasetSerializer
    # stub para drf-spectacular; get_queryset() prevalece em runtime
    queryset = ProjectPaperDataset.objects.none()

    def _get_project(self):
        return get_object_or_404(
            DaVinciProject,
            pk=self.kwargs['project_pk'],
            user=self.request.user,
        )

    def get_queryset(self):
        project = self._get_project()
        return (
            ProjectPaperDataset.objects.filter(project=project)
            .select_related(
                'project_paper__paper',
                'project_dataset__dataset',
            )
            .order_by('-created_at')
        )

    @extend_schema(
        request=None,
        responses={200: ProjectPaperDatasetSerializer},
        summary="Confirmar link literatura-ômica",
        description="Define confidence=confirmed no link entre paper e dataset.",
    )
    @action(detail=True, methods=['post'])
    def confirm(self, request, project_pk=None, pk=None):
        link = self.get_object()
        link.confidence = ProjectPaperDataset.LinkConfidence.CONFIRMED
        link.save(update_fields=['confidence'])
        return Response(ProjectPaperDatasetSerializer(link).data)

    @extend_schema(
        request=None,
        responses={200: ProjectPaperDatasetSerializer},
        summary="Rejeitar link literatura-ômica",
        description="Define confidence=rejected no link entre paper e dataset.",
    )
    @action(detail=True, methods=['post'])
    def reject(self, request, project_pk=None, pk=None):
        link = self.get_object()
        link.confidence = ProjectPaperDataset.LinkConfidence.REJECTED
        link.save(update_fields=['confidence'])
        return Response(ProjectPaperDatasetSerializer(link).data)
