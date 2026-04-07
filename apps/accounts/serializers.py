from rest_framework import serializers
from django.contrib.auth import get_user_model
from apps.accounts.models import UserProfile

User = get_user_model()


class UserProfileSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source='user.email', read_only=True)
    first_name = serializers.CharField(source='user.first_name', read_only=True)
    last_name = serializers.CharField(source='user.last_name', read_only=True)

    class Meta:
        model = UserProfile
        fields = [
            'id', 'email', 'first_name', 'last_name',
            'firebase_uid', 'auth_provider', 'orcid_id',
            'institution', 'research_area', 'avatar_url',
            'ncbi_api_key',
        ]
        extra_kwargs = {
            'ncbi_api_key': {'write_only': True},
        }
        read_only_fields = ['id', 'firebase_uid', 'auth_provider']


class UserProfileUpdateSerializer(serializers.ModelSerializer):
    """Para atualização de perfil pelo pesquisador."""
    first_name = serializers.CharField(write_only=True, required=False)
    last_name = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = UserProfile
        fields = ['orcid_id', 'institution', 'research_area', 'first_name', 'last_name', 'ncbi_api_key']

    def update(self, instance, validated_data):
        first_name = validated_data.pop('first_name', None)
        last_name = validated_data.pop('last_name', None)

        if first_name is not None:
            instance.user.first_name = first_name
        if last_name is not None:
            instance.user.last_name = last_name
        if first_name is not None or last_name is not None:
            instance.user.save(update_fields=['first_name', 'last_name'])

        return super().update(instance, validated_data)
