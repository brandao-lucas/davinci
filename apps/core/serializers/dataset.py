from rest_framework import serializers
from apps.core.models import OmicDataset, ProjectDataset


class OmicDatasetSerializer(serializers.ModelSerializer):
    class Meta:
        model = OmicDataset
        fields = [
            'id', 'accession', 'source_db', 'bioproject_id',
            'title', 'summary', 'omic_type', 'omic_subcategory',
            'organism', 'tax_id', 'n_samples', 'platform',
            'extra_metadata', 'is_active', 'ingested_at',
        ]


class ProjectDatasetListSerializer(serializers.ModelSerializer):
    """Compact list representation."""
    accession = serializers.CharField(source='dataset.accession', read_only=True)
    source_db = serializers.CharField(source='dataset.source_db', read_only=True)
    title = serializers.CharField(source='dataset.title', read_only=True)
    omic_type = serializers.CharField(source='dataset.omic_type', read_only=True)
    omic_subcategory = serializers.CharField(source='dataset.omic_subcategory', read_only=True)
    organism = serializers.CharField(source='dataset.organism', read_only=True)
    n_samples = serializers.IntegerField(source='dataset.n_samples', read_only=True)

    class Meta:
        model = ProjectDataset
        fields = [
            'id', 'accession', 'source_db', 'title',
            'omic_type', 'omic_subcategory', 'organism', 'n_samples',
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
