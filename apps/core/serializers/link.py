from rest_framework import serializers
from apps.core.models import ProjectPaperDataset


class ProjectPaperDatasetSerializer(serializers.ModelSerializer):
    pmid = serializers.IntegerField(source='project_paper.paper.pmid', read_only=True)
    paper_title = serializers.CharField(source='project_paper.paper.title', read_only=True)
    accession = serializers.CharField(source='project_dataset.dataset.accession', read_only=True)
    dataset_title = serializers.CharField(source='project_dataset.dataset.title', read_only=True)
    omic_type = serializers.CharField(source='project_dataset.dataset.omic_type', read_only=True)

    class Meta:
        model = ProjectPaperDataset
        fields = [
            'id', 'pmid', 'paper_title', 'accession', 'dataset_title',
            'omic_type', 'confidence', 'created_at',
        ]
        read_only_fields = ['id', 'created_at']
