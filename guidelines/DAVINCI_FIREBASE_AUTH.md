# DaVinci — Firebase Authentication (Prompt de Implementação)

## Resumo

Este documento define a implementação do sistema de autenticação do DaVinci usando **Firebase Authentication** como provider externo + **Django** como backend de validação. O Firebase emite JWTs no frontend, o Django valida esses tokens a cada request via um custom authentication backend no DRF, e o usuário Firebase é mapeado automaticamente para um `User` Django na primeira requisição autenticada.

**Pré-requisito:** Fase 4 do DaVinci já concluída (models, migrations, API básica, Celery).

---

## 1. Arquitetura de Autenticação

```
Frontend (Next.js / Tauri / React Native)
    ↓
Firebase Auth SDK (login Google, Email/Password, ORCID via OIDC)
    ↓
Firebase emite ID Token (JWT)
    ↓
Frontend envia JWT no header: Authorization: Bearer <token>
    ↓
Django DRF → FirebaseAuthentication backend
    ↓
Valida JWT com firebase-admin SDK
    ↓
Busca ou cria User Django (get_or_create por firebase_uid)
    ↓
request.user = User Django (com perfil vinculado)
```

### Princípio Fundamental

O Firebase **nunca** toca no banco do Django. Ele é apenas o porteiro — valida identidade e emite tokens. O Django mantém sua própria tabela de usuários com um campo `firebase_uid` como ponte. Toda lógica de permissões, projetos e dados continua no Django.

---

## 2. Setup Firebase (Console)

### 2.1 Criar Projeto Firebase

1. Acessar https://console.firebase.google.com/
2. Criar projeto: `platomics-davinci`
3. Desabilitar Google Analytics (não necessário para auth)

### 2.2 Habilitar Providers

No Firebase Console → Authentication → Sign-in method, habilitar:

| Provider | Config | Prioridade |
|----------|--------|------------|
| **Email/Password** | Habilitar (sem email link) | MVP |
| **Google** | Habilitar com Web Client ID | MVP |
| **ORCID** (via OpenID Connect) | Custom OIDC provider | Pós-MVP |

### 2.3 ORCID como Custom OIDC Provider (Pós-MVP)

ORCID suporta OAuth 2.0 / OpenID Connect. No Firebase:
1. Authentication → Sign-in method → Add new provider → OpenID Connect
2. Provider ID: `oidc.orcid`
3. Client ID: obtido em https://orcid.org/developer-tools
4. Issuer URL: `https://orcid.org`
5. Client Secret: obtido no ORCID

### 2.4 Gerar Service Account Key

1. Firebase Console → Project Settings → Service accounts
2. Gerar nova chave privada (JSON)
3. Salvar como `firebase-service-account.json` na raiz do projeto
4. **NUNCA commitar esse arquivo** — adicionar ao `.gitignore`

---

## 3. Backend Django — Implementação

### 3.1 Dependências

```bash
pip install firebase-admin
```

Adicionar ao `requirements.txt`:
```
firebase-admin>=6.0
```

### 3.2 Configuração (`config/settings/base.py`)

```python
import firebase_admin
from firebase_admin import credentials
import os

# Firebase Admin SDK
FIREBASE_CREDENTIALS_PATH = os.environ.get(
    'FIREBASE_CREDENTIALS_PATH',
    os.path.join(BASE_DIR, 'firebase-service-account.json')
)

# Inicializar Firebase Admin (uma única vez)
if not firebase_admin._apps:
    cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
    firebase_admin.initialize_app(cred)

# DRF Authentication
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'apps.accounts.authentication.FirebaseAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    # ... manter demais configs existentes
}
```

### 3.3 App `accounts` — Estrutura

```
apps/accounts/
├── __init__.py
├── admin.py
├── apps.py
├── authentication.py       # Custom DRF Authentication Backend
├── models.py               # UserProfile (extends User)
├── serializers.py
├── services/
│   ├── __init__.py
│   └── user_service.py     # Lógica de criação/atualização de usuário
├── views.py
├── urls.py
├── migrations/
│   └── 0001_initial.py
└── tests/
    ├── test_authentication.py
    └── test_user_service.py
```

### 3.4 Model — `UserProfile`

```python
# apps/accounts/models.py

import uuid
from django.conf import settings
from django.db import models


class UserProfile(models.Model):
    """
    Extensão do User Django com dados do Firebase.
    Relacionamento OneToOne com auth.User.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='profile'
    )
    firebase_uid = models.CharField(
        max_length=128,
        unique=True,
        db_index=True,
        help_text="UID do Firebase Authentication"
    )
    auth_provider = models.CharField(
        max_length=50,
        default='email',
        help_text="Provider usado no login (google.com, password, oidc.orcid)"
    )
    orcid_id = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        unique=True,
        help_text="ORCID iD do pesquisador (formato: 0000-0000-0000-0000)"
    )
    institution = models.CharField(max_length=255, blank=True, default='')
    research_area = models.CharField(max_length=255, blank=True, default='')
    avatar_url = models.URLField(blank=True, default='')
    last_firebase_sync = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'accounts_userprofile'
        verbose_name = 'User Profile'

    def __str__(self):
        return f"{self.user.email} ({self.firebase_uid})"
```

### 3.5 Authentication Backend

```python
# apps/accounts/authentication.py

from rest_framework import authentication, exceptions
from firebase_admin import auth as firebase_auth
from apps.accounts.services.user_service import UserService


class FirebaseAuthentication(authentication.BaseAuthentication):
    """
    Autentica requests usando Firebase ID Tokens.
    
    O frontend envia: Authorization: Bearer <firebase_id_token>
    Este backend:
    1. Extrai o token do header
    2. Valida com Firebase Admin SDK
    3. Busca ou cria o User Django correspondente
    4. Retorna (user, decoded_token)
    """

    def authenticate(self, request):
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')

        if not auth_header.startswith('Bearer '):
            return None  # Não é Firebase auth, deixa outro backend tentar

        token = auth_header.split('Bearer ')[1].strip()

        if not token:
            return None

        try:
            decoded_token = firebase_auth.verify_id_token(token)
        except firebase_auth.ExpiredIdTokenError:
            raise exceptions.AuthenticationFailed('Token expirado. Faça login novamente.')
        except firebase_auth.InvalidIdTokenError:
            raise exceptions.AuthenticationFailed('Token inválido.')
        except firebase_auth.RevokedIdTokenError:
            raise exceptions.AuthenticationFailed('Token revogado.')
        except Exception:
            raise exceptions.AuthenticationFailed('Erro ao validar token.')

        user = UserService.get_or_create_from_firebase(decoded_token)
        return (user, decoded_token)

    def authenticate_header(self, request):
        return 'Bearer realm="firebase"'
```

### 3.6 User Service

```python
# apps/accounts/services/user_service.py

from django.contrib.auth import get_user_model
from apps.accounts.models import UserProfile

User = get_user_model()


class UserService:
    """
    Service para gerenciar usuários a partir de tokens Firebase.
    Segue a regra: Services over Signals.
    """

    @staticmethod
    def get_or_create_from_firebase(decoded_token: dict) -> User:
        """
        Busca ou cria um User Django a partir de um token Firebase decodificado.
        
        O token contém:
        - uid: ID único do Firebase
        - email: email do usuário
        - name: nome completo (pode não existir)
        - picture: URL do avatar (Google login)
        - firebase.sign_in_provider: provider usado
        """
        firebase_uid = decoded_token['uid']
        email = decoded_token.get('email', '')
        name = decoded_token.get('name', '')
        picture = decoded_token.get('picture', '')
        provider = decoded_token.get('firebase', {}).get('sign_in_provider', 'password')

        try:
            profile = UserProfile.objects.select_related('user').get(
                firebase_uid=firebase_uid
            )
            user = profile.user
            # Atualizar dados que podem ter mudado no Firebase
            UserService._sync_profile(profile, decoded_token)
            return user

        except UserProfile.DoesNotExist:
            # Primeiro login — criar User + Profile
            return UserService._create_user(
                firebase_uid=firebase_uid,
                email=email,
                name=name,
                picture=picture,
                provider=provider,
            )

    @staticmethod
    def _create_user(firebase_uid, email, name, picture, provider):
        """Cria User Django + UserProfile."""
        first_name, last_name = UserService._split_name(name)

        user = User.objects.create(
            username=firebase_uid,  # username = firebase_uid (garante unicidade)
            email=email,
            first_name=first_name,
            last_name=last_name,
            is_active=True,
        )
        # Senha inutilizável — login é via Firebase
        user.set_unusable_password()
        user.save()

        UserProfile.objects.create(
            user=user,
            firebase_uid=firebase_uid,
            auth_provider=provider,
            avatar_url=picture,
        )

        return user

    @staticmethod
    def _sync_profile(profile, decoded_token):
        """Sincroniza dados do Firebase com o profile local."""
        updated = False
        picture = decoded_token.get('picture', '')
        provider = decoded_token.get('firebase', {}).get('sign_in_provider', '')

        if picture and profile.avatar_url != picture:
            profile.avatar_url = picture
            updated = True
        if provider and profile.auth_provider != provider:
            profile.auth_provider = provider
            updated = True

        if updated:
            profile.save(update_fields=['avatar_url', 'auth_provider', 'last_firebase_sync'])

    @staticmethod
    def _split_name(name: str) -> tuple:
        """Divide nome completo em first_name e last_name."""
        if not name:
            return ('', '')
        parts = name.strip().split(' ', 1)
        return (parts[0], parts[1] if len(parts) > 1 else '')
```

### 3.7 Serializers

```python
# apps/accounts/serializers.py

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
        ]
        read_only_fields = ['id', 'firebase_uid', 'auth_provider']


class UserProfileUpdateSerializer(serializers.ModelSerializer):
    """Para atualização de perfil pelo pesquisador."""
    first_name = serializers.CharField(write_only=True, required=False)
    last_name = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = UserProfile
        fields = ['orcid_id', 'institution', 'research_area', 'first_name', 'last_name']

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
```

### 3.8 Views

```python
# apps/accounts/views.py

from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from apps.accounts.models import UserProfile
from apps.accounts.serializers import UserProfileSerializer, UserProfileUpdateSerializer


class MeView(APIView):
    """
    GET: Retorna perfil do usuário autenticado.
    PATCH: Atualiza perfil.
    
    Este endpoint é o primeiro que o frontend chama após login.
    Se o user não tem profile ainda, o FirebaseAuthentication já criou.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        profile = UserProfile.objects.select_related('user').get(
            user=request.user
        )
        serializer = UserProfileSerializer(profile)
        return Response(serializer.data)

    def patch(self, request):
        profile = UserProfile.objects.select_related('user').get(
            user=request.user
        )
        serializer = UserProfileUpdateSerializer(
            profile, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(UserProfileSerializer(profile).data)


class VerifyTokenView(APIView):
    """
    POST: Verifica se o token Firebase é válido.
    Usado pelo frontend para checar sessão sem carregar dados.
    Retorna 200 se válido, 401 se inválido.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        return Response({'status': 'valid', 'uid': request.user.profile.firebase_uid})
```

### 3.9 URLs

```python
# apps/accounts/urls.py

from django.urls import path
from apps.accounts.views import MeView, VerifyTokenView

urlpatterns = [
    path('me/', MeView.as_view(), name='user-me'),
    path('verify/', VerifyTokenView.as_view(), name='verify-token'),
]
```

```python
# config/urls.py (adicionar)

urlpatterns = [
    # ... existentes
    path('api/v1/auth/', include('apps.accounts.urls')),
]
```

### 3.10 Admin

```python
# apps/accounts/admin.py

from django.contrib import admin
from apps.accounts.models import UserProfile


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'firebase_uid', 'auth_provider', 'institution', 'last_firebase_sync']
    search_fields = ['user__email', 'firebase_uid', 'orcid_id']
    list_filter = ['auth_provider']
    readonly_fields = ['firebase_uid', 'last_firebase_sync']
```

---

## 4. Variáveis de Ambiente

```bash
# .env (desenvolvimento local)

FIREBASE_CREDENTIALS_PATH=/path/to/firebase-service-account.json
NCBI_API_KEY=your_ncbi_key

# Opcionais
FIREBASE_PROJECT_ID=platomics-davinci
```

```bash
# .gitignore (adicionar)

firebase-service-account.json
.env
```

---

## 5. Atualização do Model `DaVinciProject`

O `DaVinciProject` já tem um campo `user` (FK). Confirme que ele aponta para `settings.AUTH_USER_MODEL`:

```python
# apps/core/models.py — ajuste se necessário

class DaVinciProject(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='davinci_projects'
    )
    # ... demais campos
```

Com o Firebase Auth, o `request.user` em todos os ViewSets já será o User Django correto, então o filtro `DaVinciProject.objects.filter(user=self.request.user)` funciona sem alterações.

---

## 6. Testes

```python
# apps/accounts/tests/test_authentication.py

from unittest.mock import patch, MagicMock
from django.test import TestCase, RequestFactory
from django.contrib.auth import get_user_model
from apps.accounts.authentication import FirebaseAuthentication
from apps.accounts.models import UserProfile

User = get_user_model()


class FirebaseAuthenticationTest(TestCase):

    def setUp(self):
        self.factory = RequestFactory()
        self.auth = FirebaseAuthentication()

    def test_no_auth_header_returns_none(self):
        request = self.factory.get('/')
        result = self.auth.authenticate(request)
        self.assertIsNone(result)

    def test_non_bearer_header_returns_none(self):
        request = self.factory.get('/', HTTP_AUTHORIZATION='Basic abc123')
        result = self.auth.authenticate(request)
        self.assertIsNone(result)

    @patch('apps.accounts.authentication.firebase_auth.verify_id_token')
    def test_valid_token_creates_user(self, mock_verify):
        mock_verify.return_value = {
            'uid': 'firebase_test_uid_001',
            'email': 'researcher@university.edu',
            'name': 'Maria Silva',
            'picture': 'https://example.com/avatar.jpg',
            'firebase': {'sign_in_provider': 'google.com'},
        }

        request = self.factory.get('/', HTTP_AUTHORIZATION='Bearer valid_token')
        user, token = self.auth.authenticate(request)

        self.assertEqual(user.email, 'researcher@university.edu')
        self.assertEqual(user.first_name, 'Maria')
        self.assertEqual(user.last_name, 'Silva')
        self.assertTrue(UserProfile.objects.filter(firebase_uid='firebase_test_uid_001').exists())

    @patch('apps.accounts.authentication.firebase_auth.verify_id_token')
    def test_valid_token_returns_existing_user(self, mock_verify):
        # Criar user existente
        user = User.objects.create(username='firebase_existing', email='existing@test.com')
        UserProfile.objects.create(user=user, firebase_uid='existing_uid')

        mock_verify.return_value = {
            'uid': 'existing_uid',
            'email': 'existing@test.com',
            'firebase': {'sign_in_provider': 'password'},
        }

        request = self.factory.get('/', HTTP_AUTHORIZATION='Bearer valid_token')
        returned_user, _ = self.auth.authenticate(request)

        self.assertEqual(returned_user.id, user.id)
        # Não criou user novo
        self.assertEqual(User.objects.filter(email='existing@test.com').count(), 1)

    @patch('apps.accounts.authentication.firebase_auth.verify_id_token')
    def test_expired_token_raises_error(self, mock_verify):
        from firebase_admin.auth import ExpiredIdTokenError
        mock_verify.side_effect = ExpiredIdTokenError('Token expired', cause=None)

        request = self.factory.get('/', HTTP_AUTHORIZATION='Bearer expired_token')
        
        from rest_framework.exceptions import AuthenticationFailed
        with self.assertRaises(AuthenticationFailed):
            self.auth.authenticate(request)
```

---

## 7. Fluxo no Frontend (Referência para o Prompt de Frontend)

O frontend usa o Firebase SDK para autenticação. Após login, o SDK fornece um `idToken` que deve ser enviado em toda request ao Django:

```typescript
// Pseudo-código — será detalhado no prompt de frontend

// Login
const result = await signInWithPopup(auth, googleProvider);
const idToken = await result.user.getIdToken();

// Toda request ao Django
const response = await fetch('/api/v1/projects/', {
    headers: {
        'Authorization': `Bearer ${idToken}`,
        'Content-Type': 'application/json',
    },
});

// Refresh automático do token (Firebase faz a cada ~1h)
onIdTokenChanged(auth, async (user) => {
    if (user) {
        const newToken = await user.getIdToken();
        // Atualizar token no state/storage
    }
});
```

---

## 8. Checklist de Implementação

- [ ] Criar projeto Firebase no Console
- [ ] Habilitar Email/Password e Google como providers
- [ ] Gerar e salvar `firebase-service-account.json`
- [ ] `pip install firebase-admin`
- [ ] Criar app `accounts` com a estrutura da Seção 3.3
- [ ] Implementar `UserProfile` model + migration
- [ ] Implementar `FirebaseAuthentication` backend
- [ ] Implementar `UserService`
- [ ] Implementar serializers, views, urls
- [ ] Atualizar `config/settings/base.py` com Firebase init e DRF auth
- [ ] Atualizar `config/urls.py`
- [ ] Rodar `python manage.py makemigrations accounts && python manage.py migrate`
- [ ] Rodar testes: `python manage.py test apps.accounts`
- [ ] Testar manualmente: criar user no Firebase Console → obter token → chamar `/api/v1/auth/me/`

---

*Este documento é um sub-módulo do contrato de desenvolvimento DaVinci. A autenticação Firebase segue as mesmas regras arquiteturais: Services over Signals, sem processamento pesado no Django, lógica de negócio nos Services.*
