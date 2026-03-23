from rest_framework.test import APITestCase
from rest_framework import status
from django.contrib.auth.models import User
from apps.core.models import DaVinciProject

class DaVinciProjectApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='password')
        self.client.force_authenticate(user=self.user)

    def test_create_project(self):
        url = '/api/v1/projects/'
        data = {
            'title': 'Test Project',
            'query_term': 'cancer AND biomaker',
            'date_from': 2020,
            'date_to': 2024
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(DaVinciProject.objects.count(), 1)
        project = DaVinciProject.objects.first()
        self.assertEqual(project.slug, 'test-project-tester-davinci')

    def test_list_projects(self):
        DaVinciProject.objects.create(user=self.user, title='P1', slug='p1-tester-davinci')
        url = '/api/v1/projects/'
        response = self.client.get(url, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Assumes pagination
        self.assertEqual(response.data['count'], 1)
