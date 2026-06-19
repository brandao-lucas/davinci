from unittest.mock import patch

from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from apps.core.models import (
    ClinicalCategory, DaVinciProject, IngestionJob, OmicDataset, OmicSample,
    Paper, ProjectDataset, ProjectPaper, ProjectPaperDataset, ProjectSample,
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
        response = self.client.get(self.base, {'curation_status': 'included'})
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

    def test_list_datasets_filter_by_curation_status(self):
        """
        Bug 2 — regressão: ?curation_status= funciona para datasets (mesmo param de papers).
        O filtro por 'included' deve retornar só o dataset com esse status.
        """
        ds2 = make_dataset(accession='GSE002b', omic_type='genomic')
        make_project_dataset(self.project, ds2, curation_status='included')
        response = self.client.get(self.base, {'curation_status': 'included'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)
        self.assertEqual(response.data['results'][0]['curation_status'], 'included')

    def test_list_datasets_filter_curation_status_pending_returns_pending_only(self):
        """
        Garante que ?curation_status=pending filtra corretamente, ignorando
        outros status — cobre o cenário oposto ao de 'included'.
        """
        ds2 = make_dataset(accession='GSE003b', omic_type='genomic')
        make_project_dataset(self.project, ds2, curation_status='excluded')
        response = self.client.get(self.base, {'curation_status': 'pending'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # setUp cria pd com status 'pending'; ds2 tem 'excluded' → só 1 resultado
        self.assertEqual(len(response.data['results']), 1)
        self.assertEqual(response.data['results'][0]['curation_status'], 'pending')


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


# ─── Phase 4 — Auditoria de curadoria e shape de erros ────────────────────────
# Testes focados em lacunas NÃO cobertas por PaperApiTests. Cada teste
# referencia um ponto do mapa de falhas em
# .claude/plans/2026-04-19-testes-pipeline-artigos.md

class PaperAuditAndErrorShapeTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='audituser', password='pw')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='Audit Project')
        self.paper1 = make_paper(pmid=7001, title='Paper 7001')
        self.paper2 = make_paper(pmid=7002, title='Paper 7002')
        self.pp1 = make_project_paper(self.project, self.paper1, curation_status='pending')
        self.pp2 = make_project_paper(self.project, self.paper2, curation_status='pending')
        self.base = f'/api/v1/projects/{self.project.id}/papers/'

    # ── Auditoria (curation-audit-trail) ─────────────────────────────────────

    def test_patch_sets_curated_at(self):
        """✅ Skill curation-audit-trail: PATCH de status preenche curated_at."""
        self.assertIsNone(self.pp1.curated_at)

        response = self.client.patch(
            f'{self.base}{self.pp1.id}/',
            {'curation_status': 'included', 'notes': 'primeira nota'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.pp1.refresh_from_db()
        self.assertEqual(self.pp1.curation_status, 'included')
        self.assertIsNotNone(self.pp1.curated_at, 'curated_at deve ser preenchido ao mudar status')

    def test_patch_preserves_notes_across_transitions(self):
        """
        ✅ Invariante crítica: transições pending → included → excluded → included
        preservam notes. Perder notes é inaceitável (skill curation-audit-trail).
        """
        # 1. inclui com nota
        self.client.patch(
            f'{self.base}{self.pp1.id}/',
            {'curation_status': 'included', 'notes': 'nota original'},
            format='json',
        )
        # 2. exclui sem tocar notes
        self.client.patch(
            f'{self.base}{self.pp1.id}/',
            {'curation_status': 'excluded', 'exclusion_reason': 'fora do escopo'},
            format='json',
        )
        self.pp1.refresh_from_db()
        self.assertEqual(self.pp1.notes, 'nota original', 'notes foi perdida na transição included→excluded')

        # 3. volta para included
        self.client.patch(
            f'{self.base}{self.pp1.id}/',
            {'curation_status': 'included'},
            format='json',
        )
        self.pp1.refresh_from_db()
        self.assertEqual(self.pp1.notes, 'nota original', 'notes foi perdida na transição excluded→included')

    def test_bulk_curate_sets_curated_at_for_all(self):
        """
        ✅ Skill curation-audit-trail: bulk_curate preserva auditoria para todos os registros.
        Caso ausente em PaperApiTests.test_bulk_curate original.
        """
        self.assertIsNone(self.pp1.curated_at)
        self.assertIsNone(self.pp2.curated_at)

        response = self.client.post(
            f'{self.base}bulk_curate/',
            {'paper_ids': [self.pp1.id, self.pp2.id], 'curation_status': 'excluded'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['updated'], 2)

        self.pp1.refresh_from_db()
        self.pp2.refresh_from_db()
        self.assertIsNotNone(self.pp1.curated_at)
        self.assertIsNotNone(self.pp2.curated_at)

    # ── Shape dos erros (input para o front) ─────────────────────────────────

    def test_bulk_curate_invalid_status_returns_detail_key(self):
        """
        ✅ Documenta SHAPE do erro 400 para o front. O cliente axios vai ler
        err.response.data.detail — garantir que essa chave existe.
        """
        response = self.client.post(
            f'{self.base}bulk_curate/',
            {'paper_ids': [self.pp1.id], 'curation_status': 'not_a_real_status'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        body = response.json()
        self.assertIn('detail', body, 'front espera chave "detail" no erro 400')
        self.assertIsInstance(body['detail'], str)
        self.assertIn('valid', body['detail'].lower() + body['detail'])  # "Invalid status..."

    def test_bulk_curate_invalid_status_does_not_echo_input(self):
        """
        ❌ LACUNA E6 extra (falha intencional — UX): a mensagem de erro 400 NÃO
        inclui o valor inválido que o usuário mandou. Ex: usuário digita
        "includd" (typo) e recebe apenas a lista de valores válidos, sem dica
        do que ele mandou de errado.

        Comportamento desejado: "Invalid status 'includd'. Choose from [...]"
        para o front conseguir mostrar toast claro.
        """
        response = self.client.post(
            f'{self.base}bulk_curate/',
            {'paper_ids': [self.pp1.id], 'curation_status': 'includd'},
            format='json',
        )
        body = response.json()
        self.assertIn(
            'includd',
            body['detail'],
            'E6: mensagem deveria ecoar o valor inválido para UX. '
            'Hoje não ecoa — toast no front fica genérico.'
        )

    def test_bulk_curate_without_paper_ids_returns_400(self):
        """✅ Erro controlado com mensagem específica quando paper_ids está vazio."""
        response = self.client.post(
            f'{self.base}bulk_curate/',
            {'paper_ids': [], 'curation_status': 'included'},
            format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('detail', response.json())

    # ── Auth / isolamento (firebase-auth-guard) ───────────────────────────────

    def test_list_without_auth_returns_401(self):
        """✅ Skill firebase-auth-guard: endpoint exige autenticação."""
        anon_client = APIClient()
        response = anon_client.get(self.base)
        self.assertIn(response.status_code, (401, 403))

    def test_user_b_cannot_patch_paper_of_user_a(self):
        """
        ✅ Skill firebase-auth-guard: User B recebe 404 (não 403) ao tentar
        editar paper do projeto de User A — não vazar existência do recurso.
        """
        user_b = User.objects.create_user(username='userB', password='pw')
        other_client = APIClient()
        other_client.force_authenticate(user=user_b)

        response = other_client.patch(
            f'{self.base}{self.pp1.id}/',
            {'curation_status': 'included'},
            format='json',
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND,
            'User B tentando patch em projeto de User A deve receber 404, não 403 '
            '(não vazar existência — skill firebase-auth-guard).',
        )


# ─── Op 4.3 — Samples ────────────────────────────────────────────────────────

def make_sample(dataset, accession='GSM001', title='Sample 001', organism='Homo sapiens'):
    return OmicSample.objects.create(
        dataset=dataset,
        accession=accession,
        title=title,
        organism=organism,
    )


def make_project_sample(project, sample, curation_status='pending'):
    return ProjectSample.objects.create(
        project=project, sample=sample, curation_status=curation_status
    )


class SampleApiTests(APITestCase):
    """
    Testes de listagem, detalhe e curadoria individual de ProjectSample.
    Cobre: list, retrieve, partial_update (curated_at), filtros.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='sampleuser', password='pw')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user)
        self.dataset = make_dataset(accession='GSE100')
        # ProjectDataset necessário para a validação de isolamento do achado #1
        # Guardamos o retorno para usar ProjectDataset.id na URL (identificador canônico).
        self.pd = make_project_dataset(self.project, self.dataset)
        self.sample = make_sample(self.dataset, accession='GSM100')
        self.ps = make_project_sample(self.project, self.sample)
        self.base = f'/api/v1/projects/{self.project.id}/samples/'
        # dataset_base usa ProjectDataset.id (pd.id), que é o mesmo 'id' exposto
        # pelo serializer da lista de datasets — identificador canônico único.
        self.dataset_base = f'/api/v1/projects/{self.project.id}/datasets/{self.pd.id}/samples/'

    def test_list_samples_of_project(self):
        response = self.client.get(self.base)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)
        self.assertEqual(response.data['results'][0]['accession'], 'GSM100')

    def test_list_samples_by_dataset_route(self):
        """Rota aninhada /datasets/{dataset_pk}/samples/ lista só os samples do dataset."""
        other_ds = make_dataset(accession='GSE200')
        make_project_dataset(self.project, other_ds)
        other_sample = make_sample(other_ds, accession='GSM200')
        make_project_sample(self.project, other_sample)

        response = self.client.get(self.dataset_base)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        accessions = [r['accession'] for r in response.data['results']]
        self.assertIn('GSM100', accessions)
        self.assertNotIn('GSM200', accessions)

    def test_list_samples_filter_curation_status(self):
        s2 = make_sample(self.dataset, accession='GSM101')
        make_project_sample(self.project, s2, curation_status='included')
        response = self.client.get(self.base, {'curation_status': 'included'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)
        self.assertEqual(response.data['results'][0]['curation_status'], 'included')

    def test_list_samples_filter_by_dataset_query_param(self):
        """
        Filtro ?dataset=<id> na rota plana /samples/ restringe ao dataset.
        O valor passado é ProjectDataset.id (mesmo 'id' da lista de datasets),
        não OmicDataset.id.
        """
        other_ds = make_dataset(accession='GSE300')
        other_pd = make_project_dataset(self.project, other_ds)
        other_sample = make_sample(other_ds, accession='GSM300')
        make_project_sample(self.project, other_sample)

        # Usa self.pd.id (ProjectDataset.id), não self.dataset.id (OmicDataset.id)
        response = self.client.get(self.base, {'dataset': self.pd.id})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        accessions = [r['accession'] for r in response.data['results']]
        self.assertIn('GSM100', accessions)
        self.assertNotIn('GSM300', accessions)

    def test_retrieve_sample_detail(self):
        response = self.client.get(f'{self.base}{self.ps.id}/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('sample', response.data)
        self.assertEqual(response.data['sample']['accession'], 'GSM100')

    def test_patch_sample_curation_sets_curated_at(self):
        """curation-audit-trail: PATCH preenche curated_at."""
        self.assertIsNone(self.ps.curated_at)
        response = self.client.patch(
            f'{self.base}{self.ps.id}/',
            {'curation_status': 'included', 'notes': 'boa amostra'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.ps.refresh_from_db()
        self.assertEqual(self.ps.curation_status, 'included')
        self.assertEqual(self.ps.notes, 'boa amostra')
        self.assertIsNotNone(self.ps.curated_at)

    def test_patch_preserves_notes_across_transitions(self):
        """curation-audit-trail: notes não é perdida em transições de status."""
        self.client.patch(
            f'{self.base}{self.ps.id}/',
            {'curation_status': 'included', 'notes': 'nota preservada'},
            format='json',
        )
        self.client.patch(
            f'{self.base}{self.ps.id}/',
            {'curation_status': 'excluded', 'exclusion_reason': 'fora do escopo'},
            format='json',
        )
        self.ps.refresh_from_db()
        self.assertEqual(self.ps.notes, 'nota preservada')


class SampleBulkCurateTests(APITestCase):
    """
    Testes de bulk_curate de samples: auditoria, erros, idempotência.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='bulksampleuser', password='pw')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user)
        self.dataset = make_dataset(accession='GSE400')
        self.s1 = make_sample(self.dataset, accession='GSM400')
        self.s2 = make_sample(self.dataset, accession='GSM401')
        self.ps1 = make_project_sample(self.project, self.s1)
        self.ps2 = make_project_sample(self.project, self.s2)
        self.base = f'/api/v1/projects/{self.project.id}/samples/'

    def test_bulk_curate_sets_curated_at_for_all(self):
        """curation-audit-trail: bulk_curate preenche curated_at em todos os registros."""
        self.assertIsNone(self.ps1.curated_at)
        self.assertIsNone(self.ps2.curated_at)
        response = self.client.post(
            f'{self.base}bulk_curate/',
            {'sample_ids': [self.ps1.id, self.ps2.id], 'curation_status': 'included'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['updated'], 2)
        self.ps1.refresh_from_db()
        self.ps2.refresh_from_db()
        self.assertIsNotNone(self.ps1.curated_at)
        self.assertIsNotNone(self.ps2.curated_at)

    def test_bulk_curate_invalid_status_returns_detail(self):
        """Shape do erro 400 — chave 'detail' com o status inválido ecoado."""
        response = self.client.post(
            f'{self.base}bulk_curate/',
            {'sample_ids': [self.ps1.id], 'curation_status': 'invalid_status'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        body = response.json()
        self.assertIn('detail', body)
        self.assertIn('invalid_status', body['detail'])

    def test_bulk_curate_empty_sample_ids_returns_400(self):
        response = self.client.post(
            f'{self.base}bulk_curate/',
            {'sample_ids': [], 'curation_status': 'included'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('detail', response.json())


class SampleDatasetRouteTests(APITestCase):
    """
    Bug 1 — regressão: /datasets/{dataset_pk}/samples/ usa ProjectDataset.id
    (o mesmo 'id' que a lista de datasets expõe), não OmicDataset.id.

    Garante que:
    - dataset que pertence ao projeto + sem samples → 200 lista vazia (nunca 404)
    - dataset que pertence ao projeto + com samples → 200 lista com samples
    - dataset de outro projeto → 404 (segurança: não vaza existência)
    """

    def setUp(self):
        self.user = User.objects.create_user(username='dsroute_user', password='pw')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, title='DS Route Project', query_term='x')
        self.dataset = make_dataset(accession='GSE700')
        self.pd = make_project_dataset(self.project, self.dataset, curation_status='pending')
        # URL usa ProjectDataset.id (campo 'id' exposto pelo serializer da lista)
        self.samples_url = (
            f'/api/v1/projects/{self.project.id}/datasets/{self.pd.id}/samples/'
        )

    def test_dataset_in_project_no_samples_returns_200_empty(self):
        """
        Bug 1 — caso principal: dataset está no projeto mas ainda não tem samples
        ingeridos. Deve retornar 200 com lista vazia, não 404.
        A causa raiz era get_object_or_404(..., dataset_id=dataset_pk) resolvendo
        o ProjectDataset.id como OmicDataset.id → miss → 404.
        """
        response = self.client.get(self.samples_url)
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            f'Esperado 200 (lista vazia), obtido {response.status_code}. '
            'Bug 1: a rota de samples deve aceitar ProjectDataset.id.',
        )
        self.assertEqual(
            len(response.data['results']),
            0,
            'Lista deve ser vazia quando nenhum sample foi ingerido para o dataset.',
        )

    def test_dataset_in_project_with_samples_returns_200_with_results(self):
        """Dataset no projeto + samples ingeridos → 200 com samples listados."""
        sample = make_sample(self.dataset, accession='GSM700')
        make_project_sample(self.project, sample)
        response = self.client.get(self.samples_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)
        self.assertEqual(response.data['results'][0]['accession'], 'GSM700')

    def test_dataset_not_in_project_returns_404(self):
        """
        dataset_pk que não pertence ao projeto do usuário → 404.
        Garante que a validação de segurança continua funcionando após a correção.
        """
        other_user = User.objects.create_user(username='dsroute_other', password='pw')
        other_project = make_project(other_user, title='Other', query_term='y')
        other_dataset = make_dataset(accession='GSE701')
        other_pd = make_project_dataset(other_project, other_dataset)
        # Usa o ProjectDataset.id de outro projeto — deve ser 404
        response = self.client.get(
            f'/api/v1/projects/{self.project.id}/datasets/{other_pd.id}/samples/'
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND,
            'dataset_pk de outro projeto deve retornar 404 (não vazar existência).',
        )


class SampleIsolationTests(APITestCase):
    """
    Testes de isolamento entre usuários (firebase-auth-guard).
    Usuário B não pode listar nem curar samples do projeto de Usuário A.
    """

    def setUp(self):
        self.user_a = User.objects.create_user(username='isolation_a', password='pw')
        self.user_b = User.objects.create_user(username='isolation_b', password='pw')
        self.client_a = APIClient()
        self.client_b = APIClient()
        self.client_a.force_authenticate(user=self.user_a)
        self.client_b.force_authenticate(user=self.user_b)

        self.project_a = make_project(self.user_a, title='Project A', query_term='x')
        dataset = make_dataset(accession='GSE500')
        sample = make_sample(dataset, accession='GSM500')
        self.ps = make_project_sample(self.project_a, sample)

    def test_user_b_cannot_list_samples_of_user_a_project(self):
        """firebase-auth-guard: 404 ao listar samples de projeto alheio."""
        response = self.client_b.get(
            f'/api/v1/projects/{self.project_a.id}/samples/'
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_user_b_cannot_patch_sample_of_user_a(self):
        """firebase-auth-guard: 404 ao tentar curar sample de projeto alheio."""
        response = self.client_b.patch(
            f'/api/v1/projects/{self.project_a.id}/samples/{self.ps.id}/',
            {'curation_status': 'included'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_user_b_cannot_bulk_curate_sample_of_user_a(self):
        """firebase-auth-guard: bulk_curate em projeto alheio → 0 registros alterados (404 no projeto)."""
        response = self.client_b.post(
            f'/api/v1/projects/{self.project_a.id}/samples/bulk_curate/',
            {'sample_ids': [self.ps.id], 'curation_status': 'included'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_dataset_pk_outside_project_returns_404(self):
        """
        Achado #1 (007): dataset_pk da rota aninhada validado contra o projeto.

        Usuário A possui projeto_a. Usuário B possui projeto_b com pd_b
        (ProjectDataset de projeto_b). Acessar
        /projects/{projeto_a}/datasets/{pd_b.id}/samples/ deve retornar 404
        — não lista vazia — para não vazar existência do dataset.

        Nota: dataset_pk é o ProjectDataset.id (mesmo 'id' exposto na lista de
        datasets). O teste usa pd_b.id (ProjectDataset do projeto B), não
        dataset_b.id (OmicDataset), pois o identificador canônico é o ProjectDataset.id.
        """
        project_b = make_project(self.user_b, title='Project B', query_term='y')
        dataset_b = make_dataset(accession='GSE501')
        # pd_b é o ProjectDataset de dataset_b no projeto_b
        pd_b = make_project_dataset(project_b, dataset_b)

        # Usa pd_b.id (ProjectDataset do projeto B) na URL do projeto A
        response = self.client_a.get(
            f'/api/v1/projects/{self.project_a.id}/datasets/{pd_b.id}/samples/'
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND,
            'Achado #1: ProjectDataset.id de outro projeto deve retornar 404, não lista vazia.',
        )

    def test_dataset_query_param_outside_project_returns_404(self):
        """
        Achado #1 (007): query param ?dataset=<id> validado contra o projeto.

        Mesmo cenário do teste anterior, mas usando a rota plana com ?dataset=<id>.
        dataset_pk via query param também é ProjectDataset.id.
        """
        project_b = make_project(self.user_b, title='Project B2', query_term='z')
        dataset_b = make_dataset(accession='GSE502')
        pd_b = make_project_dataset(project_b, dataset_b)

        response = self.client_a.get(
            f'/api/v1/projects/{self.project_a.id}/samples/',
            {'dataset': pd_b.id},
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND,
            'Achado #1: ?dataset=<ProjectDataset.id de outro projeto> deve retornar 404.',
        )


class SampleIngestionTriggerTests(APITestCase):
    """
    Testa o trigger de ingestão sob demanda:
    - PATCH de dataset para 'included' dispara run_sample_ingestion.delay
    - bulk_curate de dataset para 'included' dispara run_sample_ingestion.delay
    - Não redispara se já há OmicSamples para o dataset (idempotência)
    """

    def setUp(self):
        self.user = User.objects.create_user(username='triggeruser', password='pw')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user)
        self.dataset = make_dataset(accession='GSE600')
        self.pd = make_project_dataset(self.project, self.dataset)
        self.base = f'/api/v1/projects/{self.project.id}/datasets/'

    def test_patch_dataset_to_included_dispatches_sample_ingestion(self):
        """Trigger: PATCH curation_status=included dispara run_sample_ingestion.delay."""
        with patch(
            'apps.core.views.dataset_views.run_sample_ingestion'
        ) as mock_task:
            response = self.client.patch(
                f'{self.base}{self.pd.id}/',
                {'curation_status': 'included'},
                format='json',
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.pd.refresh_from_db()
        self.assertEqual(self.pd.curation_status, 'included')
        mock_task.delay.assert_called_once_with(str(self.project.id), self.dataset.id)

    def test_patch_dataset_to_included_no_dispatch_if_samples_exist(self):
        """Idempotência: se já há OmicSamples para o dataset, .delay() não é chamado."""
        make_sample(self.dataset, accession='GSM600')
        with patch(
            'apps.core.views.dataset_views.run_sample_ingestion'
        ) as mock_task:
            self.client.patch(
                f'{self.base}{self.pd.id}/',
                {'curation_status': 'included'},
                format='json',
            )
            mock_task.delay.assert_not_called()

    def test_bulk_curate_datasets_to_included_dispatches_sample_ingestion(self):
        """Trigger: bulk_curate curation_status=included dispara run_sample_ingestion.delay."""
        with patch(
            'apps.core.views.dataset_views.run_sample_ingestion'
        ) as mock_task:
            response = self.client.post(
                f'{self.base}bulk_curate/',
                {'dataset_ids': [self.pd.id], 'curation_status': 'included'},
                format='json',
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            mock_task.delay.assert_called_once_with(str(self.project.id), self.dataset.id)

    def test_bulk_curate_datasets_no_dispatch_if_samples_exist(self):
        """Idempotência: bulk_curate não redispara se já há OmicSamples."""
        make_sample(self.dataset, accession='GSM601')
        with patch(
            'apps.core.views.dataset_views.run_sample_ingestion'
        ) as mock_task:
            self.client.post(
                f'{self.base}bulk_curate/',
                {'dataset_ids': [self.pd.id], 'curation_status': 'included'},
                format='json',
            )
            mock_task.delay.assert_not_called()

    def test_patch_dataset_to_excluded_does_not_dispatch(self):
        """Trigger só dispara para 'included' — outros status não disparam."""
        with patch(
            'apps.core.views.dataset_views.run_sample_ingestion'
        ) as mock_task:
            self.client.patch(
                f'{self.base}{self.pd.id}/',
                {'curation_status': 'excluded'},
                format='json',
            )
            mock_task.delay.assert_not_called()
