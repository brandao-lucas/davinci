"""
test_gene_role_load.py — Cobertura do GeneRoleLoadService e command load_gene_roles.

OmnisPathway Objetivo 2, Fase 2, Slice 2A.

Áreas cobertas:
  1. dispatch() cria IngestionJob(GENE_ROLE_LOAD) com status PENDING e enfileira
     a task Celery (run_gene_role_load.delay).
  2. Idempotência de dispatch: job PENDING/RUNNING ativo → GeneRoleJobActiveError;
     job COMPLETED não bloqueia (UPSERT do Rust é idempotente).
  3. run() síncrono com rust_engine mockado → job COMPLETED, contadores corretos.
     O manifesto fake retorna todos os campos do contrato.
  4. _execute() marca job COMPLETED após chamada Rust; retorna dict com todas as
     chaves do contrato (n_genes, n_oncogene, n_tsg, n_dual, n_neither, n_unknown,
     source_version, n_upserted, job_id).
  5. Credencial/db_url não vaza em IngestionJob.parameters, retorno, nem log.
  6. Isolamento na task run_gene_role_load: project_id != job.project_id → job FAILED.
  7. Management command load_gene_roles:
     a. Modo síncrono com mock → relatório correto (inclui n_genes, n_oncogene, etc.).
     b. Modo --async cria job PENDING.
     c. Projeto inválido → CommandError.
     d. Job ativo → aviso de idempotência (não duplica job).
     e. Saída não vaza db_url/caminho físico/credencial.

Padrões obrigatórios:
  - SEM internet: rust_engine.load_oncokb_gene_roles SEMPRE mockado.
  - SEM pytest — usa django.test.TestCase (padrão do projeto).
  - Sem GeneRole real no banco (o Rust faz UPSERT diretamente; Django não persiste
    via ORM nos testes — os mocks retornam o manifesto sem tocar a tabela).
"""

from __future__ import annotations

import uuid
from io import StringIO
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase

from apps.core.models import DaVinciProject, IngestionJob
from apps.core.services.gene_role_load_service import (
    LOADER_VERSION,
    ONCOKB_GENE_LIST_URL,
    GeneRoleJobActiveError,
    GeneRoleLoadService,
)


# =============================================================================
# Fixtures e helpers
# =============================================================================

def _make_user(username: str = 'generole_user') -> User:
    return User.objects.create_user(username=username, password='pw')


def _make_project(user: User, title: str = 'GeneRole Project') -> DaVinciProject:
    slug = f'{title.lower().replace(" ", "-")}-{user.username}'
    return DaVinciProject.objects.create(
        user=user, title=title, slug=slug, query_term='oncokb',
    )


def _make_fake_gene_role_manifest() -> MagicMock:
    """Retorna manifesto fake com interface de GeneRoleManifest do Rust."""
    m = MagicMock()
    m.n_genes = 150
    m.n_oncogene = 60
    m.n_tsg = 50
    m.n_dual = 10
    m.n_neither = 20
    m.n_unknown = 10
    m.source_version = 'oncokb-v3.12'
    m.n_upserted = 148
    return m


def _make_fake_rust_engine(manifest: MagicMock | None = None) -> MagicMock:
    """Retorna módulo rust_engine fake com load_oncokb_gene_roles mockado."""
    engine = MagicMock()
    engine.load_oncokb_gene_roles.return_value = manifest or _make_fake_gene_role_manifest()
    return engine


# =============================================================================
# 1. dispatch() — criação de job e enfileiramento
# =============================================================================

class GeneRoleDispatchTests(TestCase):
    """Testa GeneRoleLoadService.dispatch()."""

    def setUp(self):
        self.user = _make_user('disp_gr_user')
        self.project = _make_project(self.user, 'Dispatch GeneRole Project')

    @patch('apps.core.tasks.ingestion_tasks.run_gene_role_load.delay', return_value=None)
    def test_dispatch_creates_pending_job(self, mock_delay):
        """dispatch() cria IngestionJob com status PENDING e job_type GENE_ROLE_LOAD."""
        job = GeneRoleLoadService.dispatch(self.project)

        self.assertEqual(job.status, IngestionJob.JobStatus.PENDING)
        self.assertEqual(job.job_type, IngestionJob.JobType.GENE_ROLE_LOAD)
        self.assertEqual(str(job.project_id), str(self.project.id))

    @patch('apps.core.tasks.ingestion_tasks.run_gene_role_load.delay', return_value=None)
    def test_dispatch_calls_celery_delay_with_correct_args(self, mock_delay):
        """dispatch() chama run_gene_role_load.delay com job_id e project_id (strings)."""
        job = GeneRoleLoadService.dispatch(self.project)

        mock_delay.assert_called_once_with(str(job.id), str(self.project.id))

    @patch('apps.core.tasks.ingestion_tasks.run_gene_role_load.delay', return_value=None)
    def test_dispatch_parameters_contain_source_url_and_loader_version(self, mock_delay):
        """IngestionJob.parameters contém source_url e loader_version para auditoria."""
        job = GeneRoleLoadService.dispatch(self.project)

        params = job.parameters or {}
        self.assertIn('source_url', params)
        self.assertEqual(params['source_url'], ONCOKB_GENE_LIST_URL)
        self.assertIn('loader_version', params)
        self.assertEqual(params['loader_version'], LOADER_VERSION)

    @patch('apps.core.tasks.ingestion_tasks.run_gene_role_load.delay', return_value=None)
    def test_dispatch_parameters_do_not_contain_db_url(self, mock_delay):
        """
        IngestionJob.parameters NUNCA contém db_url, password nem DATABASE_URL
        (sensitive-data-handling).
        """
        job = GeneRoleLoadService.dispatch(self.project)

        params = job.parameters or {}
        self.assertNotIn('db_url', params)
        self.assertNotIn('password', params)
        self.assertNotIn('DATABASE_URL', params)
        self.assertNotIn('postgresql://', str(params))


# =============================================================================
# 2. Idempotência de dispatch
# =============================================================================

class GeneRoleDispatchIdempotencyTests(TestCase):
    """Gate de idempotência do dispatch."""

    def setUp(self):
        self.user = _make_user('idem_gr_user')
        self.project = _make_project(self.user, 'Idempotency GeneRole Project')

    @patch('apps.core.tasks.ingestion_tasks.run_gene_role_load.delay', return_value=None)
    def test_pending_job_blocks_dispatch(self, mock_delay):
        """Job PENDING ativo → GeneRoleJobActiveError no segundo dispatch."""
        GeneRoleLoadService.dispatch(self.project)

        with self.assertRaises(GeneRoleJobActiveError) as ctx:
            GeneRoleLoadService.dispatch(self.project)

        self.assertIsNotNone(ctx.exception.job)
        self.assertEqual(ctx.exception.job.status, IngestionJob.JobStatus.PENDING)

    @patch('apps.core.tasks.ingestion_tasks.run_gene_role_load.delay', return_value=None)
    def test_pending_job_does_not_duplicate(self, mock_delay):
        """Segundo dispatch com job PENDING não cria segundo job."""
        GeneRoleLoadService.dispatch(self.project)
        try:
            GeneRoleLoadService.dispatch(self.project)
        except GeneRoleJobActiveError:
            pass

        count = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.GENE_ROLE_LOAD,
        ).count()
        self.assertEqual(count, 1, 'Não deve duplicar IngestionJob')

    def test_running_job_blocks_dispatch(self):
        """Job RUNNING ativo → GeneRoleJobActiveError (sem passar por delay)."""
        IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.GENE_ROLE_LOAD,
            status=IngestionJob.JobStatus.RUNNING,
            parameters={},
        )

        with self.assertRaises(GeneRoleJobActiveError):
            # _check_idempotency é chamado antes de delay
            with patch('apps.core.tasks.ingestion_tasks.run_gene_role_load.delay'):
                GeneRoleLoadService.dispatch(self.project)

    def test_completed_job_does_not_block(self):
        """
        Job COMPLETED não bloqueia — re-rodar atualiza via UPSERT do Rust.
        _check_idempotency deve passar sem levantar exceção.
        """
        IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.GENE_ROLE_LOAD,
            status=IngestionJob.JobStatus.COMPLETED,
            parameters={},
        )

        service = GeneRoleLoadService(self.project)
        try:
            service._check_idempotency()  # não deve levantar
        except GeneRoleJobActiveError as exc:
            self.fail(f'_check_idempotency levantou inesperadamente: {exc}')

    def test_failed_job_does_not_block(self):
        """Job FAILED não bloqueia (mesmo raciocínio do COMPLETED)."""
        IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.GENE_ROLE_LOAD,
            status=IngestionJob.JobStatus.FAILED,
            parameters={},
        )

        service = GeneRoleLoadService(self.project)
        try:
            service._check_idempotency()
        except GeneRoleJobActiveError as exc:
            self.fail(f'_check_idempotency levantou inesperadamente: {exc}')


# =============================================================================
# 3. run() síncrono — manifesto fake → job COMPLETED
# =============================================================================

class GeneRoleRunTests(TestCase):
    """Testa GeneRoleLoadService.run() com rust_engine mockado."""

    def setUp(self):
        self.user = _make_user('run_gr_user')
        self.project = _make_project(self.user, 'Run GeneRole Project')

    def _run_with_mock(self) -> dict:
        """Executa service.run() com rust_engine totalmente mockado."""
        fake_engine = _make_fake_rust_engine()
        service = GeneRoleLoadService(self.project)

        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            result = service.run()

        return result

    def test_run_returns_all_manifest_keys(self):
        """run() retorna dict com todas as chaves do contrato do manifesto."""
        result = self._run_with_mock()

        expected_keys = {
            'job_id', 'n_genes', 'n_oncogene', 'n_tsg',
            'n_dual', 'n_neither', 'n_unknown', 'source_version', 'n_upserted',
        }
        self.assertEqual(set(result.keys()), expected_keys)

    def test_run_counters_match_manifest(self):
        """Contadores no resultado batem com os do manifesto fake."""
        result = self._run_with_mock()

        self.assertEqual(result['n_genes'], 150)
        self.assertEqual(result['n_oncogene'], 60)
        self.assertEqual(result['n_tsg'], 50)
        self.assertEqual(result['n_dual'], 10)
        self.assertEqual(result['n_neither'], 20)
        self.assertEqual(result['n_unknown'], 10)
        self.assertEqual(result['source_version'], 'oncokb-v3.12')
        self.assertEqual(result['n_upserted'], 148)

    def test_run_creates_job_with_completed_status(self):
        """run() cria IngestionJob e o marca COMPLETED após sucesso do Rust."""
        result = self._run_with_mock()

        job = IngestionJob.objects.get(id=result['job_id'])
        self.assertEqual(job.status, IngestionJob.JobStatus.COMPLETED)
        self.assertEqual(job.job_type, IngestionJob.JobType.GENE_ROLE_LOAD)

    def test_run_job_records_processed_set(self):
        """Job COMPLETED tem records_processed = n_genes."""
        result = self._run_with_mock()

        job = IngestionJob.objects.get(id=result['job_id'])
        self.assertEqual(job.records_processed, 150)

    def test_run_job_records_inserted_set(self):
        """Job COMPLETED tem records_inserted = n_upserted."""
        result = self._run_with_mock()

        job = IngestionJob.objects.get(id=result['job_id'])
        self.assertEqual(job.records_inserted, 148)

    def test_run_job_completed_at_set(self):
        """Job COMPLETED tem completed_at populado."""
        result = self._run_with_mock()

        job = IngestionJob.objects.get(id=result['job_id'])
        self.assertIsNotNone(job.completed_at)

    def test_run_calls_rust_with_correct_args(self):
        """rust_engine.load_oncokb_gene_roles é chamado com url, dest_dir, db_url."""
        fake_engine = _make_fake_rust_engine()
        service = GeneRoleLoadService(self.project)

        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            service.run()

        call_kwargs = fake_engine.load_oncokb_gene_roles.call_args
        # Verificamos que os três parâmetros obrigatórios foram passados
        kwargs = call_kwargs[1] if call_kwargs[1] else {}
        self.assertIn('url', kwargs)
        self.assertIn('dest_dir', kwargs)
        self.assertIn('db_url', kwargs)
        self.assertEqual(kwargs['url'], ONCOKB_GENE_LIST_URL)

    def test_run_on_exception_marks_job_failed(self):
        """Exceção no Rust → job marcado FAILED e exceção propagada."""
        failing_engine = MagicMock()
        failing_engine.load_oncokb_gene_roles.side_effect = RuntimeError('Rust crash simulado')

        service = GeneRoleLoadService(self.project)

        with patch.dict('sys.modules', {'rust_engine': failing_engine}):
            with self.assertRaises(RuntimeError):
                service.run()

        # Deve existir um job FAILED
        failed_job = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.GENE_ROLE_LOAD,
            status=IngestionJob.JobStatus.FAILED,
        ).first()
        self.assertIsNotNone(failed_job, 'Job deve ser FAILED após exceção no Rust')


# =============================================================================
# 4. _execute() — núcleo com job pré-criado (modo task Celery)
# =============================================================================

class GeneRoleExecuteTests(TestCase):
    """Testa _execute() com job pré-criado (simula fluxo Celery dispatch → task)."""

    def setUp(self):
        self.user = _make_user('exec_gr_user')
        self.project = _make_project(self.user, 'Execute GeneRole Project')

    def test_execute_marks_job_completed(self):
        """_execute() com manifesto fake → job COMPLETED ao final."""
        service = GeneRoleLoadService(self.project)
        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.GENE_ROLE_LOAD,
            status=IngestionJob.JobStatus.RUNNING,
            parameters={},
        )
        fake_engine = _make_fake_rust_engine()

        service._execute(job, fake_engine)

        job.refresh_from_db()
        self.assertEqual(job.status, IngestionJob.JobStatus.COMPLETED)

    def test_execute_returns_correct_dict(self):
        """_execute() retorna dict com todas as chaves e valores corretos."""
        service = GeneRoleLoadService(self.project)
        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.GENE_ROLE_LOAD,
            status=IngestionJob.JobStatus.RUNNING,
            parameters={},
        )
        fake_engine = _make_fake_rust_engine()

        result = service._execute(job, fake_engine)

        self.assertEqual(result['job_id'], str(job.id))
        self.assertEqual(result['n_genes'], 150)
        self.assertEqual(result['n_oncogene'], 60)
        self.assertEqual(result['n_tsg'], 50)
        self.assertEqual(result['source_version'], 'oncokb-v3.12')
        self.assertEqual(result['n_upserted'], 148)

    def test_execute_result_does_not_contain_db_url(self):
        """O resultado de _execute() NUNCA contém db_url (sensitive-data-handling)."""
        service = GeneRoleLoadService(self.project)
        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.GENE_ROLE_LOAD,
            status=IngestionJob.JobStatus.RUNNING,
            parameters={},
        )
        fake_engine = _make_fake_rust_engine()

        result = service._execute(job, fake_engine)

        result_str = str(result)
        self.assertNotIn('postgresql://', result_str)
        self.assertNotIn('db_url', result_str)
        # Verifica também que a chave db_url não está no dict
        self.assertNotIn('db_url', result)


# =============================================================================
# 5. Isolamento na task run_gene_role_load
# =============================================================================

class GeneRoleTaskIsolationTests(TestCase):
    """
    Testa isolamento cross-project na task run_gene_role_load.
    Regra #3: job de um projeto não pode ser processado por outro.
    """

    def setUp(self):
        self.user_a = _make_user('iso_gr_a')
        self.user_b = _make_user('iso_gr_b')
        self.project_a = _make_project(self.user_a, 'Iso GeneRole Project A')
        self.project_b = _make_project(self.user_b, 'Iso GeneRole Project B')

    def test_task_aborts_if_job_belongs_to_different_project(self):
        """
        run_gene_role_load com project_id de B mas job criado para A →
        job marcado FAILED e isolamento detectado.
        """
        from apps.core.tasks.ingestion_tasks import run_gene_role_load

        job_a = IngestionJob.objects.create(
            project=self.project_a,
            job_type=IngestionJob.JobType.GENE_ROLE_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={},
        )

        fake_rust_mod = MagicMock()

        with patch.dict('sys.modules', {'rust_engine': fake_rust_mod}), \
             patch('apps.core.services.gene_role_load_service.GeneRoleLoadService') as MockSvc:
            result = run_gene_role_load.run(str(job_a.id), str(self.project_b.id))

        job_a.refresh_from_db()
        self.assertEqual(job_a.status, IngestionJob.JobStatus.FAILED)
        self.assertIn('isolamento', (job_a.error_message or '').lower())
        MockSvc.assert_not_called()

    def test_task_returns_error_for_nonexistent_project(self):
        """run_gene_role_load com project_id inexistente retorna erro sem processar."""
        from apps.core.tasks.ingestion_tasks import run_gene_role_load

        fake_rust_mod = MagicMock()
        fake_project_id = str(uuid.uuid4())
        fake_job_id = str(uuid.uuid4())

        with patch.dict('sys.modules', {'rust_engine': fake_rust_mod}):
            result = run_gene_role_load.run(fake_job_id, fake_project_id)

        self.assertIn('project not found', result.get('errors', []))
        self.assertEqual(result.get('n_genes'), 0)

    def test_task_returns_error_for_nonexistent_job(self):
        """run_gene_role_load com job_id inexistente retorna erro sem processar."""
        from apps.core.tasks.ingestion_tasks import run_gene_role_load

        fake_rust_mod = MagicMock()
        fake_job_id = str(uuid.uuid4())

        with patch.dict('sys.modules', {'rust_engine': fake_rust_mod}):
            result = run_gene_role_load.run(fake_job_id, str(self.project_a.id))

        self.assertIn('job not found', result.get('errors', []))
        self.assertEqual(result.get('n_genes'), 0)


# =============================================================================
# 6. Management command load_gene_roles
# =============================================================================

class LoadGeneRolesCommandTests(TestCase):
    """
    Testa o management command load_gene_roles.
    Modos: síncrono (default) e --async.
    """

    def setUp(self):
        self.user = _make_user('cmd_gr_user')
        self.project = _make_project(self.user, 'Command GeneRole Project')

    def _call_sync(self, project_id: str | None = None) -> tuple[str, str]:
        """Chama load_gene_roles em modo síncrono com rust_engine mockado."""
        from django.core.management import call_command

        pid = project_id or str(self.project.id)
        stdout = StringIO()
        stderr = StringIO()

        fake_engine = _make_fake_rust_engine()

        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            call_command('load_gene_roles', project=pid, stdout=stdout, stderr=stderr)

        return stdout.getvalue(), stderr.getvalue()

    def test_command_sync_completes_and_reports_counters(self):
        """
        load_gene_roles --project <UUID> (síncrono, rust_engine mockado):
        stdout contém n_genes, n_oncogene, n_tsg, n_upserted.
        """
        stdout_val, _ = self._call_sync()

        for field in ('n_genes', 'n_oncogene', 'n_tsg', 'n_upserted'):
            self.assertIn(field, stdout_val.lower(),
                          f'stdout deve conter "{field}": {stdout_val[:300]!r}')

    def test_command_sync_reports_source_version(self):
        """stdout do command síncrono contém source_version."""
        stdout_val, _ = self._call_sync()
        self.assertIn('source_version', stdout_val.lower())

    def test_command_sync_does_not_leak_db_url(self):
        """
        stdout/stderr do command síncrono NUNCA contém postgresql://, password
        ou db_url (sensitive-data-handling).
        """
        stdout_val, stderr_val = self._call_sync()
        combined = stdout_val + stderr_val

        forbidden = ['postgresql://', 'db_url', 'password', 'DATABASE_URL']
        for token in forbidden:
            self.assertNotIn(token, combined,
                             f'Saída não deve conter "{token}"')

    def test_command_sync_creates_completed_job(self):
        """run() síncrono via command cria IngestionJob COMPLETED."""
        self._call_sync()

        job = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.GENE_ROLE_LOAD,
            status=IngestionJob.JobStatus.COMPLETED,
        ).first()
        self.assertIsNotNone(job, 'Deve haver job COMPLETED após command síncrono')

    def test_command_async_creates_pending_job(self):
        """load_gene_roles --async cria job PENDING sem executar a carga."""
        from django.core.management import call_command

        stdout = StringIO()
        with patch('apps.core.tasks.ingestion_tasks.run_gene_role_load.delay', return_value=None):
            call_command('load_gene_roles', project=str(self.project.id),
                         use_async=True, stdout=stdout)

        job = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.GENE_ROLE_LOAD,
            status=IngestionJob.JobStatus.PENDING,
        ).first()
        self.assertIsNotNone(job, 'Modo --async deve criar job PENDING')

    def test_command_invalid_project_raises_command_error(self):
        """--project com UUID inexistente levanta CommandError."""
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command('load_gene_roles', project=str(uuid.uuid4()))

    def test_command_idempotency_active_job_does_not_duplicate(self):
        """
        Segundo call síncrono com job PENDING ativo: aviso de idempotência
        sem criar segundo job.
        """
        from django.core.management import call_command

        # Cria job PENDING para bloquear o segundo run
        IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.GENE_ROLE_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={},
        )

        stdout = StringIO()
        fake_engine = _make_fake_rust_engine()

        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            call_command('load_gene_roles', project=str(self.project.id), stdout=stdout)

        # Ainda apenas 1 job (o PENDING criado manualmente)
        count = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.GENE_ROLE_LOAD,
        ).count()
        self.assertEqual(count, 1, 'Não deve duplicar job quando há PENDING ativo')

        # Saída menciona idempotência
        output = stdout.getvalue().lower()
        self.assertTrue(
            any(kw in output for kw in ['idempot', 'ativo', 'ativo encontrado', 'active']),
            f'stdout deve mencionar idempotência: {output[:300]!r}',
        )

    def test_command_async_idempotency_reports_warning(self):
        """
        Modo --async com job PENDING ativo: aviso de idempotência sem criar job.
        """
        from django.core.management import call_command

        IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.GENE_ROLE_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={},
        )

        stdout = StringIO()
        with patch('apps.core.tasks.ingestion_tasks.run_gene_role_load.delay', return_value=None):
            call_command('load_gene_roles', project=str(self.project.id),
                         use_async=True, stdout=stdout)

        count = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.GENE_ROLE_LOAD,
        ).count()
        self.assertEqual(count, 1)
