import datetime

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from apps.core.models import DaVinciProject

# Limites para year_buckets (fix DoS — item 1 do laudo 007)
_YEAR_BUCKETS_MAX_LENGTH = 20
_YEAR_MIN = 1900
_YEAR_MAX_OFFSET = 1  # aceita até ano_atual + 1

_ARRAY_STR_SCHEMA = {'type': 'array', 'items': {'type': 'string'}}

# Schema do item MeSH selecionado
_MESH_ENTRY_SCHEMA = {
    'type': 'object',
    'properties': {
        'descriptor': {'type': 'string', 'description': 'Nome canônico do descritor MeSH'},
        'ui': {'type': 'string', 'description': 'ID MeSH (ex: D003920)'},
        'qualifiers': {
            'type': 'array',
            'items': {'type': 'string'},
            'description': 'Subheadings MeSH selecionados',
        },
        'mode': {
            'type': 'string',
            'enum': ['and', 'or'],
            'description': 'Como este bloco se une à query: AND (precisão) ou OR (recall)',
        },
        'major_only': {
            'type': 'boolean',
            'description': 'True → [majr] (major topic), False → [mh]',
        },
    },
    'required': ['descriptor'],
}


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

    # Campos de pesquisa avançada premium (MeSH + magnitude)
    selected_mesh = extend_schema_field({
        'type': 'array',
        'items': _MESH_ENTRY_SCHEMA,
        'description': (
            'Descritores MeSH selecionados pelo usuário. '
            'Cada entrada define um bloco da query booleana PubMed.'
        ),
    })(
        serializers.JSONField(required=False, default=list)
    )
    mesh_default_mode = serializers.ChoiceField(
        choices=['and', 'or'],
        required=False,
        default='and',
        help_text="Modo padrão para blocos MeSH sem 'mode' explícito: 'and' (precisão) ou 'or' (recall)",
    )
    magnitude_snapshot = extend_schema_field({
        'type': 'object',
        'description': (
            'Último preview de magnitude calculado. Snapshot do MagnitudePreview retornado '
            'pelo endpoint search/preview. Salvo pelo PATCH do projeto ao confirmar configuração.'
        ),
        'additionalProperties': True,
    })(
        serializers.JSONField(required=False, default=dict)
    )

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


# ── Serializers de pesquisa avançada premium (MeSH + magnitude) ───────────────

class MeshSuggestRequestSerializer(serializers.Serializer):
    """Body de POST /projects/{id}/mesh/suggest/"""
    term = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=500,
        help_text=(
            "Termo livre para sugestão MeSH. "
            "Se ausente ou vazio, usa a query_term + query_synonyms do projeto."
        ),
    )


class MeshSuggestionSerializer(serializers.Serializer):
    """Uma sugestão de descritor MeSH retornada pelo rust_engine.mesh_suggest."""
    descriptor = serializers.CharField(help_text="Nome canônico do descritor MeSH")
    ui = serializers.CharField(help_text="ID MeSH único (ex: D003920)")
    tree_numbers = serializers.ListField(
        child=serializers.CharField(),
        help_text="Números de posição na árvore MeSH (ex: ['C18.452.394.750'])",
    )
    scope_note = serializers.CharField(
        allow_blank=True,
        help_text="Nota de escopo do descritor MeSH",
    )
    allowable_qualifiers = serializers.ListField(
        child=serializers.CharField(),
        help_text="Subheadings válidos para este descritor",
    )
    pubmed_count = serializers.IntegerField(
        help_text="Número de artigos no PubMed indexados com este descritor",
    )


class PanelFlagsSerializer(serializers.Serializer):
    """Flags que habilitam métricas adicionais no preview de magnitude."""
    by_year = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Incluir contagens por ano (buckets temporais)",
    )
    by_pub_type = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Incluir contagens por tipo de publicação",
    )
    open_access = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Incluir contagens de acesso aberto (PMC)",
    )
    year_buckets = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        allow_null=True,
        default=None,
        max_length=_YEAR_BUCKETS_MAX_LENGTH,
        help_text=(
            f"Anos específicos para bucketing temporal (null = padrão do Rust). "
            f"Máximo {_YEAR_BUCKETS_MAX_LENGTH} entradas. "
            f"Cada ano deve estar entre {_YEAR_MIN} e ano_atual+1."
        ),
    )

    def validate_year_buckets(self, value):
        if value is None:
            return value
        year_max = datetime.datetime.now().year + _YEAR_MAX_OFFSET
        invalid = [y for y in value if not (_YEAR_MIN <= y <= year_max)]
        if invalid:
            raise serializers.ValidationError(
                f"Anos fora do intervalo permitido ({_YEAR_MIN}–{year_max}): {invalid}"
            )
        return value


class SearchPreviewRequestSerializer(serializers.Serializer):
    """Body de POST /projects/{id}/search/preview/"""
    selected_mesh = extend_schema_field({
        'type': 'array',
        'items': _MESH_ENTRY_SCHEMA,
    })(
        serializers.JSONField(
            required=False,
            default=list,
            help_text="Lista de descritores MeSH para calcular o preview. Se ausente, usa selected_mesh do projeto.",
        )
    )
    mesh_default_mode = serializers.ChoiceField(
        choices=['and', 'or'],
        required=False,
        default='and',
        help_text="Modo padrão para blocos sem mode explícito",
    )
    panel_flags = PanelFlagsSerializer(
        required=False,
        default=dict,
        help_text="Flags de painéis adicionais do preview de magnitude",
    )


class MagnitudePreviewSerializer(serializers.Serializer):
    """Resposta do endpoint POST /projects/{id}/search/preview/"""
    # Contagens core (sempre presentes)
    free_text_count = serializers.IntegerField(
        help_text="Artigos encontrados só pela query de texto livre",
    )
    mesh_count = serializers.IntegerField(
        help_text="Artigos encontrados pelos termos MeSH combinados",
    )
    combined_count = serializers.IntegerField(
        help_text="Artigos encontrados pela query final combinada (preview idêntico à ingestão)",
    )
    overlap = serializers.IntegerField(
        help_text="Artigos presentes tanto em free_text quanto em MeSH",
    )
    only_free_text = serializers.IntegerField(
        help_text="Artigos presentes só em free_text (não indexados em MeSH ainda ou não cobertos)",
    )
    only_mesh = serializers.IntegerField(
        help_text="Artigos presentes só em MeSH (não cobertos pela query de texto livre)",
    )
    not_yet_indexed = serializers.IntegerField(
        help_text="Artigos fornecidos pelo publisher, ainda sem indexação MeSH completa",
    )
    reviews = serializers.IntegerField(
        help_text="Revisões (Review[pt]) na query combinada",
    )
    systematic_reviews = serializers.IntegerField(
        help_text="Revisões sistemáticas e meta-análises na query combinada",
    )
    # Campos opcionais (presentes quando panel_flags habilitados)
    by_year = extend_schema_field({
        'type': 'array',
        'items': {
            'type': 'array',
            'items': {'type': 'integer'},
            'minItems': 2,
            'maxItems': 2,
            'description': '[ano, contagem]',
        },
        'description': 'Contagens por ano (quando flag by_year=true)',
    })(
        serializers.ListField(
            child=serializers.ListField(child=serializers.IntegerField()),
            required=False,
            default=list,
        )
    )
    by_pub_type = extend_schema_field({
        'type': 'array',
        'items': {
            'type': 'array',
            'minItems': 2,
            'maxItems': 2,
            'description': '[tipo_publicacao, contagem]',
        },
        'description': 'Contagens por tipo de publicação (quando flag by_pub_type=true)',
    })(
        serializers.ListField(
            child=serializers.ListField(),
            required=False,
            default=list,
        )
    )
    open_access = extend_schema_field({
        'type': 'array',
        'items': {'type': 'integer'},
        'minItems': 2,
        'maxItems': 2,
        'description': '[free_full_text_count, pubmed_pmc_count]',
        'nullable': True,
    })(
        serializers.ListField(
            child=serializers.IntegerField(),
            required=False,
            allow_null=True,
            default=None,
        )
    )
    # Query que foi efetivamente usada no preview (para debugging e paridade)
    query_used = serializers.CharField(
        help_text="Query PubMed exata usada no preview (idêntica à que será usada na ingestão)",
        read_only=True,
    )
