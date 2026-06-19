"""
ViewSet de medicamentos do projeto.

Endpoints:
    GET /projects/{project_pk}/drugs/                    → lista agregada paginada
    GET /projects/{project_pk}/drugs/<drug_name_lower>/  → detalhe com snippets

Rate protection:
    O endpoint de detalhe usa um lock leve no cache Django (LocMemCache em dev,
    configurável em prod) com TTL de 60s para evitar disparar a task Celery
    derive_drug_contexts repetidamente para o mesmo (project_id, drug_name_lower)
    enquanto a derivação está em andamento. O lock é definido apenas quando a
    task é disparada e liberado naturalmente pelo TTL.

Chave canônica:
    drug_name_lower é a chave de agrupamento e de lookup. O front-end deve
    encodeURIComponent() o valor antes de inserir na URL (espaços → %20, etc.).
"""

import hashlib
import logging
from urllib.parse import unquote

from django.core.cache import cache
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, OpenApiParameter
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from apps.core.models import DaVinciProject, PaperDrug
from apps.core.serializers.drug import (
    ProjectDrugListSerializer,
    ProjectDrugDetailSerializer,
)
from apps.core.services.drug_service import DrugService, DRUG_NAME_MAX_LEN

logger = logging.getLogger(__name__)

_VALID_ORDERINGS = {
    'unique_citations_included',
    '-unique_citations_included',
    'unique_citations_total',
    '-unique_citations_total',
    'mention_count_total',
    '-mention_count_total',
    'drug_name',
    '-drug_name',
}

# TTL do lock de derivação em segundos.
# Depois desse tempo, um novo GET pode re-disparar a task se ainda não houver cache.
_DERIVE_LOCK_TTL = 60


def _derive_lock_key(project_id: str, drug_name_lower: str) -> str:
    """Chave de cache para o lock de derivação de contexto de medicamento.

    O drug_name_lower é hasheado (MD5, 16 hex chars) para evitar espaços e
    qualquer outro caractere especial que gere CacheKeyWarning com Memcached.
    A semântica do lock (por project_id + drug_name_lower, TTL 60s) não muda.
    """
    name_hash = hashlib.md5(drug_name_lower.encode()).hexdigest()[:16]
    return f'drug_derive_lock:{project_id}:{name_hash}'


class DrugPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class ProjectDrugViewSet(viewsets.GenericViewSet):
    """
    Medicamentos agregados por projeto.

    Isolamento: _get_project() filtra por request.user — projeto de outro
    usuário retorna 404 (skill firebase-auth-guard).
    """

    # stub para drf-spectacular; nenhum queryset padrão usado em runtime
    queryset = PaperDrug.objects.none()
    pagination_class = DrugPagination

    def _get_project(self):
        return get_object_or_404(
            DaVinciProject,
            pk=self.kwargs['project_pk'],
            user=self.request.user,
        )

    # ------------------------------------------------------------------
    # GET /projects/{project_pk}/drugs/
    # ------------------------------------------------------------------

    @extend_schema(
        summary="Lista medicamentos do projeto (agregada)",
        description=(
            "Retorna lista paginada de medicamentos mencionados nos papers do projeto, "
            "agrupados por drug_name_lower (chave canônica normalizada). "
            "Inclui contagens de citações únicas (included e total), soma de menções "
            "e URLs externas (DrugBank quando drugbank_id presente; PubChem sempre). "
            "Filtros: ?q= (icontains no drug_name_lower), ?ordering= "
            "(unique_citations_included | unique_citations_total | "
            "mention_count_total | drug_name; prefixe com '-' para DESC), "
            "?included_only=true (omite medicamentos sem nenhum paper incluído). "
            "Default: -unique_citations_included. Paginação: ?page=, ?page_size= (máx 100)."
        ),
        parameters=[
            OpenApiParameter(name='q', type=str, description='Filtro por nome do medicamento (icontains).'),
            OpenApiParameter(
                name='ordering',
                type=str,
                description=(
                    'Campo de ordenação. Valores válidos: unique_citations_included, '
                    'unique_citations_total, mention_count_total, drug_name '
                    '(prefixe com - para DESC). Default: -unique_citations_included.'
                ),
            ),
            OpenApiParameter(
                name='included_only',
                type=bool,
                description=(
                    'Quando true, retorna apenas medicamentos com ao menos um paper '
                    'com curation_status=included (unique_citations_included > 0). '
                    'Aceita true/false/1/0. Default: false.'
                ),
            ),
        ],
        responses={200: ProjectDrugListSerializer(many=True)},
    )
    def list(self, request, project_pk=None):
        project = self._get_project()

        q = request.query_params.get('q', '').strip() or None
        ordering = request.query_params.get('ordering', '-unique_citations_included')
        if ordering not in _VALID_ORDERINGS:
            ordering = '-unique_citations_included'

        included_only_raw = request.query_params.get('included_only', '').lower()
        included_only = included_only_raw in ('true', '1')

        qs = DrugService.list_drugs_for_project(
            project, q=q, included_only=included_only,
        ).order_by(ordering)

        # Paginação sobre ValuesQuerySet
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(qs, request, view=self)

        # Enriquecer cada item com drugbank_url e pubchem_search_url
        from apps.core.services.drug_service import _drugbank_url, _pubchem_search_url

        if page is not None:
            enriched = [
                {
                    **item,
                    'drugbank_url': _drugbank_url(item.get('drugbank_id') or ''),
                    'pubchem_search_url': _pubchem_search_url(item.get('drug_name') or item['drug_name_lower']),
                }
                for item in page
            ]
            serializer = ProjectDrugListSerializer(enriched, many=True)
            return paginator.get_paginated_response(serializer.data)

        enriched = [
            {
                **item,
                'drugbank_url': _drugbank_url(item.get('drugbank_id') or ''),
                'pubchem_search_url': _pubchem_search_url(item.get('drug_name') or item['drug_name_lower']),
            }
            for item in qs
        ]
        serializer = ProjectDrugListSerializer(enriched, many=True)
        return Response(serializer.data)

    # ------------------------------------------------------------------
    # GET /projects/{project_pk}/drugs/<drug_name_lower>/
    # ------------------------------------------------------------------

    @extend_schema(
        summary="Detalhe de medicamento do projeto (com snippets de contexto)",
        description=(
            "Retorna métricas do medicamento e, para cada paper do projeto que o cita, "
            "as sentenças do abstract onde o medicamento aparece (snippets). "
            "Se o cache de snippets estiver frio ou stale para algum paper, "
            "uma task Celery é disparada e context_status='computing' é retornado; "
            "chamadas subsequentes retornam 'ready' quando o cache estiver pronto. "
            "drug_name_lower inválido (> 255 chars) ou inexistente no projeto → 404. "
            "O front-end deve encodeURIComponent() o drug_name_lower na URL."
        ),
        responses={200: ProjectDrugDetailSerializer},
    )
    @action(detail=False, methods=['get'], url_path=r'(?P<drug_name_lower>[^/]+)')
    def drug_detail(self, request, project_pk=None, drug_name_lower=None):
        # Decodificar drug_name_lower URL-encoded (ex: 'aspirin%20b' → 'aspirin b')
        drug_name_lower = unquote(drug_name_lower) if drug_name_lower else drug_name_lower

        # Validar comprimento antes de qualquer regex sobre abstracts (proteção ReDoS)
        if drug_name_lower and len(drug_name_lower) > DRUG_NAME_MAX_LEN:
            return Response(
                {'detail': f'drug_name_lower excede o comprimento máximo permitido ({DRUG_NAME_MAX_LEN} caracteres).'},
                status=status.HTTP_404_NOT_FOUND,
            )

        project = self._get_project()

        detail = DrugService.get_drug_detail(project, drug_name_lower)
        if detail is None:
            return Response(
                {'detail': f"Medicamento '{drug_name_lower}' não encontrado nos papers deste projeto."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Disparar task somente se não houver lock ativo para este (project, drug_name_lower).
        # O lock previne reenfileiramento enquanto a derivação está em andamento.
        # cache.add() é atômico: retorna True apenas se a chave não existia.
        if detail['context_status'] == 'computing':
            lock_key = _derive_lock_key(str(project.id), drug_name_lower)
            if cache.add(lock_key, '1', timeout=_DERIVE_LOCK_TTL):
                from apps.core.tasks.drug_tasks import derive_drug_contexts
                derive_drug_contexts.delay(str(project.id), drug_name_lower)
                logger.info(
                    'drug_detail: cache frio/stale — task disparada projeto=%s drug=%s',
                    project.id,
                    drug_name_lower,
                )
            else:
                logger.debug(
                    'drug_detail: lock ativo — task já enfileirada projeto=%s drug=%s',
                    project.id,
                    drug_name_lower,
                )

        serializer = ProjectDrugDetailSerializer(detail)
        return Response(serializer.data)
