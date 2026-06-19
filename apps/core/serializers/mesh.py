"""
Serializers para o endpoint de termos MeSH do projeto.

GET /projects/{project_pk}/mesh/              → ProjectMeSHListSerializer
GET /projects/{project_pk}/mesh/<descriptor>/ → ProjectMeSHDetailSerializer
"""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers


class ProjectMeSHListSerializer(serializers.Serializer):
    """
    Item da lista agregada de termos MeSH do projeto.

    Cada registro representa um descriptor único com contagens
    agregadas calculadas numa única query no MeshService.

    Métrica primária: major_topic_count (papers included onde is_major_topic=True).
    """
    descriptor = serializers.CharField(
        help_text="Descriptor MeSH (ex.: 'Diabetes Mellitus', 'Neoplasms').",
    )

    @extend_schema_field({'type': 'integer', 'minimum': 0})
    def get_major_topic_count(self, obj):  # pragma: no cover — proxy para anotação
        return obj.get('major_topic_count', 0)

    major_topic_count = serializers.IntegerField(
        help_text=(
            "Número de papers distintos com curation_status='included' onde "
            "este descriptor é tópico principal (is_major_topic=True). Métrica primária."
        ),
    )

    @extend_schema_field({'type': 'integer', 'minimum': 0})
    def get_unique_citations_included(self, obj):  # pragma: no cover
        return obj.get('unique_citations_included', 0)

    unique_citations_included = serializers.IntegerField(
        help_text="Número de papers distintos com curation_status='included' que citam o descriptor.",
    )

    @extend_schema_field({'type': 'integer', 'minimum': 0})
    def get_unique_citations_total(self, obj):  # pragma: no cover
        return obj.get('unique_citations_total', 0)

    unique_citations_total = serializers.IntegerField(
        help_text="Número de papers distintos (qualquer status) do projeto que citam o descriptor.",
    )

    ncbi_mesh_url = serializers.CharField(
        help_text=(
            "URL de busca NCBI MeSH para o descriptor "
            "(https://www.ncbi.nlm.nih.gov/mesh/?term=<descriptor> URL-encoded)."
        ),
    )


class MeSHSnippetSerializer(serializers.Serializer):
    """Uma sentença do abstract que contém o descriptor MeSH."""
    sentence = serializers.CharField(
        help_text="Sentença do abstract contendo o descriptor.",
    )
    sentence_position = serializers.IntegerField(
        help_text="Índice 0-based da sentença no abstract.",
    )


class MeSHReferenceSerializer(serializers.Serializer):
    """
    Paper do projeto que cita o descriptor MeSH, com sentenças de contexto.
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

    @extend_schema_field({'type': 'boolean'})
    def get_is_major_topic(self, obj):  # pragma: no cover — proxy para anotação
        return obj.get('is_major_topic', False)

    is_major_topic = serializers.BooleanField(
        help_text="True se este descriptor é tópico principal (MajorTopicYN='Y') no paper.",
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

    snippets = MeSHSnippetSerializer(many=True)


class ProjectMeSHDetailSerializer(serializers.Serializer):
    """
    Detalhe de um descriptor MeSH no projeto:
    métricas agregadas + qualifiers + referências com snippets.

    context_status:
        'ready'     — cache de snippets completo e fresco para todos os papers.
        'computing' — task de derivação foi disparada; alguns snippets podem faltar.

    Nota sobre snippets: MeSH não garante presença literal do descriptor no
    abstract; zero snippets é comum e esperado (coberto pelo sentinela -1).
    """
    descriptor = serializers.CharField(
        help_text="Descriptor MeSH.",
    )
    major_topic_count = serializers.IntegerField(
        help_text=(
            "Papers distintos com status 'included' onde o descriptor é tópico principal."
        ),
    )
    unique_citations_included = serializers.IntegerField(
        help_text="Papers distintos com status 'included' que citam o descriptor.",
    )
    unique_citations_total = serializers.IntegerField(
        help_text="Papers distintos (qualquer status) que citam o descriptor neste projeto.",
    )

    @extend_schema_field({
        'type': 'array',
        'items': {'type': 'string'},
        'description': "Qualifiers MeSH distintos e não-vazios entre os papers do projeto.",
    })
    def get_qualifiers(self, obj):  # pragma: no cover — proxy para anotação
        return obj.get('qualifiers', [])

    qualifiers = serializers.ListField(
        child=serializers.CharField(),
        help_text="Qualifiers MeSH distintos não-vazios entre os papers do projeto.",
    )

    ncbi_mesh_url = serializers.CharField(
        help_text="URL de busca NCBI MeSH para o descriptor.",
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
                'is_major_topic': {'type': 'boolean'},
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
                'journal', 'curation_status', 'is_major_topic', 'snippets',
            ],
        },
    })
    def get_references(self, obj):  # pragma: no cover — proxy para anotação
        return obj.get('references', [])

    references = MeSHReferenceSerializer(many=True)

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
