"""
ViewSet de termos MeSH do projeto.

Endpoints:
    GET /projects/{project_pk}/mesh/                 → lista agregada paginada
    GET /projects/{project_pk}/mesh/<descriptor>/    → detalhe com snippets

Rate protection:
    O endpoint de detalhe usa um lock leve no cache Django (LocMemCache em dev,
    configurável em prod) com TTL de 60s para evitar disparar a task Celery
    derive_mesh_contexts repetidamente para o mesmo (project_id, descriptor)
    enquanto a derivação está em andamento. O lock é definido apenas quando a
    task é disparada e liberado naturalmente pelo TTL.
"""

import hashlib
import logging
from urllib.parse import unquote, quote as url_quote

from django.core.cache import cache
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, OpenApiParameter
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from apps.core.models import DaVinciProject, PaperMeSHTerm
from apps.core.serializers.mesh import (
    ProjectMeSHListSerializer,
    ProjectMeSHDetailSerializer,
)
from apps.core.services.mesh_service import MeshService, MESH_DESCRIPTOR_MAX_LEN

logger = logging.getLogger(__name__)

_VALID_ORDERINGS = {
    'major_topic_count',
    '-major_topic_count',
    'unique_citations_included',
    '-unique_citations_included',
    'unique_citations_total',
    '-unique_citations_total',
    'descriptor',
    '-descriptor',
}

# TTL do lock de derivação em segundos.
# Depois desse tempo, um novo GET pode re-disparar a task se ainda não houver cache.
_DERIVE_LOCK_TTL = 60


def _derive_lock_key(project_id: str, descriptor: str) -> str:
    """Chave de cache para o lock de derivação de contexto de descriptor MeSH.

    O descriptor é hasheado (MD5, 16 hex chars) para evitar espaços e qualquer
    outro caractere especial que gere CacheKeyWarning com Memcached.
    A semântica do lock (por project_id + descriptor, TTL 60s) não muda.
    """
    descriptor_hash = hashlib.md5(descriptor.encode()).hexdigest()[:16]
    return f'mesh_derive_lock:{project_id}:{descriptor_hash}'


class MeSHPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class ProjectMeSHViewSet(viewsets.GenericViewSet):
    """
    Termos MeSH agregados por projeto.

    Isolamento: _get_project() filtra por request.user — projeto de outro
    usuário retorna 404 (skill firebase-auth-guard).
    """

    # stub para drf-spectacular; nenhum queryset padrão usado em runtime
    queryset = PaperMeSHTerm.objects.none()
    pagination_class = MeSHPagination

    def _get_project(self):
        return get_object_or_404(
            DaVinciProject,
            pk=self.kwargs['project_pk'],
            user=self.request.user,
        )

    # ------------------------------------------------------------------
    # GET /projects/{project_pk}/mesh/
    # ------------------------------------------------------------------

    @extend_schema(
        summary="Lista termos MeSH do projeto (agregada)",
        description=(
            "Retorna lista paginada de termos MeSH indexados nos papers do projeto, "
            "agrupados por descriptor. Inclui major_topic_count (métrica primária: "
            "papers distintos included onde is_major_topic=True), contagens de "
            "citações únicas (included e total) e URL NCBI MeSH. "
            "Filtros: ?q= (icontains no descriptor), ?ordering= "
            "(major_topic_count | unique_citations_included | unique_citations_total | "
            "descriptor; prefixe com '-' para DESC), "
            "?included_only=true (omite descriptors sem nenhum paper incluído). "
            "Default: -major_topic_count. Paginação: ?page=, ?page_size= (máx 100)."
        ),
        parameters=[
            OpenApiParameter(name='q', type=str, description='Filtro por descriptor (icontains).'),
            OpenApiParameter(
                name='ordering',
                type=str,
                description=(
                    'Campo de ordenação. Valores válidos: major_topic_count, '
                    'unique_citations_included, unique_citations_total, descriptor '
                    '(prefixe com - para DESC). Default: -major_topic_count.'
                ),
            ),
            OpenApiParameter(
                name='included_only',
                type=bool,
                description=(
                    'Quando true, retorna apenas descriptors com ao menos um paper '
                    'com curation_status=included (unique_citations_included > 0). '
                    'Aceita true/false/1/0. Default: false.'
                ),
            ),
        ],
        responses={200: ProjectMeSHListSerializer(many=True)},
    )
    def list(self, request, project_pk=None):
        project = self._get_project()

        q = request.query_params.get('q', '').strip() or None
        ordering = request.query_params.get('ordering', '-major_topic_count')
        if ordering not in _VALID_ORDERINGS:
            ordering = '-major_topic_count'

        included_only_raw = request.query_params.get('included_only', '').lower()
        included_only = included_only_raw in ('true', '1')

        qs = MeshService.list_mesh_for_project(
            project, q=q, included_only=included_only,
        ).order_by(ordering)

        # Paginação sobre ValuesQuerySet
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(qs, request, view=self)

        # Enriquecer cada item com ncbi_mesh_url (não vem da query anotada)
        if page is not None:
            enriched = [
                {**item, 'ncbi_mesh_url': f"https://www.ncbi.nlm.nih.gov/mesh/?term={url_quote(item['descriptor'])}"}
                for item in page
            ]
            serializer = ProjectMeSHListSerializer(enriched, many=True)
            return paginator.get_paginated_response(serializer.data)

        enriched = [
            {**item, 'ncbi_mesh_url': f"https://www.ncbi.nlm.nih.gov/mesh/?term={url_quote(item['descriptor'])}"}
            for item in qs
        ]
        serializer = ProjectMeSHListSerializer(enriched, many=True)
        return Response(serializer.data)

    # ------------------------------------------------------------------
    # GET /projects/{project_pk}/mesh/<descriptor>/
    # ------------------------------------------------------------------

    @extend_schema(
        summary="Detalhe de descriptor MeSH do projeto (com snippets de contexto)",
        description=(
            "Retorna métricas do descriptor MeSH e, para cada paper do projeto que o cita, "
            "as sentenças do abstract onde o descriptor aparece (snippets). "
            "Inclui qualifiers MeSH distintos entre os papers do projeto. "
            "Se o cache de snippets estiver frio ou stale para algum paper, "
            "uma task Celery é disparada e context_status='computing' é retornado; "
            "chamadas subsequentes retornam 'ready' quando o cache estiver pronto. "
            "NOTA: MeSH não garante presença literal do descriptor no abstract — "
            "zero snippets é comum e esperado (coberto pelo sentinela). "
            "descriptor inválido (> 255 chars) ou inexistente no projeto → 404."
        ),
        responses={200: ProjectMeSHDetailSerializer},
    )
    @action(detail=False, methods=['get'], url_path=r'(?P<descriptor>[^/]+)')
    def mesh_detail(self, request, project_pk=None, descriptor=None):
        # Decodificar descriptor URL-encoded (ex: 'Diabetes%20Mellitus' → 'Diabetes Mellitus')
        descriptor = unquote(descriptor) if descriptor else descriptor

        # Validar comprimento antes de qualquer regex sobre abstracts
        if descriptor and len(descriptor) > MESH_DESCRIPTOR_MAX_LEN:
            return Response(
                {'detail': f'descriptor excede o comprimento máximo permitido ({MESH_DESCRIPTOR_MAX_LEN} caracteres).'},
                status=status.HTTP_404_NOT_FOUND,
            )

        project = self._get_project()

        detail = MeshService.get_mesh_detail(project, descriptor)
        if detail is None:
            return Response(
                {'detail': f"Descriptor MeSH '{descriptor}' não encontrado nos papers deste projeto."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Disparar task somente se não houver lock ativo para este (project, descriptor).
        # O lock previne reenfileiramento enquanto a derivação está em andamento.
        # cache.add() é atômico: retorna True apenas se a chave não existia.
        if detail['context_status'] == 'computing':
            lock_key = _derive_lock_key(str(project.id), descriptor)
            if cache.add(lock_key, '1', timeout=_DERIVE_LOCK_TTL):
                from apps.core.tasks.mesh_tasks import derive_mesh_contexts
                derive_mesh_contexts.delay(str(project.id), descriptor)
                logger.info(
                    'mesh_detail: cache frio/stale — task disparada projeto=%s descriptor=%s',
                    project.id,
                    descriptor,
                )
            else:
                logger.debug(
                    'mesh_detail: lock ativo — task já enfileirada projeto=%s descriptor=%s',
                    project.id,
                    descriptor,
                )

        serializer = ProjectMeSHDetailSerializer(detail)
        return Response(serializer.data)
