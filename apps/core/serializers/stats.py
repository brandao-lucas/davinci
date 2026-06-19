from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from apps.core.models import ProjectStats

_DICT_INT_SCHEMA = {
    'type': 'object',
    'additionalProperties': {'type': 'integer'},
}


class ProjectStatsSerializer(serializers.ModelSerializer):
    @extend_schema_field(_DICT_INT_SCHEMA)
    def get_papers_by_year(self, obj):  # noqa: D102 — proxy usado só para anotação
        return obj.papers_by_year

    @extend_schema_field(_DICT_INT_SCHEMA)
    def get_papers_by_journal(self, obj):
        return obj.papers_by_journal

    @extend_schema_field(_DICT_INT_SCHEMA)
    def get_papers_by_country(self, obj):
        return obj.papers_by_country

    @extend_schema_field(_DICT_INT_SCHEMA)
    def get_papers_by_clinical_category(self, obj):
        return obj.papers_by_clinical_category

    @extend_schema_field(_DICT_INT_SCHEMA)
    def get_datasets_by_omic_type(self, obj):
        return obj.datasets_by_omic_type

    @extend_schema_field(_DICT_INT_SCHEMA)
    def get_datasets_by_organism(self, obj):
        return obj.datasets_by_organism

    @extend_schema_field({
        'type': 'array',
        'items': {
            'type': 'object',
            'properties': {
                'gene': {'type': 'string'},
                'count': {'type': 'integer'},
            },
            'required': ['gene', 'count'],
        },
    })
    def get_top_genes(self, obj):
        return obj.top_genes

    @extend_schema_field({
        'type': 'array',
        'items': {
            'type': 'object',
            'properties': {
                'drug': {'type': 'string'},
                'count': {'type': 'integer'},
            },
            'required': ['drug', 'count'],
        },
    })
    def get_top_drugs(self, obj):
        return obj.top_drugs

    @extend_schema_field({
        'type': 'array',
        'items': {
            'type': 'object',
            'properties': {
                'term': {'type': 'string'},
                'count': {'type': 'integer'},
            },
            'required': ['term', 'count'],
        },
    })
    def get_top_mesh_terms(self, obj):
        return obj.top_mesh_terms

    @extend_schema_field({
        'type': 'array',
        'items': {
            'type': 'object',
            'properties': {
                'rs_number': {'type': 'string'},
                'count': {'type': 'integer'},
            },
            'required': ['rs_number', 'count'],
        },
    })
    def get_top_variants(self, obj):
        return obj.top_variants

    papers_by_year = serializers.SerializerMethodField()
    papers_by_journal = serializers.SerializerMethodField()
    papers_by_country = serializers.SerializerMethodField()
    papers_by_clinical_category = serializers.SerializerMethodField()
    datasets_by_omic_type = serializers.SerializerMethodField()
    datasets_by_organism = serializers.SerializerMethodField()
    top_genes = serializers.SerializerMethodField()
    top_drugs = serializers.SerializerMethodField()
    top_mesh_terms = serializers.SerializerMethodField()
    top_variants = serializers.SerializerMethodField()

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
