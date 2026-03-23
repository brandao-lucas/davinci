from django.test import TransactionTestCase
from apps.core.models import DaVinciProject, IngestionJob
from apps.core.tasks.ingestion_tasks import run_pubmed_ingestion
from django.contrib.auth.models import User
import uuid

class IngestionTasksTestCase(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='test', password='pw')
        self.project = DaVinciProject.objects.create(
            title='Test',
            user=self.user,
            query_term='cancer',
            slug='test'
        )
        self.job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.PUBMED_SEARCH,
            parameters={'query': 'cancer'}
        )

    def test_run_pubmed_ingestion(self):
        # We expect the rust module to be called and update the job status to COMPLETED
        result = run_pubmed_ingestion(str(self.job.id))
        self.job.refresh_from_db()

        self.assertEqual(self.job.status, IngestionJob.JobStatus.COMPLETED)
        self.assertIn('processed', result)
        self.assertIn('inserted', result)
