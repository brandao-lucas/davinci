"""
Testes de QA para:
  1. Transições de status do pipeline (DaVinciProject.PipelineStatus)
  2. bulk_curate por filtro (papers e datasets)
  3. Filtros relevance_min/max e ingestion_job
  4. Audit-trail (curated_at, notes, exclusion_reason)
  5. Isolamento por usuário (firebase-auth-guard)
  6. Proteção de race-condition (advance_to_curating_if_done + guard de status)

Padrão de setup: sem pytest; usa APITestCase do DRF.
Sem chamadas NCBI: tasks Celery são mockadas via unittest.mock.patch.
"""

from unittest.mock import patch

from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from apps.core.models import (
    DaVinciProject,
    IngestionJob,
    OmicDataset,
    Paper,
    ProjectDataset,
    ProjectPaper,
)
from apps.core.services.project_status import (
    advance_to_curating_if_done,
    revert_to_draft,
    start_searching,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def make_user(username='tester', password='pw'):
    return User.objects.create_user(username=username, password=password)


def make_project(user, title='Test Project', query_term='cancer', status=None):
    p = DaVinciProject.objects.create(
        user=user,
        title=title,
        slug=f'{title.lower().replace(" ", "-")}-{user.username}-davinci-{title[:3]}',
        query_term=query_term,
    )
    if status is not None:
        DaVinciProject.objects.filter(pk=p.pk).update(status=status)
        p.refresh_from_db()
    return p


def make_paper(pmid, pub_year=2020):
    return Paper.objects.create(pmid=pmid, title=f'Paper {pmid}', pub_year=pub_year)


def make_project_paper(project, paper, curation_status='pending',
                       relevance_score=None, notes='', ingestion_job=None):
    return ProjectPaper.objects.create(
        project=project,
        paper=paper,
        curation_status=curation_status,
        relevance_score=relevance_score,
        notes=notes,
        ingestion_job=ingestion_job,
    )


def make_dataset(accession, omic_type='transcriptomic'):
    return OmicDataset.objects.create(
        accession=accession,
        source_db='geo',
        title=f'Dataset {accession}',
        omic_type=omic_type,
        organism='Homo sapiens',
    )


def make_project_dataset(project, dataset, curation_status='pending',
                         relevance_score=None, notes='', ingestion_job=None):
    return ProjectDataset.objects.create(
        project=project,
        dataset=dataset,
        curation_status=curation_status,
        relevance_score=relevance_score,
        notes=notes,
        ingestion_job=ingestion_job,
    )


def make_search_job(project, job_status=IngestionJob.JobStatus.PENDING,
                    job_type=IngestionJob.JobType.PUBMED_SEARCH):
    return IngestionJob.objects.create(
        project=project,
        job_type=job_type,
        status=job_status,
        parameters={'query': 'cancer'},
    )


# ─── 1. Transições de status (service direto) ────────────────────────────────


class StartSearchingServiceTests(APITestCase):
    """start_searching(): draft → searching, com idempotência."""

    def setUp(self):
        self.user = make_user('ss_user')
        self.project = make_project(self.user)

    def test_draft_to_searching(self):
        """Projeto em draft → searching após start_searching."""
        self.assertEqual(self.project.status, DaVinciProject.PipelineStatus.DRAFT)
        start_searching(self.project)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, DaVinciProject.PipelineStatus.SEARCHING)

    def test_idempotent_if_already_searching(self):
        """Chamar start_searching em projeto já em searching não gera erro nem altera status."""
        DaVinciProject.objects.filter(pk=self.project.pk).update(
            status=DaVinciProject.PipelineStatus.SEARCHING
        )
        self.project.refresh_from_db()
        # Deve retornar sem erro
        start_searching(self.project)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, DaVinciProject.PipelineStatus.SEARCHING)

    def test_does_not_revert_curating(self):
        """start_searching é no-op em projeto curating (guard != DRAFT corrigido)."""
        DaVinciProject.objects.filter(pk=self.project.pk).update(
            status=DaVinciProject.PipelineStatus.CURATING
        )
        self.project.refresh_from_db()
        start_searching(self.project)
        self.project.refresh_from_db()
        self.assertEqual(
            self.project.status,
            DaVinciProject.PipelineStatus.CURATING,
            'start_searching deve ser no-op em projeto curating.',
        )

    def test_does_not_revert_analyzing(self):
        """start_searching é no-op em projeto analyzing."""
        DaVinciProject.objects.filter(pk=self.project.pk).update(
            status=DaVinciProject.PipelineStatus.ANALYZING
        )
        self.project.refresh_from_db()
        start_searching(self.project)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, DaVinciProject.PipelineStatus.ANALYZING)

    def test_does_not_revert_complete(self):
        """start_searching é no-op em projeto complete."""
        DaVinciProject.objects.filter(pk=self.project.pk).update(
            status=DaVinciProject.PipelineStatus.COMPLETE
        )
        self.project.refresh_from_db()
        start_searching(self.project)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, DaVinciProject.PipelineStatus.COMPLETE)


class RevertToDraftServiceTests(APITestCase):
    """revert_to_draft(): cancela jobs PENDING/RUNNING, status → draft."""

    def setUp(self):
        self.user = make_user('revert_user')
        self.project = make_project(self.user, status=DaVinciProject.PipelineStatus.SEARCHING)

    def test_revert_cancels_active_jobs(self):
        """Jobs em PENDING e RUNNING viram CANCELLED ao reverter."""
        j_pending = make_search_job(self.project, IngestionJob.JobStatus.PENDING)
        j_running = make_search_job(self.project, IngestionJob.JobStatus.RUNNING)
        j_done = make_search_job(self.project, IngestionJob.JobStatus.COMPLETED)

        revert_to_draft(self.project)

        j_pending.refresh_from_db()
        j_running.refresh_from_db()
        j_done.refresh_from_db()

        self.assertEqual(j_pending.status, IngestionJob.JobStatus.CANCELLED)
        self.assertEqual(j_running.status, IngestionJob.JobStatus.CANCELLED)
        self.assertEqual(j_done.status, IngestionJob.JobStatus.COMPLETED)  # não toca completed

    def test_revert_changes_project_status_to_draft(self):
        """Projeto volta para draft após revert_to_draft."""
        revert_to_draft(self.project)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, DaVinciProject.PipelineStatus.DRAFT)


class AdvanceToCuratingServiceTests(APITestCase):
    """advance_to_curating_if_done(): searching → curating quando não há jobs ativos."""

    def setUp(self):
        self.user = make_user('advance_user')
        self.project = make_project(self.user, status=DaVinciProject.PipelineStatus.SEARCHING)

    def test_advance_when_all_jobs_done(self):
        """Com jobs de busca concluídos, projeto avança para curating."""
        make_search_job(self.project, IngestionJob.JobStatus.COMPLETED)

        advance_to_curating_if_done(self.project)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, DaVinciProject.PipelineStatus.CURATING)

    def test_stays_searching_while_job_pending(self):
        """Com job de busca ainda PENDING, projeto permanece em searching."""
        make_search_job(self.project, IngestionJob.JobStatus.PENDING)

        advance_to_curating_if_done(self.project)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, DaVinciProject.PipelineStatus.SEARCHING)

    def test_stays_searching_while_job_running(self):
        """Com job de busca RUNNING, projeto permanece em searching."""
        make_search_job(self.project, IngestionJob.JobStatus.RUNNING)

        advance_to_curating_if_done(self.project)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, DaVinciProject.PipelineStatus.SEARCHING)

    def test_race_condition_guard_after_revert(self):
        """
        Race-condition guard: se revert_to_draft foi chamado primeiro
        (status voltou para draft), advance_to_curating_if_done NÃO
        deve flipar para curating — guard de status deve barrar.
        """
        # Simula situação de race: jobs cancelados + status draft
        j = make_search_job(self.project, IngestionJob.JobStatus.PENDING)
        revert_to_draft(self.project)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, DaVinciProject.PipelineStatus.DRAFT)

        # Mesmo sem jobs ativos, NÃO deve flipar para curating (status não é searching)
        advance_to_curating_if_done(self.project)

        self.project.refresh_from_db()
        self.assertEqual(
            self.project.status,
            DaVinciProject.PipelineStatus.DRAFT,
            'advance_to_curating_if_done deve respeitar guard de status — '
            'não avançar quando status é draft (race-condition após revert).',
        )

    def test_guard_ignores_non_search_job_types(self):
        """
        Jobs de tipos não-busca (ex.: SAMPLE_FETCH) PENDING não bloqueiam
        a transição para curating — o helper só verifica SEARCH_JOB_TYPES.
        """
        make_search_job(self.project, IngestionJob.JobStatus.PENDING,
                        job_type=IngestionJob.JobType.SAMPLE_FETCH)

        advance_to_curating_if_done(self.project)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, DaVinciProject.PipelineStatus.CURATING)


# ─── 2. perform_update / revert via API ──────────────────────────────────────


class ProjectPatchRevertTests(APITestCase):
    """
    PATCH de campo de busca em projeto searching → revert_to_draft.
    PATCH de campo não-busca (title) → status permanece searching.
    """

    def setUp(self):
        self.user = make_user('patch_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, query_term='cancer')
        # Coloca projeto em searching
        DaVinciProject.objects.filter(pk=self.project.pk).update(
            status=DaVinciProject.PipelineStatus.SEARCHING
        )
        self.project.refresh_from_db()
        self.url = f'/api/v1/projects/{self.project.id}/'

    def test_patch_search_field_reverts_to_draft_and_cancels_jobs(self):
        """PATCH em query_term (campo de busca) reverte para draft e cancela jobs."""
        j = make_search_job(self.project, IngestionJob.JobStatus.PENDING)

        response = self.client.patch(
            self.url,
            {'query_term': 'novo_termo'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, DaVinciProject.PipelineStatus.DRAFT)

        j.refresh_from_db()
        self.assertEqual(j.status, IngestionJob.JobStatus.CANCELLED)

    def test_patch_date_from_reverts_to_draft(self):
        """PATCH em date_from (campo de busca) também reverte para draft."""
        j = make_search_job(self.project, IngestionJob.JobStatus.RUNNING)

        response = self.client.patch(
            self.url,
            {'date_from': 2015},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, DaVinciProject.PipelineStatus.DRAFT)
        j.refresh_from_db()
        self.assertEqual(j.status, IngestionJob.JobStatus.CANCELLED)

    def test_patch_title_does_not_revert(self):
        """PATCH em title (campo não-busca) NÃO reverte o status."""
        j = make_search_job(self.project, IngestionJob.JobStatus.PENDING)

        response = self.client.patch(
            self.url,
            {'title': 'Novo Título'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, DaVinciProject.PipelineStatus.SEARCHING)

        j.refresh_from_db()
        self.assertEqual(j.status, IngestionJob.JobStatus.PENDING)

    def test_patch_description_does_not_revert(self):
        """PATCH em description (campo não-busca) NÃO reverte o status."""
        response = self.client.patch(
            self.url,
            {'description': 'Uma nova descrição'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, DaVinciProject.PipelineStatus.SEARCHING)


# ─── 3. Dispatch de busca via API (mock de .delay) ───────────────────────────


class SearchDispatchTests(APITestCase):
    """
    Testa que o endpoint /search/ cria job e transiciona para searching.
    Celery é mockado para não acionar Rust nem rede.
    """

    def setUp(self):
        self.user = make_user('dispatch_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user)
        self.url = f'/api/v1/projects/{self.project.id}/search/'

    def test_search_dispatch_transitions_to_searching(self):
        """POST /search/ com mock de .delay → projeto vai para searching."""
        with patch('apps.core.services.search_service.run_pubmed_ingestion.delay'):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, DaVinciProject.PipelineStatus.SEARCHING)
        self.assertIn('job_id', response.data)

    def test_search_dispatch_creates_ingestion_job(self):
        """POST /search/ cria um IngestionJob do tipo PUBMED_SEARCH."""
        with patch('apps.core.services.search_service.run_pubmed_ingestion.delay'):
            self.client.post(self.url)
        self.assertTrue(
            IngestionJob.objects.filter(
                project=self.project,
                job_type=IngestionJob.JobType.PUBMED_SEARCH,
            ).exists()
        )

    def test_search_dispatch_idempotent_already_searching(self):
        """POST /search/ enquanto já searching cria novo job mas mantém status searching."""
        DaVinciProject.objects.filter(pk=self.project.pk).update(
            status=DaVinciProject.PipelineStatus.SEARCHING
        )
        with patch('apps.core.services.search_service.run_pubmed_ingestion.delay'):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, DaVinciProject.PipelineStatus.SEARCHING)


# ─── 4. bulk_curate por filtro — Papers ──────────────────────────────────────


class BulkCurateByFilterPapersTests(APITestCase):
    """
    bulk_curate com `filters` em vez de `paper_ids`.
    Verifica filtragem correta, audit-trail, e isolamento.
    """

    def setUp(self):
        self.user = make_user('bulk_filter_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Bulk Filter Project')
        self.base = f'/api/v1/projects/{self.project.id}/papers/'

        # p1: 2010, relevance 0.9 → deve ser excluído pelo filtro pub_year_max=2015
        self.paper1 = make_paper(pmid=9001, pub_year=2010)
        self.pp1 = make_project_paper(
            self.project, self.paper1,
            curation_status='pending', relevance_score=0.9, notes='nota preservada',
        )

        # p2: 2018, relevance 0.5 → NÃO deve ser excluído pelo filtro pub_year_max=2015
        self.paper2 = make_paper(pmid=9002, pub_year=2018)
        self.pp2 = make_project_paper(
            self.project, self.paper2,
            curation_status='pending', relevance_score=0.5, notes='',
        )

        # p3: 2012, já excluded → não tem pendente, mas pode ser afetado pelo filtro
        self.paper3 = make_paper(pmid=9003, pub_year=2012)
        self.pp3 = make_project_paper(
            self.project, self.paper3,
            curation_status='included', relevance_score=0.3, notes='nota do 3',
        )

    def test_filter_pub_year_max_excludes_only_matching(self):
        """filters={pub_year_max: 2015} + excluded → só papers até 2015 ficam excluded."""
        response = self.client.post(
            f'{self.base}bulk_curate/',
            {
                'filters': {'pub_year_max': 2015, 'curation_status': 'pending'},
                'curation_status': 'excluded',
                'exclusion_reason': 'fora da janela',
            },
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Apenas pp1 (2010, pending) casa: pp2 é 2018, pp3 não é pending
        self.assertEqual(response.data['updated'], 1)

        self.pp1.refresh_from_db()
        self.pp2.refresh_from_db()
        self.pp3.refresh_from_db()

        self.assertEqual(self.pp1.curation_status, 'excluded')
        self.assertEqual(self.pp2.curation_status, 'pending')   # não alterado
        self.assertEqual(self.pp3.curation_status, 'included')  # não alterado

    def test_filter_preserves_notes(self):
        """bulk_curate por filtro NÃO apaga notes dos papers atingidos."""
        self.client.post(
            f'{self.base}bulk_curate/',
            {
                'filters': {'pub_year_max': 2015},
                'curation_status': 'excluded',
            },
            format='json',
        )
        self.pp1.refresh_from_db()
        self.assertEqual(
            self.pp1.notes,
            'nota preservada',
            'notes deve ser preservado após bulk_curate por filtro.',
        )

    def test_filter_sets_curated_at(self):
        """bulk_curate por filtro preenche curated_at (curation-audit-trail)."""
        self.assertIsNone(self.pp1.curated_at)
        self.client.post(
            f'{self.base}bulk_curate/',
            {
                'filters': {'pub_year_max': 2015},
                'curation_status': 'excluded',
            },
            format='json',
        )
        self.pp1.refresh_from_db()
        self.assertIsNotNone(self.pp1.curated_at)

    def test_filter_pub_year_min(self):
        """filters={pub_year_min: 2016} filtra apenas papers a partir de 2016."""
        response = self.client.post(
            f'{self.base}bulk_curate/',
            {
                'filters': {'pub_year_min': 2016},
                'curation_status': 'excluded',
            },
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['updated'], 1)  # apenas pp2 (2018)

        self.pp1.refresh_from_db()
        self.pp2.refresh_from_db()
        self.assertEqual(self.pp1.curation_status, 'pending')   # não tocado
        self.assertEqual(self.pp2.curation_status, 'excluded')  # atingido


class BulkCurateRelevanceFilterTests(APITestCase):
    """Filtros relevance_min e relevance_max em bulk_curate de papers."""

    def setUp(self):
        self.user = make_user('relev_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Relevance Filter')
        self.base = f'/api/v1/projects/{self.project.id}/papers/'

        # pp_high: score 0.8
        paper_h = make_paper(pmid=8001, pub_year=2020)
        self.pp_high = make_project_paper(self.project, paper_h, relevance_score=0.8)

        # pp_low: score 0.2
        paper_l = make_paper(pmid=8002, pub_year=2020)
        self.pp_low = make_project_paper(self.project, paper_l, relevance_score=0.2)

        # pp_none: sem score
        paper_n = make_paper(pmid=8003, pub_year=2020)
        self.pp_none = make_project_paper(self.project, paper_n, relevance_score=None)

    def test_relevance_min_filter(self):
        """filters={relevance_min: 0.5} → só papers com score >= 0.5."""
        response = self.client.post(
            f'{self.base}bulk_curate/',
            {'filters': {'relevance_min': 0.5}, 'curation_status': 'included'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['updated'], 1)  # apenas pp_high

        self.pp_high.refresh_from_db()
        self.pp_low.refresh_from_db()
        self.assertEqual(self.pp_high.curation_status, 'included')
        self.assertEqual(self.pp_low.curation_status, 'pending')

    def test_relevance_max_filter(self):
        """filters={relevance_max: 0.5} → só papers com score <= 0.5."""
        response = self.client.post(
            f'{self.base}bulk_curate/',
            {'filters': {'relevance_max': 0.5}, 'curation_status': 'excluded'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['updated'], 1)  # apenas pp_low (0.2)

        self.pp_low.refresh_from_db()
        self.pp_high.refresh_from_db()
        self.assertEqual(self.pp_low.curation_status, 'excluded')
        self.assertEqual(self.pp_high.curation_status, 'pending')


class BulkCurateIngestionJobFilterTests(APITestCase):
    """Filtro ingestion_job em bulk_curate de papers (proveniência)."""

    def setUp(self):
        self.user = make_user('job_filter_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Job Filter Project')
        self.base = f'/api/v1/projects/{self.project.id}/papers/'

        self.job_a = make_search_job(self.project, IngestionJob.JobStatus.COMPLETED)
        self.job_b = make_search_job(self.project, IngestionJob.JobStatus.COMPLETED)

        paper_a = make_paper(pmid=7001, pub_year=2020)
        paper_b = make_paper(pmid=7002, pub_year=2020)

        self.pp_job_a = make_project_paper(
            self.project, paper_a, ingestion_job=self.job_a
        )
        self.pp_job_b = make_project_paper(
            self.project, paper_b, ingestion_job=self.job_b
        )

    def test_filter_by_ingestion_job(self):
        """filters={ingestion_job: <id>} exclui apenas papers daquele job."""
        response = self.client.post(
            f'{self.base}bulk_curate/',
            {
                'filters': {'ingestion_job': str(self.job_a.id)},
                'curation_status': 'excluded',
            },
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['updated'], 1)

        self.pp_job_a.refresh_from_db()
        self.pp_job_b.refresh_from_db()
        self.assertEqual(self.pp_job_a.curation_status, 'excluded')
        self.assertEqual(self.pp_job_b.curation_status, 'pending')


class BulkCuratePapersByIdRegressionTests(APITestCase):
    """Modo IDs antigo (paper_ids) não regrediu."""

    def setUp(self):
        self.user = make_user('ids_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='IDs Regression')
        self.base = f'/api/v1/projects/{self.project.id}/papers/'

        self.p1 = make_paper(pmid=6001)
        self.p2 = make_paper(pmid=6002)
        self.pp1 = make_project_paper(self.project, self.p1)
        self.pp2 = make_project_paper(self.project, self.p2)

    def test_bulk_curate_by_ids_still_works(self):
        """paper_ids ainda funciona conforme esperado (regressão)."""
        response = self.client.post(
            f'{self.base}bulk_curate/',
            {'paper_ids': [self.pp1.id, self.pp2.id], 'curation_status': 'included'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['updated'], 2)

        self.pp1.refresh_from_db()
        self.pp2.refresh_from_db()
        self.assertEqual(self.pp1.curation_status, 'included')
        self.assertEqual(self.pp2.curation_status, 'included')

    def test_bulk_curate_sets_curated_at_by_ids(self):
        """curated_at preenchido no modo IDs (curation-audit-trail)."""
        self.assertIsNone(self.pp1.curated_at)
        self.client.post(
            f'{self.base}bulk_curate/',
            {'paper_ids': [self.pp1.id], 'curation_status': 'excluded'},
            format='json',
        )
        self.pp1.refresh_from_db()
        self.assertIsNotNone(self.pp1.curated_at)

    def test_bulk_curate_neither_ids_nor_filters_returns_400(self):
        """Sem paper_ids nem filters → 400 com detail."""
        response = self.client.post(
            f'{self.base}bulk_curate/',
            {'curation_status': 'excluded'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('detail', response.json())


# ─── 5. bulk_curate por filtro — Datasets ────────────────────────────────────


class BulkCurateByFilterDatasetsTests(APITestCase):
    """
    bulk_curate com `filters` em ProjectDataset.
    Análogo aos testes de papers.
    """

    def setUp(self):
        self.user = make_user('ds_bulk_filter_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='DS Bulk Filter')
        self.base = f'/api/v1/projects/{self.project.id}/datasets/'

        # ds1: transcriptomic, relevance 0.9
        self.ds1 = make_dataset('DS001', omic_type='transcriptomic')
        self.pd1 = make_project_dataset(
            self.project, self.ds1,
            curation_status='pending', relevance_score=0.9, notes='nota ds1',
        )

        # ds2: genomic, relevance 0.3
        self.ds2 = make_dataset('DS002', omic_type='genomic')
        self.pd2 = make_project_dataset(
            self.project, self.ds2,
            curation_status='pending', relevance_score=0.3, notes='',
        )

    def test_filter_omic_type_excludes_only_matching(self):
        """filters={omic_type: transcriptomic} → só datasets desse tipo são afetados."""
        with patch('apps.core.views.dataset_views.run_sample_ingestion'):
            response = self.client.post(
                f'{self.base}bulk_curate/',
                {
                    'filters': {'omic_type': 'transcriptomic'},
                    'curation_status': 'excluded',
                },
                format='json',
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['updated'], 1)

        self.pd1.refresh_from_db()
        self.pd2.refresh_from_db()
        self.assertEqual(self.pd1.curation_status, 'excluded')
        self.assertEqual(self.pd2.curation_status, 'pending')

    def test_filter_sets_curated_at(self):
        """bulk_curate por filtro preenche curated_at em datasets (curation-audit-trail)."""
        self.assertIsNone(self.pd1.curated_at)
        with patch('apps.core.views.dataset_views.run_sample_ingestion'):
            self.client.post(
                f'{self.base}bulk_curate/',
                {'filters': {'omic_type': 'transcriptomic'}, 'curation_status': 'excluded'},
                format='json',
            )
        self.pd1.refresh_from_db()
        self.assertIsNotNone(self.pd1.curated_at)

    def test_filter_preserves_notes_dataset(self):
        """bulk_curate por filtro NÃO apaga notes dos datasets atingidos."""
        with patch('apps.core.views.dataset_views.run_sample_ingestion'):
            self.client.post(
                f'{self.base}bulk_curate/',
                {'filters': {'omic_type': 'transcriptomic'}, 'curation_status': 'excluded'},
                format='json',
            )
        self.pd1.refresh_from_db()
        self.assertEqual(self.pd1.notes, 'nota ds1')

    def test_filter_relevance_min_dataset(self):
        """filters={relevance_min: 0.5} filtra só datasets com score >= 0.5."""
        with patch('apps.core.views.dataset_views.run_sample_ingestion'):
            response = self.client.post(
                f'{self.base}bulk_curate/',
                {'filters': {'relevance_min': 0.5}, 'curation_status': 'included'},
                format='json',
            )
        self.assertEqual(response.data['updated'], 1)
        self.pd1.refresh_from_db()
        self.pd2.refresh_from_db()
        self.assertEqual(self.pd1.curation_status, 'included')
        self.assertEqual(self.pd2.curation_status, 'pending')

    def test_filter_ingestion_job_dataset(self):
        """filters={ingestion_job: <id>} afeta só datasets daquele job."""
        job_a = make_search_job(self.project, IngestionJob.JobStatus.COMPLETED,
                                job_type=IngestionJob.JobType.GEO_SEARCH)
        job_b = make_search_job(self.project, IngestionJob.JobStatus.COMPLETED,
                                job_type=IngestionJob.JobType.GEO_SEARCH)

        ds_a = make_dataset('DSA01')
        ds_b = make_dataset('DSB01')
        pd_a = make_project_dataset(self.project, ds_a, ingestion_job=job_a)
        pd_b = make_project_dataset(self.project, ds_b, ingestion_job=job_b)

        with patch('apps.core.views.dataset_views.run_sample_ingestion'):
            response = self.client.post(
                f'{self.base}bulk_curate/',
                {
                    'filters': {'ingestion_job': str(job_a.id)},
                    'curation_status': 'excluded',
                },
                format='json',
            )
        self.assertEqual(response.data['updated'], 1)
        pd_a.refresh_from_db()
        pd_b.refresh_from_db()
        self.assertEqual(pd_a.curation_status, 'excluded')
        self.assertEqual(pd_b.curation_status, 'pending')

    def test_bulk_curate_dataset_by_ids_regression(self):
        """Modo dataset_ids ainda funciona (regressão)."""
        with patch('apps.core.views.dataset_views.run_sample_ingestion'):
            response = self.client.post(
                f'{self.base}bulk_curate/',
                {'dataset_ids': [self.pd1.id, self.pd2.id], 'curation_status': 'excluded'},
                format='json',
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['updated'], 2)

    def test_bulk_curate_dataset_empty_ids_returns_400(self):
        """
        Paridade com papers: dataset_ids=[] deve retornar 400.

        A view de datasets tem a mesma borda: `dataset_ids is not None` é True
        para lista vazia, então sem guard explícito de len==0 o update seria
        executado com queryset vazio e retornaria 200/updated=0.

        Se este teste FALHAR com 200, encaminhar para vitruvio corrigir
        apps/core/views/dataset_views.py (bulk_curate, linha ~235):
          adicionar: if dataset_ids is not None and len(dataset_ids) == 0: return 400
        """
        response = self.client.post(
            f'{self.base}bulk_curate/',
            {'dataset_ids': [], 'curation_status': 'excluded'},
            format='json',
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_400_BAD_REQUEST,
            'dataset_ids=[] deve retornar 400 (paridade com paper_ids=[]).',
        )
        self.assertIn('detail', response.json())


# ─── 6. Isolamento por usuário (firebase-auth-guard) ─────────────────────────


class BulkCurateIsolationTests(APITestCase):
    """
    Usuário B não consegue bulk-curate papers/datasets do projeto do usuário A.
    """

    def setUp(self):
        self.user_a = make_user('isolate_a')
        self.user_b = make_user('isolate_b')

        self.client_a = APIClient()
        self.client_b = APIClient()
        self.client_a.force_authenticate(user=self.user_a)
        self.client_b.force_authenticate(user=self.user_b)

        self.project_a = make_project(self.user_a, title='Project A Isolate')

        paper = make_paper(pmid=5001)
        self.pp = make_project_paper(self.project_a, paper)

        ds = make_dataset('ISO001')
        self.pd = make_project_dataset(self.project_a, ds)

    def test_user_b_cannot_bulk_curate_papers_of_user_a(self):
        """User B tentando bulk_curate papers do projeto de A recebe 404."""
        response = self.client_b.post(
            f'/api/v1/projects/{self.project_a.id}/papers/bulk_curate/',
            {'paper_ids': [self.pp.id], 'curation_status': 'excluded'},
            format='json',
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND,
            'User B deve receber 404 ao tentar bulk_curate de projeto alheio (firebase-auth-guard).',
        )

        # Garante que nenhum registro foi alterado
        self.pp.refresh_from_db()
        self.assertEqual(self.pp.curation_status, 'pending')

    def test_user_b_cannot_bulk_curate_datasets_of_user_a(self):
        """User B tentando bulk_curate datasets do projeto de A recebe 404."""
        response = self.client_b.post(
            f'/api/v1/projects/{self.project_a.id}/datasets/bulk_curate/',
            {'dataset_ids': [self.pd.id], 'curation_status': 'excluded'},
            format='json',
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND,
            'User B deve receber 404 ao tentar bulk_curate de datasets de projeto alheio.',
        )

        self.pd.refresh_from_db()
        self.assertEqual(self.pd.curation_status, 'pending')

    def test_user_b_cannot_bulk_curate_papers_via_filter_of_user_a(self):
        """
        User B usando filters (com conteúdo) em bulk_curate de projeto alheio → 404.

        Nota: filters={} (dict vazio) é tratado pela view como ausência de filtros
        e retorna 400 antes de checar o projeto — portanto usa-se um filtro real.
        O guard de projeto (_get_project) é chamado somente após a validação de
        paper_ids/filters, garantindo 404 quando o projeto não pertence ao usuário.
        """
        response = self.client_b.post(
            f'/api/v1/projects/{self.project_a.id}/papers/bulk_curate/',
            {'filters': {'curation_status': 'pending'}, 'curation_status': 'excluded'},
            format='json',
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND,
            'User B deve receber 404 ao tentar bulk_curate via filters de projeto alheio.',
        )


# ─── 7. Curation audit-trail (curated_at, notes, exclusion_reason) ───────────


class CurationAuditTrailTests(APITestCase):
    """
    Verifica invariantes de auditoria em PATCH individual e bulk:
      - curated_at preenchido na mudança de status
      - notes preservado entre transições
      - exclusion_reason não sobrescreve quando não enviado
    """

    def setUp(self):
        self.user = make_user('audit_trail_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Audit Trail Project')
        paper = make_paper(pmid=4001)
        self.pp = make_project_paper(
            self.project, paper, curation_status='pending', notes='nota original'
        )
        self.base = f'/api/v1/projects/{self.project.id}/papers/'

    def test_patch_sets_curated_at(self):
        """PATCH individual → curated_at preenchido."""
        self.assertIsNone(self.pp.curated_at)
        response = self.client.patch(
            f'{self.base}{self.pp.id}/',
            {'curation_status': 'included'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.pp.refresh_from_db()
        self.assertIsNotNone(self.pp.curated_at)

    def test_patch_preserves_notes_when_not_sent(self):
        """PATCH sem enviar notes → notes original preservado."""
        response = self.client.patch(
            f'{self.base}{self.pp.id}/',
            {'curation_status': 'excluded', 'exclusion_reason': 'fora do escopo'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.pp.refresh_from_db()
        self.assertEqual(
            self.pp.notes,
            'nota original',
            'notes deve ser preservado quando não enviado no PATCH.',
        )

    def test_bulk_curate_does_not_wipe_notes(self):
        """bulk_curate por IDs NÃO apaga notes existentes."""
        response = self.client.post(
            f'{self.base}bulk_curate/',
            {'paper_ids': [self.pp.id], 'curation_status': 'excluded'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.pp.refresh_from_db()
        self.assertEqual(
            self.pp.notes,
            'nota original',
            'bulk_curate NÃO deve apagar notes pré-existentes (curation-audit-trail).',
        )

    def test_exclusion_reason_not_overwritten_by_bulk_without_explicit_send(self):
        """
        bulk_curate sem `exclusion_reason` no body NÃO sobrescreve exclusion_reason anterior.
        """
        # Define exclusion_reason inicial
        self.client.patch(
            f'{self.base}{self.pp.id}/',
            {'curation_status': 'excluded', 'exclusion_reason': 'motivo original'},
            format='json',
        )
        self.pp.refresh_from_db()
        self.assertEqual(self.pp.exclusion_reason, 'motivo original')

        # bulk_curate sem exclusion_reason
        self.client.post(
            f'{self.base}bulk_curate/',
            {'paper_ids': [self.pp.id], 'curation_status': 'pending'},
            format='json',
        )
        self.pp.refresh_from_db()
        self.assertEqual(
            self.pp.exclusion_reason,
            'motivo original',
            'exclusion_reason NÃO deve ser apagado quando bulk_curate não envia o campo.',
        )

    def test_exclusion_reason_overwritten_when_sent_in_bulk(self):
        """bulk_curate COM `exclusion_reason` no body sobrescreve o anterior."""
        self.client.patch(
            f'{self.base}{self.pp.id}/',
            {'curation_status': 'excluded', 'exclusion_reason': 'antigo'},
            format='json',
        )

        self.client.post(
            f'{self.base}bulk_curate/',
            {
                'paper_ids': [self.pp.id],
                'curation_status': 'excluded',
                'exclusion_reason': 'novo motivo',
            },
            format='json',
        )
        self.pp.refresh_from_db()
        self.assertEqual(self.pp.exclusion_reason, 'novo motivo')
