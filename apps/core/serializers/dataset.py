from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from apps.core.models import OmicDataset, ProjectDataset, ProjectPaperDataset


class OmicDatasetSerializer(serializers.ModelSerializer):
    # extra_metadata: dict livre de campos específicos da fonte (BioProject, GEO, SRA…)
    @extend_schema_field({'type': 'object'})
    def get_extra_metadata(self, obj):
        return obj.extra_metadata

    extra_metadata = serializers.SerializerMethodField()

    class Meta:
        model = OmicDataset
        fields = [
            'id', 'accession', 'source_db', 'bioproject_id',
            'title', 'summary', 'omic_type', 'omic_subcategory',
            'organism', 'tax_id', 'n_samples', 'platform',
            'extra_metadata', 'is_active', 'ingested_at',
            # Contrato de dados OmnisPathway (migrations 0017/0018)
            'omics_count', 'omics_layers', 'is_single_cell', 'has_control_group',
            'disease_axis', 'data_format', 'access_type', 'sample_join_key',
            'contract_confidence',
        ]


class ProjectDatasetListSerializer(serializers.ModelSerializer):
    """Compact list — includes summary and platform for the detail panel."""
    accession = serializers.CharField(source='dataset.accession', read_only=True)
    source_db = serializers.CharField(source='dataset.source_db', read_only=True)
    bioproject_id = serializers.CharField(source='dataset.bioproject_id', read_only=True)
    title = serializers.CharField(source='dataset.title', read_only=True)
    summary = serializers.CharField(source='dataset.summary', read_only=True)
    omic_type = serializers.CharField(source='dataset.omic_type', read_only=True)
    omic_subcategory = serializers.CharField(source='dataset.omic_subcategory', read_only=True)
    organism = serializers.CharField(source='dataset.organism', read_only=True)
    n_samples = serializers.IntegerField(source='dataset.n_samples', read_only=True, allow_null=True)
    platform = serializers.CharField(source='dataset.platform', read_only=True)

    @extend_schema_field({'type': 'object'})
    def get_extra_metadata(self, obj):
        return obj.dataset.extra_metadata

    extra_metadata = serializers.SerializerMethodField()

    # Eixos do contrato OmnisPathway — usados pela UI de curadoria
    has_control_group = serializers.CharField(source='dataset.has_control_group', read_only=True)
    disease_axis = serializers.CharField(source='dataset.disease_axis', read_only=True)
    omics_count = serializers.IntegerField(source='dataset.omics_count', read_only=True, allow_null=True)
    is_single_cell = serializers.CharField(source='dataset.is_single_cell', read_only=True)
    access_type = serializers.CharField(source='dataset.access_type', read_only=True)
    data_format = serializers.CharField(source='dataset.data_format', read_only=True)
    omics_layers = serializers.ListField(
        child=serializers.CharField(),
        source='dataset.omics_layers',
        read_only=True,
    )

    class Meta:
        model = ProjectDataset
        fields = [
            'id', 'accession', 'source_db', 'bioproject_id', 'title', 'summary',
            'omic_type', 'omic_subcategory', 'organism', 'n_samples', 'platform',
            'extra_metadata',
            'curation_status', 'exclusion_reason', 'notes',
            'relevance_score', 'added_at', 'curated_at',
            # Eixos OmnisPathway para UI de curadoria
            'has_control_group', 'disease_axis', 'omics_count',
            'is_single_cell', 'access_type', 'data_format', 'omics_layers',
        ]


class LinkedPaperBriefSerializer(serializers.ModelSerializer):
    """
    Resumo de um vínculo ProjectPaperDataset para exibir no detalhe de um dataset.

    Expõe os campos do paper vinculado + metadados do link.
    Filtrado pelo project_pk da rota — sem vazamento cross-project (Regra #3).
    """
    paper_pmid = serializers.IntegerField(
        source='project_paper.paper.pmid', read_only=True
    )
    paper_title = serializers.CharField(
        source='project_paper.paper.title', read_only=True
    )
    project_paper_id = serializers.IntegerField(
        source='project_paper.id', read_only=True
    )

    class Meta:
        model = ProjectPaperDataset
        fields = [
            'id', 'project_paper_id', 'paper_pmid', 'paper_title',
            'confidence', 'created_at',
        ]


class ProjectDatasetDetailSerializer(serializers.ModelSerializer):
    """Full detail: dataset content + curation fields + linked papers."""
    dataset = OmicDatasetSerializer(read_only=True)

    @extend_schema_field(LinkedPaperBriefSerializer(many=True))
    def get_linked_papers(self, obj):
        """
        Retorna os papers vinculados a este dataset dentro do projeto da rota.

        Filtrado por project_pk do contexto da view (Regra #3 — sem vazamento cross-project).
        Usa prefetch_related('projectpaperdataset__project_paper__paper') do viewset
        para evitar N+1. O obj aqui é ProjectDataset.
        """
        view = self.context.get('view')
        project_pk = view.kwargs.get('project_pk') if view else None
        if not project_pk:
            return []

        # accessor reverso de ProjectPaperDataset.project_dataset é 'projectpaperdataset_set'.
        links = obj.projectpaperdataset_set.all()
        # Filtra pelo project_pk da rota (segurança: impede cross-project)
        links = [lnk for lnk in links if str(lnk.project_id) == str(project_pk)]
        return LinkedPaperBriefSerializer(links, many=True).data

    linked_papers = serializers.SerializerMethodField()

    class Meta:
        model = ProjectDataset
        fields = [
            'id', 'dataset',
            'curation_status', 'exclusion_reason', 'notes',
            'relevance_score', 'added_at', 'curated_at',
            'linked_papers',
        ]
        read_only_fields = ['id', 'dataset', 'added_at', 'curated_at']


class ProjectDatasetCurateSerializer(serializers.ModelSerializer):
    """Write-only: update curation fields."""
    class Meta:
        model = ProjectDataset
        fields = ['curation_status', 'exclusion_reason', 'notes', 'relevance_score']


# ── Serializers de schema para ações customizadas ─────────────────────────────

class DatasetBulkFiltersSerializer(serializers.Serializer):
    """
    Filtros opcionais para bulk_curate por filtro (em vez de lista de IDs).

    Corresponde exatamente aos parâmetros aceitos por apply_dataset_filters().
    """
    curation_status = serializers.ChoiceField(
        choices=ProjectDataset.CurationStatus.choices,
        required=False,
        help_text="Filtrar por status de curadoria atual.",
    )
    omic_type = serializers.CharField(
        required=False,
        help_text="Tipo ômico exato (ex: 'transcriptomic', 'genomic').",
    )
    organism = serializers.CharField(
        required=False,
        help_text="Filtro parcial (icontains) no organismo.",
    )
    source_db = serializers.CharField(
        required=False,
        help_text="Banco de origem exato (ex: 'geo', 'sra').",
    )
    has_summary = serializers.CharField(
        required=False,
        help_text="'true' para incluir apenas datasets com summary.",
    )
    relevance_min = serializers.FloatField(
        required=False,
        help_text="Score de relevância mínimo (0.0–1.0).",
    )
    relevance_max = serializers.FloatField(
        required=False,
        help_text="Score de relevância máximo (0.0–1.0).",
    )
    ingestion_job = serializers.UUIDField(
        required=False,
        help_text="UUID do IngestionJob de proveniência.",
    )
    # ── Filtros do contrato OmnisPathway ──────────────────────────────────────
    has_control_group = serializers.ChoiceField(
        choices=OmicDataset.ControlGroup.choices,
        required=False,
        help_text="Presença de grupo controle (yes/no/unknown).",
    )
    disease_axis = serializers.ChoiceField(
        choices=OmicDataset.DiseaseAxis.choices,
        required=False,
        help_text="Eixo de doença (monogenic/multifactorial/indeterminate).",
    )
    is_single_cell = serializers.ChoiceField(
        choices=OmicDataset.SingleCell.choices,
        required=False,
        help_text="Tipo de sequenciamento (single_cell/bulk/unknown).",
    )
    data_format = serializers.ChoiceField(
        choices=OmicDataset.DataFormat.choices,
        required=False,
        help_text="Formato dos dados (raw/processed/unknown).",
    )
    access_type = serializers.ChoiceField(
        choices=OmicDataset.AccessType.choices,
        required=False,
        help_text="Tipo de acesso (public/controlled/unknown).",
    )
    omics_count_min = serializers.IntegerField(
        required=False,
        help_text="Número mínimo de camadas ômicas.",
    )
    omics_count_max = serializers.IntegerField(
        required=False,
        help_text="Número máximo de camadas ômicas.",
    )
    omics_layer = serializers.CharField(
        required=False,
        help_text="Camada ômica a filtrar por containment (ex: 'transcriptomic').",
    )
    has_sample_join_key = serializers.BooleanField(
        required=False,
        help_text="True para retornar apenas datasets com sample_join_key preenchido.",
    )


class DatasetBulkCurateRequestSerializer(serializers.Serializer):
    """
    Body de bulk_curate de datasets.

    Modos mutuamente exclusivos:
      - dataset_ids: lista explícita de IDs de ProjectDataset
      - filters: objeto de filtros (mesmos params da listagem + relevance_min/max + ingestion_job)

    Exatamente um dos dois deve estar presente.
    """
    dataset_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        help_text="Lista de IDs de ProjectDataset a atualizar.",
    )
    filters = DatasetBulkFiltersSerializer(
        required=False,
        help_text="Filtros para selecionar datasets (alternativa a dataset_ids).",
    )
    curation_status = serializers.ChoiceField(
        choices=ProjectDataset.CurationStatus.choices,
        help_text="Status de curadoria a aplicar.",
    )
    exclusion_reason = serializers.CharField(
        required=False,
        default='',
        allow_blank=True,
        help_text="Motivo de exclusão (usado quando curation_status=excluded).",
    )

    def validate(self, data):
        has_ids = data.get('dataset_ids') is not None
        has_filters = data.get('filters') is not None
        if not has_ids and not has_filters:
            raise serializers.ValidationError(
                "Forneça 'dataset_ids' ou 'filters'."
            )
        return data


class BulkCurateResponseSerializer(serializers.Serializer):
    """Resposta genérica de operações bulk: quantidade de registros atualizados."""
    updated = serializers.IntegerField()
