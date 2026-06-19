import csv
import hashlib
import io
import logging
import uuid

from django.core.cache import cache
from django.db import IntegrityError
from django.db.models import Count
from django.http import JsonResponse, StreamingHttpResponse
from django.utils.text import slugify
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from apps.core.models import DaVinciProject, ProjectPaper
from apps.core.serializers.project import (
    DaVinciProjectSerializer,
    JobDispatchResponseSerializer,
    MagnitudePreviewSerializer,
    MeshSuggestRequestSerializer,
    MeshSuggestionSerializer,
    OmicsSearchRequestSerializer,
    SearchPreviewRequestSerializer,
)
from apps.core.serializers.stats import ProjectStatsSerializer
from apps.core.services.query_builder import build_pubmed_query, _build_mesh_block
from apps.core.services.search_service import SearchService
from apps.core.services.stats_service import StatsService

logger = logging.getLogger(__name__)

# TTL do cache para o preview de magnitude (segundos).
# Queries idênticas dentro deste intervalo não re-chamam o Rust.
_PREVIEW_CACHE_TTL = 120


class DaVinciProjectViewSet(viewsets.ModelViewSet):
    serializer_class = DaVinciProjectSerializer
    # queryset stub para que o drf-spectacular consiga inspecionar o modelo
    # sem disparar _get_project(); get_queryset() prevalece em runtime.
    queryset = DaVinciProject.objects.none()

    # Actions que disparam I/O externo caro (NCBI) — throttle por escopo 'preview'.
    # ScopedRateThrottle lê self.throttle_scope em get_throttles(); sobrescrevemos
    # o método para aplicar o scope apenas nas duas actions, sem afetar as demais.
    _THROTTLED_ACTIONS = frozenset({'mesh_suggest', 'search_preview'})

    def get_throttles(self):
        if getattr(self, 'action', None) in self._THROTTLED_ACTIONS:
            self.throttle_scope = 'preview'
            return [ScopedRateThrottle()]
        return super().get_throttles()

    def get_queryset(self):
        return DaVinciProject.objects.filter(user=self.request.user).annotate(
            total_papers=Count('project_papers', distinct=True),
            total_datasets=Count('project_datasets', distinct=True),
        )

    def perform_create(self, serializer):
        title = serializer.validated_data.get('title', 'project')
        base_slug = slugify(f"{title}-{self.request.user.username}-davinci")
        slug = base_slug
        for attempt in range(6):
            try:
                if attempt > 0:
                    slug = f"{base_slug}-{uuid.uuid4().hex[:6]}"
                serializer.save(user=self.request.user, slug=slug)
                return
            except IntegrityError:
                continue
        raise IntegrityError(f"Could not generate unique slug for '{title}'")

    @extend_schema(
        request=None,
        responses={202: JobDispatchResponseSerializer},
        summary="Disparar busca PubMed",
        description="Cria e enfileira um job de busca de literatura no PubMed para o projeto.",
    )
    @action(detail=True, methods=['post'])
    def search(self, request, pk=None):
        """Dispara busca no PubMed."""
        project = self.get_object()
        job = SearchService.dispatch_pubmed_search(project, user=request.user)
        return Response(
            {'job_id': str(job.id), 'status': job.status},
            status=status.HTTP_202_ACCEPTED,
        )

    @extend_schema(
        request=OmicsSearchRequestSerializer,
        responses={202: JobDispatchResponseSerializer},
        summary="Disparar busca ômica",
        description="Cria e enfileira um job de busca em GEO/SRA/BioProject/GWAS para o projeto.",
    )
    @action(detail=True, methods=['post'], url_path='omics_search')
    def omics_search(self, request, pk=None):
        """Dispara busca em GEO/SRA/BioProject/GWAS."""
        project = self.get_object()
        sources = request.data.get('sources', None)
        max_per_source = request.data.get('max_per_source', 500)
        job = SearchService.dispatch_omics_search(
            project, sources=sources, max_per_source=max_per_source, user=request.user
        )
        return Response(
            {'job_id': str(job.id), 'status': job.status},
            status=status.HTTP_202_ACCEPTED,
        )

    @extend_schema(
        responses={200: ProjectStatsSerializer},
        summary="Estatísticas do projeto",
        description="Retorna ProjectStats calculadas on-the-fly se ausentes.",
    )
    @action(detail=True, methods=['get'])
    def stats(self, request, pk=None):
        """Return ProjectStats, computing on the fly if missing."""
        project = self.get_object()
        project_stats = StatsService.compute_and_save(project)
        serializer = ProjectStatsSerializer(project_stats)
        return Response(serializer.data)

    @extend_schema(
        request=MeshSuggestRequestSerializer,
        responses={200: MeshSuggestionSerializer(many=True)},
        summary="Sugerir descritores MeSH para o projeto",
        description=(
            "Chama rust_engine.mesh_suggest para sugerir descritores MeSH a partir de um "
            "termo livre. Se 'term' estiver ausente ou vazio, usa a query_term + "
            "query_synonyms do projeto como termo de busca. "
            "A NCBI API key do perfil do usuário é usada internamente — nunca retornada. "
            "Isolamento: só o dono do projeto pode chamar este endpoint."
        ),
    )
    @action(detail=True, methods=['post'], url_path='mesh/suggest')
    def mesh_suggest(self, request, pk=None):
        """Sugere descritores MeSH via rust_engine.mesh_suggest."""
        project = self.get_object()

        serializer = MeshSuggestRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        term = serializer.validated_data.get('term', '').strip()
        if not term:
            # Usa os termos do projeto como fallback
            parts = [project.query_term] + (project.query_synonyms or [])
            term = ' OR '.join(p for p in parts if p)

        # Obtém NCBI key do perfil — nunca retornada nem logada
        ncbi_key = None
        try:
            ncbi_key = request.user.profile.ncbi_api_key or None
        except Exception:
            pass

        try:
            import rust_engine
            suggestions = rust_engine.mesh_suggest(term, ncbi_key)
        except Exception as exc:
            logger.error('mesh_suggest: erro no rust_engine: %s', exc)
            return Response(
                {'detail': 'Erro ao consultar sugestões MeSH. Tente novamente em instantes.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        data = [
            {
                'descriptor': s.descriptor,
                'ui': s.ui,
                'tree_numbers': s.tree_numbers,
                'scope_note': s.scope_note,
                'allowable_qualifiers': s.allowable_qualifiers,
                'pubmed_count': s.pubmed_count,
            }
            for s in suggestions
        ]
        out_serializer = MeshSuggestionSerializer(data, many=True)
        return Response(out_serializer.data)

    @extend_schema(
        request=SearchPreviewRequestSerializer,
        responses={200: MagnitudePreviewSerializer},
        summary="Preview de magnitude da query PubMed",
        description=(
            "Monta a query PubMed a partir da configuração MeSH enviada no body "
            "(selected_mesh, mesh_default_mode, panel_flags) e retorna contagens "
            "comparativas via rust_engine.pubmed_magnitude_preview. "
            "Operação READ-ONLY: não cria IngestionJob, não altera o projeto. "
            "Cache curto (120s) por hash da query + flags para evitar re-chamadas "
            "ao Rust enquanto o usuário faz ajustes. "
            "A NCBI API key do perfil do usuário é usada internamente — nunca retornada. "
            "Isolamento: só o dono do projeto pode chamar este endpoint."
        ),
    )
    @action(detail=True, methods=['post'], url_path='search/preview')
    def search_preview(self, request, pk=None):
        """Preview de magnitude via rust_engine.pubmed_magnitude_preview. Read-only."""
        project = self.get_object()

        req_serializer = SearchPreviewRequestSerializer(data=request.data)
        req_serializer.is_valid(raise_exception=True)
        vd = req_serializer.validated_data

        # Monta um projeto sintético temporário para o builder, aplicando os
        # overrides do body (sem persistir no banco — preview é read-only).
        class _ProjectProxy:
            """Proxy leve que aplica overrides do body sobre os campos do projeto."""
            def __init__(self, base, overrides):
                self._base = base
                self._overrides = overrides

            def __getattr__(self, name):
                if name in self._overrides:
                    return self._overrides[name]
                return getattr(self._base, name)

        selected_mesh = vd.get('selected_mesh')
        mesh_default_mode = vd.get('mesh_default_mode', project.mesh_default_mode)

        overrides = {}
        if selected_mesh is not None:
            overrides['selected_mesh'] = selected_mesh
            overrides['advanced_search_enabled'] = bool(selected_mesh)
        if mesh_default_mode:
            overrides['mesh_default_mode'] = mesh_default_mode

        proxy = _ProjectProxy(project, overrides)
        query = build_pubmed_query(proxy)

        # Flags de painel
        flags = vd.get('panel_flags') or {}
        flag_by_year = bool(flags.get('by_year', False))
        flag_by_pub_type = bool(flags.get('by_pub_type', False))
        flag_open_access = bool(flags.get('open_access', False))
        year_buckets = flags.get('year_buckets') or None

        # Cache por hash(query + flags) para não re-chamar o Rust em loops de debounce
        cache_key_raw = (
            f'preview:{project.id}:{query}:'
            f'{flag_by_year}:{flag_by_pub_type}:{flag_open_access}:{year_buckets}'
        )
        cache_key = 'search_preview:' + hashlib.sha256(cache_key_raw.encode()).hexdigest()[:32]
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        # NCBI key — nunca retornada nem logada
        ncbi_key = None
        try:
            ncbi_key = request.user.profile.ncbi_api_key or None
        except Exception:
            pass

        # Monta mesh_terms no formato que o Rust espera: list[tuple[str, str]]
        # onde cada item é (mesh_term_string_pronto_pra_pubmed, "and"|"or")
        mesh_default = proxy.mesh_default_mode or 'and'
        mesh_terms = []
        for entry in (proxy.selected_mesh or []):
            block = _build_mesh_block(entry)
            if block:
                mode = entry.get('mode') or mesh_default
                mesh_terms.append((block, mode))

        try:
            import rust_engine
            preview = rust_engine.pubmed_magnitude_preview(
                free_text=query,
                mesh_terms=mesh_terms,
                date_from=project.date_from,
                date_to=project.date_to,
                ncbi_api_key=ncbi_key,
                flag_by_year=flag_by_year,
                flag_by_pub_type=flag_by_pub_type,
                flag_open_access=flag_open_access,
                year_buckets=year_buckets,
            )
        except Exception as exc:
            logger.error('search_preview: erro no rust_engine: %s', exc)
            return Response(
                {'detail': 'Erro ao calcular preview de magnitude. Tente novamente em instantes.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        result = {
            'free_text_count': preview.free_text_count,
            'mesh_count': preview.mesh_count,
            'combined_count': preview.combined_count,
            'overlap': preview.overlap,
            'only_free_text': preview.only_free_text,
            'only_mesh': preview.only_mesh,
            'not_yet_indexed': preview.not_yet_indexed,
            'reviews': preview.reviews,
            'systematic_reviews': preview.systematic_reviews,
            'by_year': list(preview.by_year) if flag_by_year else [],
            'by_pub_type': list(preview.by_pub_type) if flag_by_pub_type else [],
            'open_access': list(preview.open_access) if flag_open_access else None,
            'query_used': query,
        }

        cache.set(cache_key, result, _PREVIEW_CACHE_TTL)
        return Response(result)

    @extend_schema(
        responses={
            200: OpenApiResponse(description="JSON com papers incluídos"),
            200: OpenApiResponse(description="CSV com papers incluídos (quando export_format=csv)"),
        },
        summary="Exportar papers incluídos",
        description="Exporta papers com curation_status=included em JSON (padrão) ou CSV (?export_format=csv).",
    )
    @action(detail=True, methods=['get'])
    def export(self, request, pk=None):
        """
        Export included papers as JSON or CSV.

        Query param: ?format=json (default) | ?format=csv
        """
        project = self.get_object()
        export_format = request.query_params.get('export_format', 'json')

        included = (
            ProjectPaper.objects.filter(
                project=project,
                curation_status=ProjectPaper.CurationStatus.INCLUDED,
            )
            .select_related('paper')
            .prefetch_related(
                'paper__authors', 'paper__genes', 'paper__drugs',
                'paper__mesh_terms', 'paper__contexts',
                'clinical_categories', 'user_categories',
            )
        )

        if export_format == 'csv':
            return _export_csv(project, included)

        return _export_json(project, included)


# ── Export helpers ────────────────────────────────────────────────────────────

def _export_json(project, included_qs):
    data = {
        'project': project.title,
        'query_term': project.query_term,
        'papers': [],
    }
    for pp in included_qs:
        p = pp.paper
        data['papers'].append({
            'pmid': p.pmid,
            'title': p.title,
            'abstract': p.abstract,
            'journal': p.journal,
            'pub_year': p.pub_year,
            'curation_notes': pp.notes,
            'clinical_categories': [c.slug for c in pp.clinical_categories.all()],
            'user_categories': [c.name for c in pp.user_categories.all()],
            'genes': [
                {'symbol': g.gene_symbol, 'mentions': g.mention_count}
                for g in p.genes.all()
            ],
            'drugs': [
                {'name': d.drug_name, 'mentions': d.mention_count}
                for d in p.drugs.all()
            ],
            'mesh_terms': [
                m.descriptor for m in p.mesh_terms.filter(is_major_topic=True)
            ],
            'contexts': [
                {
                    'entity_type': c.entity_type,
                    'entity_name': c.entity_name,
                    'sentence': c.sentence,
                }
                for c in p.contexts.all()
            ],
        })
    return JsonResponse(data, json_dumps_params={'ensure_ascii': False})


def _export_csv(project, included_qs):
    def rows():
        header = ['pmid', 'title', 'journal', 'pub_year', 'clinical_categories', 'notes']
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(header)
        yield buf.getvalue()
        for pp in included_qs:
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow([
                pp.paper.pmid,
                pp.paper.title,
                pp.paper.journal,
                pp.paper.pub_year,
                '|'.join(c.slug for c in pp.clinical_categories.all()),
                pp.notes,
            ])
            yield buf.getvalue()

    response = StreamingHttpResponse(rows(), content_type='text/csv')
    response['Content-Disposition'] = (
        f'attachment; filename="{slugify(project.title)}_papers.csv"'
    )
    return response
