"""
CurationQueueViewSet — Fila de curadoria manual (Fase 2, OmnisPathway).

Expõe datasets com eixos classificados-indeterminados (score < 0.5 em
has_control_group) para revisão manual pelo curador.

Isolamento por usuário:
    Queryset filtra OmicDataset via ProjectDataset.project.user = request.user
    (Regra #3 — sem vazamento cross-user). OmicDataset é compartilhado, mas a
    fila só expõe datasets que estão em algum projeto do usuário autenticado.

Auditoria de curadoria:
    A ação de resolução manual (POST /resolve/) grava curated_at/notes no
    ProjectDataset e marca a origem como manual no contract_confidence do
    OmicDataset (score=1.0, is_manual=True). Nunca deleta. Respeita tri-estado.
    Conforme skill curation-audit-trail.

Endpoints:
    GET  /projects/{project_pk}/curation-queue/
        Lista ProjectDatasets do projeto com has_control_group indeterminado
        (chave presente em contract_confidence + score < 0.5).

    POST /projects/{project_pk}/curation-queue/{id}/resolve/
        Curador seta has_control_group manualmente ('yes' ou 'no').
        Preserva auditoria (curated_at, notes, origem manual em contract_confidence).
"""

import logging

from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.models import DaVinciProject, OmicDataset, ProjectDataset
from apps.core.serializers.curation_queue import (
    CurationQueueItemSerializer,
    CurationQueueResolveSerializer,
    CurationQueueResolveResponseSerializer,
)

logger = logging.getLogger(__name__)

# Limiar de confiança (travado em D2): score < 0.5 → fila de curadoria
_CONFIDENCE_THRESHOLD = 0.5


class CurationQueueViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    Fila de curadoria manual para datasets com eixos classificados-indeterminados.

    list:   GET  /projects/{project_pk}/curation-queue/
    resolve: POST /projects/{project_pk}/curation-queue/{pk}/resolve/
    """
    # stub para drf-spectacular; get_queryset() prevalece em runtime
    queryset = ProjectDataset.objects.none()
    serializer_class = CurationQueueItemSerializer
    http_method_names = ['get', 'post', 'head', 'options']

    def _get_project(self) -> DaVinciProject:
        """
        Resolve o projeto do URL, garantindo que pertence ao request.user.
        HTTP 404 se não existir ou pertencer a outro usuário (Regra #3).
        """
        return get_object_or_404(
            DaVinciProject,
            pk=self.kwargs['project_pk'],
            user=self.request.user,
        )

    def get_queryset(self):
        """
        Retorna ProjectDatasets do projeto do usuário onde has_control_group
        está classificado-indeterminado:
          - chave 'has_control_group' presente em contract_confidence (rodou)
          - score < 0.5 (baixa confiança → fila de curadoria manual)
          - has_control_group == 'unknown' (limiar não atingido, não auto-classificado)

        Ordenado por adição ao projeto (mais antigo primeiro para facilitar revisão).
        """
        project = self._get_project()
        return (
            ProjectDataset.objects
            .filter(project=project)
            .filter(dataset__has_control_group=OmicDataset.ControlGroup.UNKNOWN)
            # Filtro JSON: chave presente em contract_confidence
            .filter(dataset__contract_confidence__has_key='has_control_group')
            .select_related('dataset')
            .order_by('added_at')
        )

    @extend_schema(
        responses={200: CurationQueueItemSerializer(many=True)},
        summary='Listar fila de curadoria manual',
        description=(
            'Lista datasets do projeto com has_control_group classificado-indeterminado '
            '(score < 0.5 pelo classificador automático). '
            'Apenas datasets do projeto do usuário autenticado são expostos '
            '(sem vazamento cross-user). '
            'Retorna lista vazia se não houver itens pendentes de revisão.'
        ),
    )
    def list(self, request, *args, **kwargs):
        """
        GET /projects/{project_pk}/curation-queue/

        Filtra no banco: has_control_group='unknown' + chave em contract_confidence.
        O score exato (< 0.5) é verificado inline para evitar query PostgreSQL JSON
        com comparação numérica (não suportada diretamente em Django ORM sem
        Cast/KeyTextTransform). Datasets com score >= 0.5 não deveriam ter
        has_control_group='unknown' após o classificador, mas o filtro inline
        garante consistência mesmo em casos de re-run parcial.
        """
        qs = self.get_queryset()
        # Filtro inline de score: descartar itens onde o score já é >= limiar
        # (proteção contra inconsistência entre has_control_group e contract_confidence)
        items = [
            pd for pd in qs
            if (pd.dataset.contract_confidence or {}).get('has_control_group', 0.0) < _CONFIDENCE_THRESHOLD
        ]

        serializer = CurationQueueItemSerializer(items, many=True)
        return Response(serializer.data)

    @extend_schema(
        request=CurationQueueResolveSerializer,
        responses={200: CurationQueueResolveResponseSerializer},
        summary='Resolver item da fila de curadoria manualmente',
        description=(
            'Curador define has_control_group como "yes" ou "no" manualmente. '
            'Preserva auditoria: grava curated_at e notes no ProjectDataset; '
            'marca origem manual em contract_confidence do OmicDataset '
            '(score=1.0, is_manual=True). '
            'Nunca deleta. '
            'HTTP 403 se o ProjectDataset não pertencer ao projeto do usuário.'
        ),
    )
    @action(detail=True, methods=['post'], url_path='resolve', throttle_scope='download')
    def resolve(self, request, project_pk=None, pk=None):
        """
        POST /projects/{project_pk}/curation-queue/{pk}/resolve/

        Body: {"has_control_group": "yes"|"no", "notes": "..."}

        Fluxo:
          1. Valida body (CurationQueueResolveSerializer).
          2. Resolve ProjectDataset e verifica pertencimento ao projeto do user.
          3. Atualiza OmicDataset.has_control_group (via save + update_fields).
          4. Marca origem manual em contract_confidence do OmicDataset.
          5. Atualiza auditoria no ProjectDataset (curated_at, notes).
          6. Retorna estado atualizado.

        Preservação de auditoria (curation-audit-trail):
          - curated_at: timestamp da resolução manual.
          - notes: notas do curador (acumuladas — não substitui notas anteriores
            se já existiam; concatena com separador).
          - contract_confidence['has_control_group']: substituído por 1.0.
          - contract_confidence['has_control_group_manual']: True (marca origem).
          - has_control_group no OmicDataset: valor decidido pelo curador.
        """
        project = self._get_project()

        # Resolve ProjectDataset garantindo pertencimento ao projeto do user
        project_dataset = get_object_or_404(ProjectDataset, pk=pk, project=project)
        dataset = project_dataset.dataset

        # Validação do body
        serializer = CurationQueueResolveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_value = serializer.validated_data['has_control_group']
        notes = serializer.validated_data.get('notes', '')

        # ── Atualiza OmicDataset (campos de contrato) ─────────────────────────
        # Mesclar confiança: preservar outros eixos, marcar has_control_group como manual
        current_confidence = dataset.contract_confidence or {}
        updated_confidence = {
            **current_confidence,
            'has_control_group': 1.0,           # score máximo (decisão humana)
            'has_control_group_manual': True,    # origem manual (auditável)
        }

        dataset.has_control_group = new_value
        dataset.contract_confidence = updated_confidence
        dataset.updated_at = timezone.now()
        dataset.save(update_fields=['has_control_group', 'contract_confidence', 'updated_at'])

        # ── Atualiza auditoria no ProjectDataset ──────────────────────────────
        now = timezone.now()

        # Notas: acumula (nova nota prefixada com timestamp + usuário)
        if notes:
            note_entry = f'[{now.strftime("%Y-%m-%d %H:%M")} {request.user.username}] {notes}'
            if project_dataset.notes:
                project_dataset.notes = f'{project_dataset.notes}\n{note_entry}'
            else:
                project_dataset.notes = note_entry
        project_dataset.curated_at = now
        project_dataset.save(update_fields=['notes', 'curated_at'])

        logger.info(
            'CurationQueueViewSet.resolve: dataset=%s → has_control_group=%s '
            'project=%s user=%s',
            dataset.accession,
            new_value,
            project.id,
            request.user.username,
        )

        response_serializer = CurationQueueResolveResponseSerializer(project_dataset)
        return Response(response_serializer.data, status=status.HTTP_200_OK)
