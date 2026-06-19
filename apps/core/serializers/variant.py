"""
Serializers para o endpoint de variantes do projeto.

GET /projects/{project_pk}/variants/           → ProjectVariantListSerializer
GET /projects/{project_pk}/variants/<rs_number>/  → ProjectVariantDetailSerializer
"""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers


class VariantAnnotationSerializer(serializers.Serializer):
    """
    Dados de anotação clínica/dbSNP de uma variante (VariantAnnotation).

    Todos os campos são opcionais/nullable: a anotação pode não existir (D2).
    Usado tanto na lista (campos resumidos via seleção no ViewSet)
    quanto no detalhe (campos completos).
    """
    gene_symbol = serializers.CharField(
        allow_blank=True,
        help_text="Símbolo do gene associado à variante (ex.: MTHFR). Vazio se não disponível.",
    )
    gene_name = serializers.CharField(
        allow_blank=True,
        help_text="Nome completo do gene associado. Vazio se não disponível.",
    )
    entrez_id = serializers.IntegerField(
        allow_null=True,
        help_text="Entrez Gene ID do gene associado. Nulo se não disponível.",
    )
    chromosome = serializers.CharField(
        allow_blank=True,
        help_text="Cromossomo (ex.: '1', 'X'). Vazio se não disponível.",
    )
    position = serializers.IntegerField(
        allow_null=True,
        help_text="Posição genômica (base 1). Nulo se não disponível.",
    )
    alleles = serializers.CharField(
        allow_blank=True,
        help_text="Alelos (ex.: 'A/G'). Vazio se não disponível.",
    )
    maf = serializers.FloatField(
        allow_null=True,
        help_text="Minor Allele Frequency (0–1). Nulo se não disponível.",
    )
    clinical_significance = serializers.CharField(
        allow_blank=True,
        help_text=(
            "Significância clínica (ClinVar): 'pathogenic', 'benign', "
            "'uncertain_significance', etc. Vazio se não disponível."
        ),
    )


class ProjectVariantListSerializer(serializers.Serializer):
    """
    Item da lista agregada de variantes do projeto.

    Cada registro representa um rs_number único com contagens
    agregadas calculadas numa única query no VariantService.
    O campo 'annotation' é preenchido via in_bulk() pós-paginação
    no ViewSet e pode ser None quando não há VariantAnnotation (D2).
    """
    rs_number = serializers.CharField(
        help_text="RS Number da variante (ex.: rs1801133).",
    )
    unique_citations_included = serializers.IntegerField(
        help_text=(
            "Número de papers distintos com curation_status='included' "
            "que citam a variante."
        ),
    )
    unique_citations_total = serializers.IntegerField(
        help_text=(
            "Número de papers distintos (qualquer status) do projeto "
            "que citam a variante."
        ),
    )
    mention_count_total = serializers.IntegerField(
        help_text="Soma de mention_count de todos os PaperVariant do projeto para esta variante.",
    )

    @extend_schema_field({
        'oneOf': [
            {
                'type': 'object',
                'properties': {
                    'gene_symbol': {'type': 'string'},
                    'clinical_significance': {'type': 'string'},
                    'chromosome': {'type': 'string'},
                    'maf': {'type': 'number', 'nullable': True},
                },
                'required': ['gene_symbol', 'clinical_significance', 'chromosome', 'maf'],
            },
            {'type': 'null'},
        ],
        'description': (
            'Anotação resumida da variante (gene_symbol, clinical_significance, '
            'chromosome, maf). Null quando não há VariantAnnotation para este rs_number.'
        ),
    })
    def get_annotation(self, obj):  # pragma: no cover — proxy para anotação
        return obj.get('annotation')

    annotation = serializers.SerializerMethodField(
        help_text=(
            "Anotação resumida (gene_symbol, clinical_significance, chromosome, maf). "
            "Null quando não há VariantAnnotation para este rs_number."
        ),
    )


class VariantSnippetSerializer(serializers.Serializer):
    """Uma sentença do abstract que contém a variante."""
    sentence = serializers.CharField(
        help_text="Sentença do abstract contendo a variante.",
    )
    sentence_position = serializers.IntegerField(
        help_text="Índice 0-based da sentença no abstract.",
    )


class VariantReferenceSerializer(serializers.Serializer):
    """
    Paper do projeto que cita a variante, com suas sentenças de contexto.
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

    snippets = VariantSnippetSerializer(many=True)


class ProjectVariantDetailSerializer(serializers.Serializer):
    """
    Detalhe de uma variante no projeto: métricas agregadas + anotação clínica
    + referências com snippets.

    context_status:
        'ready'     — cache de snippets completo e fresco para todos os papers.
        'computing' — task de derivação foi disparada; alguns snippets podem faltar.

    annotation:
        Objeto VariantAnnotationSerializer completo quando disponível.
        Null quando não há VariantAnnotation para este rs_number (D2).
    """
    rs_number = serializers.CharField(
        help_text="RS Number da variante (ex.: rs1801133).",
    )
    unique_citations_included = serializers.IntegerField(
        help_text="Papers distintos com status 'included' que citam a variante.",
    )
    unique_citations_total = serializers.IntegerField(
        help_text="Papers distintos (qualquer status) que citam a variante neste projeto.",
    )
    mention_count_total = serializers.IntegerField(
        help_text="Soma de mention_count de todos os PaperVariant do projeto para esta variante.",
    )

    @extend_schema_field({
        'oneOf': [
            {
                'type': 'object',
                'properties': {
                    'gene_symbol': {'type': 'string'},
                    'gene_name': {'type': 'string'},
                    'entrez_id': {'type': 'integer', 'nullable': True},
                    'chromosome': {'type': 'string'},
                    'position': {'type': 'integer', 'nullable': True},
                    'alleles': {'type': 'string'},
                    'maf': {'type': 'number', 'nullable': True},
                    'clinical_significance': {'type': 'string'},
                },
                'required': [
                    'gene_symbol', 'gene_name', 'entrez_id', 'chromosome',
                    'position', 'alleles', 'maf', 'clinical_significance',
                ],
            },
            {'type': 'null'},
        ],
        'description': (
            'Anotação completa da variante (dbSNP/ClinVar). '
            'Null quando não há VariantAnnotation para este rs_number.'
        ),
    })
    def get_annotation(self, obj):  # pragma: no cover — proxy para anotação
        return obj.get('annotation')

    annotation = serializers.SerializerMethodField(
        help_text=(
            "Anotação completa da variante (dbSNP/ClinVar). "
            "Null quando não há VariantAnnotation para este rs_number."
        ),
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

    references = VariantReferenceSerializer(many=True)

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
