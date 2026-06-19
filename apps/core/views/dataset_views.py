import logging

from django.contrib.postgres.search import SearchQuery
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.models import DaVinciProject, IngestionJob, OmicDataset, OmicSample, ProjectDataset
from apps.core.serializers.dataset import (
    ProjectDatasetListSerializer,
    ProjectDatasetDetailSerializer,
    ProjectDatasetCurateSerializer,
    DatasetBulkCurateRequestSerializer,
    BulkCurateResponseSerializer,
)
from apps.core.serializers.link import AddDatasetToProjectRequestSerializer
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
    # stub para drf-spectacular; get_queryset() prevalece em runtime
    queryset = ProjectDataset.objects.none()

    def _get_project(self):
        return get_object_or_404(
            DaVinciProject,
            pk=self.kwargs['project_pk'],
            user=self.request.user,
        )

    def get_queryset(self):
        project = self._get_project()
        qs = ProjectDataset.objects.filter(project=project).select_related('dataset')

        # Para detalhe: pré-carrega vínculos project-scoped para evitar N+1 no linked_papers.
        # O filtro por project_id é feito no serializer (Regra #3 — sem cross-project).
        if self.action == 'retrieve':
            qs = qs.prefetch_related(
                'projectpaperdataset_set__project_paper__paper',
            )

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

    @extend_schema(
        request=DatasetBulkCurateRequestSerializer,
        responses={200: BulkCurateResponseSerializer},
        summary="Curadoria em massa de datasets",
        description="Atualiza curation_status de múltiplos ProjectDatasets. Dispara SAMPLE_FETCH para os incluídos.",
    )
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

    @extend_schema(
        responses={200: ProjectDatasetListSerializer(many=True)},
        summary="Busca FTS em datasets do projeto",
        description="Busca full-text em datasets do projeto via search_vector. Parâmetro obrigatório: ?q=termo.",
    )
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

    @extend_schema(
        request=AddDatasetToProjectRequestSerializer,
        responses={200: ProjectDatasetListSerializer, 201: ProjectDatasetListSerializer},
        summary="Adicionar dataset ao projeto a partir de sugestão de órfão",
        description=(
            "Vincula um OmicDataset global existente ao projeto como ProjectDataset "
            "(curation_status='pending'). Idempotente: se o vínculo já existir, "
            "retorna o existente com HTTP 200. Criação nova retorna HTTP 201.\n\n"
            "Após criar o vínculo, dispara materialize_project_links para que a "
            "ponta recém-adicionada promova automaticamente o par órfão a "
            "ProjectPaperDataset(confidence='auto') (Nível 1).\n\n"
            "Request body: { \"dataset_id\": <int> }  — dataset_id vindo de "
            "GET /links/suggestions/ (campo OrphanLinkSuggestionSerializer.dataset_id)."
        ),
    )
    @action(detail=False, methods=['post'], url_path='add_from_suggestion')
    def add_from_suggestion(self, request, project_pk=None):
        """
        Adiciona dataset global (identificado por dataset_id / PK de OmicDataset) ao projeto.

        Fluxo:
          1. Valida request body { "dataset_id": <int> }.
          2. Resolve projeto via _get_project() — 404 se alheio (Regra #3).
          3. Busca OmicDataset global — 404 se inexistente.
          4. get_or_create ProjectDataset com curation_status='pending'.
          5. Dispara materialize_project_links para promover vínculos.
          6. Retorna ProjectDataset serializado (201 se criado, 200 se existente).
        """
        serializer = AddDatasetToProjectRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        dataset_id = serializer.validated_data['dataset_id']

        project = self._get_project()

        dataset = get_object_or_404(OmicDataset, pk=dataset_id)

        project_dataset, created = ProjectDataset.objects.get_or_create(
            project=project,
            dataset=dataset,
            defaults={'curation_status': ProjectDataset.CurationStatus.PENDING},
        )

        # Re-dispara materialização para promover o par órfão recém-completado.
        # Chamada síncrona — mesma convenção de run_pubmed_ingestion/run_omics_ingestion.
        # Falha não derruba a resposta — o vínculo ProjectDataset já foi criado.
        try:
            from apps.core.services.link_service import materialize_project_links
            materialize_project_links(project.id)
        except Exception as exc:
            logger.error(
                'materialize_project_links falhou após add_from_suggestion (projeto %s, dataset_id %s): %s',
                project.id, dataset_id, exc,
            )

        response_serializer = ProjectDatasetListSerializer(
            project_dataset,
            context={'request': request},
        )
        http_status = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(response_serializer.data, status=http_status)
