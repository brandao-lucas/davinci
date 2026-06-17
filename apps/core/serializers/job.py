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


class JobCancelErrorSerializer(serializers.Serializer):
    """Resposta de erro quando o job já está em estado terminal."""
    detail = serializers.CharField()
