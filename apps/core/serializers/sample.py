from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from apps.core.models import OmicSample, ProjectSample


class OmicSampleSerializer(serializers.ModelSerializer):
    """Serializer read-only do dado compartilhado OmicSample."""

    # characteristics: dict key/value de metadados da amostra (ex: {"age": "45", "sex": "F"})
    @extend_schema_field({
        'type': 'object',
        'additionalProperties': {'type': 'string'},
    })
    def get_characteristics(self, obj):
        return obj.characteristics

    # extra_metadata: dict livre de campos específicos da fonte
    @extend_schema_field({'type': 'object'})
    def get_extra_metadata(self, obj):
        return obj.extra_metadata

    characteristics = serializers.SerializerMethodField()
    extra_metadata = serializers.SerializerMethodField()

    class Meta:
        model = OmicSample
        fields = [
            'id', 'accession', 'title', 'source_name',
            'organism', 'tax_id', 'platform',
            'characteristics', 'extra_metadata',
            'ingested_at', 'updated_at',
        ]


class ProjectSampleListSerializer(serializers.ModelSerializer):
    """Lista compacta — campos do sample achatados para evitar N+1 com select_related."""

    accession = serializers.CharField(source='sample.accession', read_only=True)
    title = serializers.CharField(source='sample.title', read_only=True)
    source_name = serializers.CharField(source='sample.source_name', read_only=True)
    organism = serializers.CharField(source='sample.organism', read_only=True)
    tax_id = serializers.IntegerField(source='sample.tax_id', read_only=True, allow_null=True)
    platform = serializers.CharField(source='sample.platform', read_only=True)
    dataset_id = serializers.IntegerField(source='sample.dataset_id', read_only=True)
    dataset_accession = serializers.CharField(source='sample.dataset.accession', read_only=True)

    class Meta:
        model = ProjectSample
        fields = [
            'id', 'accession', 'title', 'source_name',
            'organism', 'tax_id', 'platform',
            'dataset_id', 'dataset_accession',
            'curation_status', 'exclusion_reason', 'notes',
            'relevance_score', 'added_at', 'curated_at',
        ]


class ProjectSampleDetailSerializer(serializers.ModelSerializer):
    """Detalhe completo — sample aninhado + campos de curadoria."""

    sample = OmicSampleSerializer(read_only=True)

    class Meta:
        model = ProjectSample
        fields = [
            'id', 'sample',
            'curation_status', 'exclusion_reason', 'notes',
            'relevance_score', 'added_at', 'curated_at',
        ]
        read_only_fields = ['id', 'sample', 'added_at', 'curated_at']


class ProjectSampleCurateSerializer(serializers.ModelSerializer):
    """Write-only: atualiza apenas campos de curadoria."""

    class Meta:
        model = ProjectSample
        fields = ['curation_status', 'exclusion_reason', 'notes', 'relevance_score']


# ── Serializers de schema para ações customizadas ─────────────────────────────

class SampleBulkCurateRequestSerializer(serializers.Serializer):
    """Body de bulk_curate de samples."""
    sample_ids = serializers.ListField(
        child=serializers.IntegerField(),
        help_text="Lista de IDs de ProjectSample a atualizar.",
    )
    curation_status = serializers.ChoiceField(
        choices=ProjectSample.CurationStatus.choices,
        help_text="Status de curadoria a aplicar.",
    )
    exclusion_reason = serializers.CharField(
        required=False,
        default='',
        allow_blank=True,
        help_text="Motivo de exclusão (usado quando curation_status=excluded).",
    )
