from django.contrib.postgres.search import SearchQuery
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.models import DaVinciProject, ProjectDataset
from apps.core.serializers.dataset import (
    ProjectDatasetListSerializer,
    ProjectDatasetDetailSerializer,
    ProjectDatasetCurateSerializer,
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

        curation_status = self.request.query_params.get('status')
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

        return qs.order_by('-added_at')

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ProjectDatasetDetailSerializer
        if self.action in ('update', 'partial_update'):
            return ProjectDatasetCurateSerializer
        return ProjectDatasetListSerializer

    def perform_update(self, serializer):
        serializer.save(curated_at=timezone.now())

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
