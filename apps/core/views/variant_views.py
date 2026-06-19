"""
ViewSet de variantes genéticas do projeto.

Endpoints:
    GET /projects/{project_pk}/variants/               → lista agregada paginada
    GET /projects/{project_pk}/variants/<rs_number>/   → detalhe com snippets

Rate protection:
    O endpoint de detalhe usa um lock leve no cache Django (LocMemCache em dev,
    configurável em prod) com TTL de 60s para evitar disparar a task Celery
    derive_variant_contexts repetidamente para o mesmo (project_id, rs_number)
    enquanto a derivação está em andamento. O lock é definido apenas quando a
    task é disparada e liberado naturalmente pelo TTL.

Chave canônica:
    rs_number (ex.: 'rs1801133'). Validado contra RS_NUMBER_MAX_LEN (32 chars)
    antes de qualquer regex sobre abstracts (anti-ReDoS, item 4 do 007).

Anotação (D5):
    VariantAnnotation é mesclada via in_bulk() pós-paginação na lista,
    evitando JOIN que inflaria o GROUP BY. No detalhe é consultada no service.
"""

import logging

from django.core.cache import cache
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, OpenApiParameter
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from apps.core.models import DaVinciProject, PaperVariant, VariantAnnotation
from apps.core.serializers.variant import (
    ProjectVariantListSerializer,
    ProjectVariantDetailSerializer,
)
from apps.core.services.variant_service import VariantService, RS_NUMBER_MAX_LEN

logger = logging.getLogger(__name__)

_VALID_ORDERINGS = {
    'unique_citations_included',
    '-unique_citations_included',
    'unique_citations_total',
    '-unique_citations_total',
    'mention_count_total',
    '-mention_count_total',
    'rs_number',
    '-rs_number',
}

# TTL do lock de derivação em segundos.
_DERIVE_LOCK_TTL = 60


def _derive_lock_key(project_id: str, rs_number: str) -> str:
    """Chave de cache para o lock de derivação de contexto de variante."""
    return f'variant_derive_lock:{project_id}:{rs_number}'


class VariantPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class ProjectVariantViewSet(viewsets.GenericViewSet):
    """
    Variantes genéticas agregadas por projeto.

    Isolamento: _get_project() filtra por request.user — projeto de outro
    usuário retorna 404 (skill firebase-auth-guard).
    """

    # stub para drf-spectacular; nenhum queryset padrão usado em runtime
    queryset = PaperVariant.objects.none()
    pagination_class = VariantPagination

    def _get_project(self):
        return get_object_or_404(
            DaVinciProject,
            pk=self.kwargs['project_pk'],
            user=self.request.user,
        )

    # ------------------------------------------------------------------
    # GET /projects/{project_pk}/variants/
    # ------------------------------------------------------------------

    @extend_schema(
        summary="Lista variantes do projeto (agregada)",
        description=(
            "Retorna lista paginada de variantes (rs numbers) mencionadas nos papers "
            "do projeto, agrupadas por rs_number. Inclui contagens de citações únicas "
            "(included e total), soma de menções e anotação clínica resumida (nullable). "
            "Filtros: ?q= (icontains no rs_number), ?ordering= "
            "(unique_citations_included | unique_citations_total | "
            "mention_count_total | rs_number; prefixe com '-' para DESC), "
            "?included_only=true (omite variantes sem nenhum paper incluído). "
            "Default: -unique_citations_included. Paginação: ?page=, ?page_size= (máx 100)."
        ),
        parameters=[
            OpenApiParameter(name='q', type=str, description='Filtro por rs_number (icontains).'),
            OpenApiParameter(
                name='ordering',
                type=str,
                description=(
                    'Campo de ordenação. Valores válidos: unique_citations_included, '
                    'unique_citations_total, mention_count_total, rs_number '
                    '(prefixe com - para DESC).'
                ),
            ),
            OpenApiParameter(
                name='included_only',
                type=bool,
                description=(
                    'Quando true, retorna apenas variantes com ao menos um paper '
                    'com curation_status=included (unique_citations_included > 0). '
                    'Aceita true/false/1/0. Default: false.'
                ),
            ),
        ],
        responses={200: ProjectVariantListSerializer(many=True)},
    )
    def list(self, request, project_pk=None):
        project = self._get_project()

        q = request.query_params.get('q', '').strip() or None
        ordering = request.query_params.get('ordering', '-unique_citations_included')
        if ordering not in _VALID_ORDERINGS:
            ordering = '-unique_citations_included'

        included_only_raw = request.query_params.get('included_only', '').lower()
        included_only = included_only_raw in ('true', '1')

        qs = VariantService.list_variants_for_project(
            project, q=q, included_only=included_only,
        ).order_by(ordering)

        # Paginação sobre ValuesQuerySet
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(qs, request, view=self)

        if page is not None:
            # D5: mesclar VariantAnnotation via in_bulk() sobre os rs_numbers da página
            # — evita JOIN que inflaria o GROUP BY.
            rs_numbers_on_page = [row['rs_number'] for row in page]
            annotations_map = VariantAnnotation.objects.in_bulk(rs_numbers_on_page)

            enriched = []
            for row in page:
                rs = row['rs_number']
                ann = annotations_map.get(rs)
                annotation_summary = None
                if ann:
                    annotation_summary = {
                        'gene_symbol': ann.gene_symbol,
                        'clinical_significance': ann.clinical_significance,
                        'chromosome': ann.chromosome,
                        'maf': ann.maf,
                    }
                enriched.append({**row, 'annotation': annotation_summary})

            serializer = ProjectVariantListSerializer(enriched, many=True)
            return paginator.get_paginated_response(serializer.data)

        # Fallback sem paginação (raro, mas mantém consistência)
        rows = list(qs)
        rs_numbers_all = [row['rs_number'] for row in rows]
        annotations_map = VariantAnnotation.objects.in_bulk(rs_numbers_all)
        enriched = []
        for row in rows:
            rs = row['rs_number']
            ann = annotations_map.get(rs)
            annotation_summary = None
            if ann:
                annotation_summary = {
                    'gene_symbol': ann.gene_symbol,
                    'clinical_significance': ann.clinical_significance,
                    'chromosome': ann.chromosome,
                    'maf': ann.maf,
                }
            enriched.append({**row, 'annotation': annotation_summary})

        serializer = ProjectVariantListSerializer(enriched, many=True)
        return Response(serializer.data)

    # ------------------------------------------------------------------
    # GET /projects/{project_pk}/variants/<rs_number>/
    # ------------------------------------------------------------------

    @extend_schema(
        summary="Detalhe de variante do projeto (com snippets de contexto)",
        description=(
            "Retorna métricas da variante, anotação clínica completa (nullable) "
            "e, para cada paper do projeto que a cita, as sentenças do abstract "
            "onde a variante aparece (snippets). "
            "Se o cache de snippets estiver frio ou stale para algum paper, "
            "uma task Celery é disparada e context_status='computing' é retornado; "
            "chamadas subsequentes retornam 'ready' quando o cache estiver pronto. "
            "rs_number inválido (> 32 chars) ou inexistente no projeto → 404."
        ),
        responses={200: ProjectVariantDetailSerializer},
    )
    @action(detail=False, methods=['get'], url_path=r'(?P<rs_number>[^/]+)')
    def variant_detail(self, request, project_pk=None, rs_number=None):
        # Anti-ReDoS: validar comprimento antes de qualquer regex sobre abstracts.
        if rs_number and len(rs_number) > RS_NUMBER_MAX_LEN:
            return Response(
                {'detail': f'rs_number excede o comprimento máximo permitido ({RS_NUMBER_MAX_LEN} caracteres).'},
                status=status.HTTP_404_NOT_FOUND,
            )

        project = self._get_project()

        detail = VariantService.get_variant_detail(project, rs_number)
        if detail is None:
            return Response(
                {'detail': f"Variante '{rs_number}' não encontrada nos papers deste projeto."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Disparar task somente se não houver lock ativo para este (project, rs_number).
        # cache.add() é atômico: retorna True apenas se a chave não existia.
        if detail['context_status'] == 'computing':
            lock_key = _derive_lock_key(str(project.id), rs_number)
            if cache.add(lock_key, '1', timeout=_DERIVE_LOCK_TTL):
                from apps.core.tasks.variant_tasks import derive_variant_contexts
                derive_variant_contexts.delay(str(project.id), rs_number)
                logger.info(
                    'variant_detail: cache frio/stale — task disparada projeto=%s rs_number=%s',
                    project.id,
                    rs_number,
                )
            else:
                logger.debug(
                    'variant_detail: lock ativo — task já enfileirada projeto=%s rs_number=%s',
                    project.id,
                    rs_number,
                )

        serializer = ProjectVariantDetailSerializer(detail)
        return Response(serializer.data)
