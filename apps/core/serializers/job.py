from rest_framework import serializers
from apps.core.models import IngestionJob


class IngestionJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = IngestionJob
        fields = [
            'id', 'job_type', 'status', 'parameters',
            'records_processed', 'records_inserted', 'records_updated',
            'error_message', 'created_at', 'started_at', 'completed_at',
        ]
        read_only_fields = fields
