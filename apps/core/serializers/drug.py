"""
Serializers para o endpoint de medicamentos do projeto.

GET /projects/{project_pk}/drugs/                    → ProjectDrugListSerializer
GET /projects/{project_pk}/drugs/<drug_name_lower>/  → ProjectDrugDetailSerializer
"""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers


class ProjectDrugListSerializer(serializers.Serializer):
    """
    Item da lista agregada de medicamentos do projeto.

    Cada registro representa um drug_name_lower único com contagens
    agregadas calculadas numa única query no DrugService.

    Chave de agrupamento: drug_name_lower (canônica).
    Exibição: drug_name representativo (Max do grupo).
    """

    @extend_schema_field({'type': 'string'})
    def get_drug_name(self, obj):  # pragma: no cover — proxy para anotação
        return obj.get('drug_name', '')

    drug_name = serializers.CharField(
        help_text="Nome representativo do medicamento (Max do grupo por drug_name_lower).",
    )

    @extend_schema_field({'type': 'string', 'nullable': True})
    def get_drugbank_id(self, obj):  # pragma: no cover — proxy para anotação
        return obj.get('drugbank_id') or None

    drugbank_id = serializers.CharField(
        allow_blank=True,
        help_text=(
            "DrugBank ID representativo do grupo (primeiro não-vazio). "
            "String vazia ou null quando ausente."
        ),
    )

    @extend_schema_field({'type': 'integer', 'minimum': 0})
    def get_unique_citations_included(self, obj):  # pragma: no cover
        return obj.get('unique_citations_included', 0)

    unique_citations_included = serializers.IntegerField(
        help_text=(
            "Número de papers distintos com curation_status='included' "
            "que citam o medicamento."
        ),
    )

    @extend_schema_field({'type': 'integer', 'minimum': 0})
    def get_unique_citations_total(self, obj):  # pragma: no cover
        return obj.get('unique_citations_total', 0)

    unique_citations_total = serializers.IntegerField(
        help_text="Número de papers distintos (qualquer status) do projeto que citam o medicamento.",
    )

    @extend_schema_field({'type': 'integer', 'minimum': 0})
    def get_mention_count_total(self, obj):  # pragma: no cover
        return obj.get('mention_count_total', 0)

    mention_count_total = serializers.IntegerField(
        help_text=(
            "Soma de mention_count de todos os PaperDrug do projeto para este medicamento. "
            "Pode contar o mesmo paper mais de uma vez se houver múltiplas menções."
        ),
    )

    @extend_schema_field({'type': 'string', 'nullable': True})
    def get_drugbank_url(self, obj):  # pragma: no cover — proxy para anotação
        return obj.get('drugbank_url')

    drugbank_url = serializers.CharField(
        allow_null=True,
        help_text=(
            "URL direta no DrugBank (https://go.drugbank.com/drugs/<drugbank_id>). "
            "Null quando drugbank_id ausente."
        ),
    )

    @extend_schema_field({'type': 'string'})
    def get_pubchem_search_url(self, obj):  # pragma: no cover — proxy para anotação
        return obj.get('pubchem_search_url', '')

    pubchem_search_url = serializers.CharField(
        help_text=(
            "URL de busca PubChem por nome do medicamento "
            "(https://pubchem.ncbi.nlm.nih.gov/#query=<drug_name> URL-encoded). "
            "Sempre presente."
        ),
    )


class DrugSnippetSerializer(serializers.Serializer):
    """Uma sentença do abstract que contém o medicamento."""
    sentence = serializers.CharField(
        help_text="Sentença do abstract contendo o medicamento.",
    )
    sentence_position = serializers.IntegerField(
        help_text="Índice 0-based da sentença no abstract.",
    )


class DrugReferenceSerializer(serializers.Serializer):
    """
    Paper do projeto que cita o medicamento, com suas sentenças de contexto.
    """
    project_paper_id = serializers.IntegerField(
        help_text=(
            "PK de ProjectPaper — usada no PATCH /projects/{id}/papers/<pk>/ "
            "para toggle de curadoria. Distinta da PK de Paper (pmid para exibição)."
        ),
    )
    pmid = serializers.IntegerField(
        help_text="PubMed ID do paper.",
    )
    title = serializers.CharField(
        help_text="Título do paper.",
    )
    pub_year = serializers.IntegerField(
        allow_null=True,
        help_text="Ano de publicação.",
    )
    journal = serializers.CharField(
        help_text="Periódico (ISO abbrev).",
    )
    curation_status = serializers.CharField(
        help_text="Status de curadoria do paper neste projeto (included, excluded, pending, maybe).",
    )

    @extend_schema_field({
        'type': 'array',
        'items': {
            'type': 'object',
            'properties': {
                'sentence': {'type': 'string'},
                'sentence_position': {'type': 'integer'},
            },
            'required': ['sentence', 'sentence_position'],
        },
    })
    def get_snippets(self, obj):  # pragma: no cover — proxy para anotação
        return obj.get('snippets', [])

    snippets = DrugSnippetSerializer(many=True)


class ProjectDrugDetailSerializer(serializers.Serializer):
    """
    Detalhe de um medicamento no projeto: métricas agregadas + referências com snippets.

    context_status:
        'ready'     — cache de snippets completo e fresco para todos os papers.
        'computing' — task de derivação foi disparada; alguns snippets podem faltar.
    """
    drug_name = serializers.CharField(
        help_text="Nome representativo do medicamento.",
    )
    drugbank_id = serializers.CharField(
        allow_blank=True,
        help_text="DrugBank ID representativo (primeiro não-vazio). String vazia quando ausente.",
    )
    unique_citations_included = serializers.IntegerField(
        help_text="Papers distintos com status 'included' que citam o medicamento.",
    )
    unique_citations_total = serializers.IntegerField(
        help_text="Papers distintos (qualquer status) que citam o medicamento neste projeto.",
    )

    @extend_schema_field({'type': 'string', 'nullable': True})
    def get_drugbank_url(self, obj):  # pragma: no cover — proxy para anotação
        return obj.get('drugbank_url')

    drugbank_url = serializers.CharField(
        allow_null=True,
        help_text=(
            "URL direta no DrugBank. Null quando drugbank_id ausente."
        ),
    )

    @extend_schema_field({'type': 'string'})
    def get_pubchem_search_url(self, obj):  # pragma: no cover — proxy para anotação
        return obj.get('pubchem_search_url', '')

    pubchem_search_url = serializers.CharField(
        help_text="URL de busca PubChem por nome. Sempre presente.",
    )

    @extend_schema_field({
        'type': 'array',
        'items': {
            'type': 'object',
            'properties': {
                'project_paper_id': {
                    'type': 'integer',
                    'description': (
                        'PK de ProjectPaper — usada no PATCH /projects/{id}/papers/<pk>/ '
                        'para toggle de curadoria.'
                    ),
                },
                'pmid': {'type': 'integer'},
                'title': {'type': 'string'},
                'pub_year': {'type': 'integer', 'nullable': True},
                'journal': {'type': 'string'},
                'curation_status': {'type': 'string'},
                'snippets': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'sentence': {'type': 'string'},
                            'sentence_position': {'type': 'integer'},
                        },
                        'required': ['sentence', 'sentence_position'],
                    },
                },
            },
            'required': [
                'project_paper_id', 'pmid', 'title', 'pub_year',
                'journal', 'curation_status', 'snippets',
            ],
        },
    })
    def get_references(self, obj):  # pragma: no cover — proxy para anotação
        return obj.get('references', [])

    references = DrugReferenceSerializer(many=True)

    @extend_schema_field({
        'type': 'string',
        'enum': ['ready', 'computing'],
        'description': (
            "'ready' indica cache completo e fresco. "
            "'computing' indica que a task Celery ainda está populando os snippets."
        ),
    })
    def get_context_status(self, obj):  # pragma: no cover — proxy para anotação
        return obj.get('context_status', 'ready')

    context_status = serializers.ChoiceField(
        choices=['ready', 'computing'],
        help_text="'ready' | 'computing' — estado do cache de snippets.",
    )
