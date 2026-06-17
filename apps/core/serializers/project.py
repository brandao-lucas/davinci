from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from apps.core.models import DaVinciProject

_ARRAY_STR_SCHEMA = {'type': 'array', 'items': {'type': 'string'}}


def _array_str_field():
    """Instancia nova de ListField opcional anotada com schema de array de strings."""
    return extend_schema_field(_ARRAY_STR_SCHEMA)(
        serializers.ListField(
            child=serializers.CharField(allow_blank=True),
            required=False,
            default=list,
        )
    )


class DaVinciProjectSerializer(serializers.ModelSerializer):
    query_synonyms = _array_str_field()
    target_organisms = _array_str_field()
    target_tissues = _array_str_field()
    total_papers = serializers.IntegerField(read_only=True, default=0)
    total_datasets = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = DaVinciProject
        fields = '__all__'
        read_only_fields = ['id', 'user', 'slug', 'status', 'created_at', 'updated_at']


# ── Serializers de schema para ações customizadas ─────────────────────────────

class JobDispatchResponseSerializer(serializers.Serializer):
    """Resposta de ações que despacham um job de ingestão (202 Accepted)."""
    job_id = serializers.UUIDField()
    status = serializers.CharField()


class OmicsSearchRequestSerializer(serializers.Serializer):
    """Body de /projects/{id}/omics_search/ — todos os campos são opcionais."""
    sources = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        allow_null=True,
        help_text="Lista de fontes ômicas (GEO, SRA, BioProject, GWAS). Null = todas.",
    )
    max_per_source = serializers.IntegerField(
        required=False,
        default=500,
        help_text="Máximo de registros por fonte.",
    )
