"""
Serializer para a fila de curadoria de alto valor (disease_axis) — Fase 3 OmnisPathway.

DiseaseAxisQueueItemSerializer:
    Representa um ProjectDataset que entrou na fila de alto valor por um dos dois
    critérios: disease_axis == 'mixed' OU discordância (monogenic_gene_hit presente
    mas disease_axis fora de {monogenic, mixed}).

    Campos expostos:
        id                  — PK de ProjectDataset
        dataset_id          — PK de OmicDataset
        accession           — accession do dataset (GSE*, PRJNA*, PXD*, etc.)
        source_db           — banco de origem
        title               — título do dataset
        disease_axis        — eixo classificado pelo classificador
        disease_axis_confidence — sub-dict de contract_confidence['disease_axis']
                               com score, method e axis_hits (mono + multi)
        monogenic_gene_hit  — dict com genes, confidence e gene_details
                               (de extra_metadata['contract']['monogenic_gene_hit'])
        queue_reason        — 'mixed' ou 'discordance' (campo derivado, para UI)
        curation_status     — status de curadoria do vínculo projeto-dataset
        notes               — notas do curador (acumuladas)
        added_at            — quando foi adicionado ao projeto
        curated_at          — quando foi curado pela última vez

    NÃO expõe:
        - extra_metadata inteiro (apenas a sub-chave monogenic_gene_hit do contract)
        - contract_confidence inteiro (apenas a sub-chave disease_axis)
        - campos de outros usuários (isolamento garantido na view)
"""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.core.models import ProjectDataset


class DiseaseAxisQueueItemSerializer(serializers.ModelSerializer):
    """
    Item da fila de curadoria de alto valor: ProjectDataset com disease_axis
    'mixed' ou com discordância (monogenic_gene_hit presente e axis divergente).

    O campo `queue_reason` é sintético: alimentado pela view a partir do atributo
    `_queue_reason` que high_value_queue_queryset() anota em cada instância.
    """

    dataset_id = serializers.IntegerField(source='dataset.id', read_only=True)
    accession = serializers.CharField(source='dataset.accession', read_only=True)
    source_db = serializers.CharField(source='dataset.source_db', read_only=True)
    title = serializers.CharField(source='dataset.title', read_only=True)
    disease_axis = serializers.CharField(source='dataset.disease_axis', read_only=True)

    disease_axis_confidence = serializers.SerializerMethodField()
    monogenic_gene_hit = serializers.SerializerMethodField()
    queue_reason = serializers.SerializerMethodField()

    @extend_schema_field({
        'type': 'object',
        'nullable': True,
        'description': (
            'Sub-dict de contract_confidence[disease_axis]: score (float), '
            'method (str), n_candidates (int), axis_hits {monogenic, multifactorial} '
            'com matched_name, matched_source, score, method.'
        ),
        'properties': {
            'score': {'type': 'number'},
            'method': {'type': 'string'},
            'n_candidates': {'type': 'integer'},
            'axis_hits': {
                'type': 'object',
                'properties': {
                    'monogenic': {
                        'type': 'object',
                        'properties': {
                            'score': {'type': 'number'},
                            'method': {'type': 'string'},
                            'matched_name': {'type': 'string'},
                            'matched_source': {'type': 'string'},
                        },
                    },
                    'multifactorial': {
                        'type': 'object',
                        'properties': {
                            'score': {'type': 'number'},
                            'method': {'type': 'string'},
                            'matched_name': {'type': 'string'},
                            'matched_source': {'type': 'string'},
                        },
                    },
                },
            },
        },
    })
    def get_disease_axis_confidence(self, obj) -> dict | None:
        """
        Retorna apenas contract_confidence['disease_axis'] — score, method e axis_hits.
        Não expõe outras chaves de contract_confidence (has_control_group, etc.).
        """
        cc = obj.dataset.contract_confidence or {}
        return cc.get('disease_axis') or None

    @extend_schema_field({
        'type': 'object',
        'nullable': True,
        'description': (
            'Sinal de gene monogênico de extra_metadata[contract][monogenic_gene_hit]. '
            'Contém: genes (list[str]), confidence (float), gene_details (list[dict]). '
            'None se o dataset não passou pelo classificador de gene hit ou não houve hit.'
        ),
        'properties': {
            'genes': {'type': 'array', 'items': {'type': 'string'}},
            'confidence': {'type': 'number'},
            'gene_details': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'gene_symbol': {'type': 'string'},
                        'score': {'type': 'number'},
                        'sources': {'type': 'array', 'items': {'type': 'string'}},
                        'mendelian_confidence': {'type': 'string'},
                    },
                },
            },
        },
    })
    def get_monogenic_gene_hit(self, obj) -> dict | None:
        """
        Retorna apenas extra_metadata['contract']['monogenic_gene_hit'].
        Não vaza extra_metadata inteiro nem outros campos do contract.
        """
        contract = (obj.dataset.extra_metadata or {}).get('contract') or {}
        return contract.get('monogenic_gene_hit') or None

    @extend_schema_field({
        'type': 'string',
        'enum': ['mixed', 'discordance'],
        'description': (
            "'mixed': disease_axis == 'mixed' (âncora mono + multi detectadas). "
            "'discordance': monogenic_gene_hit presente mas disease_axis fora de "
            "{monogenic, mixed} — sinal de gene sugere monogênico mas texto não confirmou."
        ),
    })
    def get_queue_reason(self, obj) -> str:
        """
        Lê o atributo sintético `_queue_reason` anotado por high_value_queue_queryset().
        Fallback para 'mixed' se ausente (não deve ocorrer em uso normal).
        """
        return getattr(obj, '_queue_reason', 'mixed')

    class Meta:
        model = ProjectDataset
        fields = [
            'id',                       # PK de ProjectDataset — referência para ações futuras
            'dataset_id',
            'accession',
            'source_db',
            'title',
            'disease_axis',
            'disease_axis_confidence',
            'monogenic_gene_hit',
            'queue_reason',
            'curation_status',
            'notes',
            'added_at',
            'curated_at',
        ]
        read_only_fields = fields
