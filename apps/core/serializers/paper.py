from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from apps.core.models import (
    Paper, PaperAuthor, PaperKeyword, PaperMeSHTerm,
    PaperGene, PaperDrug, PaperVariant, EntityContext,
    ProjectPaper, ProjectPaperDataset, ClinicalCategory, UserCategory,
)


class PaperAuthorSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaperAuthor
        fields = ['position', 'last_name', 'initials', 'affiliation', 'country']


class PaperKeywordSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaperKeyword
        fields = ['keyword']


class PaperMeSHTermSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaperMeSHTerm
        fields = ['descriptor', 'qualifier', 'is_major_topic']


class PaperGeneSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaperGene
        fields = ['gene_symbol', 'entrez_id', 'mention_count']


class PaperDrugSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaperDrug
        fields = ['drug_name', 'mention_count', 'drugbank_id']


class PaperVariantSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaperVariant
        fields = ['rs_number']


class EntityContextSerializer(serializers.ModelSerializer):
    class Meta:
        model = EntityContext
        fields = ['entity_type', 'entity_name', 'sentence', 'sentence_position']


class PaperDetailSerializer(serializers.ModelSerializer):
    """Full paper detail with all nested entities."""
    authors = PaperAuthorSerializer(many=True, read_only=True)
    keywords = PaperKeywordSerializer(many=True, read_only=True)
    mesh_terms = PaperMeSHTermSerializer(many=True, read_only=True)
    genes = PaperGeneSerializer(many=True, read_only=True)
    drugs = PaperDrugSerializer(many=True, read_only=True)
    variants = PaperVariantSerializer(many=True, read_only=True)
    contexts = EntityContextSerializer(many=True, read_only=True)

    class Meta:
        model = Paper
        fields = [
            'id', 'pmid', 'pmc_id', 'doi', 'title', 'abstract',
            'journal', 'pub_year', 'pub_month',
            'authors', 'keywords', 'mesh_terms', 'genes', 'drugs',
            'variants', 'contexts', 'ingested_at',
        ]


class LinkedDatasetBriefSerializer(serializers.ModelSerializer):
    """
    Resumo de um vínculo ProjectPaperDataset para exibir no detalhe de um paper.

    Expõe os campos do dataset vinculado + metadados do link.
    Filtrado pelo project_pk da rota — sem vazamento cross-project (Regra #3).
    """
    dataset_accession = serializers.CharField(
        source='project_dataset.dataset.accession', read_only=True
    )
    dataset_title = serializers.CharField(
        source='project_dataset.dataset.title', read_only=True
    )
    omic_type = serializers.CharField(
        source='project_dataset.dataset.omic_type', read_only=True
    )
    project_dataset_id = serializers.IntegerField(
        source='project_dataset.id', read_only=True
    )

    class Meta:
        model = ProjectPaperDataset
        fields = [
            'id', 'project_dataset_id', 'dataset_accession', 'dataset_title',
            'omic_type', 'confidence', 'created_at',
        ]


class ClinicalCategoryBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClinicalCategory
        fields = ['id', 'slug', 'name']


class UserCategoryBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserCategory
        fields = ['id', 'name', 'color']


class ProjectPaperListSerializer(serializers.ModelSerializer):
    """Compact representation for list views — includes abstract for detail panel."""
    pmid = serializers.IntegerField(source='paper.pmid', read_only=True)
    pmc_id = serializers.CharField(source='paper.pmc_id', read_only=True)
    doi = serializers.CharField(source='paper.doi', read_only=True)
    title = serializers.CharField(source='paper.title', read_only=True)
    abstract = serializers.CharField(source='paper.abstract', read_only=True)
    journal = serializers.CharField(source='paper.journal', read_only=True)
    pub_year = serializers.IntegerField(source='paper.pub_year', read_only=True)
    pub_month = serializers.IntegerField(source='paper.pub_month', read_only=True, allow_null=True)
    pub_type = serializers.CharField(source='paper.pub_type', read_only=True)
    clinical_categories = ClinicalCategoryBriefSerializer(many=True, read_only=True)
    user_categories = UserCategoryBriefSerializer(many=True, read_only=True)

    class Meta:
        model = ProjectPaper
        fields = [
            'id', 'pmid', 'pmc_id', 'doi', 'title', 'abstract',
            'journal', 'pub_year', 'pub_month', 'pub_type',
            'curation_status', 'exclusion_reason', 'notes',
            'relevance_score', 'clinical_categories', 'user_categories',
            'added_at', 'curated_at',
        ]


class ProjectPaperDetailSerializer(serializers.ModelSerializer):
    """Full detail: paper content + curation fields + linked datasets."""
    paper = PaperDetailSerializer(read_only=True)
    clinical_categories = ClinicalCategoryBriefSerializer(many=True, read_only=True)
    user_categories = UserCategoryBriefSerializer(many=True, read_only=True)

    @extend_schema_field(LinkedDatasetBriefSerializer(many=True))
    def get_linked_datasets(self, obj):
        """
        Retorna os datasets vinculados a este paper dentro do projeto da rota.

        Filtrado por project_pk do contexto da view (Regra #3 — sem vazamento cross-project).
        Usa prefetch_related('projectpaperdataset__project_dataset__dataset') do viewset
        para evitar N+1. O obj aqui é ProjectPaper (não Paper).
        """
        view = self.context.get('view')
        project_pk = view.kwargs.get('project_pk') if view else None
        if not project_pk:
            return []

        # Aproveita o prefetch quando disponível (see viewset).
        # accessor reverso de ProjectPaperDataset.project_paper é 'projectpaperdataset_set'.
        links = obj.projectpaperdataset_set.all()
        # Filtra pelo project_pk da rota (segurança: impede cross-project)
        links = [lnk for lnk in links if str(lnk.project_id) == str(project_pk)]
        return LinkedDatasetBriefSerializer(links, many=True).data

    linked_datasets = serializers.SerializerMethodField()

    class Meta:
        model = ProjectPaper
        fields = [
            'id', 'paper',
            'curation_status', 'exclusion_reason', 'notes',
            'relevance_score', 'clinical_categories', 'user_categories',
            'added_at', 'curated_at',
            'linked_datasets',
        ]
        read_only_fields = ['id', 'paper', 'added_at', 'curated_at']


class ProjectPaperCurateSerializer(serializers.ModelSerializer):
    """Write-only: update curation fields."""
    class Meta:
        model = ProjectPaper
        fields = ['curation_status', 'exclusion_reason', 'notes', 'relevance_score']


# ── Serializers de schema para ações customizadas ─────────────────────────────

class PaperBulkFiltersSerializer(serializers.Serializer):
    """
    Filtros opcionais para bulk_curate por filtro (em vez de lista de IDs).

    Corresponde exatamente aos parâmetros aceitos por apply_paper_filters().
    """
    curation_status = serializers.ChoiceField(
        choices=ProjectPaper.CurationStatus.choices,
        required=False,
        help_text="Filtrar por status de curadoria atual.",
    )
    pub_year_min = serializers.IntegerField(
        required=False,
        help_text="Ano de publicação mínimo (inclusive).",
    )
    pub_year_max = serializers.IntegerField(
        required=False,
        help_text="Ano de publicação máximo (inclusive).",
    )
    journal = serializers.CharField(
        required=False,
        help_text="Filtro parcial (icontains) no nome do periódico.",
    )
    pub_type = serializers.CharField(
        required=False,
        help_text="Tipo de publicação exato (ex: 'Review', 'Clinical Trial').",
    )
    has_abstract = serializers.CharField(
        required=False,
        help_text="'true' para incluir apenas papers com abstract.",
    )
    free_full_text = serializers.CharField(
        required=False,
        help_text="'true' para incluir apenas papers com PMC ID (full text disponível).",
    )
    clinical_category = serializers.CharField(
        required=False,
        help_text="Slug de ClinicalCategory.",
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


class PaperBulkCurateRequestSerializer(serializers.Serializer):
    """
    Body de bulk_curate: atualiza curation_status para múltiplos papers.

    Modos mutuamente exclusivos:
      - paper_ids: lista explícita de IDs de ProjectPaper
      - filters: objeto de filtros (mesmos params da listagem + relevance_min/max + ingestion_job)

    Exatamente um dos dois deve estar presente.
    """
    paper_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        help_text="Lista de IDs de ProjectPaper a atualizar.",
    )
    filters = PaperBulkFiltersSerializer(
        required=False,
        help_text="Filtros para selecionar papers (alternativa a paper_ids).",
    )
    curation_status = serializers.ChoiceField(
        choices=ProjectPaper.CurationStatus.choices,
        help_text="Status de curadoria a aplicar.",
    )
    exclusion_reason = serializers.CharField(
        required=False,
        allow_blank=True,
        default='',
        help_text="Motivo de exclusão (recomendado quando curation_status=excluded).",
    )

    def validate(self, data):
        has_ids = data.get('paper_ids') is not None
        has_filters = data.get('filters') is not None
        if not has_ids and not has_filters:
            raise serializers.ValidationError(
                "Forneça 'paper_ids' ou 'filters'."
            )
        return data


class PaperBulkCurateResponseSerializer(serializers.Serializer):
    """Resposta de bulk_curate: quantidade de registros atualizados."""
    updated = serializers.IntegerField()


class PaperCategorizeRequestSerializer(serializers.Serializer):
    """Body de categorize: adiciona/remove categorias clínicas e de usuário."""
    clinical_add = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
        help_text="Slugs de categorias clínicas a adicionar.",
    )
    clinical_remove = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
        help_text="Slugs de categorias clínicas a remover.",
    )
    user_add = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        default=list,
        help_text="IDs de UserCategory a adicionar.",
    )
    user_remove = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        default=list,
        help_text="IDs de UserCategory a remover.",
    )
