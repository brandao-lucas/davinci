"""
Serializers de links literatura↔ômica.

--- ProjectPaperDatasetSerializer (Nível 1 — vínculos confirmados) ---

Contrato JSON canônico exposto por GET /projects/{id}/links/:

    {
        "id":                 <int>,
        "paper_pmid":         <int>,        // antes: "pmid"
        "paper_title":        <str>,
        "dataset_accession":  <str>,        // antes: "accession"
        "dataset_title":      <str>,
        "omic_type":          <str>,
        "confidence":         "auto" | "confirmed" | "rejected",
        "created_at":         <ISO 8601>
    }

--- OrphanLinkSuggestionSerializer (Nível 2 — read-only, sugestões de órfãos) ---

Contrato JSON canônico exposto por GET /projects/{id}/links/suggestions/:

    {
        "suggestion_type":    "dataset_missing" | "paper_missing",
        "global_link_id":     <int>,
        "link_source":        <str>,

        // Ponta que JÁ está no projeto:
        "project_paper_id":   <int | null>,    // null quando suggestion_type == "paper_missing"
        "paper_pmid":         <int>,
        "paper_title":        <str>,

        "project_dataset_id": <int | null>,    // null quando suggestion_type == "dataset_missing"
        "dataset_id":         <int>,
        "dataset_accession":  <str>,
        "dataset_title":      <str>,
        "omic_type":          <str>
    }

    Quando suggestion_type == "dataset_missing":
        project_paper_id   = <int>  (paper já está no projeto)
        project_dataset_id = null   (dataset não está — id do OmicDataset global em dataset_id)

    Quando suggestion_type == "paper_missing":
        project_paper_id   = null   (paper não está — PMID em paper_pmid, sem project_paper_id)
        project_dataset_id = <int>  (dataset já está no projeto)

Handoff atelier: o front já usa paper_pmid/dataset_accession em
davinci-frontend/src/lib/api/links.ts (tipo PaperDatasetLink).
Este serializer agora casa com esse contrato — nenhuma mudança no frontend é necessária.
"""

from rest_framework import serializers
from apps.core.models import ProjectPaperDataset


# ---------------------------------------------------------------------------
# Serializers de criação avulsa de ponta órfã (B2 — "adicionar ao projeto")
# ---------------------------------------------------------------------------

class AddPaperToProjectRequestSerializer(serializers.Serializer):
    """
    Request body para POST /projects/{project_pk}/papers/add_from_suggestion/.

    O campo `pmid` identifica o Paper global a ser vinculado ao projeto.
    Esse PMID vem de OrphanLinkSuggestionSerializer.paper_pmid (Nível 2).
    """
    pmid = serializers.IntegerField(
        help_text='PMID do paper global (Paper.pmid) a adicionar ao projeto.',
        min_value=1,
    )


class AddDatasetToProjectRequestSerializer(serializers.Serializer):
    """
    Request body para POST /projects/{project_pk}/datasets/add_from_suggestion/.

    O campo `dataset_id` identifica o OmicDataset global a ser vinculado.
    Esse id vem de OrphanLinkSuggestionSerializer.dataset_id (Nível 2).
    """
    dataset_id = serializers.IntegerField(
        help_text='PK de OmicDataset (dataset global) a adicionar ao projeto.',
        min_value=1,
    )


class ProjectPaperDatasetSerializer(serializers.ModelSerializer):
    # Renomeados de pmid/accession para paper_pmid/dataset_accession
    # para casar com o tipo PaperDatasetLink do frontend.
    paper_pmid = serializers.IntegerField(source='project_paper.paper.pmid', read_only=True)
    paper_title = serializers.CharField(source='project_paper.paper.title', read_only=True)
    dataset_accession = serializers.CharField(source='project_dataset.dataset.accession', read_only=True)
    dataset_title = serializers.CharField(source='project_dataset.dataset.title', read_only=True)
    omic_type = serializers.CharField(source='project_dataset.dataset.omic_type', read_only=True)

    class Meta:
        model = ProjectPaperDataset
        fields = [
            'id', 'paper_pmid', 'paper_title', 'dataset_accession', 'dataset_title',
            'omic_type', 'confidence', 'created_at',
        ]
        read_only_fields = ['id', 'created_at']


class OrphanLinkSuggestionSerializer(serializers.Serializer):
    """
    Serializer READ-ONLY para sugestões de órfãos (Nível 2).

    Recebe dicts vindos de suggest_orphan_links() — não é um ModelSerializer
    porque o resultado é uma projeção SQL, não uma instância de model.

    Nunca grava nada: não há create/update.
    """
    suggestion_type = serializers.ChoiceField(
        choices=['dataset_missing', 'paper_missing'],
        help_text=(
            "'dataset_missing': paper já no projeto, dataset ausente. "
            "'paper_missing': dataset já no projeto, paper ausente."
        ),
    )
    global_link_id = serializers.IntegerField(
        help_text='PK de DatasetPaperLink (vínculo global descoberto pelo Rust via elink).'
    )
    link_source = serializers.CharField(
        help_text="Origem do vínculo global: 'elink', 'geo_xml', 'manual'."
    )

    # Ponta paper (sempre presente — é a base do vínculo)
    project_paper_id = serializers.IntegerField(
        allow_null=True,
        help_text=(
            'PK de ProjectPaper (paper já no projeto). '
            'null quando suggestion_type == paper_missing (paper ainda não está no projeto).'
        ),
    )
    paper_pmid = serializers.IntegerField(
        help_text='PMID do paper (ponta existente ou sugerida).'
    )
    paper_title = serializers.CharField(
        help_text='Título do paper.'
    )

    # Ponta dataset (sempre presente — é a base do vínculo)
    project_dataset_id = serializers.IntegerField(
        allow_null=True,
        help_text=(
            'PK de ProjectDataset (dataset já no projeto). '
            'null quando suggestion_type == dataset_missing (dataset ainda não está no projeto).'
        ),
    )
    dataset_id = serializers.IntegerField(
        help_text='PK de OmicDataset (dataset global — usado para adicionar ao projeto depois).'
    )
    dataset_accession = serializers.CharField(
        help_text='Accession do dataset (GSE*, SRP*, etc.).'
    )
    dataset_title = serializers.CharField(
        help_text='Título do dataset.'
    )
    omic_type = serializers.CharField(
        help_text='Tipo ômico do dataset (transcriptomic, genomic, etc.).'
    )
