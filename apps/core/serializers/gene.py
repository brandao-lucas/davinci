"""
Serializers para o endpoint de genes do projeto.

GET /projects/{project_pk}/genes/           → ProjectGeneListSerializer
GET /projects/{project_pk}/genes/<symbol>/  → ProjectGeneDetailSerializer
"""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers


class ProjectGeneListSerializer(serializers.Serializer):
    """
    Item da lista agregada de genes do projeto.

    Cada registro representa um gene_symbol único com contagens
    agregadas calculadas numa única query no GeneService.
    """
    gene_symbol = serializers.CharField(
        help_text="Símbolo do gene (ex.: TNF, BRCA1).",
    )

    @extend_schema_field({'type': 'integer', 'nullable': True})
    def get_entrez_id(self, obj):  # pragma: no cover — proxy para anotação
        return obj.get('entrez_id')

    entrez_id = serializers.IntegerField(
        allow_null=True,
        help_text="Entrez Gene ID representativo do grupo (primeiro não-nulo). Nulo se ausente.",
    )
    unique_citations_included = serializers.IntegerField(
        help_text="Número de papers distintos com curation_status='included' que citam o gene.",
    )
    unique_citations_total = serializers.IntegerField(
        help_text="Número de papers distintos (qualquer status) do projeto que citam o gene.",
    )
    mention_count_total = serializers.IntegerField(
        help_text="Soma de mention_count de todos os PaperGene do projeto para este gene.",
    )


class GeneSnippetSerializer(serializers.Serializer):
    """Uma sentença do abstract que contém o gene."""
    sentence = serializers.CharField(
        help_text="Sentença do abstract contendo o gene.",
    )
    sentence_position = serializers.IntegerField(
        help_text="Índice 0-based da sentença no abstract.",
    )


class GeneReferenceSerializer(serializers.Serializer):
    """
    Paper do projeto que cita o gene, com suas sentenças de contexto.
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

    snippets = GeneSnippetSerializer(many=True)


class ProjectGeneDetailSerializer(serializers.Serializer):
    """
    Detalhe de um gene no projeto: métricas agregadas + referências com snippets.

    context_status:
        'ready'     — cache de snippets completo e fresco para todos os papers.
        'computing' — task de derivação foi disparada; alguns snippets podem faltar.
    """
    gene_symbol = serializers.CharField()
    entrez_id = serializers.IntegerField(
        allow_null=True,
        help_text="Entrez Gene ID representativo (primeiro não-nulo). Nulo se ausente.",
    )
    unique_citations_included = serializers.IntegerField(
        help_text="Papers distintos com status 'included' que citam o gene.",
    )
    unique_citations_total = serializers.IntegerField(
        help_text="Papers distintos (qualquer status) que citam o gene neste projeto.",
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
            'required': ['project_paper_id', 'pmid', 'title', 'pub_year', 'journal', 'curation_status', 'snippets'],
        },
    })
    def get_references(self, obj):  # pragma: no cover — proxy para anotação
        return obj.get('references', [])

    references = GeneReferenceSerializer(many=True)

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
