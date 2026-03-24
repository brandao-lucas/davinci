from rest_framework import serializers
from apps.core.models import ClinicalCategory, UserCategory


class ClinicalCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ClinicalCategory
        fields = ['id', 'slug', 'name', 'description', 'keywords', 'is_default', 'priority']
        read_only_fields = ['id']


class UserCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = UserCategory
        fields = ['id', 'name', 'keywords', 'color', 'created_at']
        read_only_fields = ['id', 'created_at']
