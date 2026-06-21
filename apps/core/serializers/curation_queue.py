"""
Serializers para a fila de curadoria manual da Fase 2 — OmnisPathway.

CurationQueueItemSerializer:
    Representa um item da fila: ProjectDataset com eixo classificado-indeterminado.
    Inclui campos mínimos do dataset para o curador tomar decisão.
    NÃO expõe extra_metadata (reduz superfície de dado sensível).

CurationQueueResolveSerializer:
    Valida o body da ação de resolução manual (POST).
    Campos: has_control_group (required), notes (opcional).

CurationQueueResolveResponseSerializer:
    Resposta após resolução bem-sucedida.
"""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.core.models import OmicDataset, ProjectDataset


_VALID_HAS_CONTROL_GROUP = [
    OmicDataset.ControlGroup.YES,
    OmicDataset.ControlGroup.NO,
]


class CurationQueueItemSerializer(serializers.ModelSerializer):
    """
    Item da fila de curadoria: ProjectDataset com has_control_group indeterminado.

    Campos expostos são suficientes para o curador decidir sem vazar metadados sensíveis.
    dataset_id é o PK de OmicDataset (necessário para o endpoint de resolução).
    """
    dataset_id = serializers.IntegerField(source='dataset.id', read_only=True)
    accession = serializers.CharField(source='dataset.accession', read_only=True)
    source_db = serializers.CharField(source='dataset.source_db', read_only=True)
    title = serializers.CharField(source='dataset.title', read_only=True)
    summary = serializers.CharField(source='dataset.summary', read_only=True)
    omic_type = serializers.CharField(source='dataset.omic_type', read_only=True)
    organism = serializers.CharField(source='dataset.organism', read_only=True)
    n_samples = serializers.IntegerField(
        source='dataset.n_samples', read_only=True, allow_null=True
    )
    has_control_group = serializers.CharField(
        source='dataset.has_control_group', read_only=True
    )
    has_control_group_score = serializers.SerializerMethodField()

    @extend_schema_field({'type': 'number', 'format': 'float', 'nullable': True})
    def get_has_control_group_score(self, obj) -> float | None:
        """Score de confiança do classificador para has_control_group."""
        confidence = obj.dataset.contract_confidence or {}
        return confidence.get('has_control_group')

    class Meta:
        model = ProjectDataset
        fields = [
            'id',               # PK de ProjectDataset — usado no endpoint de resolução
            'dataset_id',
            'accession',
            'source_db',
            'title',
            'summary',
            'omic_type',
            'organism',
            'n_samples',
            'has_control_group',
            'has_control_group_score',
            'curation_status',
            'notes',
            'added_at',
            'curated_at',
        ]
        read_only_fields = fields


class CurationQueueResolveSerializer(serializers.Serializer):
    """
    Body da ação de resolução manual (POST /curation-queue/{id}/resolve/).

    has_control_group: 'yes' ou 'no' (obrigatório — curador não pode resolver como unknown).
    notes: notas do curador (opcional, auditável).
    """
    has_control_group = serializers.ChoiceField(
        choices=[
            OmicDataset.ControlGroup.YES,
            OmicDataset.ControlGroup.NO,
        ],
        help_text=(
            'Classificação manual do curador. '
            'Aceita apenas "yes" ou "no" (not "unknown").'
        ),
    )
    notes = serializers.CharField(
        required=False,
        allow_blank=True,
        default='',
        max_length=2000,
        help_text='Notas do curador sobre a decisão (auditável).',
    )


class CurationQueueResolveResponseSerializer(serializers.ModelSerializer):
    """
    Resposta após resolução bem-sucedida.
    Reflete o estado atualizado do ProjectDataset.
    """
    dataset_id = serializers.IntegerField(source='dataset.id', read_only=True)
    accession = serializers.CharField(source='dataset.accession', read_only=True)
    has_control_group = serializers.CharField(
        source='dataset.has_control_group', read_only=True
    )
    has_control_group_score = serializers.SerializerMethodField()

    @extend_schema_field({'type': 'number', 'format': 'float', 'nullable': True})
    def get_has_control_group_score(self, obj) -> float | None:
        confidence = obj.dataset.contract_confidence or {}
        return confidence.get('has_control_group')

    class Meta:
        model = ProjectDataset
        fields = [
            'id',
            'dataset_id',
            'accession',
            'has_control_group',
            'has_control_group_score',
            'notes',
            'curated_at',
        ]
        read_only_fields = fields
