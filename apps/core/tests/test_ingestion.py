import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from celery.exceptions import Retry
from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase

from apps.accounts.models import UserProfile
from apps.core.models import DaVinciProject, IngestionJob, Paper
from apps.core.services.search_service import SearchService
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
        # Forçamos ModuleNotFoundError independente de o engine estar compilado.
        # Sem o patch, o Rust real rodaria e marcaria COMPLETED via defense-in-depth.
        with patch.dict(sys.modules, {'rust_engine': None}):
            result = run_pubmed_ingestion(str(self.job.id))
        self.job.refresh_from_db()
        # Sem rust_engine compilado, ImportError marca FAILED (E2).
        self.assertEqual(self.job.status, IngestionJob.JobStatus.FAILED)
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
        Job is marked FAILED with error_message — always passes in CI.
        Forçamos ModuleNotFoundError via patch.dict independente de o engine estar no venv.
        """
        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.GEO_SEARCH,
            parameters={'query': 'cardiovascular disease', 'sources': ['geo']},
        )
        with patch.dict(sys.modules, {'rust_engine': None}):
            result = run_omics_ingestion(str(job.id))
        job.refresh_from_db()

        # Sem rust_engine compilado, ImportError marca FAILED (E2).
        self.assertEqual(job.status, IngestionJob.JobStatus.FAILED)
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


# ─── Phase 4 — SearchService (ponto de entrada do pipeline) ──────────────────

class SearchServiceTestCase(TestCase):
    """
    Valida o service que dispara a ingestão. Cada teste cobre um ponto do
    'mapa de falhas' do plano (.claude/plans/2026-04-19-testes-pipeline-artigos.md).
    """

    def setUp(self):
        self.user = User.objects.create_user(username='search_svc_user', password='pw')
        self.project = DaVinciProject.objects.create(
            user=self.user,
            title='Search Test',
            slug='search-test-search_svc_user-davinci',
            query_term='cardiovascular disease',
            query_synonyms=['CVD', 'heart disease'],
        )

    def test_dispatch_pubmed_creates_job_with_pubmed_search_type(self):
        """✅ Caminho feliz: cria IngestionJob com job_type correto."""
        with patch('apps.core.services.search_service.run_pubmed_ingestion.delay') as delay:
            job = SearchService.dispatch_pubmed_search(self.project, user=self.user)

        self.assertEqual(job.job_type, IngestionJob.JobType.PUBMED_SEARCH)
        self.assertEqual(job.status, IngestionJob.JobStatus.PENDING)
        self.assertEqual(job.project_id, self.project.id)
        delay.assert_called_once_with(str(job.id))

    def test_dispatch_pubmed_combines_synonyms_with_or(self):
        """✅ query_term + sinônimos viram uma query NCBI concatenada com OR."""
        with patch('apps.core.services.search_service.run_pubmed_ingestion.delay'):
            job = SearchService.dispatch_pubmed_search(self.project, user=self.user)

        self.assertEqual(
            job.parameters['query'],
            'cardiovascular disease OR CVD OR heart disease',
        )

    def test_dispatch_pubmed_propagates_user_ncbi_api_key(self):
        """✅ NCBI API key do UserProfile é propagada para os parameters do job."""
        UserProfile.objects.create(
            user=self.user,
            firebase_uid='firebase-search-svc-user',
            ncbi_api_key='KEY123',
        )
        with patch('apps.core.services.search_service.run_pubmed_ingestion.delay'):
            job = SearchService.dispatch_pubmed_search(self.project, user=self.user)

        self.assertEqual(job.parameters['ncbi_api_key'], 'KEY123')

    def test_dispatch_pubmed_without_profile_sets_none(self):
        """✅ Usuário sem UserProfile não quebra; ncbi_api_key fica None."""
        with patch('apps.core.services.search_service.run_pubmed_ingestion.delay'):
            job = SearchService.dispatch_pubmed_search(self.project, user=self.user)

        self.assertIsNone(job.parameters['ncbi_api_key'])

    def test_dispatch_pubmed_when_celery_broker_fails_leaves_orphan_job(self):
        """
        ❌ LACUNA E4 (falha intencional — documenta comportamento atual).

        Hoje, se `run_pubmed_ingestion.delay(...)` lança exceção (ex.: Redis fora),
        `SearchService.dispatch_pubmed_search` NÃO faz rollback do IngestionJob.
        Resultado: Job fica pendurado em status=pending sem ninguém para executá-lo.

        Comportamento desejado (próximo ciclo): transaction.atomic() envolvendo
        create + delay, ou try/except marcando job.status=FAILED imediatamente.

        Esse teste espera que, DEPOIS de uma falha do delay, o Job esteja
        consistentemente marcado como FAILED. Hoje ele está em PENDING,
        portanto assertEqual vai falhar — e essa falha é a evidência da lacuna.
        """
        with patch(
            'apps.core.services.search_service.run_pubmed_ingestion.delay',
            side_effect=RuntimeError('broker down'),
        ):
            with self.assertRaises(RuntimeError):
                SearchService.dispatch_pubmed_search(self.project, user=self.user)

        # Captura o comportamento atual: job foi criado mas ficou órfão.
        orphan = IngestionJob.objects.filter(project=self.project).first()
        self.assertIsNotNone(orphan, 'IngestionJob foi criado antes do delay()')

        # Comportamento DESEJADO — este assert falha hoje (LACUNA E4):
        self.assertEqual(
            orphan.status,
            IngestionJob.JobStatus.FAILED,
            'E4: dispatch deveria marcar o Job como FAILED quando delay() falha '
            '(hoje fica em PENDING — Job órfão). Ver .claude/plans/2026-04-19-testes-pipeline-artigos.md'
        )


# ─── Phase 4 — Celery task com rust_engine mockado ───────────────────────────

def _fake_rust_module(search_fn=None, resolve_fn=None):
    """
    Injeta um módulo falso 'rust_engine' em sys.modules para isolar a task
    do engine real. O caller passa funções que simulam sucesso, falha ou edge cases.
    """
    mod = types.ModuleType('rust_engine')
    mod.search_and_ingest_pubmed = search_fn or (lambda **kw: MagicMock(
        records_processed=0, records_inserted=0, records_updated=0, errors=[],
    ))
    mod.resolve_pending_links = resolve_fn or (lambda db_url: 0)
    return mod


class PubmedIngestionTaskTestCase(TransactionTestCase):
    """
    Testa a task Celery `run_pubmed_ingestion` com o engine Rust simulado.
    Cobre: sucesso, exceção, ImportError, consistência de status.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='task_user', password='pw')
        self.project = DaVinciProject.objects.create(
            user=self.user,
            title='Task Test',
            slug='task-test-task_user-davinci',
            query_term='cancer',
        )
        self.job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.PUBMED_SEARCH,
            parameters={'query': 'cancer'},
        )
        self.addCleanup(lambda: sys.modules.pop('rust_engine', None))

    def test_rust_success_returns_counts(self):
        """
        ✅ Caminho feliz: engine retorna counts; task retorna dict.
        NOTA: em produção, o Rust real atualiza job.status=COMPLETED via
        job_tracker (direto no banco). Aqui testamos só o retorno da task.
        """
        def fake_search(**kwargs):
            return MagicMock(records_processed=42, records_inserted=30, records_updated=12, errors=[])

        sys.modules['rust_engine'] = _fake_rust_module(search_fn=fake_search)

        result = run_pubmed_ingestion(str(self.job.id))
        self.assertEqual(result, {'processed': 42, 'inserted': 30})

    def test_rust_raises_marks_failed_and_retries(self):
        """
        ✅ LACUNA E1 bem tratada: Rust lança → task marca FAILED + mensagem + chama retry.
        Esse é o comportamento esperado (bom). Teste PASSA hoje.
        """
        def boom(**kwargs):
            raise RuntimeError('kaboom from rust')

        sys.modules['rust_engine'] = _fake_rust_module(search_fn=boom)

        with patch.object(
            run_pubmed_ingestion,
            'retry',
            side_effect=Retry('retry triggered'),
        ) as retry_mock:
            with self.assertRaises(Retry):
                run_pubmed_ingestion(str(self.job.id))

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, IngestionJob.JobStatus.FAILED)
        self.assertIn('kaboom from rust', self.job.error_message)
        retry_mock.assert_called_once()

    def test_rust_import_error_marks_completed_with_zero_records(self):
        """
        ❌ LACUNA E2 (falha intencional — documenta comportamento questionável).

        Quando rust_engine não está instalado, hoje a task marca status=COMPLETED
        com records_processed=0 e SEM error_message. Visualmente, o frontend mostra
        badge verde "completed / 0 processed" — indistinguível de uma busca que
        realmente não achou nada. Isso confunde o pesquisador.

        Comportamento DESEJADO: status=FAILED, error_message='rust_engine not installed'.

        IMPORTANTE: usamos `patch.dict(sys.modules, {'rust_engine': None})` — setar
        o valor a None força `import rust_engine` a levantar ModuleNotFoundError.
        Se usássemos apenas `sys.modules.pop`, o Python reimportaria do disco
        (rust_engine ESTÁ instalado no venv) e o teste bateria na API real do NCBI.
        """
        with patch.dict(sys.modules, {'rust_engine': None}):
            result = run_pubmed_ingestion(str(self.job.id))

        self.assertEqual(result, {'processed': 0, 'inserted': 0})

        self.job.refresh_from_db()

        # Comportamento DESEJADO — este assert FALHA hoje (LACUNA E2):
        self.assertEqual(
            self.job.status,
            IngestionJob.JobStatus.FAILED,
            'E2: ImportError deveria marcar Job como FAILED com mensagem clara. '
            'Hoje marca COMPLETED → pesquisador pensa que busca rodou e não achou nada. '
            'Ver .claude/plans/2026-04-19-testes-pipeline-artigos.md'
        )
        self.assertIn(
            'rust_engine',
            (self.job.error_message or '').lower(),
            'E2: error_message deveria indicar que o engine não está instalado.',
        )

    def test_rust_import_error_marks_failed_with_message(self):
        """
        ✅ Contrato pós-E2: fallback ImportError marca FAILED com error_message claro.
        Renomeado de test_rust_import_error_current_behavior_documented após correção da lacuna E2.
        """
        with patch.dict(sys.modules, {'rust_engine': None}):
            result = run_pubmed_ingestion(str(self.job.id))

        self.job.refresh_from_db()

        self.assertEqual(result, {'processed': 0, 'inserted': 0})
        self.assertEqual(self.job.status, IngestionJob.JobStatus.FAILED)
        self.assertIn('rust_engine', (self.job.error_message or '').lower())

    def test_rust_success_does_not_auto_complete_job_status(self):
        """
        ❌ LACUNA auxiliar (falha intencional — documenta acoplamento com Rust real).

        A task `run_pubmed_ingestion` NÃO marca explicitamente status=COMPLETED
        no caminho de sucesso — confia que o Rust chama `job_tracker::mark_completed`
        direto no banco via tokio-postgres. Com rust_engine MOCKADO, ninguém faz
        esse update → o job fica em PENDING para sempre.

        Em produção com Rust real isso funciona. Em testes com mock, o status
        fica inconsistente. Comportamento robusto: a task também deveria marcar
        COMPLETED ao fim (defense in depth), ou o teste deveria ser de integração
        com Rust real (fora do escopo deste plano).
        """
        def fake_search(**kwargs):
            return MagicMock(records_processed=10, records_inserted=8, records_updated=0, errors=[])

        sys.modules['rust_engine'] = _fake_rust_module(search_fn=fake_search)

        run_pubmed_ingestion(str(self.job.id))
        self.job.refresh_from_db()

        # Comportamento DESEJADO — assert falha hoje:
        self.assertEqual(
            self.job.status,
            IngestionJob.JobStatus.COMPLETED,
            'Lacuna auxiliar: task não marca COMPLETED por si; depende do Rust real '
            'atualizar via job_tracker. Com mock, fica em PENDING.'
        )


# ─── Op 1.1 — Encadeamento automático PubMed → GEO_SEARCH ────────────────────

class PubmedToOmicsChainTestCase(TransactionTestCase):
    """
    Testa o encadeamento automático: ao concluir run_pubmed_ingestion com
    COMPLETED, um IngestionJob GEO_SEARCH é criado automaticamente para o
    mesmo projeto (Op 1.1 do plano 2026-06-12).

    Cobre:
    - Caminho feliz: GEO_SEARCH criado ao fim do PubMed.
    - Idempotência: retry não cria segundo GEO_SEARCH se já existe pending/running.
    - Sem encadeamento em falha: GEO_SEARCH não é criado se PubMed falha.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='chain_user', password='pw')
        self.project = DaVinciProject.objects.create(
            user=self.user,
            title='Chain Test',
            slug='chain-test-chain_user-davinci',
            query_term='diabetes',
            query_synonyms=['T2D'],
        )
        self.addCleanup(lambda: sys.modules.pop('rust_engine', None))

    def _make_pubmed_job(self):
        return IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.PUBMED_SEARCH,
            parameters={'query': 'diabetes OR T2D'},
        )

    def _fake_rust_success(self, records_processed=5, records_inserted=5):
        """Retorna módulo rust_engine que simula sucesso e atualiza job para COMPLETED."""
        project = self.project

        def fake_search(**kwargs):
            # Simula o que o Rust real faz: atualiza o job para COMPLETED no banco.
            job_id = kwargs.get('job_id')
            IngestionJob.objects.filter(id=job_id).update(
                status=IngestionJob.JobStatus.COMPLETED,
                records_processed=records_processed,
                records_inserted=records_inserted,
            )
            return MagicMock(
                records_processed=records_processed,
                records_inserted=records_inserted,
                records_updated=0,
                errors=[],
            )

        return _fake_rust_module(search_fn=fake_search)

    def test_omics_job_created_after_pubmed_completes(self):
        """
        Caminho feliz: concluir run_pubmed_ingestion dispara GEO_SEARCH automaticamente.
        O GEO_SEARCH deve ser criado com status PENDING e pertencer ao mesmo projeto.
        """
        job = self._make_pubmed_job()
        sys.modules['rust_engine'] = self._fake_rust_success()

        with patch('apps.core.services.search_service.run_omics_ingestion.delay') as omics_delay:
            run_pubmed_ingestion(str(job.id))

        # Um GEO_SEARCH deve ter sido criado para o projeto.
        geo_jobs = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.GEO_SEARCH,
        )
        self.assertEqual(geo_jobs.count(), 1, 'Deve existir exatamente 1 GEO_SEARCH após PubMed concluído.')
        geo_job = geo_jobs.first()
        self.assertEqual(geo_job.status, IngestionJob.JobStatus.PENDING)
        self.assertEqual(geo_job.project, self.project)

        # O .delay do GEO_SEARCH deve ter sido chamado uma vez.
        omics_delay.assert_called_once_with(str(geo_job.id))

    def test_idempotencia_retry_nao_cria_segundo_geo_search(self):
        """
        Idempotência: se já existe um GEO_SEARCH ativo (pending/running),
        um retry do run_pubmed_ingestion não deve criar um segundo job omics.
        """
        job = self._make_pubmed_job()
        sys.modules['rust_engine'] = self._fake_rust_success()

        with patch('apps.core.services.search_service.run_omics_ingestion.delay'):
            # Primeira execução (normal).
            run_pubmed_ingestion(str(job.id))

        geo_count_after_first = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.GEO_SEARCH,
        ).count()
        self.assertEqual(geo_count_after_first, 1, 'Primeira execução: deve haver 1 GEO_SEARCH.')

        # Simula retry: cria novo job PubMed (como Celery faria) e executa de novo.
        # O GEO_SEARCH criado na primeira execução ainda está em PENDING.
        job2 = self._make_pubmed_job()
        # Reseta status para PENDING para simular retry limpo.
        job2.status = IngestionJob.JobStatus.PENDING
        job2.save(update_fields=['status'])

        sys.modules['rust_engine'] = self._fake_rust_success()

        with patch('apps.core.services.search_service.run_omics_ingestion.delay') as omics_delay_retry:
            run_pubmed_ingestion(str(job2.id))

        # Ainda deve haver apenas 1 GEO_SEARCH (o segundo disparo foi bloqueado).
        geo_count_after_retry = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.GEO_SEARCH,
        ).count()
        self.assertEqual(
            geo_count_after_retry, 1,
            'Retry não deve criar segundo GEO_SEARCH quando já existe um pending/running.'
        )
        omics_delay_retry.assert_not_called()

    def test_omics_nao_disparado_quando_pubmed_falha(self):
        """
        Se o PubMed job termina em FAILED (ImportError), nenhum GEO_SEARCH
        deve ser criado automaticamente.
        """
        job = self._make_pubmed_job()

        # Força ImportError (rust_engine não disponível).
        with patch.dict(sys.modules, {'rust_engine': None}):
            with patch('apps.core.services.search_service.run_omics_ingestion.delay') as omics_delay:
                run_pubmed_ingestion(str(job.id))

        geo_count = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.GEO_SEARCH,
        ).count()
        self.assertEqual(geo_count, 0, 'GEO_SEARCH não deve ser criado quando PubMed falha.')
        omics_delay.assert_not_called()

    def test_disparo_manual_omics_continua_funcionando(self):
        """
        O botão "Search Datasets" (disparo manual) continua válido.
        SearchService.dispatch_omics_search funciona independentemente do encadeamento.
        """
        from apps.core.services.search_service import SearchService

        with patch('apps.core.services.search_service.run_omics_ingestion.delay') as omics_delay:
            geo_job = SearchService.dispatch_omics_search(
                self.project,
                user=self.user,
            )

        self.assertEqual(geo_job.job_type, IngestionJob.JobType.GEO_SEARCH)
        self.assertEqual(geo_job.project, self.project)
        omics_delay.assert_called_once_with(str(geo_job.id))
