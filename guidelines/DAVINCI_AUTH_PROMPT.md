# DaVinci — Prompt de Implementação: Autenticação Firebase

## Resumo

Este documento especifica a implementação do sistema de autenticação do DaVinci usando **Firebase Authentication** como provider de identidade e **Django Rest Framework (DRF)** como validador de tokens. O Firebase emite JWTs no frontend; o Django valida cada request e mapeia o usuário Firebase para um `User` Django local. O frontend (futuro) lida com o fluxo de login; o Django nunca renderiza telas de autenticação.

A autenticação é um pré-requisito para todas as features do DaVinci — nenhum endpoint da API deve ser acessível sem um token válido, exceto health checks.

---

## 1. Visão Geral do Fluxo

```
Frontend (Next.js / Tauri / React Native)
    │
    ├── 1. Usuário faz login via Firebase SDK
    │      (Google, ORCID, email/password)
    │
    ├── 2. Firebase retorna ID Token (JWT)
    │
    ├── 3. Frontend envia requests à API DaVinci
    │      Header: Authorization: Bearer <firebase_id_token>
    │
    ▼
Django (DRF)
    │
    ├── 4. FirebaseAuthentication backend intercepta o request
    │      - Decodifica e valida o JWT com firebase-admin SDK
    │      - Verifica: assinatura, expiração, audience, issuer
    │
    ├── 5. Get-or-create do User Django local
    │      - Mapeia firebase_uid → User.username
    │      - Popula email, display_name do token
    │      - Cria FirebaseProfile com metadados extras
    │
    ├── 6. request.user = User Django local
    │      (disponível em todos os views/services)
    │
    └── 7. Filtros de queryset usam request.user normalmente
         Ex: DaVinciProject.objects.filter(user=request.user)
```

---

## 2. Modelo de Dados

### 2.1 FirebaseProfile (novo model)

Extensão do User Django via OneToOneField. Armazena metadados do Firebase que não existem no User padrão.

```python
# apps/accounts/models.py

import uuid
from django.conf import settings
from django.db import models


class FirebaseProfile(models.Model):
    """
    Perfil Firebase vinculado ao User Django.
    Criado automaticamente no primeiro login.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='firebase_profile',
    )
    firebase_uid = models.CharField(
        max_length=128,
        unique=True,
        db_index=True,
        help_text="UID do Firebase Authentication",
    )
    provider = models.CharField(
        max_length=50,
        default='password',
        help_text="Provider do último login: google.com, orcid.org, password",
    )
    photo_url = models.URLField(blank=True, default='')
    orcid_id = models.CharField(
        max_length=19,
        blank=True,
        default='',
        db_index=True,
        help_text="ORCID iD no formato 0000-0000-0000-0000",
    )
    last_token_refresh = models.DateTimeField(null=True, blank=True)
    is_verified = models.BooleanField(
        default=False,
        help_text="Email verificado no Firebase",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'accounts_firebase_profile'
        verbose_name = 'Firebase Profile'
        verbose_name_plural = 'Firebase Profiles'

    def __str__(self):
        return f"{self.user.email} ({self.firebase_uid})"
```

### 2.2 Relação com DaVinciProject

O campo `user` do `DaVinciProject` (já definido no prompt principal) aponta para `settings.AUTH_USER_MODEL`. Não precisa de alteração — o Firebase Authentication simplesmente garante que `request.user` seja populado corretamente.

---

## 3. App Django: accounts

### 3.1 Estrutura de Diretórios

```
apps/
└── accounts/
    ├── __init__.py
    ├── admin.py
    ├── apps.py
    ├── models.py                    # FirebaseProfile
    ├── migrations/
    │   └── 0001_initial.py
    ├── authentication/
    │   ├── __init__.py
    │   └── firebase_backend.py      # FirebaseAuthentication class
    ├── services/
    │   ├── __init__.py
    │   └── user_service.py          # Get-or-create User + FirebaseProfile
    ├── serializers/
    │   ├── __init__.py
    │   └── user.py                  # UserSerializer para /api/v1/me/
    ├── views/
    │   ├── __init__.py
    │   └── user_views.py            # /me/ endpoint
    ├── urls.py
    └── tests/
        ├── __init__.py
        ├── test_authentication.py   # Testes do backend Firebase
        └── test_user_service.py     # Testes do get-or-create
```

### 3.2 Firebase Authentication Backend

```python
# apps/accounts/authentication/firebase_backend.py

import logging

import firebase_admin
from firebase_admin import auth as firebase_auth, credentials
from django.conf import settings
from rest_framework import authentication, exceptions

from apps.accounts.services.user_service import UserService

logger = logging.getLogger(__name__)

# Inicializa o Firebase Admin SDK uma única vez
# O SDK usa a service account JSON para validar tokens
if not firebase_admin._apps:
    cred = credentials.Certificate(settings.FIREBASE_CREDENTIALS_PATH)
    firebase_admin.initialize_app(cred, {
        'projectId': settings.FIREBASE_PROJECT_ID,
    })


class FirebaseAuthentication(authentication.BaseAuthentication):
    """
    Autenticação via Firebase ID Token.

    O frontend envia o header:
        Authorization: Bearer <firebase_id_token>

    Este backend:
    1. Extrai o token do header
    2. Valida com firebase-admin (assinatura, expiração, audience)
    3. Faz get-or-create do User Django
    4. Retorna (user, decoded_token)
    """

    keyword = 'Bearer'

    def authenticate(self, request):
        auth_header = authentication.get_authorization_header(request)
        if not auth_header:
            return None  # Sem header = não tenta autenticar (permite outros backends)

        try:
            prefix, token = auth_header.decode('utf-8').split(' ', 1)
        except (ValueError, UnicodeDecodeError):
            return None

        if prefix.lower() != self.keyword.lower():
            return None

        return self._authenticate_token(token)

    def _authenticate_token(self, token: str):
        try:
            decoded_token = firebase_auth.verify_id_token(
                token,
                check_revoked=True,  # Verifica se o token foi revogado
            )
        except firebase_auth.RevokedIdTokenError:
            raise exceptions.AuthenticationFailed(
                'Token revogado. Faça login novamente.'
            )
        except firebase_auth.ExpiredIdTokenError:
            raise exceptions.AuthenticationFailed(
                'Token expirado. Faça login novamente.'
            )
        except firebase_auth.InvalidIdTokenError as e:
            logger.warning(f"Token Firebase inválido: {e}")
            raise exceptions.AuthenticationFailed(
                'Token inválido.'
            )
        except Exception as e:
            logger.error(f"Erro inesperado na validação Firebase: {e}")
            raise exceptions.AuthenticationFailed(
                'Erro na autenticação.'
            )

        # Get-or-create do User Django
        user = UserService.get_or_create_from_firebase(decoded_token)

        if not user.is_active:
            raise exceptions.AuthenticationFailed(
                'Conta desativada.'
            )

        return (user, decoded_token)

    def authenticate_header(self, request):
        """Retorna o scheme para o header WWW-Authenticate em respostas 401."""
        return self.keyword
```

### 3.3 User Service (Get-or-Create)

```python
# apps/accounts/services/user_service.py

import logging
from datetime import datetime, timezone

from django.contrib.auth import get_user_model
from django.db import transaction

from apps.accounts.models import FirebaseProfile

User = get_user_model()
logger = logging.getLogger(__name__)


class UserService:
    """
    Service para gestão de usuários a partir de tokens Firebase.
    Encapsula a lógica de get-or-create e atualização de perfil.
    """

    @staticmethod
    @transaction.atomic
    def get_or_create_from_firebase(decoded_token: dict) -> User:
        """
        Recebe o token Firebase decodificado e retorna o User Django.
        Cria o User e o FirebaseProfile se não existirem.
        Atualiza metadados a cada login.

        O decoded_token contém (entre outros):
        - uid: UID único do Firebase
        - email: email do usuário
        - name: display name (pode ser None)
        - picture: URL da foto (pode ser None)
        - email_verified: bool
        - firebase.sign_in_provider: 'google.com', 'password', 'orcid.org', etc.
        """
        firebase_uid = decoded_token['uid']
        email = decoded_token.get('email', '')
        display_name = decoded_token.get('name', '')
        photo_url = decoded_token.get('picture', '')
        email_verified = decoded_token.get('email_verified', False)

        # Provider vem de firebase.sign_in_provider
        firebase_info = decoded_token.get('firebase', {})
        provider = firebase_info.get('sign_in_provider', 'password')

        # Tenta encontrar o FirebaseProfile existente
        try:
            profile = FirebaseProfile.objects.select_related('user').get(
                firebase_uid=firebase_uid
            )
            user = profile.user

            # Atualiza metadados a cada autenticação
            _updated_fields = []

            if email and user.email != email:
                user.email = email
                _updated_fields.append('email')

            if display_name and user.get_full_name() != display_name:
                parts = display_name.split(' ', 1)
                user.first_name = parts[0]
                user.last_name = parts[1] if len(parts) > 1 else ''
                _updated_fields.extend(['first_name', 'last_name'])

            if _updated_fields:
                user.save(update_fields=_updated_fields)

            # Atualiza profile
            profile.provider = provider
            profile.is_verified = email_verified
            profile.last_token_refresh = datetime.now(timezone.utc)
            if photo_url:
                profile.photo_url = photo_url
            profile.save(update_fields=[
                'provider', 'is_verified', 'last_token_refresh',
                'photo_url', 'updated_at',
            ])

            return user

        except FirebaseProfile.DoesNotExist:
            pass

        # Cria novo User + FirebaseProfile
        logger.info(f"Criando novo usuário para Firebase UID: {firebase_uid}")

        # Username = firebase_uid (garantido único)
        user = User.objects.create_user(
            username=firebase_uid,
            email=email,
            first_name=display_name.split(' ', 1)[0] if display_name else '',
            last_name=(
                display_name.split(' ', 1)[1]
                if display_name and ' ' in display_name
                else ''
            ),
        )
        # Senha inutilizável — autenticação é sempre via Firebase
        user.set_unusable_password()
        user.save(update_fields=['password'])

        FirebaseProfile.objects.create(
            user=user,
            firebase_uid=firebase_uid,
            provider=provider,
            photo_url=photo_url or '',
            is_verified=email_verified,
            last_token_refresh=datetime.now(timezone.utc),
        )

        return user

    @staticmethod
    def get_by_firebase_uid(firebase_uid: str):
        """Busca User por Firebase UID. Retorna None se não encontrado."""
        try:
            profile = FirebaseProfile.objects.select_related('user').get(
                firebase_uid=firebase_uid
            )
            return profile.user
        except FirebaseProfile.DoesNotExist:
            return None
```

### 3.4 Views e Serializers

```python
# apps/accounts/serializers/user.py

from django.contrib.auth import get_user_model
from rest_framework import serializers

from apps.accounts.models import FirebaseProfile

User = get_user_model()


class FirebaseProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = FirebaseProfile
        fields = [
            'firebase_uid', 'provider', 'photo_url',
            'orcid_id', 'is_verified', 'created_at',
        ]
        read_only_fields = fields


class CurrentUserSerializer(serializers.ModelSerializer):
    firebase_profile = FirebaseProfileSerializer(read_only=True)
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'email', 'first_name', 'last_name',
            'full_name', 'is_active', 'date_joined',
            'firebase_profile',
        ]
        read_only_fields = fields

    def get_full_name(self, obj):
        return obj.get_full_name()
```

```python
# apps/accounts/views/user_views.py

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.serializers.user import CurrentUserSerializer


class CurrentUserView(APIView):
    """
    GET /api/v1/me/ — Retorna o usuário autenticado.
    Primeiro endpoint que o frontend chama após login.
    """

    def get(self, request):
        serializer = CurrentUserSerializer(request.user)
        return Response(serializer.data)
```

```python
# apps/accounts/urls.py

from django.urls import path

from apps.accounts.views.user_views import CurrentUserView

app_name = 'accounts'

urlpatterns = [
    path('me/', CurrentUserView.as_view(), name='current-user'),
]
```

---

## 4. Configuração Django

### 4.1 Settings (adicionar ao base.py)

```python
# --- Firebase Authentication ---

import os

# Caminho para o JSON da service account do Firebase
# Download em: Firebase Console > Project Settings > Service Accounts > Generate New Key
FIREBASE_CREDENTIALS_PATH = os.environ.get(
    'FIREBASE_CREDENTIALS_PATH',
    os.path.join(BASE_DIR, 'credentials', 'firebase-service-account.json')
)

# Project ID do Firebase (visível no Firebase Console)
FIREBASE_PROJECT_ID = os.environ.get('FIREBASE_PROJECT_ID', 'davinci-platomics')


# --- DRF Authentication ---

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'apps.accounts.authentication.firebase_backend.FirebaseAuthentication',
        # SessionAuthentication mantido para o Django Admin
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    # ... demais configs do prompt principal ...
}


# --- INSTALLED_APPS ---

INSTALLED_APPS = [
    # ... apps existentes ...
    'apps.accounts',  # ADICIONAR
]
```

### 4.2 URLs (adicionar ao config/urls.py)

```python
# config/urls.py

from django.urls import path, include

urlpatterns = [
    # ... urls existentes ...
    path('api/v1/', include('apps.accounts.urls')),
]
```

### 4.3 Dependência Python

```bash
pip install firebase-admin
```

Adicionar ao `requirements.txt`:
```
firebase-admin>=6.5.0
```

---

## 5. Configuração do Firebase Console

### 5.1 Criar Projeto

1. Acessar https://console.firebase.google.com/
2. "Add Project" → Nome: `davinci-platomics`
3. Desabilitar Google Analytics (não necessário para auth)

### 5.2 Habilitar Providers

No Firebase Console → Authentication → Sign-in method:

| Provider | Configuração | Prioridade |
|----------|-------------|------------|
| **Email/Password** | Habilitar. Habilitar "Email link" opcional. | MVP |
| **Google** | Habilitar. Configurar OAuth consent screen. | MVP |
| **ORCID** | Habilitar via "OpenID Connect" (custom provider). | Pós-MVP |

### 5.3 Configurar ORCID como Custom OIDC Provider (Pós-MVP)

ORCID suporta OpenID Connect. No Firebase:

1. Authentication → Sign-in method → "Add new provider" → "OpenID Connect"
2. Provider ID: `orcid.org`
3. Client ID: obtido em https://orcid.org/developer-tools
4. Issuer URL: `https://orcid.org`
5. Client Secret: obtido no ORCID developer tools

### 5.4 Gerar Service Account Key

1. Firebase Console → Project Settings → Service Accounts
2. "Generate new private key"
3. Salvar como `credentials/firebase-service-account.json`
4. **NUNCA commitar este arquivo no Git** — adicionar ao `.gitignore`

```gitignore
# .gitignore
credentials/
*.json
!package.json
```

---

## 6. Segurança

### 6.1 Regras Invioláveis

1. **O Service Account JSON nunca vai para o Git.** Usar variável de ambiente em produção.
2. **Tokens são validados em cada request.** Sem cache de tokens no Django — o `firebase-admin` SDK gerencia o cache das chaves públicas do Google internamente.
3. **`check_revoked=True` sempre.** Se o admin revogar o token no Firebase Console, o próximo request falha imediatamente.
4. **Senha inutilizável no User Django.** `user.set_unusable_password()` — login só via Firebase.
5. **CORS restrito.** Apenas os domínios do frontend podem fazer requests à API.

### 6.2 CORS Configuration

```python
# settings/base.py

CORS_ALLOWED_ORIGINS = [
    'http://localhost:3000',      # Next.js dev
    'http://localhost:1420',      # Tauri dev
    'https://davinci.platomics.com',  # Produção
]

CORS_ALLOW_CREDENTIALS = True
```

### 6.3 Rate Limiting (recomendado)

```python
# settings/base.py

REST_FRAMEWORK = {
    # ... demais configs ...
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '20/minute',
        'user': '200/minute',
    },
}
```

---

## 7. Endpoints da Autenticação

| Método | Endpoint | Auth | Descrição |
|--------|----------|------|-----------|
| `GET` | `/api/v1/me/` | Sim | Dados do usuário autenticado |
| `GET` | `/api/v1/health/` | Não | Health check (único endpoint público) |

O login, registro, reset de senha e OAuth são todos gerenciados pelo Firebase SDK no frontend. O Django **não tem endpoints de login/registro** — ele apenas valida os tokens que o frontend envia.

---

## 8. Testes

### 8.1 Teste do Authentication Backend

```python
# apps/accounts/tests/test_authentication.py

from unittest.mock import patch, MagicMock
from django.test import TestCase, RequestFactory
from django.contrib.auth import get_user_model
from rest_framework.exceptions import AuthenticationFailed

from apps.accounts.authentication.firebase_backend import FirebaseAuthentication
from apps.accounts.models import FirebaseProfile

User = get_user_model()


class FirebaseAuthenticationTest(TestCase):

    def setUp(self):
        self.factory = RequestFactory()
        self.auth = FirebaseAuthentication()
        self.valid_token_data = {
            'uid': 'firebase_test_uid_123',
            'email': 'researcher@university.edu',
            'name': 'Maria Silva',
            'picture': 'https://example.com/photo.jpg',
            'email_verified': True,
            'firebase': {
                'sign_in_provider': 'google.com',
            },
        }

    def _make_request(self, token='valid-token'):
        request = self.factory.get('/api/v1/me/')
        request.META['HTTP_AUTHORIZATION'] = f'Bearer {token}'
        return request

    @patch('apps.accounts.authentication.firebase_backend.firebase_auth.verify_id_token')
    def test_valid_token_creates_user(self, mock_verify):
        """Primeiro login: cria User + FirebaseProfile."""
        mock_verify.return_value = self.valid_token_data
        request = self._make_request()

        user, decoded = self.auth.authenticate(request)

        self.assertIsNotNone(user)
        self.assertEqual(user.email, 'researcher@university.edu')
        self.assertEqual(user.first_name, 'Maria')
        self.assertEqual(user.last_name, 'Silva')
        self.assertTrue(user.firebase_profile.is_verified)
        self.assertEqual(user.firebase_profile.provider, 'google.com')

    @patch('apps.accounts.authentication.firebase_backend.firebase_auth.verify_id_token')
    def test_valid_token_returns_existing_user(self, mock_verify):
        """Segundo login: retorna User existente, atualiza metadados."""
        mock_verify.return_value = self.valid_token_data

        # Primeiro login
        request = self._make_request()
        user1, _ = self.auth.authenticate(request)

        # Segundo login
        user2, _ = self.auth.authenticate(request)

        self.assertEqual(user1.id, user2.id)
        self.assertEqual(User.objects.count(), 1)

    @patch('apps.accounts.authentication.firebase_backend.firebase_auth.verify_id_token')
    def test_expired_token_raises_401(self, mock_verify):
        """Token expirado retorna 401."""
        from firebase_admin.auth import ExpiredIdTokenError
        mock_verify.side_effect = ExpiredIdTokenError('Token expired', cause=None)
        request = self._make_request()

        with self.assertRaises(AuthenticationFailed):
            self.auth.authenticate(request)

    @patch('apps.accounts.authentication.firebase_backend.firebase_auth.verify_id_token')
    def test_revoked_token_raises_401(self, mock_verify):
        """Token revogado retorna 401."""
        from firebase_admin.auth import RevokedIdTokenError
        mock_verify.side_effect = RevokedIdTokenError('Token revoked')
        request = self._make_request()

        with self.assertRaises(AuthenticationFailed):
            self.auth.authenticate(request)

    def test_no_auth_header_returns_none(self):
        """Sem header Authorization, retorna None (permite outros backends)."""
        request = self.factory.get('/api/v1/me/')
        result = self.auth.authenticate(request)
        self.assertIsNone(result)

    def test_wrong_scheme_returns_none(self):
        """Header com scheme diferente de Bearer retorna None."""
        request = self.factory.get('/api/v1/me/')
        request.META['HTTP_AUTHORIZATION'] = 'Token some-token'
        result = self.auth.authenticate(request)
        self.assertIsNone(result)

    @patch('apps.accounts.authentication.firebase_backend.firebase_auth.verify_id_token')
    def test_inactive_user_raises_401(self, mock_verify):
        """Usuário desativado retorna 401."""
        mock_verify.return_value = self.valid_token_data

        # Cria o user e desativa
        request = self._make_request()
        user, _ = self.auth.authenticate(request)
        user.is_active = False
        user.save()

        with self.assertRaises(AuthenticationFailed):
            self.auth.authenticate(request)
```

### 8.2 Teste do User Service

```python
# apps/accounts/tests/test_user_service.py

from django.test import TestCase
from django.contrib.auth import get_user_model

from apps.accounts.models import FirebaseProfile
from apps.accounts.services.user_service import UserService

User = get_user_model()


class UserServiceTest(TestCase):

    def setUp(self):
        self.token_data = {
            'uid': 'test_uid_456',
            'email': 'joao@lab.br',
            'name': 'João Souza',
            'picture': '',
            'email_verified': True,
            'firebase': {'sign_in_provider': 'password'},
        }

    def test_creates_user_and_profile(self):
        user = UserService.get_or_create_from_firebase(self.token_data)

        self.assertEqual(user.email, 'joao@lab.br')
        self.assertEqual(user.first_name, 'João')
        self.assertEqual(user.last_name, 'Souza')
        self.assertFalse(user.has_usable_password())

        profile = user.firebase_profile
        self.assertEqual(profile.firebase_uid, 'test_uid_456')
        self.assertEqual(profile.provider, 'password')
        self.assertTrue(profile.is_verified)

    def test_updates_email_on_subsequent_login(self):
        UserService.get_or_create_from_firebase(self.token_data)

        # Segundo login com email diferente
        self.token_data['email'] = 'joao.novo@lab.br'
        user = UserService.get_or_create_from_firebase(self.token_data)

        self.assertEqual(user.email, 'joao.novo@lab.br')
        self.assertEqual(User.objects.count(), 1)

    def test_get_by_firebase_uid(self):
        UserService.get_or_create_from_firebase(self.token_data)

        user = UserService.get_by_firebase_uid('test_uid_456')
        self.assertIsNotNone(user)
        self.assertEqual(user.email, 'joao@lab.br')

    def test_get_by_firebase_uid_not_found(self):
        user = UserService.get_by_firebase_uid('nonexistent')
        self.assertIsNone(user)
```

---

## 9. Integração com o DaVinciProject

O `DaVinciProject` já tem `user = ForeignKey(settings.AUTH_USER_MODEL)`. Com o Firebase Auth implementado, os ViewSets do DRF filtram automaticamente por `request.user`:

```python
# apps/core/views/project_views.py (já existente — sem alteração)

class DaVinciProjectViewSet(viewsets.ModelViewSet):
    def get_queryset(self):
        return DaVinciProject.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
```

O `request.user` é populado pelo `FirebaseAuthentication` backend. Nenhuma alteração é necessária nos views ou services existentes.

---

## 10. Checklist de Implementação

- [ ] Criar projeto no Firebase Console
- [ ] Habilitar providers: Email/Password + Google
- [ ] Gerar Service Account Key → `credentials/firebase-service-account.json`
- [ ] Adicionar `credentials/` ao `.gitignore`
- [ ] `pip install firebase-admin`
- [ ] Criar app `accounts` com a estrutura da Seção 3.1
- [ ] Implementar `FirebaseProfile` model
- [ ] Implementar `FirebaseAuthentication` backend
- [ ] Implementar `UserService`
- [ ] Implementar `/api/v1/me/` endpoint
- [ ] Configurar `settings/base.py` (Seção 4.1)
- [ ] Configurar URLs (Seção 4.2)
- [ ] Rodar migrations
- [ ] Executar testes (Seção 8)
- [ ] Configurar CORS para o frontend

---

## 11. Referência Rápida: Frontend (para o dev frontend)

O frontend precisa fazer apenas três coisas:

### 11.1 Instalar Firebase SDK

```bash
npm install firebase
```

### 11.2 Inicializar Firebase

```typescript
// lib/firebase.ts
import { initializeApp } from 'firebase/app';
import { getAuth } from 'firebase/auth';

const firebaseConfig = {
  apiKey: process.env.NEXT_PUBLIC_FIREBASE_API_KEY,
  authDomain: process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN,
  projectId: process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID,
};

export const app = initializeApp(firebaseConfig);
export const auth = getAuth(app);
```

### 11.3 Enviar Token em Cada Request

```typescript
// lib/api.ts
import { auth } from './firebase';

export async function apiRequest(path: string, options: RequestInit = {}) {
  const user = auth.currentUser;
  if (!user) throw new Error('Usuário não autenticado');

  const token = await user.getIdToken();  // Renova automaticamente se expirado

  return fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${token}`,
      ...options.headers,
    },
  });
}
```

O Firebase SDK gerencia refresh de tokens automaticamente. O `getIdToken()` retorna um token válido — se o token atual expirou, o SDK renova antes de retornar.

---

*Este documento complementa o DAVINCI_DEVELOPMENT_PROMPT.md. A implementação deve seguir as regras arquiteturais invioláveis definidas no prompt principal, especialmente: Services over Signals, ViewSets finos, e separação clara entre camadas.*
