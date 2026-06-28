"""
Serializer de export para a camada de descoberta (Fase 4 OmnisPathway).

DatasetExportItemSerializer:
    Read-only sobre ProjectDataset/OmicDataset. Expõe o shape travado do contrato §2:
    campos estruturais, contract_confidence, sub-objeto contract (allowlist explícita)
    e campo derivado access_controlled.

    NÃO expõe:
    - extra_metadata inteiro (apenas allowlist de extra_metadata['contract'])
    - storage_key, credenciais, dados de outros usuários
    - matrizes numéricas: matrix_pointer é sempre ponteiro, nunca conteúdo

    Ausência de extra_metadata['contract'] não quebra: retorna nulls/listas vazias.
    Presença de access_type='controlled' é marcada via access_controlled=True.

    snapshot_version: injetado via context (chave 'snapshot_version').
    Se ausente no context, retorna 'live'.
"""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.core.models import ProjectDataset


# Allowlist explícita das chaves de extra_metadata['contract'] expostas no export.
# NUNCA serializar o dict cru — risco de vazar chaves não previstas (R2 do plano).
_CONTRACT_ALLOWLIST = {
    'matrix_pointer',
    'proteomics_modality',
    'tissue_raw',
    'disease_raw',
    'ref_pmids',
    'ref_dois',
    'monogenic_gene_hit',
}


class DatasetExportItemSerializer(serializers.ModelSerializer):
    """
    Item do export de descoberta: ProjectDataset com campos do contrato §2.

    Shape entregue:
      Estruturais: accession, source_db, omics_layers, omics_count, is_single_cell,
                   has_control_group, disease_axis, data_format, access_type, sample_join_key.
      Confiança: contract_confidence (dict por eixo).
      Sub-objeto contract (allowlist): matrix_pointer, proteomics_modality, tissue_raw,
                   disease_raw, ref_pmids, ref_dois, monogenic_gene_hit.
      Derivados: access_controlled (bool), snapshot_version (str de context).
    """

    # ── Campos estruturais (proxy para dataset.*) ─────────────────────────────
    accession = serializers.CharField(source='dataset.accession', read_only=True)
    source_db = serializers.CharField(source='dataset.source_db', read_only=True)
    omics_layers = serializers.ListField(
        child=serializers.CharField(),
        source='dataset.omics_layers',
        read_only=True,
    )
    omics_count = serializers.IntegerField(
        source='dataset.omics_count',
        read_only=True,
        allow_null=True,
    )
    is_single_cell = serializers.CharField(source='dataset.is_single_cell', read_only=True)
    has_control_group = serializers.CharField(source='dataset.has_control_group', read_only=True)
    disease_axis = serializers.CharField(source='dataset.disease_axis', read_only=True)
    data_format = serializers.CharField(source='dataset.data_format', read_only=True)
    access_type = serializers.CharField(source='dataset.access_type', read_only=True)
    sample_join_key = serializers.ListField(
        child=serializers.CharField(),
        source='dataset.sample_join_key',
        read_only=True,
    )

    # ── Campos de método (SerializerMethodField com @extend_schema_field) ─────

    contract_confidence = serializers.SerializerMethodField()
    contract = serializers.SerializerMethodField()
    access_controlled = serializers.SerializerMethodField()
    snapshot_version = serializers.SerializerMethodField()

    @extend_schema_field({
        'type': 'object',
        'nullable': True,
        'description': (
            'Dict de confiança por eixo do contrato. Chaves possíveis: '
            'disease_axis, has_control_group, is_single_cell, sample_join_key. '
            'Ausência de chave = classificador ainda não rodou para o eixo. '
            'Valores são float 0.0–1.0 (exceto disease_axis que pode ser sub-objeto).'
        ),
        'additionalProperties': True,
    })
    def get_contract_confidence(self, obj) -> dict | None:
        """
        Retorna contract_confidence inteiro — é o dict de confiança por eixo,
        necessário para o consumidor do export filtrar/ranquear por confiança.
        Não contém dados sensíveis (scores floats de classificadores).
        """
        return obj.dataset.contract_confidence or None

    @extend_schema_field({
        'type': 'object',
        'nullable': True,
        'description': (
            'Sub-objeto de extra_metadata["contract"] com allowlist explícita de campos. '
            'Chaves expostas: matrix_pointer (ponteiro, nunca conteúdo), '
            'proteomics_modality, tissue_raw (list), disease_raw (list), '
            'ref_pmids (list[int]), ref_dois (list[str]), '
            'monogenic_gene_hit (objeto com genes/confidence/gene_details). '
            'Ausência de extra_metadata["contract"] retorna null.'
        ),
        'properties': {
            'matrix_pointer': {
                'type': 'string',
                'nullable': True,
                'description': 'Ponteiro (URL FTP, path S3 ou accession) — nunca conteúdo numérico.',
            },
            'proteomics_modality': {
                'type': 'string',
                'nullable': True,
                'enum': ['phospho', 'global', None],
            },
            'tissue_raw': {
                'type': 'array',
                'items': {'type': 'string'},
                'nullable': True,
                'description': 'Nomes brutos de tecidos declarados na fonte (PRIDE).',
            },
            'disease_raw': {
                'type': 'array',
                'items': {'type': 'string'},
                'nullable': True,
                'description': 'Nomes brutos de doenças declarados na fonte (PRIDE). Insumo da Fase 3.',
            },
            'ref_pmids': {
                'type': 'array',
                'items': {'type': 'integer'},
                'nullable': True,
            },
            'ref_dois': {
                'type': 'array',
                'items': {'type': 'string'},
                'nullable': True,
            },
            'monogenic_gene_hit': {
                'type': 'object',
                'nullable': True,
                'properties': {
                    'genes': {'type': 'array', 'items': {'type': 'string'}},
                    'confidence': {'type': 'number'},
                    'gene_details': {
                        'type': 'array',
                        'items': {'type': 'object', 'additionalProperties': True},
                    },
                },
            },
        },
    })
    def get_contract(self, obj) -> dict | None:
        """
        Retorna sub-objeto de extra_metadata['contract'] com allowlist explícita.
        Nunca serializa o dict cru — apenas as chaves da _CONTRACT_ALLOWLIST.
        matrix_pointer é ponteiro (URL/path/accession), nunca dado numérico.
        Ausência de extra_metadata['contract'] retorna None sem quebrar.
        """
        raw_contract = (obj.dataset.extra_metadata or {}).get('contract')
        if not raw_contract:
            return None
        # Allowlist explícita — nunca expor chave fora do conjunto autorizado
        return {key: raw_contract.get(key) for key in _CONTRACT_ALLOWLIST}

    @extend_schema_field({
        'type': 'boolean',
        'description': (
            'True quando access_type == "controlled" (ex: dbGaP, EGA). '
            'matrix_pointer presente nestes datasets é ponteiro de acesso controlado — '
            'o carregamento real requer credenciais/aprovação de acesso (Objetivo 2).'
        ),
    })
    def get_access_controlled(self, obj) -> bool:
        """
        True quando access_type == 'controlled'.
        Permite ao consumidor do export identificar datasets de acesso controlado
        sem precisar interpretar o campo access_type.
        """
        return obj.dataset.access_type == 'controlled'

    @extend_schema_field({
        'type': 'string',
        'description': (
            'Versão do snapshot em que este item foi incluído. '
            '"live" indica consulta ao vivo sem versionamento. '
            'Injetado via context["snapshot_version"] pela view ou command.'
        ),
    })
    def get_snapshot_version(self, obj) -> str:
        """
        Retorna snapshot_version injetado via context.
        Default 'live' quando não especificado (consulta ao vivo).
        """
        return self.context.get('snapshot_version', 'live')

    class Meta:
        model = ProjectDataset
        fields = [
            # Estruturais
            'accession',
            'source_db',
            'omics_layers',
            'omics_count',
            'is_single_cell',
            'has_control_group',
            'disease_axis',
            'data_format',
            'access_type',
            'sample_join_key',
            # Confiança por eixo
            'contract_confidence',
            # Sub-objeto contract (allowlist)
            'contract',
            # Derivados
            'access_controlled',
            'snapshot_version',
        ]
        read_only_fields = fields
