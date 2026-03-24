from unittest.mock import patch, MagicMock
from django.test import TestCase, RequestFactory
from django.contrib.auth import get_user_model
from rest_framework.exceptions import AuthenticationFailed

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

    def test_empty_bearer_token_returns_none(self):
        request = self.factory.get('/', HTTP_AUTHORIZATION='Bearer ')
        result = self.auth.authenticate(request)
        self.assertIsNone(result)

    @patch('apps.accounts.authentication.firebase_auth')
    def test_valid_token_creates_user(self, mock_firebase_auth):
        mock_firebase_auth.verify_id_token.return_value = {
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
        profile = UserProfile.objects.get(firebase_uid='firebase_test_uid_001')
        self.assertEqual(profile.auth_provider, 'google.com')

    @patch('apps.accounts.authentication.firebase_auth')
    def test_valid_token_returns_existing_user(self, mock_firebase_auth):
        user = User.objects.create(username='firebase_existing', email='existing@test.com')
        UserProfile.objects.create(user=user, firebase_uid='existing_uid')

        mock_firebase_auth.verify_id_token.return_value = {
            'uid': 'existing_uid',
            'email': 'existing@test.com',
            'firebase': {'sign_in_provider': 'password'},
        }

        request = self.factory.get('/', HTTP_AUTHORIZATION='Bearer valid_token')
        returned_user, _ = self.auth.authenticate(request)

        self.assertEqual(returned_user.id, user.id)
        self.assertEqual(User.objects.filter(email='existing@test.com').count(), 1)

    @patch('apps.accounts.authentication.firebase_auth')
    def test_invalid_token_raises_error(self, mock_firebase_auth):
        mock_firebase_auth.verify_id_token.side_effect = Exception('invalid')

        request = self.factory.get('/', HTTP_AUTHORIZATION='Bearer bad_token')
        with self.assertRaises(AuthenticationFailed):
            self.auth.authenticate(request)

    def test_authenticate_header(self):
        request = self.factory.get('/')
        self.assertEqual(self.auth.authenticate_header(request), 'Bearer realm="firebase"')


class UserServiceTest(TestCase):

    def test_creates_user_from_firebase_token(self):
        from apps.accounts.services.user_service import UserService

        decoded = {
            'uid': 'new_uid_xyz',
            'email': 'newuser@test.com',
            'name': 'João Souza',
            'picture': 'https://example.com/pic.jpg',
            'firebase': {'sign_in_provider': 'google.com'},
        }
        user = UserService.get_or_create_from_firebase(decoded)

        self.assertEqual(user.email, 'newuser@test.com')
        self.assertEqual(user.first_name, 'João')
        self.assertEqual(user.last_name, 'Souza')
        self.assertFalse(user.has_usable_password())

        profile = UserProfile.objects.get(firebase_uid='new_uid_xyz')
        self.assertEqual(profile.auth_provider, 'google.com')
        self.assertEqual(profile.avatar_url, 'https://example.com/pic.jpg')

    def test_returns_existing_user_by_firebase_uid(self):
        from apps.accounts.services.user_service import UserService

        user = User.objects.create(username='uid_abc', email='old@test.com')
        UserProfile.objects.create(user=user, firebase_uid='uid_abc', auth_provider='password')

        decoded = {
            'uid': 'uid_abc',
            'email': 'old@test.com',
            'firebase': {'sign_in_provider': 'password'},
        }
        returned = UserService.get_or_create_from_firebase(decoded)
        self.assertEqual(returned.id, user.id)
        self.assertEqual(User.objects.filter(username='uid_abc').count(), 1)

    def test_split_name_single_word(self):
        from apps.accounts.services.user_service import UserService
        first, last = UserService._split_name('Einstein')
        self.assertEqual(first, 'Einstein')
        self.assertEqual(last, '')

    def test_split_name_full_name(self):
        from apps.accounts.services.user_service import UserService
        first, last = UserService._split_name('Albert Einstein')
        self.assertEqual(first, 'Albert')
        self.assertEqual(last, 'Einstein')

    def test_split_name_empty(self):
        from apps.accounts.services.user_service import UserService
        first, last = UserService._split_name('')
        self.assertEqual(first, '')
        self.assertEqual(last, '')


class MeViewTest(TestCase):

    def setUp(self):
        from rest_framework.test import APIClient
        self.client = APIClient()
        self.user = User.objects.create(username='uid_me', email='me@test.com', first_name='Ana')
        UserProfile.objects.create(
            user=self.user,
            firebase_uid='uid_me',
            institution='FIOCRUZ',
        )
        self.client.force_authenticate(user=self.user)

    def test_get_me(self):
        response = self.client.get('/api/v1/auth/me/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['email'], 'me@test.com')
        self.assertEqual(response.data['institution'], 'FIOCRUZ')

    def test_patch_me_institution(self):
        response = self.client.patch(
            '/api/v1/auth/me/',
            {'institution': 'USP', 'research_area': 'Cardiology'},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['institution'], 'USP')
        self.assertEqual(response.data['research_area'], 'Cardiology')

    def test_patch_me_name(self):
        response = self.client.patch(
            '/api/v1/auth/me/',
            {'first_name': 'Ana', 'last_name': 'Lima'},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, 'Ana')
        self.assertEqual(self.user.last_name, 'Lima')
