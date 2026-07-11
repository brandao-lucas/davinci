"""
test_cnv_seed.py — Cobertura do CnvSeedService e command derive_cnv_seeds.

OmnisPathway Objetivo 2, Fase 2, Slice CNV-seed (derivação).

Áreas cobertas:
  1. resolve_matrix:
     - Acha a OmicMatrix copy_number do projeto (via ProjectDataset ativo).
     - cross-project (dataset não vinculado ao projeto) → CnvSeedMatrixNotFoundError.
     - matrix_id de projeto alheio → CnvSeedMatrixNotFoundError.
     - Múltiplas matrizes copy_number sem --matrix especificado → CnvSeedMatrixNotFoundError.
     - Nenhuma matriz copy_number → CnvSeedMatrixNotFoundError.
     - ProjectDataset EXCLUDED → CnvSeedMatrixNotFoundError (regressão A1).

  2. Pré-checks:
     - GeneRole vazio → CnvSeedGeneRoleEmptyError (mensagem menciona load_gene_roles).
     - GeneRole populado → passa sem exceção.

  3. Gate de idempotência:
     - Job CNV_SEED_LOAD PENDING/RUNNING para mesma matriz → CnvSeedJobActiveError.
     - Job COMPLETED para mesma matriz → não bloqueia (UPSERT idempotente).
     - Job FAILED para mesma matriz → não bloqueia.

  4. run() síncrono com mock de default_storage (Parquet dummy) + mock
     seed_cnv_from_parquet → job COMPLETED, manifesto reportado.
     - tempfile limpo após run.
     - db_url NUNCA logada/retornada (sensitive-data-handling).
     - storage_key/caminho físico não vaza no retorno.
     - Resultado contém todas as chaves do contrato.

  5. dispatch():
     - Cria job PENDING com parâmetros corretos (matrix_id, method_version,
       gain_threshold, loss_threshold); sem db_url.
     - Idempotência: job ativo → CnvSeedJobActiveError.

  6. Management command derive_cnv_seeds:
     - --project obrigatório (UUID inválido → CommandError).
     - Modo síncrono com mock: relatório completo; sem vazar caminho/credencial.
     - --matrix específico funciona.
     - GeneRole vazio → CommandError com orientação.
     - Matriz não encontrada → CommandError.
     - Modo --async cria job PENDING.
     - Idempotência: job ativo → aviso.
     - --loss-threshold >= 0 → CommandError.
     - --gain-threshold <= 0 → CommandError.

  7. Isolamento na task run_cnv_seed_load:
     - job.project_id != project_id → job FAILED.
     - project_id inexistente → erro sem processar.
     - job_id inexistente → erro sem processar.

Padrões obrigatórios:
  - SEM internet: rust_engine.seed_cnv_from_parquet SEMPRE mockado.
  - default_storage mockado (FileSystemStorage em tmpdir para leitura do Parquet).
  - SEM pytest — usa django.test.TestCase (padrão do projeto).
  - GeneRole populado via ORM diretamente (não via Rust).
"""

from __future__ import annotations

import os
import tempfile
import uuid
from io import StringIO
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.files.storage import FileSystemStorage
from django.test import TestCase

from apps.core.models import (
    DaVinciProject,
    GeneRole,
    IngestionJob,
    OmicDataset,
    OmicMatrix,
    ProjectDataset,
)
from apps.core.services.cnv_seed_service import (
    METHOD_VERSION,
    CnvSeedGeneRoleEmptyError,
    CnvSeedJobActiveError,
    CnvSeedMatrixNotFoundError,
    CnvSeedService,
)


# =============================================================================
# Helpers de factory — reutiliza padrões dos outros testes do projeto
# =============================================================================

def _make_user(username: str = 'seed_user') -> User:
    return User.objects.create_user(username=username, password='pw')


def _make_project(user: User, title: str = 'CNV Seed Project') -> DaVinciProject:
    slug = f'{title.lower().replace(" ", "-")}-{user.username}'
    return DaVinciProject.objects.create(
        user=user, title=title, slug=slug, query_term='cnv-seed',
    )


def _make_cnv_dataset(accession: str = 'CPTAC-CCRCC-CNV-SEED') -> OmicDataset:
    return OmicDataset.objects.create(
        accession=accession,
        source_db='cptac',
        title='CNV Seed Test Dataset',
        access_type='public',
    )


def _link_dataset(
    project: DaVinciProject,
    dataset: OmicDataset,
    status: str = ProjectDataset.CurationStatus.INCLUDED,
) -> ProjectDataset:
    return ProjectDataset.objects.create(
        project=project,
        dataset=dataset,
        curation_status=status,
    )


def _make_cnv_matrix(
    dataset: OmicDataset,
    storage_key: str = 'omics/_shared/CPTAC-CCRCC-CNV-SEED/cnv.parquet',
) -> OmicMatrix:
    return OmicMatrix.objects.create(
        dataset=dataset,
        omics_layer='copy_number',
        data_format_level='log_ratio',
        feature_axis='gene',
        loader_version='test-cnv-v1',
        n_features=50,
        n_samples=5,
        storage_key=storage_key,
        checksum_md5='aabbcc112233445566778899aabbcc11',
    )


def _populate_gene_role(n: int = 5) -> None:
    """Popula GeneRole com n genes de papel 'oncogene' via ORM."""
    for i in range(n):
        GeneRole.objects.get_or_create(
            gene_symbol=f'GENE{i:03d}',
            source=GeneRole.Source.ONCOKB,
            defaults={'role': GeneRole.Role.ONCOGENE},
        )


def _make_fake_cnv_seed_manifest() -> MagicMock:
    """Retorna manifesto fake com interface de CnvSeedManifest do Rust."""
    m = MagicMock()
    m.n_seeds_written = 42
    m.n_activator = 25
    m.n_inactivator = 17
    m.n_neutral_skipped = 100
    m.n_genes_with_role = 45
    m.n_genes_skipped_no_role = 5
    m.n_cells_subthreshold = 80
    m.n_cells_null = 20
    return m


def _create_dummy_parquet_bytes() -> bytes:
    """Conteúdo dummy de Parquet (não inspecionado em Python)."""
    return b'PAR1' + b'\x00' * 64 + b'PAR1'


# =============================================================================
# Base com storage real (FileSystem) para testes que fazem download
# =============================================================================

class _SeedStorageTestCase(TestCase):
    """
    Substitui default_storage por FileSystemStorage em tmpdir.
    Cria um arquivo Parquet dummy no storage para que default_storage.open()
    funcione durante o _execute() do CnvSeedService.
    """

    def setUp(self):
        super().setUp()
        self._storage_tmpdir = tempfile.mkdtemp(prefix='davinci_cnvseed_test_')
        self._storage = FileSystemStorage(location=self._storage_tmpdir)
        self._storage_patcher = patch(
            'apps.core.services.cnv_seed_service.default_storage',
            new=self._storage,
        )
        self._storage_patcher.start()

    def _put_parquet_in_storage(self, storage_key: str) -> None:
        """
        Salva um Parquet dummy no storage local substituído usando a
        storage_key fornecida, para que default_storage.open() funcione.
        """
        full_path = os.path.join(self._storage_tmpdir, storage_key)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, 'wb') as f:
            f.write(_create_dummy_parquet_bytes())

    def tearDown(self):
        self._storage_patcher.stop()
        import shutil
        shutil.rmtree(self._storage_tmpdir, ignore_errors=True)
        super().tearDown()


# =============================================================================
# 1. resolve_matrix
# =============================================================================

class CnvSeedResolveMatrixTests(TestCase):
    """Testa CnvSeedService.resolve_matrix() — isolamento e auto-detecção."""

    def setUp(self):
        self.user = _make_user('resolve_seed_user')
        self.project = _make_project(self.user, 'Resolve Matrix Project')
        self.dataset = _make_cnv_dataset('CPTAC-RESOLVE-CNV')
        _link_dataset(self.project, self.dataset)
        self.matrix = _make_cnv_matrix(self.dataset)

    def test_resolve_by_id_success(self):
        """resolve_matrix com matrix_id do próprio projeto retorna a OmicMatrix."""
        matrix = CnvSeedService.resolve_matrix(self.project, matrix_id=self.matrix.id)
        self.assertEqual(matrix.id, self.matrix.id)

    def test_resolve_autodetect_success(self):
        """Auto-detecção (matrix_id=None) acha a única matriz copy_number do projeto."""
        matrix = CnvSeedService.resolve_matrix(self.project, matrix_id=None)
        self.assertEqual(matrix.id, self.matrix.id)

    def test_resolve_cross_project_raises(self):
        """
        matrix_id de outro projeto → CnvSeedMatrixNotFoundError.
        Projeto B não pode usar a matriz do Projeto A.
        """
        user_b = _make_user('resolve_seed_b')
        project_b = _make_project(user_b, 'Resolve Project B')

        with self.assertRaises(CnvSeedMatrixNotFoundError):
            CnvSeedService.resolve_matrix(project_b, matrix_id=self.matrix.id)

    def test_resolve_dataset_not_linked_raises(self):
        """
        Matriz de dataset não vinculado ao projeto → CnvSeedMatrixNotFoundError,
        mesmo que a matriz exista no banco.
        """
        project_c = _make_project(self.user, 'Resolve Project C')
        # project_c não tem ProjectDataset para self.dataset

        with self.assertRaises(CnvSeedMatrixNotFoundError):
            CnvSeedService.resolve_matrix(project_c, matrix_id=self.matrix.id)

    def test_resolve_no_cnv_matrix_raises(self):
        """Projeto sem OmicMatrix copy_number → CnvSeedMatrixNotFoundError."""
        project_empty = _make_project(self.user, 'Empty CNV Project')
        empty_ds = _make_cnv_dataset('EMPTY-CNV-DS')
        _link_dataset(project_empty, empty_ds)
        # Sem OmicMatrix copy_number vinculada

        with self.assertRaises(CnvSeedMatrixNotFoundError):
            CnvSeedService.resolve_matrix(project_empty, matrix_id=None)

    def test_resolve_multiple_matrices_without_id_raises(self):
        """
        Múltiplas matrizes copy_number no projeto sem matrix_id especificado
        → CnvSeedMatrixNotFoundError (erro sugere --matrix).
        """
        # Cria segundo dataset/matriz para o mesmo projeto (loader_version diferente
        # para não violar a unique constraint natural key do OmicMatrix)
        second_ds = _make_cnv_dataset('CPTAC-RESOLVE-CNV-SECOND')
        _link_dataset(self.project, second_ds)
        OmicMatrix.objects.create(
            dataset=second_ds,
            omics_layer='copy_number',
            data_format_level='log_ratio',
            feature_axis='gene',
            loader_version='test-cnv-v2',
            n_features=30,
            n_samples=2,
            storage_key='omics/_shared/CPTAC-RESOLVE-CNV-SECOND/cnv.parquet',
        )

        with self.assertRaises(CnvSeedMatrixNotFoundError) as ctx:
            CnvSeedService.resolve_matrix(self.project, matrix_id=None)

        # Mensagem deve mencionar múltiplas matrizes
        self.assertIn('ltiplas', str(ctx.exception),
                      'Erro deve mencionar "múltiplas"')

    def test_resolve_excluded_dataset_raises(self):
        """
        Regressão A1: ProjectDataset com curation_status=EXCLUDED → não resolvível.
        Datasets descartados não são elegíveis a derivação.
        """
        ProjectDataset.objects.filter(
            project=self.project, dataset=self.dataset,
        ).update(curation_status=ProjectDataset.CurationStatus.EXCLUDED)

        with self.assertRaises(CnvSeedMatrixNotFoundError):
            CnvSeedService.resolve_matrix(self.project, matrix_id=self.matrix.id)

    def test_resolve_included_dataset_not_blocked(self):
        """
        Regressão A1 (lado positivo): ProjectDataset INCLUDED é resolvível.
        """
        matrix = CnvSeedService.resolve_matrix(self.project, matrix_id=self.matrix.id)
        self.assertEqual(matrix.id, self.matrix.id)

    def test_resolve_error_message_contains_project_id(self):
        """CnvSeedMatrixNotFoundError menciona o project.id na mensagem."""
        project_c = _make_project(self.user, 'Error Msg Project')

        with self.assertRaises(CnvSeedMatrixNotFoundError) as ctx:
            CnvSeedService.resolve_matrix(project_c, matrix_id=None)

        self.assertIn(str(project_c.id), str(ctx.exception))


# =============================================================================
# 2. Pré-checks — GeneRole
# =============================================================================

class CnvSeedGeneRoleCheckTests(TestCase):
    """Testa _check_gene_role_populated()."""

    def setUp(self):
        self.user = _make_user('grcheck_user')
        self.project = _make_project(self.user, 'GeneRole Check Project')

    def test_empty_gene_role_raises_error(self):
        """GeneRole vazio → CnvSeedGeneRoleEmptyError."""
        # Garante que a tabela está vazia neste teste
        GeneRole.objects.all().delete()

        service = CnvSeedService(self.project)
        with self.assertRaises(CnvSeedGeneRoleEmptyError) as ctx:
            service._check_gene_role_populated()

        # Mensagem deve orientar o operador
        error_msg = str(ctx.exception).lower()
        self.assertTrue(
            any(kw in error_msg for kw in ['load_gene_roles', 'gene_role', 'vazio', 'empty']),
            f'Mensagem de erro deve mencionar load_gene_roles ou vazio: {error_msg!r}',
        )

    def test_populated_gene_role_does_not_raise(self):
        """GeneRole com ao menos 1 registro → sem exceção."""
        _populate_gene_role(n=3)

        service = CnvSeedService(self.project)
        try:
            service._check_gene_role_populated()
        except CnvSeedGeneRoleEmptyError as exc:
            self.fail(f'_check_gene_role_populated levantou inesperadamente: {exc}')


# =============================================================================
# 3. Gate de idempotência — _check_no_active_job
# =============================================================================

class CnvSeedJobGateTests(TestCase):
    """Testa _check_no_active_job()."""

    def setUp(self):
        self.user = _make_user('jobgate_user')
        self.project = _make_project(self.user, 'Job Gate Project')
        self.dataset = _make_cnv_dataset('CPTAC-JOBGATE-CNV')
        _link_dataset(self.project, self.dataset)
        self.matrix = _make_cnv_matrix(self.dataset)

    def _create_seed_job(self, status: str) -> IngestionJob:
        return IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.CNV_SEED_LOAD,
            status=status,
            parameters={'matrix_id': self.matrix.id},
        )

    def test_pending_job_blocks(self):
        """Job CNV_SEED_LOAD PENDING para a mesma matriz → CnvSeedJobActiveError."""
        self._create_seed_job(IngestionJob.JobStatus.PENDING)

        service = CnvSeedService(self.project)
        with self.assertRaises(CnvSeedJobActiveError) as ctx:
            service._check_no_active_job(self.matrix)

        self.assertIsNotNone(ctx.exception.job)

    def test_running_job_blocks(self):
        """Job CNV_SEED_LOAD RUNNING para a mesma matriz → CnvSeedJobActiveError."""
        self._create_seed_job(IngestionJob.JobStatus.RUNNING)

        service = CnvSeedService(self.project)
        with self.assertRaises(CnvSeedJobActiveError):
            service._check_no_active_job(self.matrix)

    def test_completed_job_does_not_block(self):
        """Job COMPLETED não bloqueia — UPSERT do Rust é idempotente."""
        self._create_seed_job(IngestionJob.JobStatus.COMPLETED)

        service = CnvSeedService(self.project)
        try:
            service._check_no_active_job(self.matrix)
        except CnvSeedJobActiveError as exc:
            self.fail(f'_check_no_active_job levantou para job COMPLETED: {exc}')

    def test_failed_job_does_not_block(self):
        """Job FAILED não bloqueia — re-seed válido após falha."""
        self._create_seed_job(IngestionJob.JobStatus.FAILED)

        service = CnvSeedService(self.project)
        try:
            service._check_no_active_job(self.matrix)
        except CnvSeedJobActiveError as exc:
            self.fail(f'_check_no_active_job levantou para job FAILED: {exc}')

    def test_job_of_different_matrix_does_not_block(self):
        """Job PENDING de outra matriz não bloqueia a matriz atual."""
        other_ds = _make_cnv_dataset('CPTAC-OTHER-CNV')
        _link_dataset(self.project, other_ds)
        other_matrix = _make_cnv_matrix(
            other_ds, storage_key='omics/_shared/CPTAC-OTHER-CNV/cnv.parquet'
        )

        IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.CNV_SEED_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={'matrix_id': other_matrix.id},  # outra matriz
        )

        service = CnvSeedService(self.project)
        try:
            service._check_no_active_job(self.matrix)  # verifica self.matrix
        except CnvSeedJobActiveError as exc:
            self.fail(
                f'Job de outra matriz não deve bloquear: {exc}'
            )


# =============================================================================
# 4. run() síncrono — fluxo completo mockado
# =============================================================================

class CnvSeedRunTests(_SeedStorageTestCase):
    """
    Testa CnvSeedService.run() de ponta a ponta:
    - default_storage mockado com FileSystemStorage real.
    - rust_engine.seed_cnv_from_parquet mockado.
    - GeneRole populado via ORM.
    """

    def setUp(self):
        super().setUp()
        self.user = _make_user('cnvseed_run_user')
        self.project = _make_project(self.user, 'CNV Seed Run Project')
        self.storage_key = 'omics/_shared/CPTAC-RUN-CNV/cnv_matrix.parquet'
        self.dataset = _make_cnv_dataset('CPTAC-RUN-CNV')
        _link_dataset(self.project, self.dataset)
        self.matrix = _make_cnv_matrix(self.dataset, storage_key=self.storage_key)
        _populate_gene_role(n=5)
        # Coloca o Parquet dummy no storage local
        self._put_parquet_in_storage(self.storage_key)

    def _run_with_mock(self, **kwargs) -> dict:
        """Executa service.run() com seed_cnv_from_parquet mockado."""
        fake_engine = MagicMock()
        fake_engine.seed_cnv_from_parquet.return_value = _make_fake_cnv_seed_manifest()
        service = CnvSeedService(self.project)

        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            result = service.run(**kwargs)

        return result

    def test_run_returns_all_manifest_keys(self):
        """run() retorna dict com todas as chaves do contrato do manifesto."""
        result = self._run_with_mock()

        expected_keys = {
            'job_id', 'matrix_id', 'method_version',
            'n_seeds_written', 'n_activator', 'n_inactivator',
            'n_neutral_skipped', 'n_genes_with_role', 'n_genes_skipped_no_role',
            'n_cells_subthreshold', 'n_cells_null',
        }
        self.assertEqual(set(result.keys()), expected_keys)

    def test_run_counters_match_manifest(self):
        """Contadores no resultado batem com o manifesto fake."""
        result = self._run_with_mock()

        self.assertEqual(result['n_seeds_written'], 42)
        self.assertEqual(result['n_activator'], 25)
        self.assertEqual(result['n_inactivator'], 17)
        self.assertEqual(result['n_neutral_skipped'], 100)
        self.assertEqual(result['n_genes_with_role'], 45)
        self.assertEqual(result['n_genes_skipped_no_role'], 5)
        self.assertEqual(result['n_cells_subthreshold'], 80)
        self.assertEqual(result['n_cells_null'], 20)

    def test_run_job_completed_after_success(self):
        """run() cria IngestionJob e o marca COMPLETED após sucesso do Rust."""
        result = self._run_with_mock()

        job = IngestionJob.objects.get(id=result['job_id'])
        self.assertEqual(job.status, IngestionJob.JobStatus.COMPLETED)
        self.assertEqual(job.job_type, IngestionJob.JobType.CNV_SEED_LOAD)
        self.assertIsNotNone(job.completed_at)

    def test_run_job_records_processed_set(self):
        """Job COMPLETED tem records_processed = n_seeds_written."""
        result = self._run_with_mock()

        job = IngestionJob.objects.get(id=result['job_id'])
        self.assertEqual(job.records_processed, 42)

    def test_run_method_version_correct(self):
        """Resultado tem method_version = METHOD_VERSION."""
        result = self._run_with_mock()
        self.assertEqual(result['method_version'], METHOD_VERSION)

    def test_run_matrix_id_in_result(self):
        """Resultado tem matrix_id correto."""
        result = self._run_with_mock()
        self.assertEqual(result['matrix_id'], self.matrix.id)

    def test_run_result_does_not_leak_db_url(self):
        """
        Resultado de run() NÃO contém db_url, postgresql:// nem password
        (sensitive-data-handling).
        """
        result = self._run_with_mock()

        result_str = str(result)
        self.assertNotIn('postgresql://', result_str)
        self.assertNotIn('db_url', result_str)
        self.assertNotIn('password', result_str)

    def test_run_result_does_not_contain_storage_key(self):
        """
        storage_key é dado interno — não deve vazar no retorno do run().
        """
        result = self._run_with_mock()
        self.assertNotIn('storage_key', result)

    def test_run_calls_rust_with_correct_args(self):
        """rust_engine.seed_cnv_from_parquet chamado com parâmetros corretos."""
        fake_engine = MagicMock()
        fake_engine.seed_cnv_from_parquet.return_value = _make_fake_cnv_seed_manifest()
        service = CnvSeedService(self.project)

        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            service.run()

        call_kwargs = fake_engine.seed_cnv_from_parquet.call_args[1]
        # Parâmetros obrigatórios do contrato Rust
        self.assertIn('parquet_path', call_kwargs)
        self.assertIn('matrix_id', call_kwargs)
        self.assertIn('db_url', call_kwargs)
        self.assertIn('method_version', call_kwargs)
        self.assertIn('gain_threshold', call_kwargs)
        self.assertIn('loss_threshold', call_kwargs)
        # Valores corretos
        self.assertEqual(call_kwargs['matrix_id'], self.matrix.id)
        self.assertEqual(call_kwargs['method_version'], METHOD_VERSION)

    def test_run_with_custom_thresholds(self):
        """run() repassa gain_threshold e loss_threshold customizados ao Rust."""
        fake_engine = MagicMock()
        fake_engine.seed_cnv_from_parquet.return_value = _make_fake_cnv_seed_manifest()
        service = CnvSeedService(self.project)

        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            service.run(gain_threshold=0.5, loss_threshold=-0.5)

        call_kwargs = fake_engine.seed_cnv_from_parquet.call_args[1]
        self.assertAlmostEqual(call_kwargs['gain_threshold'], 0.5)
        self.assertAlmostEqual(call_kwargs['loss_threshold'], -0.5)

    def test_run_on_exception_marks_job_failed(self):
        """Exceção no Rust → job FAILED, exceção propagada."""
        failing_engine = MagicMock()
        failing_engine.seed_cnv_from_parquet.side_effect = RuntimeError('Rust crash')
        service = CnvSeedService(self.project)

        with patch.dict('sys.modules', {'rust_engine': failing_engine}):
            with self.assertRaises(RuntimeError):
                service.run()

        failed_job = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.CNV_SEED_LOAD,
            status=IngestionJob.JobStatus.FAILED,
        ).first()
        self.assertIsNotNone(failed_job, 'Job deve ser FAILED após exceção no Rust')

    def test_run_gene_role_empty_raises_before_job_creation(self):
        """
        GeneRole vazio → CnvSeedGeneRoleEmptyError levantado ANTES de criar job.
        Nenhum job CNV_SEED_LOAD deve ser criado.
        """
        GeneRole.objects.all().delete()

        service = CnvSeedService(self.project)
        with self.assertRaises(CnvSeedGeneRoleEmptyError):
            with patch.dict('sys.modules', {'rust_engine': MagicMock()}):
                service.run()

        self.assertEqual(
            IngestionJob.objects.filter(
                project=self.project,
                job_type=IngestionJob.JobType.CNV_SEED_LOAD,
            ).count(),
            0,
            'Nenhum job deve ser criado quando GeneRole está vazio',
        )


# =============================================================================
# 5. run() com job pré-criado (fluxo task Celery: dispatch → task)
# =============================================================================

class CnvSeedRunWithPreCreatedJobTests(_SeedStorageTestCase):
    """Testa run(job=job) — fluxo task Celery com job pré-criado pelo dispatch."""

    def setUp(self):
        super().setUp()
        self.user = _make_user('cnvseed_task_user')
        self.project = _make_project(self.user, 'CNV Seed Task Project')
        self.storage_key = 'omics/_shared/CPTAC-TASK-CNV/cnv_matrix.parquet'
        self.dataset = _make_cnv_dataset('CPTAC-TASK-CNV')
        _link_dataset(self.project, self.dataset)
        self.matrix = _make_cnv_matrix(self.dataset, storage_key=self.storage_key)
        _populate_gene_role(n=3)
        self._put_parquet_in_storage(self.storage_key)

    def test_run_with_job_uses_job_thresholds(self):
        """
        run(job=job) extrai gain_threshold e loss_threshold do job.parameters
        em vez dos defaults (simula dispatch com thresholds customizados).
        """
        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.CNV_SEED_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={
                'matrix_id': self.matrix.id,
                'gain_threshold': 0.6,
                'loss_threshold': -0.6,
            },
        )

        fake_engine = MagicMock()
        fake_engine.seed_cnv_from_parquet.return_value = _make_fake_cnv_seed_manifest()
        service = CnvSeedService(self.project)

        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            service.run(job=job)

        call_kwargs = fake_engine.seed_cnv_from_parquet.call_args[1]
        self.assertAlmostEqual(call_kwargs['gain_threshold'], 0.6)
        self.assertAlmostEqual(call_kwargs['loss_threshold'], -0.6)

    def test_run_with_job_marks_it_completed(self):
        """run(job=job) marca o job pré-criado como COMPLETED."""
        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.CNV_SEED_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={
                'matrix_id': self.matrix.id,
                'gain_threshold': 0.3,
                'loss_threshold': -0.3,
            },
        )

        fake_engine = MagicMock()
        fake_engine.seed_cnv_from_parquet.return_value = _make_fake_cnv_seed_manifest()
        service = CnvSeedService(self.project)

        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            service.run(job=job)

        job.refresh_from_db()
        self.assertEqual(job.status, IngestionJob.JobStatus.COMPLETED)


# =============================================================================
# 6. dispatch()
# =============================================================================

class CnvSeedDispatchTests(TestCase):
    """Testa CnvSeedService.dispatch()."""

    def setUp(self):
        self.user = _make_user('cnvseed_disp_user')
        self.project = _make_project(self.user, 'CNV Seed Dispatch Project')
        self.dataset = _make_cnv_dataset('CPTAC-DISP-CNV')
        _link_dataset(self.project, self.dataset)
        self.matrix = _make_cnv_matrix(self.dataset)
        _populate_gene_role(n=3)

    @patch('apps.core.tasks.ingestion_tasks.run_cnv_seed_load.delay', return_value=None)
    def test_dispatch_creates_pending_job(self, mock_delay):
        """dispatch() cria job PENDING com job_type CNV_SEED_LOAD."""
        job = CnvSeedService.dispatch(self.project, matrix_id=self.matrix.id)

        self.assertEqual(job.status, IngestionJob.JobStatus.PENDING)
        self.assertEqual(job.job_type, IngestionJob.JobType.CNV_SEED_LOAD)

    @patch('apps.core.tasks.ingestion_tasks.run_cnv_seed_load.delay', return_value=None)
    def test_dispatch_parameters_contain_matrix_id_and_thresholds(self, mock_delay):
        """IngestionJob.parameters contém matrix_id, method_version, thresholds."""
        job = CnvSeedService.dispatch(
            self.project,
            matrix_id=self.matrix.id,
            gain_threshold=0.4,
            loss_threshold=-0.4,
        )

        params = job.parameters or {}
        self.assertEqual(params.get('matrix_id'), self.matrix.id)
        self.assertEqual(params.get('method_version'), METHOD_VERSION)
        self.assertAlmostEqual(params.get('gain_threshold', 0.0), 0.4)
        self.assertAlmostEqual(params.get('loss_threshold', 0.0), -0.4)

    @patch('apps.core.tasks.ingestion_tasks.run_cnv_seed_load.delay', return_value=None)
    def test_dispatch_parameters_no_db_url(self, mock_delay):
        """IngestionJob.parameters NÃO contém db_url (sensitive-data-handling)."""
        job = CnvSeedService.dispatch(self.project, matrix_id=self.matrix.id)

        params = job.parameters or {}
        self.assertNotIn('db_url', params)
        self.assertNotIn('postgresql://', str(params))

    @patch('apps.core.tasks.ingestion_tasks.run_cnv_seed_load.delay', return_value=None)
    def test_dispatch_calls_celery_delay(self, mock_delay):
        """dispatch() chama run_cnv_seed_load.delay com (job_id, project_id)."""
        job = CnvSeedService.dispatch(self.project, matrix_id=self.matrix.id)
        mock_delay.assert_called_once_with(str(job.id), str(self.project.id))

    @patch('apps.core.tasks.ingestion_tasks.run_cnv_seed_load.delay', return_value=None)
    def test_dispatch_gene_role_empty_raises(self, mock_delay):
        """GeneRole vazio → CnvSeedGeneRoleEmptyError no dispatch."""
        GeneRole.objects.all().delete()

        with self.assertRaises(CnvSeedGeneRoleEmptyError):
            CnvSeedService.dispatch(self.project, matrix_id=self.matrix.id)

        # Nenhum job criado
        self.assertEqual(
            IngestionJob.objects.filter(
                project=self.project,
                job_type=IngestionJob.JobType.CNV_SEED_LOAD,
            ).count(),
            0,
        )

    @patch('apps.core.tasks.ingestion_tasks.run_cnv_seed_load.delay', return_value=None)
    def test_dispatch_active_job_blocks(self, mock_delay):
        """Job CNV_SEED_LOAD PENDING para a mesma matriz → CnvSeedJobActiveError."""
        CnvSeedService.dispatch(self.project, matrix_id=self.matrix.id)

        with self.assertRaises(CnvSeedJobActiveError):
            CnvSeedService.dispatch(self.project, matrix_id=self.matrix.id)

        # Ainda apenas 1 job
        self.assertEqual(
            IngestionJob.objects.filter(
                project=self.project,
                job_type=IngestionJob.JobType.CNV_SEED_LOAD,
            ).count(),
            1,
        )

    @patch('apps.core.tasks.ingestion_tasks.run_cnv_seed_load.delay', return_value=None)
    def test_dispatch_matrix_not_found_raises(self, mock_delay):
        """Matrix ID inexistente → CnvSeedMatrixNotFoundError no dispatch."""
        with self.assertRaises(CnvSeedMatrixNotFoundError):
            CnvSeedService.dispatch(self.project, matrix_id=99999999)


# =============================================================================
# 7. Isolamento na task run_cnv_seed_load (Regra #3)
# =============================================================================

class CnvSeedTaskIsolationTests(TestCase):
    """Testa isolamento cross-project na task run_cnv_seed_load."""

    def setUp(self):
        self.user_a = _make_user('cnvseed_iso_a')
        self.user_b = _make_user('cnvseed_iso_b')
        self.project_a = _make_project(self.user_a, 'Seed Iso Project A')
        self.project_b = _make_project(self.user_b, 'Seed Iso Project B')

    def test_task_aborts_cross_project(self):
        """
        run_cnv_seed_load com project_id de B mas job criado para A →
        job marcado FAILED, CnvSeedService não instanciado.
        """
        from apps.core.tasks.ingestion_tasks import run_cnv_seed_load

        dataset_a = _make_cnv_dataset('CPTAC-ISO-SEED-A')
        _link_dataset(self.project_a, dataset_a)
        matrix_a = _make_cnv_matrix(dataset_a)

        job_a = IngestionJob.objects.create(
            project=self.project_a,
            job_type=IngestionJob.JobType.CNV_SEED_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={'matrix_id': matrix_a.id},
        )

        fake_rust_mod = MagicMock()
        with patch.dict('sys.modules', {'rust_engine': fake_rust_mod}), \
             patch('apps.core.services.cnv_seed_service.CnvSeedService') as MockSvc:
            result = run_cnv_seed_load.run(str(job_a.id), str(self.project_b.id))

        job_a.refresh_from_db()
        self.assertEqual(job_a.status, IngestionJob.JobStatus.FAILED)
        self.assertIn('isolamento', (job_a.error_message or '').lower())
        MockSvc.assert_not_called()

    def test_task_returns_error_for_nonexistent_project(self):
        """run_cnv_seed_load com project_id inexistente retorna erro."""
        from apps.core.tasks.ingestion_tasks import run_cnv_seed_load

        with patch.dict('sys.modules', {'rust_engine': MagicMock()}):
            result = run_cnv_seed_load.run(str(uuid.uuid4()), str(uuid.uuid4()))

        self.assertIn('project not found', result.get('errors', []))
        self.assertEqual(result.get('n_seeds_written'), 0)

    def test_task_returns_error_for_nonexistent_job(self):
        """run_cnv_seed_load com job_id inexistente retorna erro."""
        from apps.core.tasks.ingestion_tasks import run_cnv_seed_load

        with patch.dict('sys.modules', {'rust_engine': MagicMock()}):
            result = run_cnv_seed_load.run(str(uuid.uuid4()), str(self.project_a.id))

        self.assertIn('job not found', result.get('errors', []))


# =============================================================================
# 8. Management command derive_cnv_seeds
# =============================================================================

class DeriveCnvSeedsCommandTests(_SeedStorageTestCase):
    """Testa o management command derive_cnv_seeds."""

    def setUp(self):
        super().setUp()
        self.user = _make_user('cnvseed_cmd_user')
        self.project = _make_project(self.user, 'Seed Command Project')
        self.storage_key = 'omics/_shared/CPTAC-CMD-SEED/cnv_matrix.parquet'
        self.dataset = _make_cnv_dataset('CPTAC-CMD-SEED')
        _link_dataset(self.project, self.dataset)
        self.matrix = _make_cnv_matrix(self.dataset, storage_key=self.storage_key)
        _populate_gene_role(n=5)
        self._put_parquet_in_storage(self.storage_key)

    def _call_sync(
        self,
        project_id: str | None = None,
        matrix_id: int | None = None,
        gain_threshold: float = 0.3,
        loss_threshold: float = -0.3,
    ) -> tuple[str, str]:
        """Chama derive_cnv_seeds em modo síncrono com rust_engine mockado."""
        from django.core.management import call_command

        pid = project_id or str(self.project.id)
        stdout = StringIO()
        stderr = StringIO()

        fake_engine = MagicMock()
        fake_engine.seed_cnv_from_parquet.return_value = _make_fake_cnv_seed_manifest()

        kwargs: dict = {
            'project': pid,
            'gain_threshold': gain_threshold,
            'loss_threshold': loss_threshold,
            'stdout': stdout,
            'stderr': stderr,
        }
        if matrix_id is not None:
            kwargs['matrix_id'] = matrix_id

        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            call_command('derive_cnv_seeds', **kwargs)

        return stdout.getvalue(), stderr.getvalue()

    def test_command_sync_reports_all_seed_counters(self):
        """derive_cnv_seeds (síncrono) reporta n_seeds_written, n_activator, etc."""
        stdout_val, _ = self._call_sync()

        for field in ('n_seeds_written', 'n_activator', 'n_inactivator',
                      'n_neutral_skipped', 'n_genes_with_role', 'n_genes_skipped_no_role'):
            self.assertIn(field, stdout_val.lower(),
                          f'stdout deve conter "{field}": {stdout_val[:500]!r}')

    def test_command_sync_reports_method_version(self):
        """stdout contém method_version."""
        stdout_val, _ = self._call_sync()
        self.assertIn(METHOD_VERSION, stdout_val)

    def test_command_sync_does_not_leak_db_url(self):
        """stdout/stderr não vaza postgresql://, db_url, password."""
        stdout_val, stderr_val = self._call_sync()
        combined = stdout_val + stderr_val

        for token in ('postgresql://', 'db_url', 'password'):
            self.assertNotIn(token, combined, f'Token sensível "{token}" vazou na saída')

    def test_command_sync_does_not_leak_physical_path(self):
        """stdout/stderr não vaza caminho físico absoluto."""
        stdout_val, stderr_val = self._call_sync()
        combined = stdout_val + stderr_val

        for prefix in ('/tmp', '/var', '/home', '/Users', '/private'):
            self.assertNotIn(prefix, combined,
                             f'Caminho físico "{prefix}" vazou na saída')

    def test_command_sync_does_not_leak_storage_key(self):
        """
        storage_key (chave lógica interna) não deve vazar na saída do command.
        Ela é dado interno do serviço.
        """
        stdout_val, stderr_val = self._call_sync()
        combined = stdout_val + stderr_val

        self.assertNotIn(self.storage_key, combined,
                         f'storage_key não deve vazar na saída: {self.storage_key!r}')

    def test_command_sync_with_explicit_matrix_id(self):
        """--matrix <id> específico funciona corretamente."""
        stdout_val, _ = self._call_sync(matrix_id=self.matrix.id)
        self.assertIn('n_seeds_written', stdout_val.lower())

    def test_command_invalid_project_raises_command_error(self):
        """--project com UUID inexistente → CommandError."""
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command('derive_cnv_seeds', project=str(uuid.uuid4()),
                         stdout=StringIO())

    def test_command_gene_role_empty_raises_command_error(self):
        """GeneRole vazio → CommandError com orientação para load_gene_roles."""
        from django.core.management import call_command
        from django.core.management.base import CommandError

        GeneRole.objects.all().delete()

        with self.assertRaises(CommandError) as ctx:
            with patch.dict('sys.modules', {'rust_engine': MagicMock()}):
                call_command('derive_cnv_seeds', project=str(self.project.id),
                             stdout=StringIO())

        error_msg = str(ctx.exception).lower()
        self.assertTrue(
            any(kw in error_msg for kw in ['load_gene_roles', 'gene_role', 'vazio', 'empty']),
            f'CommandError deve mencionar load_gene_roles: {error_msg!r}',
        )

    def test_command_matrix_not_found_raises_command_error(self):
        """Projeto sem matriz CNV → CommandError com orientação para load_cnv_matrix."""
        from django.core.management import call_command
        from django.core.management.base import CommandError

        empty_project = _make_project(self.user, 'Empty Seed Project')

        with self.assertRaises(CommandError) as ctx:
            with patch.dict('sys.modules', {'rust_engine': MagicMock()}):
                call_command('derive_cnv_seeds', project=str(empty_project.id),
                             stdout=StringIO())

        error_msg = str(ctx.exception).lower()
        self.assertTrue(
            any(kw in error_msg for kw in ['load_cnv_matrix', 'não encontrada', 'not found']),
            f'CommandError deve mencionar load_cnv_matrix: {error_msg!r}',
        )

    def test_command_async_creates_pending_job(self):
        """derive_cnv_seeds --async cria job PENDING sem executar a derivação."""
        from django.core.management import call_command

        stdout = StringIO()
        with patch('apps.core.tasks.ingestion_tasks.run_cnv_seed_load.delay',
                   return_value=None):
            call_command('derive_cnv_seeds',
                         project=str(self.project.id),
                         matrix_id=self.matrix.id,
                         use_async=True,
                         stdout=stdout)

        job = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.CNV_SEED_LOAD,
            status=IngestionJob.JobStatus.PENDING,
        ).first()
        self.assertIsNotNone(job, 'Modo --async deve criar job PENDING')

    def test_command_active_job_reports_warning(self):
        """
        Job CNV_SEED_LOAD PENDING ativo → aviso de idempotência sem criar segundo job.
        """
        from django.core.management import call_command

        # Cria job PENDING manualmente
        IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.CNV_SEED_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={'matrix_id': self.matrix.id},
        )

        stdout = StringIO()
        with patch.dict('sys.modules', {'rust_engine': MagicMock()}):
            call_command('derive_cnv_seeds', project=str(self.project.id),
                         matrix_id=self.matrix.id, stdout=stdout)

        # Não deve ter criado segundo job
        count = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.CNV_SEED_LOAD,
        ).count()
        self.assertEqual(count, 1, 'Não deve duplicar job quando há PENDING ativo')

        output = stdout.getvalue().lower()
        self.assertTrue(
            any(kw in output for kw in ['idempot', 'ativo', 'active']),
            f'stdout deve mencionar idempotência: {output[:300]!r}',
        )

    def test_command_loss_threshold_positive_raises_command_error(self):
        """--loss-threshold >= 0 → CommandError (deve ser negativo)."""
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError) as ctx:
            call_command('derive_cnv_seeds', project=str(self.project.id),
                         loss_threshold=0.1, stdout=StringIO())

        self.assertIn('loss-threshold', str(ctx.exception).lower())

    def test_command_gain_threshold_zero_raises_command_error(self):
        """--gain-threshold <= 0 → CommandError (deve ser positivo)."""
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError) as ctx:
            call_command('derive_cnv_seeds', project=str(self.project.id),
                         gain_threshold=0.0, stdout=StringIO())

        self.assertIn('gain-threshold', str(ctx.exception).lower())

    def test_command_loss_threshold_zero_raises_command_error(self):
        """--loss-threshold = 0 → CommandError (deve ser estritamente negativo)."""
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command('derive_cnv_seeds', project=str(self.project.id),
                         loss_threshold=0.0, stdout=StringIO())

    def test_command_gain_threshold_negative_raises_command_error(self):
        """--gain-threshold negativo → CommandError."""
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command('derive_cnv_seeds', project=str(self.project.id),
                         gain_threshold=-0.3, stdout=StringIO())
