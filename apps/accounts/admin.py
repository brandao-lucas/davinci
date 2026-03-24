from django.contrib import admin
from apps.accounts.models import UserProfile


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'firebase_uid', 'auth_provider', 'institution', 'last_firebase_sync']
    search_fields = ['user__email', 'firebase_uid', 'orcid_id']
    list_filter = ['auth_provider']
    readonly_fields = ['firebase_uid', 'last_firebase_sync']
