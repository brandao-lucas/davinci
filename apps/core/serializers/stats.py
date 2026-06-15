from rest_framework import serializers
from apps.core.models import ProjectStats


class ProjectStatsSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectStats
        fields = [
            'total_papers', 'included_papers', 'excluded_papers', 'pending_papers',
            'total_datasets', 'included_datasets', 'total_samples', 'included_samples',
            'papers_by_year', 'papers_by_journal', 'papers_by_country',
            'papers_by_clinical_category', 'datasets_by_omic_type',
            'datasets_by_organism', 'top_genes', 'top_drugs',
            'top_mesh_terms', 'top_variants', 'last_computed',
        ]
        read_only_fields = fields
