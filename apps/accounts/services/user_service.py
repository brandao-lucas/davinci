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
            UserService._sync_profile(profile, decoded_token)
            return user

        except UserProfile.DoesNotExist:
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
        user.set_unusable_password()
        user.save()

        UserProfile.objects.create(
            user=user,
            firebase_uid=firebase_uid,
            auth_provider=provider,
            avatar_url=picture or '',
        )

        return user

    @staticmethod
    def _sync_profile(profile, decoded_token):
        """Sincroniza dados do Firebase com o profile local."""
        picture = decoded_token.get('picture', '')
        provider = decoded_token.get('firebase', {}).get('sign_in_provider', '')
        updated_fields = []

        if picture and profile.avatar_url != picture:
            profile.avatar_url = picture
            updated_fields.append('avatar_url')
        if provider and profile.auth_provider != provider:
            profile.auth_provider = provider
            updated_fields.append('auth_provider')

        if updated_fields:
            updated_fields.append('last_firebase_sync')
            profile.save(update_fields=updated_fields)

    @staticmethod
    def _split_name(name: str) -> tuple:
        """Divide nome completo em first_name e last_name."""
        if not name:
            return ('', '')
        parts = name.strip().split(' ', 1)
        return (parts[0], parts[1] if len(parts) > 1 else '')
