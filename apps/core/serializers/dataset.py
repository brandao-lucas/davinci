from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from apps.core.models import OmicDataset, ProjectDataset


class OmicDatasetSerializer(serializers.ModelSerializer):
    # extra_metadata: dict livre de campos específicos da fonte (BioProject, GEO, SRA…)
    @extend_schema_field({'type': 'object'})
    def get_extra_metadata(self, obj):
        return obj.extra_metadata

    extra_metadata = serializers.SerializerMethodField()

    class Meta:
        model = OmicDataset
        fields = [
            'id', 'accession', 'source_db', 'bioproject_id',
            'title', 'summary', 'omic_type', 'omic_subcategory',
            'organism', 'tax_id', 'n_samples', 'platform',
            'extra_metadata', 'is_active', 'ingested_at',
        ]


class ProjectDatasetListSerializer(serializers.ModelSerializer):
    """Compact list — includes summary and platform for the detail panel."""
    accession = serializers.CharField(source='dataset.accession', read_only=True)
    source_db = serializers.CharField(source='dataset.source_db', read_only=True)
    bioproject_id = serializers.CharField(source='dataset.bioproject_id', read_only=True)
    title = serializers.CharField(source='dataset.title', read_only=True)
    summary = serializers.CharField(source='dataset.summary', read_only=True)
    omic_type = serializers.CharField(source='dataset.omic_type', read_only=True)
    omic_subcategory = serializers.CharField(source='dataset.omic_subcategory', read_only=True)
    organism = serializers.CharField(source='dataset.organism', read_only=True)
    n_samples = serializers.IntegerField(source='dataset.n_samples', read_only=True, allow_null=True)
    platform = serializers.CharField(source='dataset.platform', read_only=True)

    @extend_schema_field({'type': 'object'})
    def get_extra_metadata(self, obj):
        return obj.dataset.extra_metadata

    extra_metadata = serializers.SerializerMethodField()

    class Meta:
        model = ProjectDataset
        fields = [
            'id', 'accession', 'source_db', 'bioproject_id', 'title', 'summary',
            'omic_type', 'omic_subcategory', 'organism', 'n_samples', 'platform',
            'extra_metadata',
            'curation_status', 'exclusion_reason', 'notes',
            'relevance_score', 'added_at', 'curated_at',
        ]


class ProjectDatasetDetailSerializer(serializers.ModelSerializer):
    """Full detail: dataset content + curation fields."""
    dataset = OmicDatasetSerializer(read_only=True)

    class Meta:
        model = ProjectDataset
        fields = [
            'id', 'dataset',
            'curation_status', 'exclusion_reason', 'notes',
            'relevance_score', 'added_at', 'curated_at',
        ]
        read_only_fields = ['id', 'dataset', 'added_at', 'curated_at']


class ProjectDatasetCurateSerializer(serializers.ModelSerializer):
    """Write-only: update curation fields."""
    class Meta:
        model = ProjectDataset
        fields = ['curation_status', 'exclusion_reason', 'notes', 'relevance_score']


# ── Serializers de schema para ações customizadas ─────────────────────────────

class DatasetBulkCurateRequestSerializer(serializers.Serializer):
    """Body de bulk_curate de datasets."""
    dataset_ids = serializers.ListField(
        child=serializers.IntegerField(),
        help_text="Lista de IDs de ProjectDataset a atualizar.",
    )
    curation_status = serializers.ChoiceField(
        choices=ProjectDataset.CurationStatus.choices,
        help_text="Status de curadoria a aplicar.",
    )
    exclusion_reason = serializers.CharField(
        required=False,
        default='',
        allow_blank=True,
        help_text="Motivo de exclusão (usado quando curation_status=excluded).",
    )


class BulkCurateResponseSerializer(serializers.Serializer):
    """Resposta genérica de operações bulk: quantidade de registros atualizados."""
    updated = serializers.IntegerField()
