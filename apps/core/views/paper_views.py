from django.contrib.postgres.search import SearchQuery, SearchRank
from django.contrib.postgres.aggregates import ArrayAgg
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.models import (
    DaVinciProject, ProjectPaper, ClinicalCategory, UserCategory,
)
from apps.core.serializers.paper import (
    ProjectPaperListSerializer,
    ProjectPaperDetailSerializer,
    ProjectPaperCurateSerializer,
)


class ProjectPaperViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """
    Curadoria de papers dentro de um projeto.

    list:   GET  /projects/{project_pk}/papers/
    detail: GET  /projects/{project_pk}/papers/{id}/
    patch:  PATCH /projects/{project_pk}/papers/{id}/
    categorize: POST /projects/{project_pk}/papers/{id}/categorize/
    bulk_curate: POST /projects/{project_pk}/papers/bulk_curate/
    search: GET  /projects/{project_pk}/papers/search/?q=term
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
        qs = (
            ProjectPaper.objects.filter(project=project)
            .select_related('paper')
            .prefetch_related('clinical_categories', 'user_categories')
        )

        # Filters
        curation_status = self.request.query_params.get('curation_status')
        if curation_status:
            qs = qs.filter(curation_status=curation_status)

        pub_year_min = self.request.query_params.get('pub_year_min')
        if pub_year_min:
            qs = qs.filter(paper__pub_year__gte=pub_year_min)

        pub_year_max = self.request.query_params.get('pub_year_max')
        if pub_year_max:
            qs = qs.filter(paper__pub_year__lte=pub_year_max)

        journal = self.request.query_params.get('journal')
        if journal:
            qs = qs.filter(paper__journal__icontains=journal)

        pub_type = self.request.query_params.get('pub_type')
        if pub_type:
            qs = qs.filter(paper__pub_type=pub_type)

        if self.request.query_params.get('has_abstract') == 'true':
            qs = qs.exclude(paper__abstract='')

        if self.request.query_params.get('free_full_text') == 'true':
            qs = qs.exclude(paper__pmc_id='')

        clinical_category = self.request.query_params.get('clinical_category')
        if clinical_category:
            qs = qs.filter(clinical_categories__slug=clinical_category)

        return qs.order_by('-added_at')

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ProjectPaperDetailSerializer
        if self.action in ('update', 'partial_update'):
            return ProjectPaperCurateSerializer
        return ProjectPaperListSerializer

    def perform_update(self, serializer):
        serializer.save(curated_at=timezone.now())

    @action(detail=False, methods=['get'], url_path='search')
    def search(self, request, project_pk=None):
        """FTS on project papers via paper.search_vector."""
        q = request.query_params.get('q', '').strip()
        if not q:
            return Response({'detail': 'Query parameter "q" is required.'}, status=400)

        project = self._get_project()
        search_query = SearchQuery(q)
        qs = (
            ProjectPaper.objects.filter(project=project)
            .filter(paper__search_vector=search_query)
            .select_related('paper')
            .prefetch_related('clinical_categories', 'user_categories')
            .order_by('-added_at')
        )
        serializer = ProjectPaperListSerializer(qs, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'], url_path='categorize')
    def categorize(self, request, project_pk=None, pk=None):
        """
        Assign or remove clinical/user categories.

        Body: {
            "clinical_add": [category_slug, ...],
            "clinical_remove": [category_slug, ...],
            "user_add": [user_category_id, ...],
            "user_remove": [user_category_id, ...]
        }
        """
        project_paper = self.get_object()

        clinical_add = request.data.get('clinical_add', [])
        clinical_remove = request.data.get('clinical_remove', [])
        user_add = request.data.get('user_add', [])
        user_remove = request.data.get('user_remove', [])

        if clinical_add:
            cats = ClinicalCategory.objects.filter(slug__in=clinical_add)
            for cat in cats:
                project_paper.clinical_categories.add(cat)

        if clinical_remove:
            cats = ClinicalCategory.objects.filter(slug__in=clinical_remove)
            project_paper.clinical_categories.remove(*cats)

        if user_add:
            ucats = UserCategory.objects.filter(
                id__in=user_add, project=project_paper.project
            )
            project_paper.user_categories.add(*ucats)

        if user_remove:
            ucats = UserCategory.objects.filter(
                id__in=user_remove, project=project_paper.project
            )
            project_paper.user_categories.remove(*ucats)

        serializer = ProjectPaperDetailSerializer(project_paper)
        return Response(serializer.data)

    @action(detail=False, methods=['post'], url_path='bulk_curate')
    def bulk_curate(self, request, project_pk=None):
        """
        Bulk-update curation_status for multiple project papers.

        Body: {"paper_ids": [int, ...], "curation_status": "included"}
        """
        paper_ids = request.data.get('paper_ids', [])
        new_status = request.data.get('curation_status')

        valid_statuses = [s.value for s in ProjectPaper.CurationStatus]
        if new_status not in valid_statuses:
            return Response(
                {'detail': f"Invalid status {new_status!r}. Choose from {valid_statuses}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not paper_ids:
            return Response({'detail': 'paper_ids is required.'}, status=400)

        project = self._get_project()
        updated = ProjectPaper.objects.filter(
            project=project, id__in=paper_ids
        ).update(curation_status=new_status, curated_at=timezone.now())

        return Response({'updated': updated})
