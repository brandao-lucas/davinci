"""
DiseaseAxisCurationQueueView — Fila de curadoria de alto valor (Fase 3, OmnisPathway).

Expõe ProjectDatasets do projeto do usuário que entram na fila de alto valor:
  - disease_axis == 'mixed', OU
  - discordância: monogenic_gene_hit presente MAS disease_axis ∉ {monogenic, mixed}.

'indeterminate' puro (sem gene hit) é TERMINAL — não entra nesta fila.

Isolamento por usuário (Regra #3 — firebase-auth-guard):
    _get_project() resolve o projeto via project_pk + request.user, garantindo HTTP 404
    para projetos de outros usuários. high_value_queue_queryset() filtra
    ProjectDataset.project=project (derivado do user), sem vazamento cross-user.

Sem N+1:
    high_value_queue_queryset() aplica select_related('dataset') antes de qualquer
    iteração Python. O serializer acessa apenas dataset.disease_axis,
    dataset.contract_confidence, dataset.extra_metadata e dataset.source_db —
    todos carregados em uma única query por grupo.

Read-only (GET only):
    Esta view só lista. Ações de curadoria (resolver/annotate) serão endpoints
    separados se implementados no futuro.

Endpoint:
    GET /projects/{project_pk}/disease-axis-queue/
"""

import logging

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, viewsets
from rest_framework.response import Response

from apps.core.models import DaVinciProject, ProjectDataset
from apps.core.serializers.disease_axis_queue import DiseaseAxisQueueItemSerializer
from apps.core.services.disease_axis_classifier_service import high_value_queue_queryset

logger = logging.getLogger(__name__)


class DiseaseAxisCurationQueueView(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    Fila de curadoria de alto valor para datasets com disease_axis 'mixed' ou
    discordância entre monogenic_gene_hit e disease_axis classificado.

    list: GET /projects/{project_pk}/disease-axis-queue/
    """

    # Stub para drf-spectacular; get_queryset() não é usado em runtime
    # (retornamos lista diretamente no list() por causa do scan Python dos discordantes)
    queryset = ProjectDataset.objects.none()
    serializer_class = DiseaseAxisQueueItemSerializer
    http_method_names = ['get', 'head', 'options']

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
        Stub exigido pelo GenericViewSet.
        O queryset real é computado em list() via high_value_queue_queryset().
        """
        return ProjectDataset.objects.none()

    @extend_schema(
        responses={200: DiseaseAxisQueueItemSerializer(many=True)},
        summary='Listar fila de curadoria de alto valor (disease_axis)',
        description=(
            'Lista ProjectDatasets do projeto com disease_axis classificado como '
            '"mixed" (âncora monogênica + multifatorial detectadas) ou com '
            'discordância (monogenic_gene_hit presente mas disease_axis fora de '
            '{monogenic, mixed}). '
            '"indeterminate" puro (sem gene hit) é terminal e NÃO aparece aqui. '
            'Cada item inclui o campo queue_reason ("mixed" ou "discordance") '
            'para a UI saber por que o dataset entrou na fila. '
            'Apenas datasets do projeto do usuário autenticado são expostos '
            '(sem vazamento cross-user). '
            'Retorna lista vazia se não houver itens pendentes de revisão.'
        ),
    )
    def list(self, request, *args, **kwargs):
        """
        GET /projects/{project_pk}/disease-axis-queue/

        Delega a política de fila para high_value_queue_queryset() — ponto único
        de verdade compartilhado com compute_high_value_queue_counts().
        """
        project = self._get_project()
        items = high_value_queue_queryset(project)

        logger.debug(
            'DiseaseAxisCurationQueueView.list: project=%s user=%s items=%d',
            project.id,
            request.user.username,
            len(items),
        )

        serializer = DiseaseAxisQueueItemSerializer(items, many=True)
        return Response(serializer.data)
