import logging

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.models import DaVinciProject, ProjectDataset, ProjectSample
from apps.core.serializers.sample import (
    ProjectSampleListSerializer,
    ProjectSampleDetailSerializer,
    ProjectSampleCurateSerializer,
)

logger = logging.getLogger(__name__)


class ProjectSampleViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """
    Curadoria de amostras ômicas dentro de um projeto.

    Rotas registradas em urls.py:
      list/filter:  GET  /projects/{project_pk}/samples/
      detail:       GET  /projects/{project_pk}/samples/{id}/
      patch:        PATCH /projects/{project_pk}/samples/{id}/
      bulk_curate:  POST /projects/{project_pk}/samples/bulk_curate/

    Rota por dataset (para a página de samples de um dataset específico):
      GET  /projects/{project_pk}/datasets/{dataset_pk}/samples/
      (mesma view, parâmetro dataset_pk opcional tratado em get_queryset)

    firebase-auth-guard: _get_project filtra sempre por request.user.
    curation-audit-trail: perform_update e bulk_curate setam curated_at;
      sem DELETE — exclusão é curation_status='excluded'.
    postgres-fts-patterns: select_related em todos os querysets (sem N+1).
    """

    http_method_names = ['get', 'patch', 'post', 'head', 'options']

    def _get_project(self) -> DaVinciProject:
        """
        Retorna o projeto do request.user, garantindo isolamento por usuário.
        Retorna 404 se o projeto não pertence ao usuário autenticado.
        """
        return get_object_or_404(
            DaVinciProject,
            pk=self.kwargs['project_pk'],
            user=self.request.user,
        )

    def get_queryset(self):
        project = self._get_project()

        # select_related evita N+1: sample + dataset acessados no serializer
        qs = ProjectSample.objects.filter(project=project).select_related(
            'sample', 'sample__dataset'
        )

        # Filtro por dataset — suporta tanto a rota aninhada
        # /datasets/{dataset_pk}/samples/ quanto ?dataset=<id> na rota plana.
        # Achado #1 (007): validar que o dataset pertence ao projeto do usuário
        # antes de filtrar, retornando 404 para datasets de outro projeto.
        # Espelha o padrão de _get_project(): "404 não vaza existência".
        dataset_pk = self.kwargs.get('dataset_pk') or self.request.query_params.get('dataset')
        if dataset_pk:
            get_object_or_404(ProjectDataset, project=project, dataset_id=dataset_pk)
            qs = qs.filter(sample__dataset_id=dataset_pk)

        curation_status = self.request.query_params.get('curation_status')
        if curation_status:
            qs = qs.filter(curation_status=curation_status)

        organism = self.request.query_params.get('organism')
        if organism:
            qs = qs.filter(sample__organism__icontains=organism)

        return qs.order_by('-added_at')

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ProjectSampleDetailSerializer
        if self.action in ('update', 'partial_update'):
            return ProjectSampleCurateSerializer
        return ProjectSampleListSerializer

    def perform_update(self, serializer):
        """
        curation-audit-trail: garante curated_at preenchido em toda atualização de curadoria.
        Sem DELETE — exclusão é sempre curation_status='excluded'.
        """
        serializer.save(curated_at=timezone.now())

    @action(detail=False, methods=['post'], url_path='bulk_curate')
    def bulk_curate(self, request, project_pk=None, dataset_pk=None):
        """
        Bulk-update de curation_status para múltiplos ProjectSamples.

        Body: {"sample_ids": [int, ...], "curation_status": "included",
               "exclusion_reason": "..."}  (exclusion_reason opcional)

        curation-audit-trail: seta curated_at para todos os registros atualizados;
          sobrescreve exclusion_reason com o valor do body (ou '' se ausente);
          notes NÃO é alterado pelo bulk (mesma semântica de dataset_views.bulk_curate).
        firebase-auth-guard: filtra por _get_project() (usuário isolado).
        """
        sample_ids = request.data.get('sample_ids', [])
        new_status = request.data.get('curation_status')

        valid_statuses = [s.value for s in ProjectSample.CurationStatus]
        if new_status not in valid_statuses:
            return Response(
                {'detail': f"Invalid status {new_status!r}. Choose from {valid_statuses}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not sample_ids:
            return Response({'detail': 'sample_ids is required.'}, status=status.HTTP_400_BAD_REQUEST)

        project = self._get_project()
        exclusion_reason = request.data.get('exclusion_reason', '')
        updated = ProjectSample.objects.filter(
            project=project, id__in=sample_ids
        ).update(
            curation_status=new_status,
            exclusion_reason=exclusion_reason,
            curated_at=timezone.now(),
        )
        return Response({'updated': updated})
