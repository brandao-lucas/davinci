import uuid
from django.conf import settings
from django.db import models


class UserProfile(models.Model):
    """
    Extensão do User Django com dados do Firebase.
    Relacionamento OneToOne com auth.User.

    O campo firebase_uid é a ponte entre o Firebase e o Django.
    Criado automaticamente na primeira request autenticada via FirebaseAuthentication.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='profile',
    )
    firebase_uid = models.CharField(
        max_length=128,
        unique=True,
        db_index=True,
        help_text='UID do Firebase Authentication',
    )
    auth_provider = models.CharField(
        max_length=50,
        default='password',
        help_text='Provider usado no login (google.com, password, oidc.orcid)',
    )
    orcid_id = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        unique=True,
        help_text='ORCID iD do pesquisador (formato: 0000-0000-0000-0000)',
    )
    institution = models.CharField(max_length=255, blank=True, default='')
    research_area = models.CharField(max_length=255, blank=True, default='')
    avatar_url = models.URLField(blank=True, default='')
    ncbi_api_key = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text='NCBI API Key pessoal (eleva rate limit de 3 para 10 req/s)',
    )
    last_firebase_sync = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'accounts_userprofile'
        verbose_name = 'User Profile'

    def __str__(self):
        return f"{self.user.email} ({self.firebase_uid})"
