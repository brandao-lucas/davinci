from django.contrib.postgres.search import SearchQuery, SearchRank
from django.contrib.postgres.aggregates import ArrayAgg
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.models import (
    DaVinciProject, Paper, ProjectPaper, ClinicalCategory, UserCategory,
)


def apply_paper_filters(queryset, params):
    """
    Aplica filtros de listagem/bulk a um queryset de ProjectPaper.

    params: dict-like (ex.: request.query_params ou dict explícito do bulk body).

    Filtros disponíveis:
      curation_status     — valor exato de ProjectPaper.CurationStatus
      pub_year_min        — paper__pub_year >= valor
      pub_year_max        — paper__pub_year <= valor
      journal             — paper__journal__icontains
      pub_type            — paper__pub_type exato
      has_abstract        — 'true' exclui papers sem abstract
      free_full_text      — 'true' exclui papers sem pmc_id
      clinical_category   — slug de ClinicalCategory
      relevance_min       — relevance_score >= valor
      relevance_max       — relevance_score <= valor
      ingestion_job       — ingestion_job_id == valor (proveniência)
    """
    curation_status = params.get('curation_status')
    if curation_status:
        queryset = queryset.filter(curation_status=curation_status)

    pub_year_min = params.get('pub_year_min')
    if pub_year_min:
        queryset = queryset.filter(paper__pub_year__gte=pub_year_min)

    pub_year_max = params.get('pub_year_max')
    if pub_year_max:
        queryset = queryset.filter(paper__pub_year__lte=pub_year_max)

    journal = params.get('journal')
    if journal:
        queryset = queryset.filter(paper__journal__icontains=journal)

    pub_type = params.get('pub_type')
    if pub_type:
        queryset = queryset.filter(paper__pub_type=pub_type)

    if params.get('has_abstract') == 'true':
        queryset = queryset.exclude(paper__abstract='')

    if params.get('free_full_text') == 'true':
        queryset = queryset.exclude(paper__pmc_id='')

    clinical_category = params.get('clinical_category')
    if clinical_category:
        queryset = queryset.filter(clinical_categories__slug=clinical_category)

    relevance_min = params.get('relevance_min')
    if relevance_min is not None:
        queryset = queryset.filter(relevance_score__gte=relevance_min)

    relevance_max = params.get('relevance_max')
    if relevance_max is not None:
        queryset = queryset.filter(relevance_score__lte=relevance_max)

    ingestion_job = params.get('ingestion_job')
    if ingestion_job:
        queryset = queryset.filter(ingestion_job_id=ingestion_job)

    return queryset


from apps.core.serializers.paper import (
    ProjectPaperListSerializer,
    ProjectPaperDetailSerializer,
    ProjectPaperCurateSerializer,
    PaperBulkCurateRequestSerializer,
    PaperBulkCurateResponseSerializer,
    PaperCategorizeRequestSerializer,
)
from apps.core.serializers.link import AddPaperToProjectRequestSerializer


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
    # stub para drf-spectacular; get_queryset() prevalece em runtime
    queryset = ProjectPaper.objects.none()

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

        # Para detalhe: pré-carrega vínculos project-scoped para evitar N+1 no linked_datasets.
        # O filtro por project_id é feito no serializer (Regra #3 — sem cross-project).
        if self.action == 'retrieve':
            qs = qs.prefetch_related(
                'projectpaperdataset_set__project_dataset__dataset',
            )

        qs = apply_paper_filters(qs, self.request.query_params)

        return qs.order_by('-added_at')

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ProjectPaperDetailSerializer
        if self.action in ('update', 'partial_update'):
            return ProjectPaperCurateSerializer
        return ProjectPaperListSerializer

    def perform_update(self, serializer):
        serializer.save(curated_at=timezone.now())

    @extend_schema(
        responses={200: ProjectPaperListSerializer(many=True)},
        summary="Busca FTS em papers do projeto",
        description="Busca full-text em papers do projeto via search_vector. Parâmetro obrigatório: ?q=termo.",
    )
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

    @extend_schema(
        request=PaperCategorizeRequestSerializer,
        responses={200: ProjectPaperDetailSerializer},
        summary="Categorizar paper",
        description="Adiciona ou remove categorias clínicas e de usuário de um ProjectPaper.",
    )
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

    @extend_schema(
        request=PaperBulkCurateRequestSerializer,
        responses={200: PaperBulkCurateResponseSerializer},
        summary="Curadoria em massa de papers",
        description="Atualiza curation_status de múltiplos ProjectPapers em uma operação.",
    )
    @action(detail=False, methods=['post'], url_path='bulk_curate')
    def bulk_curate(self, request, project_pk=None):
        """
        Bulk-update curation_status for multiple project papers.

        Body (por IDs):
          {"paper_ids": [int, ...], "curation_status": "excluded",
           "exclusion_reason": "fora do escopo"}

        Body (por filtro):
          {"filters": {"curation_status": "pending", "pub_year_max": 2010, ...},
           "curation_status": "excluded", "exclusion_reason": "fora do escopo"}

        Exatamente um de paper_ids ou filters deve estar presente.
        """
        paper_ids = request.data.get('paper_ids')
        filters = request.data.get('filters')
        new_status = request.data.get('curation_status')
        exclusion_reason = request.data.get('exclusion_reason', '')

        valid_statuses = [s.value for s in ProjectPaper.CurationStatus]
        if new_status not in valid_statuses:
            return Response(
                {'detail': f"Invalid status {new_status!r}. Choose from {valid_statuses}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # paper_ids presente mas vazio → 400 (contrato pré-existente preservado).
        # paper_ids ausente E filters ausente/vazio → 400.
        if paper_ids is not None and len(paper_ids) == 0:
            return Response(
                {'detail': 'paper_ids não pode ser uma lista vazia.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if paper_ids is None and not filters:
            return Response(
                {'detail': 'Forneça paper_ids ou filters.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        project = self._get_project()

        if paper_ids is not None:
            qs = ProjectPaper.objects.filter(project=project, id__in=paper_ids)
        else:
            qs = ProjectPaper.objects.filter(project=project)
            qs = apply_paper_filters(qs, filters)

        update_kwargs = {
            'curation_status': new_status,
            'curated_at': timezone.now(),
        }
        # exclusion_reason: só sobrescreve se enviado explicitamente no body.
        # bulk de inclusão/maybe não apaga motivo anterior (curation-audit-trail).
        if 'exclusion_reason' in request.data:
            update_kwargs['exclusion_reason'] = exclusion_reason

        updated = qs.update(**update_kwargs)
        return Response({'updated': updated})

    @extend_schema(
        request=AddPaperToProjectRequestSerializer,
        responses={200: ProjectPaperListSerializer, 201: ProjectPaperListSerializer},
        summary="Adicionar paper ao projeto a partir de sugestão de órfão",
        description=(
            "Vincula um Paper global existente ao projeto como ProjectPaper "
            "(curation_status='pending'). Idempotente: se o vínculo já existir, "
            "retorna o existente com HTTP 200. Criação nova retorna HTTP 201.\n\n"
            "Após criar o vínculo, dispara materialize_project_links para que a "
            "ponta recém-adicionada promova automaticamente o par órfão a "
            "ProjectPaperDataset(confidence='auto') (Nível 1).\n\n"
            "Request body: { \"pmid\": <int> }  — paper_pmid vindo de "
            "GET /links/suggestions/ (campo OrphanLinkSuggestionSerializer.paper_pmid)."
        ),
    )
    @action(detail=False, methods=['post'], url_path='add_from_suggestion')
    def add_from_suggestion(self, request, project_pk=None):
        """
        Adiciona paper global (identificado por PMID) ao projeto como ProjectPaper.

        Fluxo:
          1. Valida request body { "pmid": <int> }.
          2. Resolve projeto via _get_project() — 404 se alheio (Regra #3).
          3. Busca Paper global — 404 se inexistente.
          4. get_or_create ProjectPaper com curation_status='pending'.
          5. Dispara materialize_project_links para promover vínculos.
          6. Retorna ProjectPaper serializado (201 se criado, 200 se existente).
        """
        serializer = AddPaperToProjectRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        pmid = serializer.validated_data['pmid']

        project = self._get_project()

        paper = get_object_or_404(Paper, pmid=pmid)

        project_paper, created = ProjectPaper.objects.get_or_create(
            project=project,
            paper=paper,
            defaults={'curation_status': ProjectPaper.CurationStatus.PENDING},
        )

        # Re-dispara materialização para promover o par órfão recém-completado.
        # Chamada síncrona — mesma convenção de run_pubmed_ingestion/run_omics_ingestion,
        # que chamam materialize_project_links diretamente (não via Celery) pois a
        # operação é puramente set-based no banco e retorna em < 1 ms para projetos normais.
        # Falha não derruba a resposta — o vínculo ProjectPaper já foi criado.
        try:
            from apps.core.services.link_service import materialize_project_links
            materialize_project_links(project.id)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error(
                'materialize_project_links falhou após add_from_suggestion (projeto %s, pmid %s): %s',
                project.id, pmid, exc,
            )

        response_serializer = ProjectPaperListSerializer(
            project_paper,
            context={'request': request},
        )
        http_status = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(response_serializer.data, status=http_status)
