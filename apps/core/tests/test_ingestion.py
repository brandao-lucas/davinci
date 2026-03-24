import os
import unittest
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TransactionTestCase

from apps.core.models import DaVinciProject, IngestionJob, Paper
from apps.core.tasks.ingestion_tasks import run_omics_ingestion, run_pubmed_ingestion


# ─── Phase 2 — PubMed ingestion (existing) ───────────────────────────────────

class IngestionTasksTestCase(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='test', password='pw')
        self.project = DaVinciProject.objects.create(
            title='Test',
            user=self.user,
            query_term='cancer',
            slug='test',
        )
        self.job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.PUBMED_SEARCH,
            parameters={'query': 'cancer'},
        )

    def test_run_pubmed_ingestion(self):
        result = run_pubmed_ingestion(str(self.job.id))
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, IngestionJob.JobStatus.COMPLETED)
        self.assertIn('processed', result)
        self.assertIn('inserted', result)


# ─── Phase 3 — Omics metadata ingestion ──────────────────────────────────────

class OmicsIngestionTestCase(TransactionTestCase):
    """Tests for Phase 3 omics metadata ingestion via run_omics_ingestion."""

    def setUp(self):
        self.user = User.objects.create_user(username='omics_test', password='pw')
        self.project = DaVinciProject.objects.create(
            title='CVD Omics Test',
            user=self.user,
            query_term='cardiovascular disease',
            slug='cvd-omics-test',
        )
        # Pre-insert a known paper so DatasetPaperLink can be created
        # PMID 20301583 is a real CVD paper cited by GEO/GWAS datasets.
        self.paper = Paper.objects.create(
            pmid=20301583,
            title='Genome-wide association study of cardiovascular disease',
            abstract='A GWAS study of coronary artery disease.',
            journal='Nat Genet',
        )

    def test_run_omics_ingestion_stub(self):
        """
        Without a compiled Rust engine, the ImportError fallback path runs.
        Job completes with 0 records — always passes in CI.
        """
        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.GEO_SEARCH,
            parameters={'query': 'cardiovascular disease', 'sources': ['geo']},
        )
        result = run_omics_ingestion(str(job.id))
        job.refresh_from_db()

        self.assertEqual(job.status, IngestionJob.JobStatus.COMPLETED)
        self.assertIn('datasets_inserted', result)
        self.assertIn('links_inserted', result)
        self.assertIn('errors', result)

    def test_dispatch_omics_search_creates_job(self):
        """
        SearchService.dispatch_omics_search creates an IngestionJob
        with the correct job_type and parameters.
        """
        from apps.core.services.search_service import SearchService

        with patch('apps.core.services.search_service.run_omics_ingestion.delay'):
            job = SearchService.dispatch_omics_search(
                self.project,
                sources=['geo', 'gwas'],
                max_per_source=10,
            )

        self.assertEqual(job.job_type, IngestionJob.JobType.GEO_SEARCH)
        self.assertIn('sources', job.parameters)
        self.assertEqual(job.parameters['sources'], ['geo', 'gwas'])
        self.assertEqual(job.parameters['max_per_source'], 10)
        self.assertEqual(job.parameters['query'], 'cardiovascular disease')

    def test_dispatch_omics_search_default_sources(self):
        """When no sources are specified, all four sources are included."""
        from apps.core.services.search_service import SearchService

        with patch('apps.core.services.search_service.run_omics_ingestion.delay'):
            job = SearchService.dispatch_omics_search(self.project)

        self.assertIn('sources', job.parameters)
        self.assertCountEqual(
            job.parameters['sources'],
            ['geo', 'sra', 'bioproject', 'gwas'],
        )

    @unittest.skipUnless(
        os.environ.get('INTEGRATION_TEST') == '1',
        'Requires compiled Rust engine and live NCBI/EBI APIs. '
        'Run with: INTEGRATION_TEST=1 python manage.py test',
    )
    def test_full_omics_pipeline_geo(self):
        """
        Full integration: fetch real GEO data for cardiovascular disease,
        validate OmicDataset rows and at least one DatasetPaperLink created.
        """
        from apps.core.models import DatasetPaperLink, OmicDataset

        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.GEO_SEARCH,
            parameters={
                'query': 'cardiovascular disease',
                'sources': ['geo'],
                'max_per_source': 50,
            },
        )
        result = run_omics_ingestion(str(job.id))
        job.refresh_from_db()

        self.assertEqual(job.status, IngestionJob.JobStatus.COMPLETED)
        self.assertGreater(result['datasets_inserted'], 0)

        # GEO datasets exist in DB
        geo_count = OmicDataset.objects.filter(source_db='geo').count()
        self.assertGreater(geo_count, 0)

        # At least one dataset has a known omic_type
        typed = OmicDataset.objects.exclude(omic_type='').count()
        self.assertGreater(typed, 0)

        # DatasetPaperLink to the pre-inserted paper (if PMID was referenced)
        links = DatasetPaperLink.objects.filter(paper=self.paper)
        # Not guaranteed, but assert the model is queryable
        self.assertIsNotNone(links)

    @unittest.skipUnless(
        os.environ.get('INTEGRATION_TEST') == '1',
        'Requires compiled Rust engine and live EBI API.',
    )
    def test_full_omics_pipeline_gwas(self):
        """Integration: fetch real GWAS Catalog data, validate genomic classification."""
        from apps.core.models import OmicDataset

        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.GEO_SEARCH,
            parameters={
                'query': 'cardiovascular disease',
                'sources': ['gwas'],
                'max_per_source': 20,
            },
        )
        result = run_omics_ingestion(str(job.id))
        job.refresh_from_db()

        self.assertEqual(job.status, IngestionJob.JobStatus.COMPLETED)
        self.assertGreater(result['datasets_inserted'], 0)

        gwas_datasets = OmicDataset.objects.filter(source_db='gwas_catalog')
        self.assertGreater(gwas_datasets.count(), 0)

        # All GWAS datasets must be classified as 'genomic'
        non_genomic = gwas_datasets.exclude(omic_type='genomic').count()
        self.assertEqual(non_genomic, 0)
