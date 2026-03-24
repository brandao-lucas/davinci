from django.shortcuts import get_object_or_404
from rest_framework import mixins, viewsets

from apps.core.models import ClinicalCategory, DaVinciProject, UserCategory
from apps.core.serializers.category import ClinicalCategorySerializer, UserCategorySerializer


class ClinicalCategoryViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    Read-only global clinical categories.
    GET /api/v1/clinical-categories/
    """
    serializer_class = ClinicalCategorySerializer
    queryset = ClinicalCategory.objects.filter(is_active=True).order_by('priority')


class UserCategoryViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """
    CRUD for project-scoped custom categories.
    /projects/{project_pk}/categories/
    """
    serializer_class = UserCategorySerializer
    http_method_names = ['get', 'post', 'patch', 'delete', 'head', 'options']

    def _get_project(self):
        return get_object_or_404(
            DaVinciProject,
            pk=self.kwargs['project_pk'],
            user=self.request.user,
        )

    def get_queryset(self):
        project = self._get_project()
        return UserCategory.objects.filter(project=project).order_by('name')

    def perform_create(self, serializer):
        project = self._get_project()
        serializer.save(project=project)
