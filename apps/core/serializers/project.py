from rest_framework import serializers
from apps.core.models import DaVinciProject

class DaVinciProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = DaVinciProject
        fields = '__all__'
        read_only_fields = ['id', 'user', 'slug', 'status', 'created_at', 'updated_at']
