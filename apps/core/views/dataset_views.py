import csv
import io
import logging

from django.contrib.postgres.search import SearchQuery
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiTypes
from rest_framework import mixins, viewsets, status
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from apps.core.models import (
    DatasetFile,
    DaVinciProject,
    IngestionJob,
    OmicDataset,
    OmicSample,
    ProjectDataset,
    ProjectSample,
    SampleSraRun,
)


def apply_dataset_filters(queryset, params):
    """
    Aplica filtros de listagem/bulk a um queryset de ProjectDataset.

    params: dict-like (ex.: request.query_params ou dict explícito do bulk body).

    Filtros disponíveis:
      curation_status    — valor exato de ProjectDataset.CurationStatus
      omic_type          — dataset__omic_type exato
      organism           — dataset__organism__icontains
      source_db          — dataset__source_db exato
      has_summary        — 'true' exclui datasets sem summary
      relevance_min      — relevance_score >= valor
      relevance_max      — relevance_score <= valor
      ingestion_job      — ingestion_job_id == valor (proveniência)
      --- Contrato OmnisPathway (campos adicionados em migrations 0017/0018) ---
      has_control_group  — dataset__has_control_group exato (yes/no/unknown)
      disease_axis       — dataset__disease_axis exato (monogenic/multifactorial/indeterminate)
      is_single_cell     — dataset__is_single_cell exato (single_cell/bulk/unknown)
      data_format        — dataset__data_format exato (raw/processed/unknown)
      access_type        — dataset__access_type exato (public/controlled/unknown)
      omics_count_min    — dataset__omics_count >= valor (int)
      omics_count_max    — dataset__omics_count <= valor (int)
      omics_layer        — dataset__omics_layers contains [valor] (containment)
      has_sample_join_key — 'true' → dataset__sample_join_key não vazio
      --- Filtros de descoberta (Fase 4 OmnisPathway) ---
      disease_axis_confidence_min       — contract_confidence['disease_axis'] >= float
      has_control_group_confidence_min  — contract_confidence['has_control_group'] >= float
      is_single_cell_confidence_min     — contract_confidence['is_single_cell'] >= float
      sample_join_key_confidence_min    — contract_confidence['sample_join_key'] >= float
        Semântica: chave PRESENTE no JSON e score >= limiar.
        Datasets sem a chave NÃO passam (coerente com "não classificado" do contrato).
        Implementação: lookup ORM __<eixo>__gte sobre JSONField (transform numérico JSONB).
        Confirmado empiricamente que Django JSONB __<key>__gte faz cast para numeric no Postgres.
      gene                              — dataset__genes__gene_symbol__iexact + .distinct()
      monogenic_gene_hit                — 'true'/'True'/True → chave presente em
                                          extra_metadata['contract'] via
                                          __extra_metadata__contract__has_key='monogenic_gene_hit'
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

    # ── Filtros do contrato OmnisPathway ──────────────────────────────────────

    has_control_group = params.get('has_control_group')
    if has_control_group:
        queryset = queryset.filter(dataset__has_control_group=has_control_group)

    disease_axis = params.get('disease_axis')
    if disease_axis:
        queryset = queryset.filter(dataset__disease_axis=disease_axis)

    is_single_cell = params.get('is_single_cell')
    if is_single_cell:
        queryset = queryset.filter(dataset__is_single_cell=is_single_cell)

    data_format = params.get('data_format')
    if data_format:
        queryset = queryset.filter(dataset__data_format=data_format)

    access_type = params.get('access_type')
    if access_type:
        queryset = queryset.filter(dataset__access_type=access_type)

    omics_count_min = params.get('omics_count_min')
    if omics_count_min is not None:
        try:
            queryset = queryset.filter(dataset__omics_count__gte=int(omics_count_min))
        except (ValueError, TypeError):
            pass

    omics_count_max = params.get('omics_count_max')
    if omics_count_max is not None:
        try:
            queryset = queryset.filter(dataset__omics_count__lte=int(omics_count_max))
        except (ValueError, TypeError):
            pass

    omics_layer = params.get('omics_layer')
    if omics_layer:
        queryset = queryset.filter(dataset__omics_layers__contains=[omics_layer])

    has_sample_join_key = params.get('has_sample_join_key')
    if has_sample_join_key == 'true' or has_sample_join_key is True:
        queryset = queryset.filter(dataset__sample_join_key__len__gt=0)

    # ── Filtros de descoberta (Fase 4 OmnisPathway) ───────────────────────────
    # Filtros de confiança mínima via contract_confidence (JSONField).
    # Lookup __<eixo>__gte usa o transform numérico JSONB do Django/Postgres:
    # equivale a (contract_confidence->>'eixo')::numeric >= valor.
    # Semântica travada: chave AUSENTE → não passa (NOT NULL implícito pelo JSONB
    # que retorna NULL quando a chave não existe, e NULL >= valor é false no Postgres).
    _CONFIDENCE_AXES = {
        'disease_axis_confidence_min': 'disease_axis',
        'has_control_group_confidence_min': 'has_control_group',
        'is_single_cell_confidence_min': 'is_single_cell',
        'sample_join_key_confidence_min': 'sample_join_key',
    }
    for param_name, axis_key in _CONFIDENCE_AXES.items():
        min_val = params.get(param_name)
        if min_val is not None:
            try:
                threshold = float(min_val)
            except (ValueError, TypeError):
                continue
            # Filtra: chave presente E score >= threshold.
            # O lookup __<axis_key>__gte funciona sobre JSONField pois o Postgres
            # trata o valor JSONB numérico como numeric nas comparações.
            # Datasets sem a chave retornam NULL → não passam (comportamento correto).
            queryset = queryset.filter(
                **{f'dataset__contract_confidence__{axis_key}__gte': threshold}
            )

    # Filtro por gene: datasets que possuem DatasetGene com gene_symbol correspondente.
    # .distinct() evita duplicação quando múltiplas fontes (paper_link + dataset_metadata)
    # referenciam o mesmo gene no mesmo dataset.
    gene = params.get('gene')
    if gene:
        queryset = queryset.filter(
            dataset__genes__gene_symbol__iexact=gene
        ).distinct()

    # Filtro monogenic_gene_hit: 'true' → presença da chave em extra_metadata['contract'].
    # Usa lookup __has_key aninhado via path JSONB. O Postgres suporta:
    #   extra_metadata -> 'contract' @? '$.monogenic_gene_hit'  (alternativa interna)
    # porém o Django JSONField mapeia __<key>__has_key via KeyTransform + HasKey.
    # O lookup aninhado __<key1>__<key2> exige JSON path; usamos
    # __contract__has_key que o Django JSONField resolve como:
    #   (extra_metadata -> 'contract') ? 'monogenic_gene_hit'
    monogenic_gene_hit = params.get('monogenic_gene_hit')
    if monogenic_gene_hit == 'true' or monogenic_gene_hit is True:
        queryset = queryset.filter(
            dataset__extra_metadata__contract__has_key='monogenic_gene_hit'
        )

    return queryset
from apps.core.serializers.dataset import (
    ProjectDatasetListSerializer,
    ProjectDatasetDetailSerializer,
    ProjectDatasetCurateSerializer,
    DatasetBulkCurateRequestSerializer,
    BulkCurateResponseSerializer,
)
from apps.core.serializers.dataset_export import (
    DatasetExportItemSerializer,
    PaginatedDatasetExportSerializer,
)
from apps.core.serializers.download import (
    BatchDownloadQuotaPreviewSerializer,
    BatchDownloadRequestSerializer,
    BatchDownloadResponseSerializer,
    BatchFastqUrlListResponseSerializer,
    DatasetFileSerializer,
    DownloadDispatchRequestSerializer,
    DownloadDispatchResponseSerializer,
    DownloadQuotaPreviewSerializer,
    FastqUrlItemSerializer,
    FastqUrlListResponseSerializer,
    SraResolutionResponseSerializer,
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


def _resolve_fastq_urls_client(request, dataset, resolved_sample_ids):
    """
    Branch destination='client' — resolução síncrona de URLs públicas ENA (Inc-1).

    Chama rust_engine.resolve_fastq_urls, aplica o teto CLIENT_DOWNLOAD_MAX_RUNS
    e retorna HTTP 200 com FastqUrlListResponseSerializer.

    Contrato (sensitive-data-handling):
      - db_url e ncbi_api_key obtidos via build_db_url_and_api_key — NUNCA logados.
      - Resposta expõe apenas URLs públicas ENA e checksums — sem storage_key,
        sem credenciais, sem dados de outros projetos.

    Args:
        request: DRF Request com request.user autenticado.
        dataset: OmicDataset alvo (já validado como pertencente ao projeto do user).
        resolved_sample_ids: lista de OmicSample.id (ou None para todas as runs).

    Returns:
        DRF Response (200 ou 400).
    """
    import rust_engine
    from apps.core.services.download_service import (
        CLIENT_DOWNLOAD_MAX_RUNS,
        build_db_url_and_api_key,
    )

    # Teto de runs por resposta (D5) — evita resposta síncrona de centenas de MB.
    # Se scope='all' e resolved_sample_ids=None, o Rust vai buscar todas; a contagem
    # exata só é conhecida após a chamada. Preferimos deixar o Rust executar e depois
    # truncar, ou pré-verificar via contagem de OmicSample. Optamos por pré-verificar:
    # se a contagem de samples na seleção exceder o teto, rejeitamos antes de chamar.
    # Para scope='all' (None), contamos todos os samples do dataset.
    if resolved_sample_ids is None:
        sample_count = OmicSample.objects.filter(dataset=dataset).count()
    else:
        sample_count = len(resolved_sample_ids)

    if sample_count > CLIENT_DOWNLOAD_MAX_RUNS:
        return Response(
            {
                'detail': (
                    f"A seleção contém {sample_count} amostras, acima do teto de "
                    f"{CLIENT_DOWNLOAD_MAX_RUNS} runs por chamada destination='client'. "
                    "Refine o escopo (scope='included', 'manual' ou 'filter') ou use "
                    "destination='server' para seleções grandes."
                )
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Obtém credenciais — NUNCA logar (sensitive-data-handling)
    db_url, ncbi_api_key = build_db_url_and_api_key(request.user)

    try:
        raw_entries = rust_engine.resolve_fastq_urls(
            dataset_id=dataset.id,
            source_db=dataset.source_db,
            db_url=db_url,
            sample_ids=resolved_sample_ids,
            ncbi_api_key=ncbi_api_key,
        )
    except ImportError:
        return Response(
            {'detail': "rust_engine não instalado — compile com `maturin develop --release`."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    except Exception:
        logger.exception(
            'resolve_fastq_urls falhou para dataset %s',
            dataset.accession,
        )
        return Response(
            {'detail': 'Falha ao resolver URLs de download. Tente novamente.'},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    # Mapeia os objetos FastqUrlEntry retornados pelo Rust para dicts serializáveis.
    # FastqUrlEntry expõe: run_accession, fastq_url, file_name, size_bytes, checksum_md5
    runs = []
    bytes_total: int | None = 0
    bam_only_count = 0

    for entry in raw_entries:
        fastq_url: str = entry.fastq_url or ''
        has_public_fastq = bool(fastq_url)
        sz = entry.size_bytes  # int | None

        if sz is None:
            bytes_total = None  # sinal de "total indisponível"
        elif bytes_total is not None:
            bytes_total += sz

        if not has_public_fastq:
            bam_only_count += 1

        runs.append({
            'run_accession': entry.run_accession,
            'fastq_url': fastq_url,
            'file_name': entry.file_name or '',
            'size_bytes': sz,
            'checksum_md5': entry.checksum_md5,
            'has_public_fastq': has_public_fastq,
        })

    response_data = {
        'dataset_id': dataset.id,
        'dataset_accession': dataset.accession,
        'runs': runs,
        'total_runs': len(runs),
        'bytes_total': bytes_total,
        'bam_only_count': bam_only_count,
    }

    serializer = FastqUrlListResponseSerializer(response_data)
    return Response(serializer.data, status=status.HTTP_200_OK)


class _ExportPagination(PageNumberPagination):
    """
    Paginação para o endpoint de export de datasets (Fase 4 OmnisPathway).
    Espelha DiseaseAxisQueuePagination: page_size=50, max=200.
    """
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 200


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
        from django.db.models import Exists, OuterRef
        project = self._get_project()
        qs = ProjectDataset.objects.filter(project=project).select_related('dataset')

        # Para detalhe: pré-carrega vínculos project-scoped para evitar N+1 no linked_papers.
        # O filtro por project_id é feito no serializer (Regra #3 — sem cross-project).
        if self.action == 'retrieve':
            qs = qs.prefetch_related(
                'projectpaperdataset_set__project_paper__paper',
            )

        # Anota sra_resolved: True quando ao menos um SampleSraRun existe para
        # o dataset (MVP-B — ligação GEO→SRA resolvida via tabela estruturada).
        # Subquery Exists evita N+1 na listagem; o serializer lê a anotação diretamente.
        sra_resolved_subquery = Exists(
            SampleSraRun.objects.filter(
                sample__dataset_id=OuterRef('dataset_id'),
            )
        )
        qs = qs.annotate(sra_resolved=sra_resolved_subquery)

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
            200: FastqUrlListResponseSerializer,
            202: DownloadDispatchResponseSerializer,
            400: DownloadQuotaPreviewSerializer,
            409: DownloadQuotaPreviewSerializer,
            404: None,
        },
        summary="Iniciar download de arquivos do dataset",
        description=(
            "Enfileira o download (destination='server') ou resolve URLs diretas "
            "(destination='client') para os arquivos ômicos do dataset.\n\n"
            "**Derivação de file_kind por source_db:**\n"
            "- `source_db='geo'` → `file_kind='geo_supplementary'` (padrão F1, MB).\n"
            "  Body pode ser vazio ou omitir `file_kind`.\n"
            "- `source_db='sra'` ou `source_db='geo'` com runs SRA resolvidos → "
            "  `file_kind='fastq'` (F2, GB–TB).\n\n"
            "**Destino:**\n"
            "- `destination='server'` (padrão): enfileira Celery job → object storage → "
            "HTTP 202. Exige `confirm=true` para FASTQ. Sujeito a quota.\n"
            "- `destination='client'` (Inc-1): resolve URLs públicas ENA de forma "
            "síncrona → HTTP 200. Sem job, sem quota, sem DatasetFile. "
            "Apenas `file_kind='fastq'`. Teto: `CLIENT_DOWNLOAD_MAX_RUNS` runs "
            "(padrão 200) — se exceder retorna HTTP 400 orientativo.\n\n"
            "**Seleção de amostras:**\n"
            "- `scope='all'` (padrão): todas as runs do dataset.\n"
            "- `scope='included'`: só amostras com `curation_status='included'` no projeto.\n"
            "- `scope='manual'`: exatamente os `sample_ids` fornecidos (validados contra "
            "  o projeto do usuário — ids de outro projeto → 403/404).\n"
            "- `scope='filter'` (Inc-2): amostras que casam o objeto `filters`. "
            "  Filtros suportados: `curation_status` (list, IN), `organism` (icontains), "
            "`platform` (icontains). Requer campo `filters` preenchido.\n\n"
            "**GEO → FASTQ (MVP-B):** requer resolução SRA prévia via "
            "`POST .../resolve-sra/`. Sem resolução retorna HTTP 400 orientativo.\n\n"
            "**Quota (apenas FASTQ + destination='server'):**\n"
            "- Soma `DatasetFile.size_bytes` já baixados (`status='downloaded'`) do "
            "  projeto e compara com `DOWNLOAD_QUOTA_BYTES` (padrão: 200 GB).\n"
            "- Se excedida: HTTP 409 com `used_bytes` / `quota_bytes`.\n"
            "- Se `confirm=false`/ausente: HTTP 400 com prévia de quota.\n\n"
            "**GEO supplementary (F1):** sem gate de confirm ou quota — fluxo simples.\n\n"
            "Idempotente (server): job ativo para o mesmo dataset+scope+sample_ids "
            "retorna o existente (202).\n"
            "Progresso monitorável via GET /projects/{project_pk}/jobs/ com filtro "
            "?job_type=geo_supplementary_download ou ?job_type=fastq_download."
        ),
    )
    @action(detail=True, methods=['post'], url_path='download', throttle_scope='download')
    def download(self, request, project_pk=None, pk=None):
        """
        POST /projects/{project_pk}/datasets/{pk}/download/

        Body:
          {
            "file_kind": "fastq",       // opcional — derivado de source_db se omitido
            "confirm": true,            // obrigatório para FASTQ + destination='server'
            "scope": "included",        // "all" (default) | "included" | "manual" | "filter"
            "sample_ids": [12, 34],     // obrigatório se scope="manual"
            "filters": {"curation_status": ["included"], "organism": "homo"},  // obrigatório se scope="filter"
            "destination": "server"     // "server" (default) | "client"
          }

        destination='server':
          Seta ProjectDataset.curation_status='queued' e despacha Celery.
          Retorna HTTP 202 com o IngestionJob criado/ativo.

        destination='client':
          Chama rust_engine.resolve_fastq_urls de forma síncrona.
          Retorna HTTP 200 com lista de URLs públicas ENA por run.
          Não cria job, não consome quota, não grava DatasetFile.

        Erros:
          HTTP 400 — FASTQ (server) sem confirm=true
          HTTP 400 — GEO FASTQ sem sra_runs resolvidos
          HTTP 400 — destination='client' com geo_supplementary
          HTTP 400 — seleção excede CLIENT_DOWNLOAD_MAX_RUNS (client)
          HTTP 409 — quota de download excedida (server)
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
        # GEO → geo_supplementary (F1, sem quota/confirm)
        # SRA/ENA → fastq (F2, exige confirm + quota)
        # GEO com sra_runs resolvidos → fastq (MVP-B, exige confirm + quota)
        # Outras fontes retornam 400 explícito.
        _SOURCE_FILE_KIND = {
            'geo': 'geo_supplementary',
            'sra': 'fastq',
            'ena': 'fastq',
        }

        body_serializer = DownloadDispatchRequestSerializer(data=request.data)
        body_serializer.is_valid(raise_exception=True)
        body_data = body_serializer.validated_data

        file_kind = body_data.get('file_kind') or _SOURCE_FILE_KIND.get(dataset.source_db)
        confirm = body_data.get('confirm', False)
        scope = body_data.get('scope', 'all')
        destination = body_data.get('destination', 'server')
        requested_sample_ids = body_data.get('sample_ids')  # None ou lista de int
        requested_filters = body_data.get('filters')  # dict ou None; usado em scope='filter'

        if file_kind is None:
            return Response(
                {
                    'detail': (
                        f"Download não suportado para source_db={dataset.source_db!r}. "
                        "Fontes suportadas: 'geo' (geo_supplementary), 'sra'/'ena' (fastq)."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ── Validação de file_kind='fastq' para dataset GEO (MVP-B / Inc-3) ────
        # GEO nativo sem SampleSraRun → 400 orientativo.
        # GEO com SampleSraRun resolvidos → permitido (caminho MVP-B).
        # SRA/ENA → sempre permitido.
        if file_kind == 'fastq':
            if dataset.source_db == 'geo':
                has_resolved_runs = SampleSraRun.objects.filter(
                    sample__dataset=dataset,
                ).exists()
                if not has_resolved_runs:
                    return Response(
                        {
                            'detail': (
                                "Dataset GEO não possui runs SRA resolvidos. "
                                "Execute a resolução SRA antes de solicitar download FASTQ: "
                                f"POST /projects/{project_pk}/datasets/{pk}/resolve-sra/"
                            )
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            elif dataset.source_db not in ('sra', 'ena'):
                return Response(
                    {
                        'detail': (
                            f"file_kind='fastq' não é suportado para source_db={dataset.source_db!r}. "
                            "Suportados: 'sra', 'ena', e 'geo' com runs SRA resolvidos."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # ── Resolução de scope → sample_ids (firebase-auth-guard / Regra #3) ──
        # Toda resolução é feita contra ProjectSample do projeto do usuário autenticado.
        # NUNCA resolve ids fora do projeto — queryset sempre filtrado por project.
        resolved_sample_ids: list[int] | None = None

        if scope == 'all':
            # None → Rust usa WHERE dataset_id (comportamento original)
            resolved_sample_ids = None

        elif scope == 'included':
            # Ids dos OmicSample cujas ProjectSample têm curation_status='included'
            # no projeto do usuário autenticado (Regra #3 — sem cross-project).
            resolved_sample_ids = list(
                ProjectSample.objects.filter(
                    project=project,
                    sample__dataset=dataset,
                    curation_status=ProjectSample.CurationStatus.INCLUDED,
                ).values_list('sample_id', flat=True)
            )
            if not resolved_sample_ids:
                return Response(
                    {
                        'detail': (
                            "Nenhuma amostra com curation_status='included' encontrada "
                            "no projeto para este dataset. Cure as amostras antes de usar scope='included'."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        elif scope == 'manual':
            # sample_ids são ProjectSample.id (padrão da API — igual a bulk_curate de samples).
            # Valida pertencimento ao projeto/dataset e resolve para OmicSample.id internamente,
            # pois o Rust e o scope='included' operam com OmicSample.id.
            # Id de outro projeto → 404 (não vaza existência de ids alheios — Regra #3).
            if not requested_sample_ids:
                # O serializer já valida isso, mas defesa em profundidade.
                return Response(
                    {'detail': "sample_ids é obrigatório quando scope='manual'."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Filtra por ProjectSample.id (pk) do projeto+dataset do user autenticado.
            # values_list('sample_id') resolve diretamente para OmicSample.id.
            matched = ProjectSample.objects.filter(
                project=project,
                sample__dataset=dataset,
                id__in=requested_sample_ids,
            ).values_list('id', 'sample_id')

            matched_project_ids = {ps_id for ps_id, _ in matched}
            invalid = [sid for sid in requested_sample_ids if sid not in matched_project_ids]
            if invalid:
                return Response(
                    {
                        'detail': (
                            f"sample_ids {invalid} não pertencem ao projeto ou ao dataset. "
                            "Verifique os ids e tente novamente."
                        )
                    },
                    status=status.HTTP_404_NOT_FOUND,
                )
            # OmicSample.id para repassar ao Rust (consistente com scope='included')
            resolved_sample_ids = sorted({omic_id for _, omic_id in matched})

        elif scope == 'filter':
            # Resolve filtros → lista de OmicSample.id das amostras do dataset
            # que casam os critérios (curation_status in, organism icontains, platform icontains).
            # Isolamento Regra #3: base sempre filtrada por projeto+dataset do usuário autenticado.
            from apps.core.services.download_service import apply_sample_filters

            base_qs = ProjectSample.objects.filter(
                project=project,
                sample__dataset=dataset,
            )
            filtered_qs = apply_sample_filters(base_qs, requested_filters or {})
            resolved_sample_ids = list(
                filtered_qs.values_list('sample_id', flat=True)
            )
            if not resolved_sample_ids:
                return Response(
                    {
                        'detail': (
                            "Nenhuma amostra encontrada com os filtros fornecidos "
                            "no projeto para este dataset. Ajuste os filtros e tente novamente."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # ── Branch: destination='client' (Inc-1) ────────────────────────────
        # Resolução síncrona de URLs públicas ENA — sem job, sem quota, sem DatasetFile.
        # Isolamento garantido: project já validado via _get_project(); sample_ids
        # resolvidos acima contra ProjectSample do projeto (Regra #3).
        if destination == 'client':
            # geo_supplementary não tem URLs públicas por run na ENA — use server.
            if file_kind != 'fastq':
                return Response(
                    {
                        'detail': (
                            "destination='client' é suportado apenas para file_kind='fastq'. "
                            f"Este dataset usa file_kind={file_kind!r}. "
                            "Use destination='server' para GEO supplementary."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            return _resolve_fastq_urls_client(
                request=request,
                dataset=dataset,
                resolved_sample_ids=resolved_sample_ids,
            )

        # ── Branch: destination='server' (comportamento original) ────────────
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
                scope=scope,
                sample_ids=resolved_sample_ids,
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
        request=None,
        responses={
            202: SraResolutionResponseSerializer,
            400: None,
            404: None,
        },
        summary="Disparar resolução GSM→SRR para dataset GEO",
        description=(
            "Enfileira a resolução SRA para um dataset GEO (MVP-B).\n\n"
            "O Rust lê `OmicSample.extra_metadata['relation']` de cada GSM, "
            "extrai o SRX correspondente, resolve SRX→SRR via ENA filereport API "
            "e grava os runs em `extra_metadata['sra_runs']` de cada GSM — "
            "sem migration nova (usa JSONField existente, D1 aprovado).\n\n"
            "Após a resolução, o download FASTQ via `POST .../download/` passa a "
            "ser disponível para o dataset GEO.\n\n"
            "Apenas datasets com `source_db='geo'` são aceitos; outros retornam HTTP 400.\n\n"
            "Idempotente: job ativo para o mesmo dataset retorna o existente (202).\n"
            "Progresso monitorável via GET /projects/{project_pk}/jobs/{id}/."
        ),
    )
    @action(detail=True, methods=['post'], url_path='resolve-sra')
    def resolve_sra(self, request, project_pk=None, pk=None):
        """
        POST /projects/{project_pk}/datasets/{pk}/resolve-sra/

        Dispara resolução GSM→SRR para o dataset GEO.
        Retorna HTTP 202 com o IngestionJob criado/ativo.

        Erros:
          HTTP 400 — dataset não é GEO (source_db != 'geo')
          HTTP 404 — projeto ou dataset não pertence ao usuário
        """
        from apps.core.services.download_service import SraResolutionService

        project = self._get_project()  # 404 se projeto não pertence ao user
        project_dataset = get_object_or_404(ProjectDataset, pk=pk, project=project)
        dataset = project_dataset.dataset

        if dataset.source_db != 'geo':
            return Response(
                {
                    'detail': (
                        f"Resolução SRA é válida apenas para source_db='geo'. "
                        f"Este dataset tem source_db={dataset.source_db!r}."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        job = SraResolutionService.dispatch_resolution(
            project=project,
            dataset=dataset,
            user=request.user,
        )

        serializer = SraResolutionResponseSerializer(job)
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
        parameters=[
            OpenApiParameter('export_format', OpenApiTypes.STR, description="'json' (default, paginado) ou 'csv' (stream completo). Não use 'format' — é reservado pelo DRF para negociação de conteúdo."),
            OpenApiParameter('version', OpenApiTypes.STR, description="Versão do snapshot (default: 'live')."),
            OpenApiParameter('disease_axis_confidence_min', OpenApiTypes.FLOAT, description="Score mínimo de confiança para disease_axis."),
            OpenApiParameter('has_control_group_confidence_min', OpenApiTypes.FLOAT, description="Score mínimo de confiança para has_control_group."),
            OpenApiParameter('is_single_cell_confidence_min', OpenApiTypes.FLOAT, description="Score mínimo de confiança para is_single_cell."),
            OpenApiParameter('sample_join_key_confidence_min', OpenApiTypes.FLOAT, description="Score mínimo de confiança para sample_join_key."),
            OpenApiParameter('gene', OpenApiTypes.STR, description="Filtrar por gene_symbol (case-insensitive)."),
            OpenApiParameter('monogenic_gene_hit', OpenApiTypes.BOOL, description="True para datasets com monogenic_gene_hit presente."),
            OpenApiParameter('has_control_group', OpenApiTypes.STR, description="yes/no/unknown"),
            OpenApiParameter('disease_axis', OpenApiTypes.STR, description="monogenic/multifactorial/mixed/indeterminate"),
            OpenApiParameter('is_single_cell', OpenApiTypes.STR, description="single_cell/bulk/unknown — match exato em dataset__is_single_cell."),
            OpenApiParameter('data_format', OpenApiTypes.STR, description="raw/processed/unknown — match exato em dataset__data_format."),
            OpenApiParameter('omics_layer', OpenApiTypes.STR, description="Camada ômica (containment)."),
            OpenApiParameter('omics_count_min', OpenApiTypes.INT, description="Número mínimo de camadas ômicas."),
            OpenApiParameter('omics_count_max', OpenApiTypes.INT, description="Número máximo de camadas ômicas."),
            OpenApiParameter('has_sample_join_key', OpenApiTypes.BOOL, description="True para datasets com sample_join_key preenchido."),
            OpenApiParameter('access_type', OpenApiTypes.STR, description="public/controlled/unknown"),
        ],
        responses={200: PaginatedDatasetExportSerializer},
        summary="Exportar catálogo de datasets do projeto (JSON paginado ou CSV stream)",
        description=(
            "Entrega ponteiros + metadados + filtros do contrato §2 para todos os datasets "
            "do projeto que passam nos filtros especificados. **Nunca entrega matriz numérica** "
            "— matrix_pointer é sempre um ponteiro (URL/path/accession).\n\n"
            "Datasets com access_type='controlled' são marcados via access_controlled=True.\n\n"
            "**Formatos:**\n"
            "- `?export_format=json` (default): paginado (page_size=50, max=200), envelope DRF "
            "{count, next, previous, results} + bloco de proveniência no campo `provenance`.\n"
            "- `?export_format=csv`: StreamingHttpResponse sem paginação (dump completo), "
            "ordem de colunas fixa, campos multi-valor achatados com ';'.\n\n"
            "**Determinismo:** ordenado por accession. Mesma entrada → mesmo corpo.\n\n"
            "**Proveniência (JSON):** timestamp UTC, snapshot_version (?version=, default 'live'), "
            "versão das regras de classificação (Fases 2/3 OmnisPathway).\n\n"
            "Isolamento por usuário: apenas datasets do projeto do usuário autenticado "
            "(404 se projeto não pertence ao user)."
        ),
    )
    @action(detail=False, methods=['get'], url_path='export', throttle_scope='download_content')
    def export(self, request, project_pk=None):
        """
        GET /projects/{project_pk}/datasets/export/

        Exporta o catálogo de datasets do projeto com filtragem via apply_dataset_filters.
        Formatos: json (paginado) ou csv (stream completo).
        Determinismo: order_by('dataset__accession').
        Isolamento: _get_project() garante 404 cross-user (Regra #3).
        """
        from datetime import datetime as _datetime, timezone as _timezone

        project = self._get_project()  # 404 se projeto não pertence ao user

        snapshot_version = request.query_params.get('version', 'live')
        # Usa 'export_format' em vez de 'format': ?format= é o URL_FORMAT_OVERRIDE
        # do DRF e é interceptado na negociação de conteúdo antes de a view rodar.
        fmt = request.query_params.get('export_format', 'json').lower()

        # Queryset base: escopado ao projeto do usuário, com prefetch para evitar N+1 (R5).
        qs = (
            ProjectDataset.objects.filter(project=project)
            .select_related('dataset')
            .prefetch_related('dataset__genes')
        )

        # Aplica filtros de descoberta (reutiliza motor único — sem lógica paralela).
        qs = apply_dataset_filters(qs, request.query_params)

        # Ordenação determinística por accession (R4 — reprodutibilidade).
        qs = qs.order_by('dataset__accession')

        serializer_context = {
            'request': request,
            'snapshot_version': snapshot_version,
        }

        if fmt == 'csv':
            # ── Saída CSV: StreamingHttpResponse + .iterator() (R6 — sem carga em memória) ──
            # Sem paginação — dump completo do queryset filtrado.
            # Ordem de colunas fixa; campos multi-valor achatados com ';'.

            def _csv_rows():
                # Colunas fixas — ordem estável entre execuções
                COLUMNS = [
                    'accession', 'source_db', 'omics_layers', 'omics_count',
                    'is_single_cell', 'has_control_group', 'disease_axis',
                    'data_format', 'access_type', 'access_controlled',
                    'sample_join_key',
                    'contract_confidence',
                    # contract.* achatados
                    'contract.matrix_pointer', 'contract.proteomics_modality',
                    'contract.tissue_raw', 'contract.disease_raw',
                    'contract.ref_pmids', 'contract.ref_dois',
                    'contract.monogenic_gene_hit',
                    'snapshot_version',
                ]

                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(COLUMNS)
                yield buf.getvalue()

                for pd in qs.iterator(chunk_size=200):
                    s = DatasetExportItemSerializer(pd, context=serializer_context)
                    d = s.data
                    contract = d.get('contract') or {}
                    cc = d.get('contract_confidence') or {}

                    def _join(val):
                        """Achata lista/dict para string com ';'."""
                        if val is None:
                            return ''
                        if isinstance(val, list):
                            return ';'.join(str(v) for v in val)
                        if isinstance(val, dict):
                            return ';'.join(f"{k}={v}" for k, v in val.items())
                        return str(val)

                    row = [
                        d.get('accession', ''),
                        d.get('source_db', ''),
                        _join(d.get('omics_layers')),
                        d.get('omics_count', ''),
                        d.get('is_single_cell', ''),
                        d.get('has_control_group', ''),
                        d.get('disease_axis', ''),
                        d.get('data_format', ''),
                        d.get('access_type', ''),
                        d.get('access_controlled', ''),
                        _join(d.get('sample_join_key')),
                        _join(cc),
                        contract.get('matrix_pointer', ''),
                        contract.get('proteomics_modality', ''),
                        _join(contract.get('tissue_raw')),
                        _join(contract.get('disease_raw')),
                        _join(contract.get('ref_pmids')),
                        _join(contract.get('ref_dois')),
                        _join(contract.get('monogenic_gene_hit')),
                        d.get('snapshot_version', ''),
                    ]
                    buf = io.StringIO()
                    writer = csv.writer(buf)
                    writer.writerow(row)
                    yield buf.getvalue()

            response = StreamingHttpResponse(
                _csv_rows(),
                content_type='text/csv; charset=utf-8',
            )
            response['Content-Disposition'] = (
                f'attachment; filename="dataset_export_{snapshot_version}.csv"'
            )
            return response

        # ── Saída JSON: paginado com envelope DRF + bloco de proveniência ────
        paginator = _ExportPagination()
        page = paginator.paginate_queryset(qs, request, view=self)

        if page is not None:
            serializer = DatasetExportItemSerializer(
                page, many=True, context=serializer_context
            )
            paginated_response = paginator.get_paginated_response(serializer.data)
            # Injeta bloco de proveniência no envelope paginado
            paginated_response.data['provenance'] = {
                'timestamp_utc': _datetime.now(_timezone.utc).isoformat(),
                'snapshot_version': snapshot_version,
                'classifier_rules_version': 'omnispathway-fase2-fase3',
            }
            return paginated_response

        serializer = DatasetExportItemSerializer(
            qs, many=True, context=serializer_context
        )
        return Response({
            'provenance': {
                'timestamp_utc': _datetime.now(_timezone.utc).isoformat(),
                'snapshot_version': snapshot_version,
                'classifier_rules_version': 'omnispathway-fase2-fase3',
            },
            'results': serializer.data,
        })

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

    @extend_schema(
        request=BatchDownloadRequestSerializer,
        responses={
            200: BatchFastqUrlListResponseSerializer,
            202: BatchDownloadResponseSerializer,
            400: BatchDownloadQuotaPreviewSerializer,
            409: BatchDownloadQuotaPreviewSerializer,
            404: None,
        },
        summary="Download em lote: FASTQ de múltiplos datasets do projeto",
        description=(
            "Enfileira o download (destination='server') ou resolve URLs diretas "
            "(destination='client') para múltiplos datasets do projeto de uma vez.\n\n"
            "**Seleção de datasets:**\n"
            "- `dataset_ids` ausente/null → todos os datasets do projeto com ao menos "
            "  uma amostra resolvível no `scope` escolhido.\n"
            "- `dataset_ids` fornecido → apenas os datasets listados (validados contra "
            "  o projeto do usuário — ids de outro projeto → ignorados conforme Regra #3; "
            "404 se *nenhum* é válido).\n\n"
            "**Escopo de amostras por dataset:**\n"
            "- `scope='included'` (padrão): amostras com `curation_status='included'`.\n"
            "- `scope='all'`: todas as amostras do dataset.\n\n"
            "**Destino:**\n"
            "- `destination='server'` (padrão): despacha um IngestionJob por dataset → "
            "HTTP 202. Gate de quota FASTQ calculado sobre a soma estimada do lote ANTES "
            "de despachar qualquer job.\n"
            "- `destination='client'` (Inc-1): resolve URLs públicas ENA de forma síncrona "
            "por dataset e retorna HTTP 200 com lista agrupada. Teto agregado: "
            "`CLIENT_DOWNLOAD_MAX_RUNS` runs total (padrão 200). Sem job, sem quota.\n\n"
            "**Estratégia de jobs (server): um por dataset.**\n"
            "Idempotência, monitorabilidade e gate de quota por projeto mantidos.\n\n"
            "GEO supplementary (F1): sem gate de confirm/quota — despacha direto.\n\n"
            "Progresso monitorável via GET /projects/{project_pk}/jobs/ com filtro "
            "?job_type=fastq_download ou ?job_type=geo_supplementary_download."
        ),
    )
    @action(detail=False, methods=['post'], url_path='download-batch', throttle_scope='download')
    def download_batch(self, request, project_pk=None):
        """
        POST /projects/{project_pk}/datasets/download-batch/

        destination='server': despacha um IngestionJob por dataset alvo.
          Gate de quota FASTQ sobre a soma estimada do lote — se falhar, nenhum job criado.
          Retorna HTTP 202 com lista de jobs.

        destination='client': resolve URLs públicas ENA por dataset de forma síncrona.
          Retorna HTTP 200 com lista agrupada. Teto agregado CLIENT_DOWNLOAD_MAX_RUNS.

        Erros:
          HTTP 400 — FASTQ (server) sem confirm=true — body com prévia agregada.
          HTTP 400 — lote excede CLIENT_DOWNLOAD_MAX_RUNS (client).
          HTTP 409 — quota excedida (server) — body com prévia agregada.
          HTTP 404 — projeto não pertence ao usuário ou nenhum dataset válido.
        """
        from django.conf import settings
        from apps.core.models import SampleSraRun as _SampleSraRun
        from apps.core.services.download_service import (
            CLIENT_DOWNLOAD_MAX_RUNS,
            DownloadService,
            FastqConfirmRequiredError,
            QuotaExceededError,
            _project_used_bytes,
        )

        project = self._get_project()  # 404 se projeto não pertence ao user

        body_serializer = BatchDownloadRequestSerializer(data=request.data)
        body_serializer.is_valid(raise_exception=True)
        body_data = body_serializer.validated_data

        requested_dataset_ids = body_data.get('dataset_ids')  # None ou lista de int
        scope = body_data.get('scope', 'included')
        confirm = body_data.get('confirm', False)
        destination = body_data.get('destination', 'server')

        # ── Resolve conjunto de datasets alvo ────────────────────────────────
        # Sempre filtrado por projeto do usuário (Regra #3).
        base_pd_qs = ProjectDataset.objects.filter(project=project).select_related('dataset')
        if requested_dataset_ids:
            base_pd_qs = base_pd_qs.filter(dataset_id__in=requested_dataset_ids)

        target_project_datasets = list(base_pd_qs)

        if not target_project_datasets:
            return Response(
                {'detail': "Nenhum dataset encontrado no projeto com os critérios fornecidos."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # ── Mapeamento source_db → file_kind (mesmo padrão do single download) ─
        _SOURCE_FILE_KIND = {
            'geo': 'geo_supplementary',
            'sra': 'fastq',
            'ena': 'fastq',
        }

        # ── Resolve sample_ids por dataset e separa FASTQ dos GEO supp ────────
        # Para cada dataset: resolve as amostras conforme scope e decide file_kind.
        # Datasets sem amostras resolvíveis são listados em skipped_datasets.
        batch_items = []   # [(project_dataset, dataset, file_kind, sample_ids)]
        skipped_accessions = []

        for pd in target_project_datasets:
            dataset = pd.dataset
            file_kind = _SOURCE_FILE_KIND.get(dataset.source_db)
            if file_kind is None:
                skipped_accessions.append(dataset.accession)
                continue

            # Para GEO FASTQ, verifica resolução SRA (mesmo gate do single)
            if file_kind == 'fastq' and dataset.source_db == 'geo':
                has_runs = _SampleSraRun.objects.filter(
                    sample__dataset=dataset,
                ).exists()
                if not has_runs:
                    # GEO sem resolução: usa geo_supplementary como fallback
                    file_kind = 'geo_supplementary'

            if scope == 'included':
                sample_ids = list(
                    ProjectSample.objects.filter(
                        project=project,
                        sample__dataset=dataset,
                        curation_status=ProjectSample.CurationStatus.INCLUDED,
                    ).values_list('sample_id', flat=True)
                )
            else:  # 'all'
                sample_ids = list(
                    ProjectSample.objects.filter(
                        project=project,
                        sample__dataset=dataset,
                    ).values_list('sample_id', flat=True)
                )

            if not sample_ids:
                skipped_accessions.append(dataset.accession)
                continue

            batch_items.append((pd, dataset, file_kind, sample_ids))

        if not batch_items:
            return Response(
                {
                    'detail': (
                        "Nenhum dataset com amostras resolvíveis encontrado no escopo "
                        f"scope={scope!r}. Datasets ignorados: {skipped_accessions}."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ── Branch: destination='client' ─────────────────────────────────────
        # Resolve URLs por dataset de forma síncrona e agrega. Mesmo teto
        # CLIENT_DOWNLOAD_MAX_RUNS mas aplicado à soma de todos os datasets.
        if destination == 'client':
            total_sample_count = sum(len(sids) for _, _, _, sids in batch_items)
            if total_sample_count > CLIENT_DOWNLOAD_MAX_RUNS:
                return Response(
                    {
                        'detail': (
                            f"O lote contém {total_sample_count} amostras no total, "
                            f"acima do teto de {CLIENT_DOWNLOAD_MAX_RUNS} runs por chamada "
                            "destination='client'. Reduza dataset_ids, refine scope ou use "
                            "destination='server'."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Resolve por dataset, agrega resultados
            per_dataset_results = []
            batch_skipped = list(skipped_accessions)
            batch_bytes_total: int | None = 0
            batch_total_runs = 0

            for pd, ds, file_kind, sample_ids in batch_items:
                if file_kind != 'fastq':
                    # GEO supp sem URLs por run — pula com nota
                    batch_skipped.append(ds.accession)
                    continue

                resp = _resolve_fastq_urls_client(
                    request=request,
                    dataset=ds,
                    resolved_sample_ids=sample_ids if sample_ids else None,
                )
                if resp.status_code != status.HTTP_200_OK:
                    # Erro na resolução deste dataset — pula, não derruba o lote
                    logger.warning(
                        'BatchDownload client: falha na resolução de %s (status=%s) — ignorando.',
                        ds.accession,
                        resp.status_code,
                    )
                    batch_skipped.append(ds.accession)
                    continue

                ds_data = resp.data
                per_dataset_results.append(ds_data)
                batch_total_runs += ds_data.get('total_runs', 0)

                ds_bytes = ds_data.get('bytes_total')
                if ds_bytes is None:
                    batch_bytes_total = None
                elif batch_bytes_total is not None:
                    batch_bytes_total += ds_bytes

            batch_response = {
                'datasets': per_dataset_results,
                'total_datasets': len(per_dataset_results),
                'total_runs': batch_total_runs,
                'bytes_total': batch_bytes_total,
                'skipped_datasets': batch_skipped,
            }
            return Response(
                BatchFastqUrlListResponseSerializer(batch_response).data,
                status=status.HTTP_200_OK,
            )

        # ── Branch: destination='server' ─────────────────────────────────────
        # Gate de quota FASTQ — soma estimada do lote ANTES de despachar.
        # Apenas itens FASTQ entram no cálculo. GEO supp é isento (F1).
        fastq_items = [(pd, ds, fk, sids) for pd, ds, fk, sids in batch_items if fk == 'fastq']

        if fastq_items:
            quota_bytes = getattr(settings, 'DOWNLOAD_QUOTA_BYTES', 200 * 1024 ** 3)
            used_bytes = _project_used_bytes(project)

            # Estimativa: soma SampleSraRun.size_bytes das amostras selecionadas.
            # size_bytes pode ser None se o cache ENA não foi populado — tratamos como 0.
            fastq_sample_ids = []
            for _, _, _, sids in fastq_items:
                fastq_sample_ids.extend(sids)

            from django.db.models import Sum as _Sum
            estimated_bytes = (
                _SampleSraRun.objects.filter(sample_id__in=fastq_sample_ids)
                .aggregate(total=_Sum('size_bytes'))
            )['total'] or 0

            # Prévia por dataset para UX
            datasets_preview = [
                {
                    'dataset_accession': ds.accession,
                    'sample_count': len(sids),
                }
                for _, ds, fk, sids in batch_items
            ]

            if not confirm:
                preview_data = {
                    'detail': (
                        "Download FASTQ em lote requer confirmação explícita. "
                        "Reenvie com confirm=true para confirmar o download."
                    ),
                    'used_bytes': used_bytes,
                    'quota_bytes': quota_bytes,
                    'estimated_bytes': estimated_bytes,
                    'confirm_required': True,
                    'datasets_preview': datasets_preview,
                }
                return Response(
                    BatchDownloadQuotaPreviewSerializer(preview_data).data,
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if used_bytes + estimated_bytes > quota_bytes:
                logger.warning(
                    'BatchDownload: QuotaExceededError para projeto %s '
                    '(used=%s, estimated=%s, quota=%s)',
                    project.id, used_bytes, estimated_bytes, quota_bytes,
                )
                preview_data = {
                    'detail': (
                        "Quota de download do projeto excedida pelo lote estimado. "
                        "Remova arquivos existentes ou contate o suporte."
                    ),
                    'used_bytes': used_bytes,
                    'quota_bytes': quota_bytes,
                    'estimated_bytes': estimated_bytes,
                    'confirm_required': False,
                    'datasets_preview': datasets_preview,
                }
                return Response(
                    BatchDownloadQuotaPreviewSerializer(preview_data).data,
                    status=status.HTTP_409_CONFLICT,
                )

        # Gate passou: despacha um job por dataset.
        # DownloadService.dispatch é idempotente — job ativo retornado sem duplicar.
        created_jobs = []
        for pd, dataset, file_kind, sample_ids in batch_items:
            # Marca ProjectDataset como queued (curation-audit-trail)
            ProjectDataset.objects.filter(pk=pd.pk).update(
                curation_status=ProjectDataset.CurationStatus.QUEUED_DOWNLOAD,
            )
            try:
                job = DownloadService.dispatch(
                    project=project,
                    dataset=dataset,
                    file_kind=file_kind,
                    user=request.user,
                    confirm=confirm,
                    scope=scope,
                    sample_ids=sorted(sample_ids),
                )
                created_jobs.append(job)
            except (FastqConfirmRequiredError, QuotaExceededError):
                # Não deve acontecer: gate verificado acima — bug se chegar aqui.
                logger.error(
                    'BatchDownload: quota/confirm error inesperado em dataset %s '
                    'após gate de lote — ignorando.',
                    dataset.accession,
                )
                skipped_accessions.append(dataset.accession)
            except Exception as exc:
                logger.error(
                    'BatchDownload: falha ao despachar job para dataset %s: %s',
                    dataset.accession,
                    exc,
                )
                skipped_accessions.append(dataset.accession)

        response_data = {
            'jobs': created_jobs,
            'total_jobs': len(created_jobs),
            'skipped_datasets': skipped_accessions,
        }
        return Response(
            BatchDownloadResponseSerializer(response_data).data,
            status=status.HTTP_202_ACCEPTED,
        )
