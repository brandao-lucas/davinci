from rest_framework import serializers
from apps.core.models import (
    Paper, PaperAuthor, PaperKeyword, PaperMeSHTerm,
    PaperGene, PaperDrug, PaperVariant, EntityContext,
    ProjectPaper, ClinicalCategory, UserCategory,
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
    """Full detail: paper content + curation fields."""
    paper = PaperDetailSerializer(read_only=True)
    clinical_categories = ClinicalCategoryBriefSerializer(many=True, read_only=True)
    user_categories = UserCategoryBriefSerializer(many=True, read_only=True)

    class Meta:
        model = ProjectPaper
        fields = [
            'id', 'paper',
            'curation_status', 'exclusion_reason', 'notes',
            'relevance_score', 'clinical_categories', 'user_categories',
            'added_at', 'curated_at',
        ]
        read_only_fields = ['id', 'paper', 'added_at', 'curated_at']


class ProjectPaperCurateSerializer(serializers.ModelSerializer):
    """Write-only: update curation fields."""
    class Meta:
        model = ProjectPaper
        fields = ['curation_status', 'exclusion_reason', 'notes', 'relevance_score']


# ── Serializers de schema para ações customizadas ─────────────────────────────

class PaperBulkCurateRequestSerializer(serializers.Serializer):
    """Body de bulk_curate: atualiza curation_status para múltiplos papers."""
    paper_ids = serializers.ListField(
        child=serializers.IntegerField(),
        help_text="Lista de IDs de ProjectPaper a atualizar.",
    )
    curation_status = serializers.ChoiceField(
        choices=ProjectPaper.CurationStatus.choices,
        help_text="Status de curadoria a aplicar.",
    )


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
