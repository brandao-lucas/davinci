"""
test_cnv_matrix_load.py — Cobertura do CnvMatrixLoadService e command load_cnv_matrix.

OmnisPathway Objetivo 2, Fase 2, Slice CNV — materialização da matriz.

Áreas cobertas:
  1. ORM correto:
     - OmicMatrix criada com omics_layer='copy_number', data_format_level='log_ratio',
       feature_axis='gene'; n_features e n_samples batem com manifesto; checksum_md5.
     - OmicDataset com source_db='cptac', access_type='public'.
     - OmicSample por (case_id, role='tumor') com accession='{case_id}_tumor'.
     - OmicMatrixSample com column_index correto; sample_role='tumor' (tumor-only CNV).
     - ProjectDataset criado vinculando projeto ao dataset.
  2. Idempotência:
     - OmicMatrix já existe → CnvMatrixAlreadyLoadedError.
     - Job PENDING/RUNNING ativo → CnvMatrixJobActiveError.
     - bulk_create com ignore_conflicts não duplica OmicMatrixSample.
  3. storage_key:
     - Namespace compartilhado (_shared) para dado público — não project-scoped.
     - Nunca contém caminho físico absoluto.
     - OmicMatrix.storage_key gravado no banco.
     - Upload chega ao default_storage.
  4. Isolamento (Regra #3):
     - run_cnv_matrix_load aborta quando job.project_id != project_id.
  5. Management command load_cnv_matrix:
     - Modo síncrono: relatório com storage_key, n_features, n_samples.
     - Modo --async: job PENDING.
     - Projeto inválido → CommandError.
     - Idempotência: OmicMatrix já existe → aviso sem duplicar.
     - Saída não vaza caminho físico nem credencial.
  6. task run_cnv_matrix_load:
     - Delegação correta ao CnvMatrixLoadService.run().
     - Isolamento cross-project → job FAILED.

Padrões obrigatórios:
  - SEM internet: rust_engine.load_cnv_matrix SEMPRE mockado.
  - Arquivo Parquet dummy criado em tmpdir real p/ upload funcionar.
  - SEM pytest — usa django.test.TestCase (padrão do projeto).
"""

from __future__ import annotations

import os
import tempfile
import uuid
from io import StringIO
from typing import Any
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.files.storage import FileSystemStorage
from django.test import TestCase

from apps.core.models import (
    DaVinciProject,
    IngestionJob,
    OmicDataset,
    OmicMatrix,
    OmicMatrixSample,
    OmicSample,
    ProjectDataset,
)
from apps.core.services.cnv_matrix_load_service import (
    CNV_CCRCC_ACCESSION,
    CNV_PARQUET_NAME,
    LOADER_VERSION,
    CnvMatrixAlreadyLoadedError,
    CnvMatrixJobActiveError,
    CnvMatrixLoadService,
)


# =============================================================================
# Constantes e helpers de fixture
# =============================================================================

_FAKE_MD5 = 'deadbeef1234567890abcdef12345678'

# Manifesto CNV: apenas amostras tumor (tumor-only)
_CNV_SAMPLE_COLUMNS_FAKE = [
    {'case_id': 'C3L-CNV01', 'sample_role': 'tumor', 'column_index': 1},
    {'case_id': 'C3L-CNV02', 'sample_role': 'tumor', 'column_index': 2},
    {'case_id': 'C3L-CNV03', 'sample_role': 'tumor', 'column_index': 3},
]


def _make_sample_column(case_id: str, role: str, col_idx: int) -> Any:
    """Retorna objeto com interface de SampleColumn do Rust (acesso por atributo)."""
    obj = MagicMock()
    obj.case_id = case_id
    obj.sample_role = role
    obj.column_index = col_idx
    return obj


def _make_fake_cnv_manifest(parquet_path: str) -> Any:
    """Retorna manifesto fake com interface de CnvMatrixManifest do Rust."""
    m = MagicMock()
    m.parquet_path = parquet_path
    m.checksum_md5 = _FAKE_MD5
    m.n_features = 200  # genes CNV
    m.n_samples = 3      # 3 amostras tumor
    m.sample_columns = [
        _make_sample_column(sc['case_id'], sc['sample_role'], sc['column_index'])
        for sc in _CNV_SAMPLE_COLUMNS_FAKE
    ]
    return m


def _make_user(username: str = 'cnv_user') -> User:
    return User.objects.create_user(username=username, password='pw')


def _make_project(user: User, title: str = 'CNV Project') -> DaVinciProject:
    slug = f'{title.lower().replace(" ", "-")}-{user.username}'
    return DaVinciProject.objects.create(
        user=user, title=title, slug=slug, query_term='cnv',
    )


def _create_dummy_parquet(tmpdir: str, name: str = CNV_PARQUET_NAME) -> str:
    """Cria arquivo Parquet dummy (não inspecionado pelo Django — só pelo Rust)."""
    path = os.path.join(tmpdir, name)
    with open(path, 'wb') as f:
        f.write(b'PAR1' + b'\x00' * 128 + b'PAR1')
    return path


# =============================================================================
# Base com storage substituído por FileSystemStorage em tmpdir
# =============================================================================

class _CnvTmpStorageTestCase(TestCase):
    """
    Base que substitui default_storage por FileSystemStorage em tmpdir.
    Padrão idêntico ao _TmpStorageTestCase de test_matrix_load.py.
    """

    def setUp(self):
        super().setUp()
        self._storage_tmpdir = tempfile.mkdtemp(prefix='davinci_cnv_test_')
        self._storage = FileSystemStorage(location=self._storage_tmpdir)
        self._storage_patcher = patch(
            'apps.core.services.cnv_matrix_load_service.default_storage',
            new=self._storage,
        )
        self._storage_patcher.start()

    def tearDown(self):
        self._storage_patcher.stop()
        import shutil
        shutil.rmtree(self._storage_tmpdir, ignore_errors=True)
        super().tearDown()


# =============================================================================
# Helper compartilhado: executa _execute() com manifesto fake
# =============================================================================

def _run_cnv_execute(service: CnvMatrixLoadService) -> dict:
    """
    Executa CnvMatrixLoadService._execute() com Parquet dummy real e
    rust_engine mockado. Retorna o dict de resultado.
    """
    dataset = service._get_or_create_dataset()
    job = IngestionJob.objects.create(
        project=service.project,
        job_type=IngestionJob.JobType.CNV_MATRIX_LOAD,
        status=IngestionJob.JobStatus.RUNNING,
        parameters={},
    )
    fake_rust = MagicMock()

    with tempfile.TemporaryDirectory(prefix='davinci_cnv_exec_') as dest_dir:
        parquet_path = _create_dummy_parquet(dest_dir)
        fake_rust.load_cnv_matrix.return_value = _make_fake_cnv_manifest(parquet_path)
        return service._execute(dataset, job, fake_rust)


# =============================================================================
# 1. ORM correto — campos de OmicMatrix, OmicSample e OmicMatrixSample
# =============================================================================

class CnvMatrixOrmCreationTests(_CnvTmpStorageTestCase):
    """Verifica que a carga cria os objetos ORM com os campos corretos."""

    def setUp(self):
        super().setUp()
        self.user = _make_user('cnv_orm_user')
        self.project = _make_project(self.user, 'CNV ORM Test')

    def test_omic_matrix_created_with_copy_number_layer(self):
        """OmicMatrix tem omics_layer='copy_number'."""
        svc = CnvMatrixLoadService(self.project)
        _run_cnv_execute(svc)

        matrix = OmicMatrix.objects.get(dataset__accession=CNV_CCRCC_ACCESSION)
        self.assertEqual(matrix.omics_layer, 'copy_number')

    def test_omic_matrix_data_format_level_log_ratio(self):
        """OmicMatrix tem data_format_level='log_ratio'."""
        svc = CnvMatrixLoadService(self.project)
        _run_cnv_execute(svc)

        matrix = OmicMatrix.objects.get(dataset__accession=CNV_CCRCC_ACCESSION)
        self.assertEqual(matrix.data_format_level, OmicMatrix.DataFormatLevel.LOG_RATIO)

    def test_omic_matrix_feature_axis_gene(self):
        """OmicMatrix tem feature_axis='gene'."""
        svc = CnvMatrixLoadService(self.project)
        _run_cnv_execute(svc)

        matrix = OmicMatrix.objects.get(dataset__accession=CNV_CCRCC_ACCESSION)
        self.assertEqual(matrix.feature_axis, OmicMatrix.FeatureAxis.GENE)

    def test_omic_matrix_n_features_and_n_samples(self):
        """OmicMatrix.n_features e n_samples batem com o manifesto fake."""
        svc = CnvMatrixLoadService(self.project)
        _run_cnv_execute(svc)

        matrix = OmicMatrix.objects.get(dataset__accession=CNV_CCRCC_ACCESSION)
        self.assertEqual(matrix.n_features, 200)
        self.assertEqual(matrix.n_samples, 3)

    def test_omic_matrix_checksum_md5(self):
        """OmicMatrix.checksum_md5 gravado conforme manifesto."""
        svc = CnvMatrixLoadService(self.project)
        _run_cnv_execute(svc)

        matrix = OmicMatrix.objects.get(dataset__accession=CNV_CCRCC_ACCESSION)
        self.assertEqual(matrix.checksum_md5, _FAKE_MD5)

    def test_omic_dataset_source_db_cptac(self):
        """OmicDataset do CNV tem source_db='cptac'."""
        svc = CnvMatrixLoadService(self.project)
        _run_cnv_execute(svc)

        dataset = OmicDataset.objects.get(accession=CNV_CCRCC_ACCESSION)
        self.assertEqual(dataset.source_db, OmicDataset.SourceDB.CPTAC)

    def test_omic_dataset_access_type_public(self):
        """OmicDataset do CNV tem access_type='public'."""
        svc = CnvMatrixLoadService(self.project)
        _run_cnv_execute(svc)

        dataset = OmicDataset.objects.get(accession=CNV_CCRCC_ACCESSION)
        self.assertEqual(dataset.access_type, OmicDataset.AccessType.PUBLIC)

    def test_omic_samples_created_tumor_only(self):
        """OmicSample criado para cada case_id com accession='{case_id}_tumor'."""
        svc = CnvMatrixLoadService(self.project)
        _run_cnv_execute(svc)

        expected = {'C3L-CNV01_tumor', 'C3L-CNV02_tumor', 'C3L-CNV03_tumor'}
        actual = set(
            OmicSample.objects.filter(
                accession__in=expected
            ).values_list('accession', flat=True)
        )
        self.assertEqual(actual, expected)

    def test_omic_matrix_samples_role_all_tumor(self):
        """Todos os OmicMatrixSample têm sample_role='tumor' (CNV tumor-only)."""
        svc = CnvMatrixLoadService(self.project)
        _run_cnv_execute(svc)

        matrix = OmicMatrix.objects.get(dataset__accession=CNV_CCRCC_ACCESSION)
        roles = set(
            OmicMatrixSample.objects.filter(matrix=matrix).values_list('sample_role', flat=True)
        )
        self.assertEqual(roles, {'tumor'}, 'CNV é tumor-only — sem normal/unknown')

    def test_omic_matrix_samples_column_index_correct(self):
        """OmicMatrixSample.column_index correto conforme manifesto."""
        svc = CnvMatrixLoadService(self.project)
        _run_cnv_execute(svc)

        matrix = OmicMatrix.objects.get(dataset__accession=CNV_CCRCC_ACCESSION)

        for expected in _CNV_SAMPLE_COLUMNS_FAKE:
            accession = f"{expected['case_id']}_tumor"
            oms = OmicMatrixSample.objects.get(
                matrix=matrix, sample__accession=accession
            )
            self.assertEqual(oms.column_index, expected['column_index'])
            self.assertEqual(oms.sample_role, OmicMatrixSample.SampleRole.TUMOR)

    def test_total_omic_matrix_samples_count(self):
        """Total de OmicMatrixSample = n_samples (3 tumores)."""
        svc = CnvMatrixLoadService(self.project)
        _run_cnv_execute(svc)

        matrix = OmicMatrix.objects.get(dataset__accession=CNV_CCRCC_ACCESSION)
        self.assertEqual(OmicMatrixSample.objects.filter(matrix=matrix).count(), 3)

    def test_project_dataset_link_created(self):
        """ProjectDataset entre o projeto e o dataset CNV é criado."""
        svc = CnvMatrixLoadService(self.project)
        _run_cnv_execute(svc)

        dataset = OmicDataset.objects.get(accession=CNV_CCRCC_ACCESSION)
        self.assertTrue(
            ProjectDataset.objects.filter(project=self.project, dataset=dataset).exists()
        )

    def test_result_dict_contains_expected_keys(self):
        """Resultado de _execute() contém as chaves do contrato."""
        svc = CnvMatrixLoadService(self.project)
        result = _run_cnv_execute(svc)

        for key in ('job_id', 'storage_key', 'n_features', 'n_samples', 'checksum_md5'):
            self.assertIn(key, result)

        self.assertEqual(result['n_features'], 200)
        self.assertEqual(result['n_samples'], 3)
        self.assertEqual(result['checksum_md5'], _FAKE_MD5)


# =============================================================================
# 2. Idempotência
# =============================================================================

class CnvMatrixIdempotencyTests(_CnvTmpStorageTestCase):
    """Gate de idempotência: matriz já existe / job ativo."""

    def setUp(self):
        super().setUp()
        self.user = _make_user('cnv_idem_user')
        self.project = _make_project(self.user, 'CNV Idempotency Test')

    def _run_once(self):
        svc = CnvMatrixLoadService(self.project)
        _run_cnv_execute(svc)

    def test_second_execute_raises_already_loaded(self):
        """Depois do primeiro _execute, _check_idempotency levanta CnvMatrixAlreadyLoadedError."""
        self._run_once()

        svc = CnvMatrixLoadService(self.project)
        dataset = svc._get_or_create_dataset()

        with self.assertRaises(CnvMatrixAlreadyLoadedError) as ctx:
            svc._check_idempotency(dataset)

        self.assertIsNotNone(ctx.exception.matrix)
        self.assertEqual(
            ctx.exception.matrix.dataset.accession, CNV_CCRCC_ACCESSION
        )

    def test_second_run_does_not_duplicate_omic_matrix(self):
        """Dois runs: OmicMatrix count = 1."""
        self._run_once()
        try:
            self._run_once()
        except CnvMatrixAlreadyLoadedError:
            pass

        count = OmicMatrix.objects.filter(dataset__accession=CNV_CCRCC_ACCESSION).count()
        self.assertEqual(count, 1)

    def test_second_execute_does_not_duplicate_omic_matrix_sample(self):
        """
        Dois _execute() com ignore_conflicts: OmicMatrixSample count = 3.
        """
        self._run_once()
        # Segundo _execute sem gate (o gate real bloqueia via _check_idempotency)
        svc = CnvMatrixLoadService(self.project)
        dataset = svc._get_or_create_dataset()
        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.CNV_MATRIX_LOAD,
            status=IngestionJob.JobStatus.RUNNING,
            parameters={},
        )
        fake_rust = MagicMock()
        with tempfile.TemporaryDirectory() as d:
            pp = _create_dummy_parquet(d)
            fake_rust.load_cnv_matrix.return_value = _make_fake_cnv_manifest(pp)
            svc._execute(dataset, job, fake_rust)

        matrix = OmicMatrix.objects.get(dataset__accession=CNV_CCRCC_ACCESSION)
        self.assertEqual(OmicMatrixSample.objects.filter(matrix=matrix).count(), 3)

    def test_pending_job_raises_job_active_error(self):
        """Job PENDING ativo para mesmo dataset+projeto → CnvMatrixJobActiveError."""
        svc = CnvMatrixLoadService(self.project)
        dataset = svc._get_or_create_dataset()

        IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.CNV_MATRIX_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={'dataset_id': dataset.id},
        )

        with self.assertRaises(CnvMatrixJobActiveError) as ctx:
            svc._check_idempotency(dataset)

        self.assertIsNotNone(ctx.exception.job)

    def test_running_job_raises_job_active_error(self):
        """Job RUNNING ativo → CnvMatrixJobActiveError."""
        svc = CnvMatrixLoadService(self.project)
        dataset = svc._get_or_create_dataset()

        IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.CNV_MATRIX_LOAD,
            status=IngestionJob.JobStatus.RUNNING,
            parameters={'dataset_id': dataset.id},
        )

        with self.assertRaises(CnvMatrixJobActiveError):
            svc._check_idempotency(dataset)

    def test_completed_job_does_not_block_check_idempotency(self):
        """Job COMPLETED não bloqueia (o Rust UPSERT é idempotente)."""
        svc = CnvMatrixLoadService(self.project)
        dataset = svc._get_or_create_dataset()

        IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.CNV_MATRIX_LOAD,
            status=IngestionJob.JobStatus.COMPLETED,
            parameters={'dataset_id': dataset.id},
        )

        # Sem OmicMatrix existente, apenas job COMPLETED → não deve bloquear
        try:
            svc._check_idempotency(dataset)
        except (CnvMatrixAlreadyLoadedError, CnvMatrixJobActiveError) as exc:
            self.fail(f'_check_idempotency levantou inesperadamente: {exc}')


# =============================================================================
# 3. storage_key — namespace compartilhado, sem caminho físico
# =============================================================================

class CnvMatrixStorageKeyTests(_CnvTmpStorageTestCase):
    """Verifica que storage_key usa namespace _shared e não vaza caminho físico."""

    def setUp(self):
        super().setUp()
        self.user = _make_user('cnv_sk_user')
        self.project = _make_project(self.user, 'CNV Storage Key Test')

    def test_storage_key_uses_shared_namespace(self):
        """
        CNV é dado público → storage_key contém 'omics/_shared/{accession}/'.
        NÃO deve conter user_id nem project_id (não é project-scoped).
        """
        svc = CnvMatrixLoadService(self.project)
        result = _run_cnv_execute(svc)

        storage_key = result['storage_key']
        expected_prefix = f'omics/_shared/{CNV_CCRCC_ACCESSION}/'
        self.assertIn(expected_prefix, storage_key,
                      f'Esperava prefixo _shared; obteve: {storage_key!r}')

    def test_storage_key_not_project_scoped(self):
        """storage_key não contém user_id nem project_id no namespace público."""
        svc = CnvMatrixLoadService(self.project)
        result = _run_cnv_execute(svc)

        storage_key = result['storage_key']
        self.assertNotIn(f'omics/{self.user.id}/', storage_key,
                         f'storage_key não deve conter user_id: {storage_key!r}')
        self.assertNotIn(f'/{self.project.id}/', storage_key,
                         f'storage_key não deve conter project_id: {storage_key!r}')

    def test_storage_key_no_physical_path(self):
        """storage_key não vaza caminho físico absoluto."""
        svc = CnvMatrixLoadService(self.project)
        result = _run_cnv_execute(svc)

        storage_key = result['storage_key']
        for prefix in ('/tmp', '/var', '/home', '/Users', '/private', '/root'):
            self.assertNotIn(prefix, storage_key,
                             f'Caminho físico "{prefix}" vazou: {storage_key!r}')

    def test_storage_key_persisted_in_omic_matrix(self):
        """OmicMatrix.storage_key no banco corresponde ao resultado."""
        svc = CnvMatrixLoadService(self.project)
        result = _run_cnv_execute(svc)

        matrix = OmicMatrix.objects.get(dataset__accession=CNV_CCRCC_ACCESSION)
        self.assertEqual(matrix.storage_key, result['storage_key'])
        self.assertNotEqual(matrix.storage_key, '')

    def test_parquet_uploaded_to_storage(self):
        """Após _execute, o arquivo Parquet existe no storage local substituído."""
        svc = CnvMatrixLoadService(self.project)
        result = _run_cnv_execute(svc)

        saved_path = os.path.join(self._storage_tmpdir, result['storage_key'])
        self.assertTrue(
            os.path.exists(saved_path),
            f'Arquivo deve existir no storage após upload: {saved_path}'
        )


# =============================================================================
# 4. dispatch() — criação de job PENDING
# =============================================================================

class CnvMatrixDispatchTests(TestCase):
    """Testa CnvMatrixLoadService.dispatch()."""

    def setUp(self):
        self.user = _make_user('cnv_disp_user')
        self.project = _make_project(self.user, 'CNV Dispatch Test')

    @patch('apps.core.tasks.ingestion_tasks.run_cnv_matrix_load.delay', return_value=None)
    def test_dispatch_creates_pending_job(self, mock_delay):
        """dispatch() cria job PENDING com job_type CNV_MATRIX_LOAD."""
        job = CnvMatrixLoadService.dispatch(self.project)

        self.assertEqual(job.status, IngestionJob.JobStatus.PENDING)
        self.assertEqual(job.job_type, IngestionJob.JobType.CNV_MATRIX_LOAD)

    @patch('apps.core.tasks.ingestion_tasks.run_cnv_matrix_load.delay', return_value=None)
    def test_dispatch_parameters_contain_accession_and_layer(self, mock_delay):
        """IngestionJob.parameters contém dataset_accession, omics_layer, loader_version."""
        job = CnvMatrixLoadService.dispatch(self.project)

        params = job.parameters or {}
        self.assertEqual(params.get('dataset_accession'), CNV_CCRCC_ACCESSION)
        self.assertEqual(params.get('omics_layer'), 'copy_number')
        self.assertIn('loader_version', params)

    @patch('apps.core.tasks.ingestion_tasks.run_cnv_matrix_load.delay', return_value=None)
    def test_dispatch_parameters_no_db_url(self, mock_delay):
        """IngestionJob.parameters NÃO contém db_url (sensitive-data-handling)."""
        job = CnvMatrixLoadService.dispatch(self.project)

        params = job.parameters or {}
        self.assertNotIn('db_url', params)
        self.assertNotIn('postgresql://', str(params))

    @patch('apps.core.tasks.ingestion_tasks.run_cnv_matrix_load.delay', return_value=None)
    def test_dispatch_calls_celery_delay(self, mock_delay):
        """dispatch() chama run_cnv_matrix_load.delay com (job_id, project_id)."""
        job = CnvMatrixLoadService.dispatch(self.project)
        mock_delay.assert_called_once_with(str(job.id), str(self.project.id))

    @patch('apps.core.tasks.ingestion_tasks.run_cnv_matrix_load.delay', return_value=None)
    def test_dispatch_idempotency_matrix_already_loaded(self, mock_delay):
        """OmicMatrix já existe → CnvMatrixAlreadyLoadedError no dispatch."""
        svc = CnvMatrixLoadService(self.project)
        dataset = svc._get_or_create_dataset()
        OmicMatrix.objects.create(
            dataset=dataset,
            omics_layer='copy_number',
            feature_axis='gene',
            loader_version=LOADER_VERSION,
            data_format_level=OmicMatrix.DataFormatLevel.LOG_RATIO,
            storage_key=f'omics/_shared/{CNV_CCRCC_ACCESSION}/test.parquet',
            n_features=200,
            n_samples=3,
            checksum_md5=_FAKE_MD5,
        )

        with self.assertRaises(CnvMatrixAlreadyLoadedError):
            CnvMatrixLoadService.dispatch(self.project)

    @patch('apps.core.tasks.ingestion_tasks.run_cnv_matrix_load.delay', return_value=None)
    def test_dispatch_idempotency_active_job(self, mock_delay):
        """Job PENDING ativo → CnvMatrixJobActiveError no segundo dispatch."""
        CnvMatrixLoadService.dispatch(self.project)

        with self.assertRaises(CnvMatrixJobActiveError):
            CnvMatrixLoadService.dispatch(self.project)


# =============================================================================
# 5. Isolamento na task run_cnv_matrix_load (Regra #3)
# =============================================================================

class CnvMatrixTaskIsolationTests(TestCase):
    """Isolamento cross-project na task run_cnv_matrix_load."""

    def setUp(self):
        self.user_a = _make_user('cnv_iso_a')
        self.user_b = _make_user('cnv_iso_b')
        self.project_a = _make_project(self.user_a, 'CNV Iso Project A')
        self.project_b = _make_project(self.user_b, 'CNV Iso Project B')

    def test_task_aborts_cross_project(self):
        """
        run_cnv_matrix_load com project_id de B mas job criado para A →
        job marcado FAILED e mensagem de isolamento.
        """
        from apps.core.tasks.ingestion_tasks import run_cnv_matrix_load

        svc_a = CnvMatrixLoadService(self.project_a)
        dataset_a = svc_a._get_or_create_dataset()
        job_a = IngestionJob.objects.create(
            project=self.project_a,
            job_type=IngestionJob.JobType.CNV_MATRIX_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={'dataset_id': dataset_a.id},
        )

        fake_rust_mod = MagicMock()
        with patch.dict('sys.modules', {'rust_engine': fake_rust_mod}), \
             patch('apps.core.services.cnv_matrix_load_service.CnvMatrixLoadService') as MockSvc:
            run_cnv_matrix_load.run(str(job_a.id), str(self.project_b.id))

        job_a.refresh_from_db()
        self.assertEqual(job_a.status, IngestionJob.JobStatus.FAILED)
        self.assertIn('isolamento', (job_a.error_message or '').lower())
        MockSvc.assert_not_called()

    def test_task_returns_error_for_nonexistent_project(self):
        """run_cnv_matrix_load com project_id inexistente retorna erro."""
        from apps.core.tasks.ingestion_tasks import run_cnv_matrix_load

        with patch.dict('sys.modules', {'rust_engine': MagicMock()}):
            result = run_cnv_matrix_load.run(str(uuid.uuid4()), str(uuid.uuid4()))

        self.assertIn('project not found', result.get('errors', []))
        self.assertEqual(result.get('n_features'), 0)

    def test_task_returns_error_for_nonexistent_job(self):
        """run_cnv_matrix_load com job_id inexistente retorna erro."""
        from apps.core.tasks.ingestion_tasks import run_cnv_matrix_load

        with patch.dict('sys.modules', {'rust_engine': MagicMock()}):
            result = run_cnv_matrix_load.run(str(uuid.uuid4()), str(self.project_a.id))

        self.assertIn('job not found', result.get('errors', []))


# =============================================================================
# 6. Management command load_cnv_matrix
# =============================================================================

class LoadCnvMatrixCommandTests(_CnvTmpStorageTestCase):
    """Testa o management command load_cnv_matrix."""

    def setUp(self):
        super().setUp()
        self.user = _make_user('cnv_cmd_user')
        self.project = _make_project(self.user, 'CNV Command Test')

    def _call_sync(self) -> tuple[str, str]:
        """Chama load_cnv_matrix em modo síncrono com rust_engine mockado."""
        from django.core.management import call_command

        stdout = StringIO()
        stderr = StringIO()

        with tempfile.TemporaryDirectory() as dest_dir:
            parquet_path = _create_dummy_parquet(dest_dir)
            fake_rust = MagicMock()

            with patch('apps.core.services.cnv_matrix_load_service.default_storage',
                       new=self._storage), \
                 patch.dict('sys.modules', {'rust_engine': fake_rust}) as _fake_sys:
                # Precisa que load_cnv_matrix retorne o manifesto com o parquet_path real
                fake_rust.load_cnv_matrix.return_value = _make_fake_cnv_manifest(parquet_path)
                call_command('load_cnv_matrix', project=str(self.project.id),
                             stdout=stdout, stderr=stderr)

        return stdout.getvalue(), stderr.getvalue()

    def test_command_sync_reports_n_features_n_samples_storage_key(self):
        """load_cnv_matrix (síncrono) reporta n_features, n_samples, storage_key."""
        stdout_val, _ = self._call_sync()

        for field in ('n_features', 'n_samples', 'storage_key'):
            self.assertIn(field, stdout_val.lower(),
                          f'stdout deve conter "{field}": {stdout_val[:400]!r}')

    def test_command_sync_does_not_leak_physical_path(self):
        """Stdout não vaza caminho físico absoluto."""
        stdout_val, stderr_val = self._call_sync()
        combined = stdout_val + stderr_val

        for prefix in ('/tmp', '/var', '/home', '/Users', '/private'):
            self.assertNotIn(prefix, combined,
                             f'Caminho físico "{prefix}" vazou na saída')

    def test_command_sync_does_not_leak_db_url(self):
        """Stdout não vaza postgresql://, db_url, password."""
        stdout_val, stderr_val = self._call_sync()
        combined = stdout_val + stderr_val

        for token in ('postgresql://', 'db_url', 'password'):
            self.assertNotIn(token, combined, f'Token sensível "{token}" vazou na saída')

    def test_command_sync_creates_omic_matrix(self):
        """load_cnv_matrix cria OmicMatrix(copy_number) no banco."""
        self._call_sync()

        self.assertTrue(
            OmicMatrix.objects.filter(
                dataset__accession=CNV_CCRCC_ACCESSION,
                omics_layer='copy_number',
            ).exists()
        )

    def test_command_idempotency_matrix_already_exists(self):
        """Segundo call síncrono com OmicMatrix já carregada: aviso sem duplicar."""
        from django.core.management import call_command

        # Primeiro run
        self._call_sync()

        # Segundo run — deve reportar idempotência
        stdout2 = StringIO()
        with patch.dict('sys.modules', {'rust_engine': MagicMock()}):
            call_command('load_cnv_matrix', project=str(self.project.id), stdout=stdout2)

        # OmicMatrix não deve ter duplicado
        count = OmicMatrix.objects.filter(dataset__accession=CNV_CCRCC_ACCESSION).count()
        self.assertEqual(count, 1)

        # Saída menciona idempotência
        output = stdout2.getvalue().lower()
        self.assertTrue(
            any(kw in output for kw in ['idempot', 'já existe', 'already', 'existente']),
            f'stdout deve mencionar idempotência: {output[:300]!r}',
        )

    def test_command_async_creates_pending_job(self):
        """load_cnv_matrix --async cria job PENDING sem executar a carga."""
        from django.core.management import call_command

        stdout = StringIO()
        with patch('apps.core.tasks.ingestion_tasks.run_cnv_matrix_load.delay',
                   return_value=None):
            call_command('load_cnv_matrix', project=str(self.project.id),
                         use_async=True, stdout=stdout)

        job = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.CNV_MATRIX_LOAD,
            status=IngestionJob.JobStatus.PENDING,
        ).first()
        self.assertIsNotNone(job)

    def test_command_invalid_project_raises_command_error(self):
        """--project com UUID inexistente → CommandError."""
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command('load_cnv_matrix', project=str(uuid.uuid4()))


# =============================================================================
# 7. _get_or_create_dataset — idempotência
# =============================================================================

class CnvGetOrCreateDatasetTests(TestCase):
    """Verifica _get_or_create_dataset para o dataset CNV."""

    def setUp(self):
        self.user = _make_user('cnv_ds_user')
        self.project = _make_project(self.user, 'CNV Dataset Create Test')

    def test_creates_dataset_on_first_call(self):
        """Primeira chamada cria OmicDataset com accession=CNV_CCRCC_ACCESSION."""
        svc = CnvMatrixLoadService(self.project)
        dataset = svc._get_or_create_dataset()

        self.assertEqual(dataset.accession, CNV_CCRCC_ACCESSION)
        self.assertEqual(dataset.source_db, OmicDataset.SourceDB.CPTAC)
        self.assertEqual(dataset.access_type, OmicDataset.AccessType.PUBLIC)

    def test_idempotent_on_second_call(self):
        """Duas chamadas não duplicam OmicDataset."""
        svc = CnvMatrixLoadService(self.project)
        svc._get_or_create_dataset()
        svc._get_or_create_dataset()

        self.assertEqual(
            OmicDataset.objects.filter(accession=CNV_CCRCC_ACCESSION).count(), 1
        )

    def test_creates_project_dataset_link(self):
        """ProjectDataset criado ao chamar _get_or_create_dataset."""
        svc = CnvMatrixLoadService(self.project)
        dataset = svc._get_or_create_dataset()

        self.assertTrue(
            ProjectDataset.objects.filter(project=self.project, dataset=dataset).exists()
        )

    def test_project_dataset_link_idempotent(self):
        """Duas chamadas não duplicam ProjectDataset."""
        svc = CnvMatrixLoadService(self.project)
        svc._get_or_create_dataset()
        svc._get_or_create_dataset()

        dataset = OmicDataset.objects.get(accession=CNV_CCRCC_ACCESSION)
        count = ProjectDataset.objects.filter(
            project=self.project, dataset=dataset
        ).count()
        self.assertEqual(count, 1)
