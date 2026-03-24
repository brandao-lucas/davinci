from rest_framework import authentication, exceptions

try:
    from firebase_admin import auth as firebase_auth
    FIREBASE_AVAILABLE = True
except ImportError:
    firebase_auth = None
    FIREBASE_AVAILABLE = False

from apps.accounts.services.user_service import UserService


class FirebaseAuthentication(authentication.BaseAuthentication):
    """
    Autentica requests usando Firebase ID Tokens.

    O frontend envia: Authorization: Bearer <firebase_id_token>
    Este backend:
    1. Extrai o token do header Bearer
    2. Valida com Firebase Admin SDK
    3. Busca ou cria o User Django correspondente
    4. Retorna (user, decoded_token)

    Retorna None se não há header Bearer → deixa outros backends tentarem
    (ex: SessionAuthentication para o DRF Browsable API em dev).

    Se firebase-admin não estiver instalado ou configurado,
    levanta AuthenticationFailed em vez de crashar.
    """

    def authenticate(self, request):
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')

        if not auth_header.startswith('Bearer '):
            return None

        token = auth_header.split('Bearer ', 1)[1].strip()
        if not token:
            return None

        if firebase_auth is None:
            raise exceptions.AuthenticationFailed(
                'Firebase Admin SDK não configurado no servidor.'
            )

        try:
            decoded_token = firebase_auth.verify_id_token(token)
        except Exception as e:
            name = type(e).__name__
            if name == 'ExpiredIdTokenError':
                raise exceptions.AuthenticationFailed('Token expirado. Faça login novamente.')
            if name == 'RevokedIdTokenError':
                raise exceptions.AuthenticationFailed('Token revogado.')
            if name == 'InvalidIdTokenError':
                raise exceptions.AuthenticationFailed('Token inválido.')
            raise exceptions.AuthenticationFailed('Erro ao validar token Firebase.')

        user = UserService.get_or_create_from_firebase(decoded_token)
        return (user, decoded_token)

    def authenticate_header(self, request):
        return 'Bearer realm="firebase"'
