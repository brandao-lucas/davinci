import logging

from django.contrib.postgres.search import SearchQuery
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.models import (
    DatasetFile,
    DaVinciProject,
    IngestionJob,
    OmicDataset,
    OmicSample,
    ProjectDataset,
)


def apply_dataset_filters(queryset, params):
    """
    Aplica filtros de listagem/bulk a um queryset de ProjectDataset.

    params: dict-like (ex.: request.query_params ou dict explícito do bulk body).

    Filtros disponíveis:
      curation_status  — valor exato de ProjectDataset.CurationStatus
      omic_type        — dataset__omic_type exato
      organism         — dataset__organism__icontains
      source_db        — dataset__source_db exato
      has_summary      — 'true' exclui datasets sem summary
      relevance_min    — relevance_score >= valor
      relevance_max    — relevance_score <= valor
      ingestion_job    — ingestion_job_id == valor (proveniência)
    """
    curation_status = params.get('curation_status')
    if curation_status:
        queryset = queryset.filter(curation_status=curation_status)

    omic_type = params.get('omic_type')
    if omic_type:
        queryset = queryset.filter(dataset__omic_type=omic_type)

    organism = params.get('organism')
    if organism:
        queryset = queryset.filter(dataset__organism__icontains=organism)

    source_db = params.get('source_db')
    if source_db:
        queryset = queryset.filter(dataset__source_db=source_db)

    if params.get('has_summary') == 'true':
        queryset = queryset.exclude(dataset__summary='')

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
from apps.core.serializers.dataset import (
    ProjectDatasetListSerializer,
    ProjectDatasetDetailSerializer,
    ProjectDatasetCurateSerializer,
    DatasetBulkCurateRequestSerializer,
    BulkCurateResponseSerializer,
)
from apps.core.serializers.download import (
    DatasetFileSerializer,
    DownloadDispatchRequestSerializer,
    DownloadDispatchResponseSerializer,
    DownloadQuotaPreviewSerializer,
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

        qs = apply_dataset_filters(qs, self.request.query_params)

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

        Body (por IDs):
          {"dataset_ids": [int, ...], "curation_status": "excluded",
           "exclusion_reason": "irrelevante"}

        Body (por filtro):
          {"filters": {"curation_status": "pending", "omic_type": "transcriptomic", ...},
           "curation_status": "excluded", "exclusion_reason": "irrelevante"}

        Exatamente um de dataset_ids ou filters deve estar presente.
        """
        dataset_ids = request.data.get('dataset_ids')
        filters = request.data.get('filters')
        new_status = request.data.get('curation_status')
        exclusion_reason = request.data.get('exclusion_reason', '')

        valid_statuses = [s.value for s in ProjectDataset.CurationStatus]
        if new_status not in valid_statuses:
            return Response(
                {'detail': f"Invalid status {new_status!r}. Choose from {valid_statuses}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if dataset_ids is not None and len(dataset_ids) == 0:
            return Response(
                {'detail': 'dataset_ids não pode ser uma lista vazia.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if dataset_ids is None and not filters:
            return Response(
                {'detail': 'Forneça dataset_ids ou filters.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        project = self._get_project()

        if dataset_ids is not None:
            qs = ProjectDataset.objects.filter(project=project, id__in=dataset_ids)
        else:
            qs = ProjectDataset.objects.filter(project=project)
            qs = apply_dataset_filters(qs, filters)

        update_kwargs = {
            'curation_status': new_status,
            'curated_at': timezone.now(),
        }
        # exclusion_reason: só sobrescreve se enviado explicitamente no body.
        # bulk de inclusão não apaga motivo anterior (curation-audit-trail).
        if 'exclusion_reason' in request.data:
            update_kwargs['exclusion_reason'] = exclusion_reason

        updated = qs.update(**update_kwargs)

        # Trigger sob demanda: dispara SAMPLE_FETCH para cada dataset incluído (curation-audit-trail).
        # Necessário apenas quando o update é por IDs (filtro por filter não tem lista explícita
        # de IDs, mas precisamos buscar os datasets incluídos para disparar o sample fetch).
        if new_status == ProjectDataset.CurationStatus.INCLUDED:
            if dataset_ids is not None:
                included_qs = ProjectDataset.objects.filter(
                    project=project, id__in=dataset_ids
                ).select_related('dataset')
            else:
                included_qs = ProjectDataset.objects.filter(project=project)
                included_qs = apply_dataset_filters(included_qs, filters).select_related('dataset')

            for pd in included_qs:
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
        request=DownloadDispatchRequestSerializer,
        responses={
            202: DownloadDispatchResponseSerializer,
            400: DownloadQuotaPreviewSerializer,
            409: DownloadQuotaPreviewSerializer,
            404: None,
        },
        summary="Iniciar download de arquivos do dataset",
        description=(
            "Enfileira o download dos arquivos ômicos do dataset.\n\n"
            "**Derivação de file_kind por source_db:**\n"
            "- `source_db='geo'` → `file_kind='geo_supplementary'` (padrão F1, MB).\n"
            "  Body pode ser vazio ou omitir `file_kind`.\n"
            "- `source_db='sra'` → `file_kind='fastq'` (F2, GB–TB). Exige "
            "  `confirm=true` no body; sem confirm retorna HTTP 400 com prévia de quota.\n\n"
            "**Quota (apenas FASTQ):**\n"
            "- Soma `DatasetFile.size_bytes` já baixados (`status='downloaded'`) do "
            "  projeto e compara com `DOWNLOAD_QUOTA_BYTES` (padrão: 200 GB).\n"
            "- Se excedida: HTTP 409 com `used_bytes` / `quota_bytes`.\n"
            "- Se `confirm=false`/ausente: HTTP 400 com prévia de quota (mesmo payload).\n\n"
            "**GEO supplementary (F1):** sem gate de confirm ou quota — fluxo simples.\n\n"
            "Idempotente: job ativo para o mesmo dataset retorna o existente (202).\n"
            "Progresso monitorável via GET /projects/{project_pk}/jobs/ com filtro "
            "?job_type=geo_supplementary_download ou ?job_type=fastq_download."
        ),
    )
    @action(detail=True, methods=['post'], url_path='download', throttle_scope='download')
    def download(self, request, project_pk=None, pk=None):
        """
        POST /projects/{project_pk}/datasets/{pk}/download/

        Body (opcional para GEO; obrigatório para SRA):
          {
            "file_kind": "fastq",   // opcional — derivado de source_db se omitido
            "confirm": true         // obrigatório para FASTQ (arquivo GB–TB)
          }

        Seta ProjectDataset.curation_status='queued' e despacha
        DownloadService.dispatch para enfileirar o job Celery.
        Retorna HTTP 202 com o IngestionJob criado/ativo.

        Erros:
          HTTP 400 — FASTQ sem confirm=true (retorna prévia de quota)
          HTTP 409 — quota de download excedida
          HTTP 404 — projeto ou dataset não pertence ao usuário
        """
        from apps.core.services.download_service import (
            DownloadService,
            FastqConfirmRequiredError,
            QuotaExceededError,
        )

        project = self._get_project()  # 404 se projeto não pertence ao user
        project_dataset = get_object_or_404(ProjectDataset, pk=pk, project=project)
        dataset = project_dataset.dataset

        # ── Derivação de file_kind por source_db ──────────────────────────────
        # O body pode sobrescrever, mas o comportamento padrão é derivado da fonte.
        # GEO → geo_supplementary (F1, sem quota/confirm)
        # SRA → fastq (F2, exige confirm + quota)
        # Outras fontes sem suporte ainda retornam 400 explícito.
        _SOURCE_FILE_KIND = {
            'geo': 'geo_supplementary',
            'sra': 'fastq',
        }

        body_serializer = DownloadDispatchRequestSerializer(data=request.data)
        body_serializer.is_valid(raise_exception=True)
        body_data = body_serializer.validated_data

        file_kind = body_data.get('file_kind') or _SOURCE_FILE_KIND.get(dataset.source_db)
        confirm = body_data.get('confirm', False)

        if file_kind is None:
            return Response(
                {
                    'detail': (
                        f"Download não suportado para source_db={dataset.source_db!r}. "
                        "Fontes suportadas: 'geo' (geo_supplementary), 'sra' (fastq)."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Valida consistência: file_kind='fastq' só faz sentido para SRA
        if file_kind == 'fastq' and dataset.source_db not in ('sra', 'ena'):
            return Response(
                {'detail': "file_kind='fastq' é válido apenas para source_db='sra'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Seta status agregado do ProjectDataset como 'queued'
        # (curation-audit-trail: não toca curated_at/exclusion_reason/notes)
        ProjectDataset.objects.filter(pk=project_dataset.pk).update(
            curation_status=ProjectDataset.CurationStatus.QUEUED_DOWNLOAD,
        )

        try:
            job = DownloadService.dispatch(
                project=project,
                dataset=dataset,
                file_kind=file_kind,
                user=request.user,
                confirm=confirm,
            )
        except FastqConfirmRequiredError as exc:
            # Retorna HTTP 400 com prévia de uso — cliente reenvia com confirm=true
            preview_serializer = DownloadQuotaPreviewSerializer({
                'detail': (
                    "Download FASTQ requer confirmação explícita. "
                    "Reenvie com confirm=true para confirmar o download."
                ),
                'file_kind': file_kind,
                'used_bytes': exc.used_bytes,
                'quota_bytes': exc.quota_bytes,
                'confirm_required': True,
            })
            return Response(preview_serializer.data, status=status.HTTP_400_BAD_REQUEST)
        except QuotaExceededError as exc:
            # Retorna HTTP 409 — quota esgotada, download bloqueado
            preview_serializer = DownloadQuotaPreviewSerializer({
                'detail': (
                    "Quota de download do projeto excedida. "
                    "Remova arquivos existentes ou contate o suporte."
                ),
                'file_kind': file_kind,
                'used_bytes': exc.used_bytes,
                'quota_bytes': exc.quota_bytes,
                'confirm_required': False,
            })
            return Response(preview_serializer.data, status=status.HTTP_409_CONFLICT)

        serializer = DownloadDispatchResponseSerializer(job)
        return Response(serializer.data, status=status.HTTP_202_ACCEPTED)

    @extend_schema(
        responses={200: DatasetFileSerializer(many=True)},
        summary="Listar arquivos do dataset",
        description=(
            "Lista os DatasetFile associados ao dataset, filtrados pelo projeto "
            "do usuário autenticado (Regra #3 — sem vazamento cross-project). "
            "Cada arquivo expõe uma `download_url` de proxy autenticado "
            "(nunca o storage_key cru nem URL pública do MinIO).\n\n"
            "Arquivos com download_status != 'downloaded' têm download_url=null."
        ),
    )
    @action(detail=True, methods=['get'], url_path='files', throttle_scope='download_content')
    def files(self, request, project_pk=None, pk=None):
        """
        GET /projects/{project_pk}/datasets/{pk}/files/

        Retorna DatasetFile do dataset, garantindo isolamento por user via
        validação do ProjectDataset (dataset deve estar no projeto do user).
        """
        project = self._get_project()  # 404 se projeto não pertence ao user
        project_dataset = get_object_or_404(ProjectDataset, pk=pk, project=project)
        dataset = project_dataset.dataset

        dataset_files = DatasetFile.objects.filter(dataset=dataset).order_by('created_at')
        serializer = DatasetFileSerializer(
            dataset_files,
            many=True,
            context={'request': request, 'view': self},
        )
        return Response(serializer.data)

    @extend_schema(
        responses={200: None},
        summary="Download autenticado do conteúdo de um arquivo",
        description=(
            "Proxy autenticado: valida isolamento por usuário/projeto e serve o "
            "conteúdo do arquivo via streaming a partir do object storage "
            "(default_storage). Nunca expõe storage_key nem URL pública do MinIO.\n\n"
            "Apenas arquivos com download_status='downloaded' são servidos (HTTP 200). "
            "Outros estados retornam HTTP 404 ou HTTP 409.\n\n"
            "Content-Disposition inclui o filename original. O path é derivado "
            "exclusivamente do storage_key do registro validado — nunca de input "
            "do cliente (sem path traversal)."
        ),
    )
    @action(detail=True, methods=['get'], url_path=r'files/(?P<file_id>[0-9]+)/content', throttle_scope='download_content')
    def file_content(self, request, project_pk=None, pk=None, file_id=None):
        """
        GET /projects/{project_pk}/datasets/{pk}/files/{file_id}/content/

        Proxy autenticado de conteúdo.
        Isolamento garantido: valida que o DatasetFile pertence ao dataset
        que pertence ao ProjectDataset do projeto do usuário autenticado.
        O storage_key vem apenas do registro do banco — nunca do cliente.
        """
        from django.core.files.storage import default_storage
        import os

        project = self._get_project()  # 404 se projeto não pertence ao user
        project_dataset = get_object_or_404(ProjectDataset, pk=pk, project=project)
        dataset = project_dataset.dataset

        # Valida que o arquivo pertence ao dataset validado acima
        dataset_file = get_object_or_404(DatasetFile, pk=file_id, dataset=dataset)

        if dataset_file.download_status != DatasetFile.DownloadStatus.DOWNLOADED:
            return Response(
                {'detail': f"Arquivo não disponível (status={dataset_file.download_status!r})."},
                status=status.HTTP_409_CONFLICT,
            )

        if not dataset_file.storage_key:
            return Response(
                {'detail': 'Arquivo sem storage_key — download incompleto.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # storage_key vem do banco (nunca do cliente): sem risco de path traversal
        storage_key = dataset_file.storage_key
        filename = os.path.basename(storage_key)

        try:
            f = default_storage.open(storage_key, 'rb')
        except Exception as exc:
            logger.error(
                'Falha ao abrir arquivo %s do object storage (DatasetFile %s): %s',
                storage_key,
                dataset_file.id,
                exc,
            )
            return Response(
                {'detail': 'Arquivo não encontrado no storage.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        response = StreamingHttpResponse(
            streaming_content=f,
            content_type='application/octet-stream',
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        if dataset_file.size_bytes:
            response['Content-Length'] = str(dataset_file.size_bytes)
        return response

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
