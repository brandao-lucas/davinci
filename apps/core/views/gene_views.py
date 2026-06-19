"""
ViewSet de genes do projeto.

Endpoints:
    GET /projects/{project_pk}/genes/               → lista agregada paginada
    GET /projects/{project_pk}/genes/<gene_symbol>/ → detalhe com snippets

Rate protection (item 3 do 007):
    O endpoint de detalhe usa um lock leve no cache Django (LocMemCache em dev,
    configurável em prod) com TTL de 60s para evitar disparar a task Celery
    derive_gene_contexts repetidamente para o mesmo (project_id, gene_symbol)
    enquanto a derivação está em andamento. O lock é definido apenas quando a
    task é disparada e liberado naturalmente pelo TTL.
"""

import logging

from django.core.cache import cache
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, OpenApiParameter
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from apps.core.models import DaVinciProject, PaperGene
from apps.core.serializers.gene import (
    ProjectGeneListSerializer,
    ProjectGeneDetailSerializer,
)
from apps.core.services.gene_service import GeneService, GENE_SYMBOL_MAX_LEN

logger = logging.getLogger(__name__)

_VALID_ORDERINGS = {
    'unique_citations_included',
    '-unique_citations_included',
    'unique_citations_total',
    '-unique_citations_total',
    'mention_count_total',
    '-mention_count_total',
    'gene_symbol',
    '-gene_symbol',
}

# TTL do lock de derivação em segundos (item 3 do 007).
# Depois desse tempo, um novo GET pode re-disparar a task se ainda não houver cache.
_DERIVE_LOCK_TTL = 60


def _derive_lock_key(project_id: str, gene_symbol: str) -> str:
    """Chave de cache para o lock de derivação de contexto de gene."""
    return f'gene_derive_lock:{project_id}:{gene_symbol}'


class GenePagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class ProjectGeneViewSet(viewsets.GenericViewSet):
    """
    Genes agregados por projeto.

    Isolamento: _get_project() filtra por request.user — projeto de outro
    usuário retorna 404 (skill firebase-auth-guard).
    """

    # stub para drf-spectacular; nenhum queryset padrão usado em runtime
    queryset = PaperGene.objects.none()
    pagination_class = GenePagination

    def _get_project(self):
        return get_object_or_404(
            DaVinciProject,
            pk=self.kwargs['project_pk'],
            user=self.request.user,
        )

    # ------------------------------------------------------------------
    # GET /projects/{project_pk}/genes/
    # ------------------------------------------------------------------

    @extend_schema(
        summary="Lista genes do projeto (agregada)",
        description=(
            "Retorna lista paginada de genes mencionados nos papers do projeto, "
            "agrupados por gene_symbol. Inclui contagens de citações únicas "
            "(included e total) e soma de menções. "
            "Filtros: ?q= (icontains no símbolo), ?ordering= "
            "(unique_citations_included | unique_citations_total | "
            "mention_count_total | gene_symbol; prefixe com '-' para DESC), "
            "?included_only=true (omite genes sem nenhum paper incluído). "
            "Default: -unique_citations_included. Paginação: ?page=, ?page_size= (máx 100)."
        ),
        parameters=[
            OpenApiParameter(name='q', type=str, description='Filtro por símbolo (icontains).'),
            OpenApiParameter(
                name='ordering',
                type=str,
                description='Campo de ordenação. Valores válidos: unique_citations_included, '
                            'unique_citations_total, mention_count_total, gene_symbol '
                            '(prefixe com - para DESC).',
            ),
            OpenApiParameter(
                name='included_only',
                type=bool,
                description=(
                    'Quando true, retorna apenas genes com ao menos um paper '
                    'com curation_status=included (unique_citations_included > 0). '
                    'Aceita true/false/1/0. Default: false.'
                ),
            ),
        ],
        responses={200: ProjectGeneListSerializer(many=True)},
    )
    def list(self, request, project_pk=None):
        project = self._get_project()

        q = request.query_params.get('q', '').strip() or None
        ordering = request.query_params.get('ordering', '-unique_citations_included')
        if ordering not in _VALID_ORDERINGS:
            ordering = '-unique_citations_included'

        included_only_raw = request.query_params.get('included_only', '').lower()
        included_only = included_only_raw in ('true', '1')

        qs = GeneService.list_genes_for_project(
            project, q=q, included_only=included_only,
        ).order_by(ordering)

        # Paginação sobre ValuesQuerySet
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(qs, request, view=self)
        if page is not None:
            serializer = ProjectGeneListSerializer(page, many=True)
            return paginator.get_paginated_response(serializer.data)

        serializer = ProjectGeneListSerializer(qs, many=True)
        return Response(serializer.data)

    # ------------------------------------------------------------------
    # GET /projects/{project_pk}/genes/<gene_symbol>/
    # ------------------------------------------------------------------

    @extend_schema(
        summary="Detalhe de gene do projeto (com snippets de contexto)",
        description=(
            "Retorna métricas do gene e, para cada paper do projeto que o cita, "
            "as sentenças do abstract onde o gene aparece (snippets). "
            "Se o cache de snippets estiver frio ou stale para algum paper, "
            "uma task Celery é disparada e context_status='computing' é retornado; "
            "chamadas subsequentes retornam 'ready' quando o cache estiver pronto. "
            "gene_symbol inválido (> 64 chars) ou inexistente no projeto → 404."
        ),
        responses={200: ProjectGeneDetailSerializer},
    )
    @action(detail=False, methods=['get'], url_path=r'(?P<gene_symbol>[^/]+)')
    def gene_detail(self, request, project_pk=None, gene_symbol=None):
        # Item 4 (007): validar comprimento antes de qualquer regex sobre abstracts.
        if gene_symbol and len(gene_symbol) > GENE_SYMBOL_MAX_LEN:
            return Response(
                {'detail': 'gene_symbol excede o comprimento máximo permitido (64 caracteres).'},
                status=status.HTTP_404_NOT_FOUND,
            )

        project = self._get_project()

        detail = GeneService.get_gene_detail(project, gene_symbol)
        if detail is None:
            return Response(
                {'detail': f"Gene '{gene_symbol}' não encontrado nos papers deste projeto."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Item 3 (007): disparar task somente se não houver lock ativo para este
        # (project, gene_symbol). O lock previne reenfileiramento enquanto a
        # derivação está em andamento. cache.add() é atômico: retorna True apenas
        # se a chave não existia (add_or_nothing).
        if detail['context_status'] == 'computing':
            lock_key = _derive_lock_key(str(project.id), gene_symbol)
            if cache.add(lock_key, '1', timeout=_DERIVE_LOCK_TTL):
                from apps.core.tasks.gene_tasks import derive_gene_contexts
                derive_gene_contexts.delay(str(project.id), gene_symbol)
                logger.info(
                    'gene_detail: cache frio/stale — task disparada projeto=%s gene=%s',
                    project.id,
                    gene_symbol,
                )
            else:
                logger.debug(
                    'gene_detail: lock ativo — task já enfileirada projeto=%s gene=%s',
                    project.id,
                    gene_symbol,
                )

        serializer = ProjectGeneDetailSerializer(detail)
        return Response(serializer.data)
