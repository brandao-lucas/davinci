from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, OpenApiParameter
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from apps.core.models import DaVinciProject, ProjectPaperDataset
from apps.core.serializers.link import (
    ProjectPaperDatasetSerializer,
    OrphanLinkSuggestionSerializer,
)
from apps.core.services.link_service import suggest_orphan_links


class LinkSuggestionPagination(PageNumberPagination):
    """Paginação para sugestões de órfãos — mesma convenção dos outros endpoints de projeto."""
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 200


class ProjectPaperDatasetViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    Literature ↔ Omics links within a project.

    list:        GET  /projects/{project_pk}/links/
    suggestions: GET  /projects/{project_pk}/links/suggestions/
    confirm:     POST /projects/{project_pk}/links/{id}/confirm/
    reject:      POST /projects/{project_pk}/links/{id}/reject/
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

    # ------------------------------------------------------------------
    # GET /projects/{project_pk}/links/suggestions/
    # ------------------------------------------------------------------

    @extend_schema(
        request=None,
        responses={200: OrphanLinkSuggestionSerializer(many=True)},
        summary="Sugestões de vínculos órfãos (Nível 2)",
        description=(
            "Retorna lista paginada de sugestões de vínculos onde apenas UMA ponta "
            "do DatasetPaperLink global já está no projeto. Dois casos:\n\n"
            "- **dataset_missing**: paper já é ProjectPaper do projeto, mas o dataset "
            "vinculado (via elink/GEO) NÃO é ProjectDataset → sugerir adicionar o dataset.\n"
            "- **paper_missing**: dataset já é ProjectDataset do projeto, mas o paper "
            "vinculado NÃO é ProjectPaper → sugerir adicionar o paper.\n\n"
            "READ-ONLY: nunca grava em ProjectPaperDataset. "
            "Filtros: ?type=dataset_missing|paper_missing (opcional). "
            "Paginação: ?page=, ?page_size= (máx 200)."
        ),
        parameters=[
            OpenApiParameter(
                name='type',
                type=str,
                description=(
                    "Filtrar por tipo de sugestão: "
                    "'dataset_missing' (paper no projeto, dataset ausente) ou "
                    "'paper_missing' (dataset no projeto, paper ausente). "
                    "Omitir retorna ambos."
                ),
                required=False,
            ),
        ],
    )
    @action(detail=False, methods=['get'], url_path='suggestions')
    def suggestions(self, request, project_pk=None):
        """
        Lista sugestões de vínculos órfãos para o projeto.

        Isolamento (Regra #3): _get_project() filtra por request.user — projeto
        alheio retorna 404. A query interna também é restrita ao project_id.

        READ-ONLY: suggest_orphan_links() não cria, não salva, não altera nada.
        """
        project = self._get_project()

        raw = suggest_orphan_links(project.id)

        # Filtro opcional por tipo de sugestão (query param ?type=)
        type_filter = request.query_params.get('type', '').strip()
        if type_filter in ('dataset_missing', 'paper_missing'):
            raw = [s for s in raw if s['suggestion_type'] == type_filter]

        # Paginação manual sobre lista Python (resultado já em memória — lista curta por design)
        paginator = LinkSuggestionPagination()
        page = paginator.paginate_queryset(raw, request, view=self)
        if page is not None:
            serializer = OrphanLinkSuggestionSerializer(page, many=True)
            return paginator.get_paginated_response(serializer.data)

        serializer = OrphanLinkSuggestionSerializer(raw, many=True)
        return Response(serializer.data)
