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

    class Meta:
        model = ProjectDataset
        fields = [
            'id', 'accession', 'source_db', 'bioproject_id', 'title', 'summary',
            'omic_type', 'omic_subcategory', 'organism', 'n_samples', 'platform',
            'extra_metadata',
            'curation_status', 'exclusion_reason', 'notes',
            'relevance_score', 'added_at', 'curated_at',
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

class DatasetBulkCurateRequestSerializer(serializers.Serializer):
    """Body de bulk_curate de datasets."""
    dataset_ids = serializers.ListField(
        child=serializers.IntegerField(),
        help_text="Lista de IDs de ProjectDataset a atualizar.",
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


class BulkCurateResponseSerializer(serializers.Serializer):
    """Resposta genérica de operações bulk: quantidade de registros atualizados."""
    updated = serializers.IntegerField()
