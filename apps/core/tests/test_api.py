from unittest.mock import patch

from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APITestCase

from apps.core.models import (
    ClinicalCategory, DaVinciProject, IngestionJob, OmicDataset,
    Paper, ProjectDataset, ProjectPaper, ProjectPaperDataset,
    UserCategory,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def make_project(user, title='Test Project', query_term='cancer'):
    return DaVinciProject.objects.create(
        user=user,
        title=title,
        slug=f'{title.lower().replace(" ", "-")}-{user.username}-davinci',
        query_term=query_term,
    )


def make_paper(pmid=1001, title='Test Paper', journal='Nature', pub_year=2023):
    return Paper.objects.create(pmid=pmid, title=title, journal=journal, pub_year=pub_year)


def make_project_paper(project, paper, curation_status='pending'):
    return ProjectPaper.objects.create(
        project=project, paper=paper, curation_status=curation_status
    )


def make_dataset(accession='GSE001', omic_type='transcriptomic', organism='Homo sapiens'):
    return OmicDataset.objects.create(
        accession=accession,
        source_db='geo',
        title=f'Dataset {accession}',
        omic_type=omic_type,
        organism=organism,
    )


def make_project_dataset(project, dataset, curation_status='pending'):
    return ProjectDataset.objects.create(
        project=project, dataset=dataset, curation_status=curation_status
    )


# ─── Phase 1 — Project CRUD ──────────────────────────────────────────────────

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
            'date_to': 2024,
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(DaVinciProject.objects.count(), 1)
        project = DaVinciProject.objects.first()
        self.assertEqual(project.slug, 'test-project-tester-davinci')

    def test_list_projects(self):
        DaVinciProject.objects.create(
            user=self.user, title='P1', slug='p1-tester-davinci', query_term='x'
        )
        url = '/api/v1/projects/'
        response = self.client.get(url, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)


# ─── Phase 4 — Papers ────────────────────────────────────────────────────────

class PaperApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='paperuser', password='pw')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user)
        self.paper = make_paper(pmid=1001)
        self.pp = make_project_paper(self.project, self.paper, curation_status='pending')
        self.base = f'/api/v1/projects/{self.project.id}/papers/'

    def test_list_papers(self):
        response = self.client.get(self.base)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)

    def test_list_papers_filter_by_status(self):
        make_project_paper(self.project, make_paper(pmid=1002), curation_status='included')
        response = self.client.get(self.base, {'status': 'included'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)
        self.assertEqual(response.data['results'][0]['curation_status'], 'included')

    def test_retrieve_paper_detail(self):
        response = self.client.get(f'{self.base}{self.pp.id}/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('paper', response.data)
        self.assertEqual(response.data['paper']['pmid'], 1001)

    def test_patch_paper_curation(self):
        response = self.client.patch(
            f'{self.base}{self.pp.id}/',
            {'curation_status': 'included', 'notes': 'Relevant!'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.pp.refresh_from_db()
        self.assertEqual(self.pp.curation_status, 'included')
        self.assertEqual(self.pp.notes, 'Relevant!')

    def test_categorize_clinical(self):
        cat = ClinicalCategory.objects.create(slug='diagnosis', name='Diagnosis', priority=1)
        response = self.client.post(
            f'{self.base}{self.pp.id}/categorize/',
            {'clinical_add': ['diagnosis']},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('diagnosis', [c['slug'] for c in response.data['clinical_categories']])

    def test_bulk_curate(self):
        pp2 = make_project_paper(self.project, make_paper(pmid=1003), curation_status='pending')
        response = self.client.post(
            f'{self.base}bulk_curate/',
            {'paper_ids': [self.pp.id, pp2.id], 'curation_status': 'excluded'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['updated'], 2)
        self.pp.refresh_from_db()
        self.assertEqual(self.pp.curation_status, 'excluded')

    def test_bulk_curate_invalid_status(self):
        response = self.client.post(
            f'{self.base}bulk_curate/',
            {'paper_ids': [self.pp.id], 'curation_status': 'nonexistent'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_search_returns_400_without_q(self):
        response = self.client.get(f'{self.base}search/')
        self.assertEqual(response.status_code, 400)

    def test_search_with_q_returns_200(self):
        # search_vector is NULL in tests (no Rust trigger); result list will be empty — just verify 200
        response = self.client.get(f'{self.base}search/', {'q': 'cancer'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data, list)

    def test_cannot_access_other_user_papers(self):
        other = User.objects.create_user(username='other', password='pw')
        other_project = make_project(other, title='Other', query_term='x')
        response = self.client.get(f'/api/v1/projects/{other_project.id}/papers/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


# ─── Phase 4 — Datasets ──────────────────────────────────────────────────────

class DatasetApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='dsuser', password='pw')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user)
        self.dataset = make_dataset(accession='GSE001', omic_type='transcriptomic')
        self.pd = make_project_dataset(self.project, self.dataset)
        self.base = f'/api/v1/projects/{self.project.id}/datasets/'

    def test_list_datasets(self):
        response = self.client.get(self.base)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)

    def test_list_filter_omic_type(self):
        ds2 = make_dataset(accession='GSE002', omic_type='genomic')
        make_project_dataset(self.project, ds2)
        response = self.client.get(self.base, {'omic_type': 'genomic'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)
        self.assertEqual(response.data['results'][0]['omic_type'], 'genomic')

    def test_retrieve_dataset_detail(self):
        response = self.client.get(f'{self.base}{self.pd.id}/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('dataset', response.data)
        self.assertEqual(response.data['dataset']['accession'], 'GSE001')

    def test_patch_dataset_curation(self):
        response = self.client.patch(
            f'{self.base}{self.pd.id}/',
            {'curation_status': 'included', 'notes': 'Good dataset'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.pd.refresh_from_db()
        self.assertEqual(self.pd.curation_status, 'included')

    def test_dataset_search_requires_q(self):
        response = self.client.get(f'{self.base}search/')
        self.assertEqual(response.status_code, 400)

    def test_dataset_search_returns_200(self):
        response = self.client.get(f'{self.base}search/', {'q': 'rna'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)


# ─── Phase 4 — Categories ────────────────────────────────────────────────────

class CategoryApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='catuser', password='pw')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user)
        ClinicalCategory.objects.create(slug='diagnosis', name='Diagnosis', priority=1)
        ClinicalCategory.objects.create(slug='treatment', name='Treatment', priority=2)
        self.base = f'/api/v1/projects/{self.project.id}/categories/'

    def test_list_clinical_categories(self):
        response = self.client.get('/api/v1/clinical-categories/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 2)

    def test_create_user_category(self):
        response = self.client.post(
            self.base,
            {'name': 'My Category', 'keywords': ['foo', 'bar'], 'color': '#ff0000'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(UserCategory.objects.filter(project=self.project).count(), 1)

    def test_list_user_categories(self):
        UserCategory.objects.create(project=self.project, name='Cat A', keywords=[])
        response = self.client.get(self.base)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)

    def test_patch_user_category(self):
        cat = UserCategory.objects.create(project=self.project, name='Cat A', keywords=[])
        response = self.client.patch(
            f'{self.base}{cat.id}/',
            {'keywords': ['updated']},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        cat.refresh_from_db()
        self.assertEqual(cat.keywords, ['updated'])

    def test_delete_user_category(self):
        cat = UserCategory.objects.create(project=self.project, name='Cat A', keywords=[])
        response = self.client.delete(f'{self.base}{cat.id}/')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(UserCategory.objects.filter(project=self.project).count(), 0)


# ─── Phase 4 — Links ─────────────────────────────────────────────────────────

class LinkApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='linkuser', password='pw')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user)
        paper = make_paper(pmid=5001)
        dataset = make_dataset(accession='GSE500')
        pp = make_project_paper(self.project, paper)
        pd = make_project_dataset(self.project, dataset)
        self.link = ProjectPaperDataset.objects.create(
            project=self.project,
            project_paper=pp,
            project_dataset=pd,
            confidence='auto',
        )
        self.base = f'/api/v1/projects/{self.project.id}/links/'

    def test_list_links(self):
        response = self.client.get(self.base)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)
        self.assertEqual(response.data['results'][0]['confidence'], 'auto')

    def test_confirm_link(self):
        response = self.client.post(f'{self.base}{self.link.id}/confirm/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.link.refresh_from_db()
        self.assertEqual(self.link.confidence, 'confirmed')

    def test_reject_link(self):
        response = self.client.post(f'{self.base}{self.link.id}/reject/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.link.refresh_from_db()
        self.assertEqual(self.link.confidence, 'rejected')


# ─── Phase 4 — Jobs ──────────────────────────────────────────────────────────

class JobApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='jobuser', password='pw')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user)
        self.job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.PUBMED_SEARCH,
            parameters={'query': 'cancer'},
        )
        self.base = f'/api/v1/projects/{self.project.id}/jobs/'

    def test_list_jobs(self):
        response = self.client.get(self.base)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)

    def test_retrieve_job(self):
        response = self.client.get(f'{self.base}{self.job.id}/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['job_type'], 'pubmed_search')
        self.assertEqual(response.data['status'], 'pending')

    def test_cancel_pending_job(self):
        response = self.client.post(f'{self.base}{self.job.id}/cancel/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'cancelled')

    def test_cancel_completed_job_returns_400(self):
        self.job.status = IngestionJob.JobStatus.COMPLETED
        self.job.save()
        response = self.client.post(f'{self.base}{self.job.id}/cancel/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


# ─── Phase 4 — Stats ─────────────────────────────────────────────────────────

class StatsApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='statsuser', password='pw')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user)
        make_project_paper(self.project, make_paper(pmid=2001), curation_status='included')
        make_project_paper(self.project, make_paper(pmid=2002), curation_status='excluded')

    def test_stats_returns_200(self):
        response = self.client.get(f'/api/v1/projects/{self.project.id}/stats/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_stats_counts_are_correct(self):
        response = self.client.get(f'/api/v1/projects/{self.project.id}/stats/')
        data = response.data
        self.assertEqual(data['total_papers'], 2)
        self.assertEqual(data['included_papers'], 1)
        self.assertEqual(data['excluded_papers'], 1)

    def test_stats_has_aggregation_fields(self):
        response = self.client.get(f'/api/v1/projects/{self.project.id}/stats/')
        data = response.data
        for field in ['papers_by_year', 'papers_by_journal', 'top_genes', 'top_drugs']:
            self.assertIn(field, data)


# ─── Phase 4 — Export ────────────────────────────────────────────────────────

class ExportApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='exportuser', password='pw')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user)
        paper = make_paper(pmid=3001, title='Export Paper')
        make_project_paper(self.project, paper, curation_status='included')
        # excluded paper should not appear in export
        make_project_paper(self.project, make_paper(pmid=3002), curation_status='excluded')

    def test_export_json(self):
        response = self.client.get(
            f'/api/v1/projects/{self.project.id}/export/',
            {'export_format': 'json'},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('application/json', response['Content-Type'])
        data = response.json()
        self.assertIn('papers', data)
        self.assertEqual(len(data['papers']), 1)
        self.assertEqual(data['papers'][0]['pmid'], 3001)

    def test_export_csv(self):
        response = self.client.get(
            f'/api/v1/projects/{self.project.id}/export/',
            {'export_format': 'csv'},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('text/csv', response['Content-Type'])
        content = b''.join(response.streaming_content).decode()
        self.assertIn('Export Paper', content)
        self.assertNotIn('3002', content)  # excluded paper absent


# ─── Phase 4 — Omics search action ───────────────────────────────────────────

class OmicsSearchActionTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='omicsuser', password='pw')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user)

    def test_omics_search_dispatches_job(self):
        with patch('apps.core.services.search_service.run_omics_ingestion.delay'):
            response = self.client.post(
                f'/api/v1/projects/{self.project.id}/omics_search/',
                {'sources': ['geo', 'gwas'], 'max_per_source': 10},
                format='json',
            )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertIn('job_id', response.data)
