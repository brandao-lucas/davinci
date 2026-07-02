"""
test_matrix_load.py — Cobertura do backend de carga de matriz CPTAC.

OmnisPathway Objetivo 2, Fase 0, passo 0.4.

Áreas cobertas:
  1. Criação ORM correta:
       n_features/n_samples batem com o manifesto; source_db='cptac';
       omics_layer='proteomic'; data_format_level='intensities';
       feature_axis='gene'; checksum_md5 preenchido; storage_key lógica;
       OmicSample com accession sufixado (case_id_role); OmicMatrixSample
       com column_index e sample_role corretos; unique constraints respeitadas.
  2. Idempotência:
       Segundo run com service.run() → MatrixAlreadyLoadedError (OmicMatrix já existe).
       Job ativo (PENDING/RUNNING) → MatrixJobActiveError no dispatch.
       Registros não duplicados após segundo run.
  3. Isolamento por projeto/usuário (Regra #3):
       run_matrix_load aborta quando job.project_id != project_id recebido.
       Isolamento cross-user: job de um projeto não processa outro.
  4. storage_key lógica (não caminho físico):
       Formato "omics/{user_id}/{project_id}/{accession}/{filename}".
       Nenhum componente de caminho absoluto (/tmp, /var, /home etc.) vaza.
       default_storage recebeu o upload (mock de InMemoryStorage).
  5. Sufixo/role OmicSample:
       OmicSample.accession = "{case_id}_{role}" (tumor antes, normal depois).
       Mapeamento coluna→role correto nos OmicMatrixSample.
       Tumor: column_index menor que normal (1-based, conforme contrato Rust).
  6. management command load_cptac_matrix:
       --project resolve projeto; chama service.run(); reporta sem vazar
       caminho físico nem credencial; --async dispara dispatch (job PENDING).
       Idempotência: segundo call reporta aviso sem duplicar.
  7. task run_matrix_load:
       Delegação correta ao MatrixLoadService.run(); job COMPLETED ao final;
       violação de isolamento (job não pertence ao projeto) → job FAILED.

Padrões obrigatórios:
  - SEM internet: rust_engine.load_cptac_matrix totalmente mockado.
  - Arquivo Parquet falso = bytes dummy em TemporaryFile (upload via storage funciona).
  - Sem pytest — usa django.test.TestCase / APITestCase (padrão do projeto).
  - default_storage substituído por FileSystemStorage em tmpdir para cada teste
    de upload real; mockado em outros.
"""

from __future__ import annotations

import os
import tempfile
import uuid
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
from apps.core.services.matrix_load_service import (
    CPTAC_CCRCC_ACCESSION,
    CPTAC_PARQUET_NAME,
    LOADER_VERSION,
    MatrixAlreadyLoadedError,
    MatrixJobActiveError,
    MatrixLoadService,
)


# =============================================================================
# Constantes de fixture
# =============================================================================

# Manifesto mínimo: 3 features, 2 tumor + 1 normal — suficiente para cobrir
# todos os caminhos ORM sem download real.
_FAKE_MD5 = 'abcdef1234567890abcdef1234567890'

_SAMPLE_COLUMNS_FAKE = [
    # tumor: column_index 1 e 2 (primeiro após 'feature')
    {'case_id': 'C3L-TEST01', 'sample_role': 'tumor', 'column_index': 1},
    {'case_id': 'C3L-TEST02', 'sample_role': 'tumor', 'column_index': 2},
    # normal: column_index 3
    {'case_id': 'C3N-TEST01', 'sample_role': 'normal', 'column_index': 3},
]


def _make_sample_column(case_id: str, role: str, col_idx: int) -> Any:
    """Retorna objeto com interface de SampleColumn do Rust (acesso por atributo)."""
    obj = MagicMock()
    obj.case_id = case_id
    obj.sample_role = role
    obj.column_index = col_idx
    return obj


def _make_fake_manifest(parquet_path: str) -> Any:
    """Retorna manifesto fake com interface de CptacMatrixManifest do Rust."""
    manifest = MagicMock()
    manifest.parquet_path = parquet_path
    manifest.checksum_md5 = _FAKE_MD5
    manifest.n_features = 3
    manifest.n_samples = 3  # 2 tumor + 1 normal
    manifest.genes_discarded = 0
    manifest.sample_columns = [
        _make_sample_column(sc['case_id'], sc['sample_role'], sc['column_index'])
        for sc in _SAMPLE_COLUMNS_FAKE
    ]
    return manifest


# =============================================================================
# Helpers de factory (espelha padrão dos outros testes do projeto)
# =============================================================================

def make_user(username: str = 'matrix_user', password: str = 'pw') -> User:
    return User.objects.create_user(username=username, password=password)


def make_project(user: User, title: str = 'Matrix Project') -> DaVinciProject:
    slug = f'{title.lower().replace(" ", "-")}-{user.username}-mx'
    return DaVinciProject.objects.create(
        user=user, title=title, slug=slug, query_term='cptac'
    )


# =============================================================================
# Contexto: storage em FileSystemStorage local para testes de upload real
# =============================================================================

class _TmpStorageTestCase(TestCase):
    """
    Base que substitui default_storage por FileSystemStorage em tmpdir.

    Garante que uploads via default_storage funcionam sem S3 nem MinIO real
    e que os arquivos são limpos ao final de cada teste.
    """

    def setUp(self):
        super().setUp()
        self._storage_tmpdir = tempfile.mkdtemp(prefix='davinci_test_storage_')
        self._storage = FileSystemStorage(location=self._storage_tmpdir)
        self._storage_patcher = patch(
            'apps.core.services.matrix_load_service.default_storage',
            new=self._storage,
        )
        self._storage_patcher.start()

    def tearDown(self):
        self._storage_patcher.stop()
        import shutil
        shutil.rmtree(self._storage_tmpdir, ignore_errors=True)
        super().tearDown()


def _create_dummy_parquet(tmpdir: str) -> str:
    """Cria arquivo Parquet dummy (bytes que não são inspecionados pelo teste)."""
    parquet_path = os.path.join(tmpdir, CPTAC_PARQUET_NAME)
    with open(parquet_path, 'wb') as f:
        # Cabeçalho mágico do Parquet + payload mínimo para upload real funcionar
        f.write(b'PAR1' + b'\x00' * 256 + b'PAR1')
    return parquet_path


# =============================================================================
# 1. Criação ORM correta
# =============================================================================

class MatrixLoadOrmCreationTests(_TmpStorageTestCase):
    """
    Verifica que a carga cria OmicMatrix, OmicSample e OmicMatrixSample com
    os campos corretos, batendo com o manifesto fake.
    """

    def setUp(self):
        super().setUp()
        self.user = make_user('orm_user')
        self.project = make_project(self.user, 'ORM Test Project')

    def _run_service(self) -> tuple[MatrixLoadService, dict]:
        service = MatrixLoadService(self.project)
        with tempfile.TemporaryDirectory(prefix='davinci_matrix_') as dest_dir:
            parquet_path = _create_dummy_parquet(dest_dir)
            fake_manifest = _make_fake_manifest(parquet_path)
            fake_rust = MagicMock()
            fake_rust.load_cptac_matrix.return_value = fake_manifest
            result = service._execute(
                dataset=service._get_or_create_dataset(),
                job=IngestionJob.objects.create(
                    project=self.project,
                    job_type=IngestionJob.JobType.MATRIX_LOAD,
                    status=IngestionJob.JobStatus.RUNNING,
                    parameters={},
                ),
                rust_engine=fake_rust,
            )
        return service, result

    def test_omic_matrix_created_with_correct_fields(self):
        """OmicMatrix criada com source_db='cptac', layer='proteomic', axis='gene'."""
        _svc, _result = self._run_service()

        matrix = OmicMatrix.objects.get(
            dataset__accession=CPTAC_CCRCC_ACCESSION,
            omics_layer='proteomic',
            feature_axis='gene',
            loader_version=LOADER_VERSION,
        )
        self.assertEqual(matrix.n_features, 3)
        self.assertEqual(matrix.n_samples, 3)
        self.assertEqual(matrix.checksum_md5, _FAKE_MD5)
        self.assertEqual(matrix.data_format_level, OmicMatrix.DataFormatLevel.INTENSITIES)

    def test_omic_dataset_has_cptac_source_db(self):
        """OmicDataset do CPTAC tem source_db='cptac'."""
        _svc, _result = self._run_service()

        dataset = OmicDataset.objects.get(accession=CPTAC_CCRCC_ACCESSION)
        self.assertEqual(dataset.source_db, OmicDataset.SourceDB.CPTAC)

    def test_omic_samples_created_with_role_suffix(self):
        """OmicSample criado para cada (case_id, role) com accession='{case_id}_{role}'."""
        _svc, _result = self._run_service()

        expected_accessions = {
            'C3L-TEST01_tumor',
            'C3L-TEST02_tumor',
            'C3N-TEST01_normal',
        }
        actual_accessions = set(
            OmicSample.objects.filter(
                accession__in=expected_accessions
            ).values_list('accession', flat=True)
        )
        self.assertEqual(actual_accessions, expected_accessions)

    def test_omic_matrix_samples_column_index_and_role(self):
        """OmicMatrixSample tem column_index e sample_role corretos conforme manifesto."""
        _svc, _result = self._run_service()

        matrix = OmicMatrix.objects.get(
            dataset__accession=CPTAC_CCRCC_ACCESSION,
            omics_layer='proteomic',
        )

        # Tumor C3L-TEST01 deve estar na coluna 1
        oms_t1 = OmicMatrixSample.objects.get(
            matrix=matrix,
            sample__accession='C3L-TEST01_tumor',
        )
        self.assertEqual(oms_t1.column_index, 1)
        self.assertEqual(oms_t1.sample_role, OmicMatrixSample.SampleRole.TUMOR)

        # Tumor C3L-TEST02 deve estar na coluna 2
        oms_t2 = OmicMatrixSample.objects.get(
            matrix=matrix,
            sample__accession='C3L-TEST02_tumor',
        )
        self.assertEqual(oms_t2.column_index, 2)
        self.assertEqual(oms_t2.sample_role, OmicMatrixSample.SampleRole.TUMOR)

        # Normal C3N-TEST01 deve estar na coluna 3
        oms_n1 = OmicMatrixSample.objects.get(
            matrix=matrix,
            sample__accession='C3N-TEST01_normal',
        )
        self.assertEqual(oms_n1.column_index, 3)
        self.assertEqual(oms_n1.sample_role, OmicMatrixSample.SampleRole.NORMAL)

    def test_tumor_columns_before_normal_in_manifesto(self):
        """
        Colunas tumor precedem as colunas normal: todos os column_index de tumor
        são menores que todos os de normal (conforme contrato Rust §manifesto).
        """
        _svc, _result = self._run_service()

        matrix = OmicMatrix.objects.get(dataset__accession=CPTAC_CCRCC_ACCESSION)
        tumor_indices = list(
            OmicMatrixSample.objects.filter(
                matrix=matrix, sample_role=OmicMatrixSample.SampleRole.TUMOR
            ).values_list('column_index', flat=True)
        )
        normal_indices = list(
            OmicMatrixSample.objects.filter(
                matrix=matrix, sample_role=OmicMatrixSample.SampleRole.NORMAL
            ).values_list('column_index', flat=True)
        )

        self.assertTrue(len(tumor_indices) > 0, 'Deve haver amostras tumor')
        self.assertTrue(len(normal_indices) > 0, 'Deve haver amostras normal')
        self.assertLess(
            max(tumor_indices),
            min(normal_indices),
            'Todas as colunas tumor devem preceder as colunas normal (contrato Rust)',
        )

    def test_total_omic_matrix_samples_count(self):
        """Total de OmicMatrixSample = n_samples (2 tumor + 1 normal = 3)."""
        _svc, _result = self._run_service()

        matrix = OmicMatrix.objects.get(dataset__accession=CPTAC_CCRCC_ACCESSION)
        count = OmicMatrixSample.objects.filter(matrix=matrix).count()
        self.assertEqual(count, 3)

    def test_result_dict_contains_expected_keys(self):
        """Dicionário de resultado tem job_id, storage_key, n_features, n_samples."""
        _svc, result = self._run_service()

        for key in ('job_id', 'storage_key', 'n_features', 'n_samples',
                    'checksum_md5', 'genes_discarded', 'roles'):
            self.assertIn(key, result, f'Chave "{key}" deve estar no resultado')

        self.assertEqual(result['n_features'], 3)
        self.assertEqual(result['n_samples'], 3)
        self.assertEqual(result['checksum_md5'], _FAKE_MD5)

    def test_project_dataset_link_created(self):
        """ProjectDataset entre o projeto e o OmicDataset CPTAC é criado."""
        _svc, _result = self._run_service()

        dataset = OmicDataset.objects.get(accession=CPTAC_CCRCC_ACCESSION)
        self.assertTrue(
            ProjectDataset.objects.filter(
                project=self.project,
                dataset=dataset,
            ).exists()
        )

    def test_unique_constraint_matrix_sample(self):
        """
        UniqueConstraint (matrix, sample) e (matrix, column_index) são respeitadas:
        depois de um run, OmicMatrixSample.objects.count() é exatamente n_samples,
        não o dobro — bulk_create com ignore_conflicts não duplica.
        """
        svc = MatrixLoadService(self.project)

        def _run_once():
            with tempfile.TemporaryDirectory() as d:
                pp = _create_dummy_parquet(d)
                mfest = _make_fake_manifest(pp)
                rust_mock = MagicMock()
                rust_mock.load_cptac_matrix.return_value = mfest
                dataset = svc._get_or_create_dataset()
                job = IngestionJob.objects.create(
                    project=self.project,
                    job_type=IngestionJob.JobType.MATRIX_LOAD,
                    status=IngestionJob.JobStatus.RUNNING,
                    parameters={},
                )
                svc._execute(dataset=dataset, job=job, rust_engine=rust_mock)

        _run_once()

        # Deleta o OmicMatrix para permitir segundo _execute (simula re-carga
        # sem o gate de idempotência, mas com ignore_conflicts no bulk_create)
        OmicMatrix.objects.filter(
            dataset__accession=CPTAC_CCRCC_ACCESSION
        ).delete()

        _run_once()

        matrix = OmicMatrix.objects.get(dataset__accession=CPTAC_CCRCC_ACCESSION)
        count = OmicMatrixSample.objects.filter(matrix=matrix).count()
        self.assertEqual(count, 3, 'Não deve duplicar OmicMatrixSample')


# =============================================================================
# 2. storage_key lógica (não vaza caminho físico)
# =============================================================================

class MatrixLoadStorageKeyTests(_TmpStorageTestCase):
    """
    Verifica que storage_key gravada no OmicMatrix é a chave lógica do
    default_storage, não o caminho físico absoluto.
    """

    def setUp(self):
        super().setUp()
        self.user = make_user('sk_user')
        self.project = make_project(self.user, 'Storage Key Project')

    def test_storage_key_is_logical_not_physical_path(self):
        """
        storage_key deve seguir o padrão 'omics/{user_id}/{project_id}/{accession}/{file}'
        e NÃO conter /tmp, /var, /home nem caminho absoluto.
        """
        svc = MatrixLoadService(self.project)
        with tempfile.TemporaryDirectory() as dest_dir:
            parquet_path = _create_dummy_parquet(dest_dir)
            fake_manifest = _make_fake_manifest(parquet_path)
            fake_rust = MagicMock()
            fake_rust.load_cptac_matrix.return_value = fake_manifest
            dataset = svc._get_or_create_dataset()
            job = IngestionJob.objects.create(
                project=self.project,
                job_type=IngestionJob.JobType.MATRIX_LOAD,
                status=IngestionJob.JobStatus.RUNNING,
                parameters={},
            )
            result = svc._execute(dataset=dataset, job=job, rust_engine=fake_rust)

        storage_key = result['storage_key']

        # Deve seguir o padrão lógico
        self.assertTrue(
            storage_key.startswith('omics/'),
            f'storage_key deve começar com "omics/", obteve: {storage_key!r}',
        )

        # Não deve vazar caminhos físicos absolutos
        physical_prefixes = ('/tmp', '/var', '/home', '/Users', '/private', '/root')
        for prefix in physical_prefixes:
            self.assertNotIn(
                prefix,
                storage_key,
                f'storage_key não deve conter caminho físico "{prefix}": {storage_key!r}',
            )

    def test_storage_key_uses_shared_public_namespace(self):
        """
        storage_key usa namespace compartilhado para dataset PÚBLICO (finding M4).

        O padrão correto é 'omics/_shared/{accession}/{filename}' — sem user_id
        nem project_id. Isso garante que múltiplos projetos que carregam o mesmo
        dataset público (ex.: CPTAC-CCRCC) não duplicam o Parquet no storage.

        Verificações:
          - Contém o prefixo '_shared' (não project-scoped).
          - Contém o accession do dataset.
          - NÃO contém user_id nem project_id como segmentos de caminho.
        """
        svc = MatrixLoadService(self.project)
        with tempfile.TemporaryDirectory() as dest_dir:
            parquet_path = _create_dummy_parquet(dest_dir)
            fake_manifest = _make_fake_manifest(parquet_path)
            fake_rust = MagicMock()
            fake_rust.load_cptac_matrix.return_value = fake_manifest
            dataset = svc._get_or_create_dataset()
            job = IngestionJob.objects.create(
                project=self.project,
                job_type=IngestionJob.JobType.MATRIX_LOAD,
                status=IngestionJob.JobStatus.RUNNING,
                parameters={},
            )
            result = svc._execute(dataset=dataset, job=job, rust_engine=fake_rust)

        storage_key = result['storage_key']

        # Deve usar namespace compartilhado público (M4)
        expected_shared_prefix = f'omics/_shared/{CPTAC_CCRCC_ACCESSION}/'
        self.assertIn(
            expected_shared_prefix,
            storage_key,
            f'storage_key deve usar namespace _shared para dataset público; '
            f'esperava conter "{expected_shared_prefix}", obteve: {storage_key!r}',
        )

        # NÃO deve embutir user_id nem project_id (regressão M4)
        user_id_str = str(self.user.id)
        project_id_str = str(self.project.id)
        self.assertNotIn(
            f'omics/{user_id_str}/',
            storage_key,
            f'storage_key NÃO deve conter user_id no namespace público '
            f'(regressão M4): {storage_key!r}',
        )
        self.assertNotIn(
            f'/{project_id_str}/',
            storage_key,
            f'storage_key NÃO deve conter project_id no namespace público '
            f'(regressão M4): {storage_key!r}',
        )

    def test_upload_reaches_default_storage(self):
        """
        Após a carga, o arquivo Parquet existe no storage local substituído.
        Confirma que default_storage.save() foi chamado (upload real no tmpdir).
        """
        svc = MatrixLoadService(self.project)
        with tempfile.TemporaryDirectory() as dest_dir:
            parquet_path = _create_dummy_parquet(dest_dir)
            fake_manifest = _make_fake_manifest(parquet_path)
            fake_rust = MagicMock()
            fake_rust.load_cptac_matrix.return_value = fake_manifest
            dataset = svc._get_or_create_dataset()
            job = IngestionJob.objects.create(
                project=self.project,
                job_type=IngestionJob.JobType.MATRIX_LOAD,
                status=IngestionJob.JobStatus.RUNNING,
                parameters={},
            )
            result = svc._execute(dataset=dataset, job=job, rust_engine=fake_rust)

        storage_key = result['storage_key']
        # FileSystemStorage salva na location, verificamos que o arquivo existe
        saved_path = os.path.join(self._storage_tmpdir, storage_key)
        self.assertTrue(
            os.path.exists(saved_path),
            f'Arquivo deve existir no storage local após upload: {saved_path}',
        )

    def test_omic_matrix_storage_key_persisted_in_db(self):
        """OmicMatrix.storage_key no banco corresponde ao retorno do resultado."""
        svc = MatrixLoadService(self.project)
        with tempfile.TemporaryDirectory() as dest_dir:
            parquet_path = _create_dummy_parquet(dest_dir)
            fake_manifest = _make_fake_manifest(parquet_path)
            fake_rust = MagicMock()
            fake_rust.load_cptac_matrix.return_value = fake_manifest
            dataset = svc._get_or_create_dataset()
            job = IngestionJob.objects.create(
                project=self.project,
                job_type=IngestionJob.JobType.MATRIX_LOAD,
                status=IngestionJob.JobStatus.RUNNING,
                parameters={},
            )
            result = svc._execute(dataset=dataset, job=job, rust_engine=fake_rust)

        matrix = OmicMatrix.objects.get(dataset__accession=CPTAC_CCRCC_ACCESSION)
        self.assertEqual(matrix.storage_key, result['storage_key'])
        self.assertNotEqual(matrix.storage_key, '', 'storage_key não deve ser vazia')


# =============================================================================
# 3. Idempotência
# =============================================================================

class MatrixLoadIdempotencyTests(_TmpStorageTestCase):
    """
    Verifica idempotência em dois cenários:
    a) OmicMatrix já existe → MatrixAlreadyLoadedError no segundo run.
    b) Job PENDING/RUNNING ativo → MatrixJobActiveError no dispatch.
    """

    def setUp(self):
        super().setUp()
        self.user = make_user('idem_user')
        self.project = make_project(self.user, 'Idempotency Project')

    def _run_once(self) -> dict:
        svc = MatrixLoadService(self.project)
        with tempfile.TemporaryDirectory() as dest_dir:
            parquet_path = _create_dummy_parquet(dest_dir)
            fake_manifest = _make_fake_manifest(parquet_path)
            fake_rust = MagicMock()
            fake_rust.load_cptac_matrix.return_value = fake_manifest
            dataset = svc._get_or_create_dataset()
            job = IngestionJob.objects.create(
                project=self.project,
                job_type=IngestionJob.JobType.MATRIX_LOAD,
                status=IngestionJob.JobStatus.RUNNING,
                parameters={},
            )
            return svc._execute(dataset=dataset, job=job, rust_engine=fake_rust)

    def test_second_run_raises_matrix_already_loaded(self):
        """
        Segundo run.service() → MatrixAlreadyLoadedError.
        OmicMatrix não duplicada.
        """
        self._run_once()

        # Garante que só existe 1 OmicMatrix
        self.assertEqual(
            OmicMatrix.objects.filter(
                dataset__accession=CPTAC_CCRCC_ACCESSION
            ).count(),
            1,
        )

        svc2 = MatrixLoadService(self.project)
        dataset = svc2._get_or_create_dataset()

        with self.assertRaises(MatrixAlreadyLoadedError) as ctx:
            svc2._check_idempotency(dataset)

        # Exceção carrega a matriz existente
        self.assertIsNotNone(ctx.exception.matrix)
        self.assertEqual(ctx.exception.matrix.dataset.accession, CPTAC_CCRCC_ACCESSION)

    def test_second_run_does_not_duplicate_omic_matrix(self):
        """Dois runs sucessivos: OmicMatrix conta = 1 (não duplica)."""
        self._run_once()

        # Tenta segundo run, suprimindo o erro de idempotência
        try:
            self._run_once()
        except MatrixAlreadyLoadedError:
            pass  # esperado

        count = OmicMatrix.objects.filter(
            dataset__accession=CPTAC_CCRCC_ACCESSION,
        ).count()
        self.assertEqual(count, 1, 'OmicMatrix não deve ser duplicada')

    def test_second_run_does_not_duplicate_omic_matrix_sample(self):
        """Dois runs via _execute com ignore_conflicts: OmicMatrixSample conta = 3."""
        self._run_once()
        # Para testar que bulk_create(ignore_conflicts=True) não duplica, rodamos
        # _execute uma segunda vez sem o gate de idempotência (que teria bloqueado).
        # O gate de idempotência correto é testado acima; aqui testamos o seguro interno.
        svc = MatrixLoadService(self.project)
        with tempfile.TemporaryDirectory() as dest_dir:
            parquet_path = _create_dummy_parquet(dest_dir)
            fake_manifest = _make_fake_manifest(parquet_path)
            fake_rust = MagicMock()
            fake_rust.load_cptac_matrix.return_value = fake_manifest
            dataset = svc._get_or_create_dataset()
            job = IngestionJob.objects.create(
                project=self.project,
                job_type=IngestionJob.JobType.MATRIX_LOAD,
                status=IngestionJob.JobStatus.RUNNING,
                parameters={},
            )
            # _execute com matrix já existente: get_or_create retorna existente,
            # bulk_create ignora conflitos
            svc._execute(dataset=dataset, job=job, rust_engine=fake_rust)

        matrix = OmicMatrix.objects.get(dataset__accession=CPTAC_CCRCC_ACCESSION)
        count = OmicMatrixSample.objects.filter(matrix=matrix).count()
        self.assertEqual(count, 3, 'OmicMatrixSample não deve duplicar')

    def test_active_pending_job_raises_matrix_job_active_error(self):
        """
        Job PENDING ativo para o mesmo dataset+projeto → MatrixJobActiveError.
        """
        svc = MatrixLoadService(self.project)
        dataset = svc._get_or_create_dataset()

        # Cria job PENDING artificialmente
        IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.MATRIX_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={'dataset_id': dataset.id},
        )

        with self.assertRaises(MatrixJobActiveError) as ctx:
            svc._check_idempotency(dataset)

        self.assertIsNotNone(ctx.exception.job)

    def test_active_running_job_raises_matrix_job_active_error(self):
        """
        Job RUNNING ativo para o mesmo dataset+projeto → MatrixJobActiveError.
        """
        svc = MatrixLoadService(self.project)
        dataset = svc._get_or_create_dataset()

        IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.MATRIX_LOAD,
            status=IngestionJob.JobStatus.RUNNING,
            parameters={'dataset_id': dataset.id},
        )

        with self.assertRaises(MatrixJobActiveError):
            svc._check_idempotency(dataset)

    def test_completed_job_does_not_block_new_check_idempotency(self):
        """
        Job COMPLETED/FAILED não bloqueia (gate só reage a PENDING/RUNNING).
        Se não existir OmicMatrix e não houver job ativo, _check_idempotency
        não levanta nada.
        """
        svc = MatrixLoadService(self.project)
        dataset = svc._get_or_create_dataset()

        IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.MATRIX_LOAD,
            status=IngestionJob.JobStatus.COMPLETED,
            parameters={'dataset_id': dataset.id},
        )

        # Não deve levantar exceção: COMPLETED não é job ativo
        try:
            svc._check_idempotency(dataset)
        except (MatrixAlreadyLoadedError, MatrixJobActiveError) as exc:
            self.fail(
                f'_check_idempotency levantou inesperadamente: {exc}'
            )


# =============================================================================
# 4. Isolamento por projeto/usuário (Regra #3)
# =============================================================================

class MatrixLoadIsolationTests(TestCase):
    """
    Testa isolamento cross-user na task run_matrix_load.

    Regra #3: job de um projeto não processa projeto de outro usuário.
    """

    def setUp(self):
        self.user_a = make_user('iso_a_mx')
        self.user_b = make_user('iso_b_mx')
        self.project_a = make_project(self.user_a, 'Iso Project A')
        self.project_b = make_project(self.user_b, 'Iso Project B')

    def test_run_matrix_load_aborts_if_job_belongs_to_different_project(self):
        """
        run_matrix_load com project_id de B mas job_id criado para A →
        isolamento detectado, job marcado FAILED, sem carga de matriz.

        Invocamos via run_matrix_load.run(job_id, project_id) — o método
        Python desembrulhado do decorator @shared_task(bind=True), sem o
        `self` bound do Celery.
        """
        from apps.core.tasks.ingestion_tasks import run_matrix_load

        # Cria job para o projeto A
        svc_a = MatrixLoadService(self.project_a)
        dataset_a = svc_a._get_or_create_dataset()
        job_a = IngestionJob.objects.create(
            project=self.project_a,
            job_type=IngestionJob.JobType.MATRIX_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={'dataset_id': dataset_a.id},
        )

        # Disponibiliza rust_engine para não cair no ImportError
        fake_rust_mod = MagicMock()

        # Patch em MatrixLoadService para garantir que run() não é chamado
        with patch.dict('sys.modules', {'rust_engine': fake_rust_mod}), \
             patch(
                 'apps.core.services.matrix_load_service.MatrixLoadService',
             ) as MockService:
            # Passa project_id de B mas job_id de A → violação de isolamento
            run_matrix_load.run(str(job_a.id), str(self.project_b.id))

        # Job deve ter sido marcado FAILED pela violação de isolamento
        job_a.refresh_from_db()
        self.assertEqual(
            job_a.status,
            IngestionJob.JobStatus.FAILED,
            'Job deve ser FAILED quando project_id não bate',
        )
        self.assertIn(
            'isolamento',
            (job_a.error_message or '').lower(),
            'Mensagem de erro deve mencionar "isolamento"',
        )
        # MatrixLoadService nunca deve ter sido instanciada/chamada
        MockService.assert_not_called()

    def test_run_matrix_load_aborts_for_nonexistent_project(self):
        """
        run_matrix_load com project_id inexistente retorna sem processar.
        """
        from apps.core.tasks.ingestion_tasks import run_matrix_load

        fake_project_id = str(uuid.uuid4())
        fake_job_id = str(uuid.uuid4())
        fake_rust_mod = MagicMock()

        with patch.dict('sys.modules', {'rust_engine': fake_rust_mod}):
            result = run_matrix_load.run(fake_job_id, fake_project_id)

        # Retorna dict de erro sem processar
        self.assertEqual(result.get('n_features'), 0)
        self.assertIn('project not found', result.get('errors', []))

    def test_run_matrix_load_aborts_for_nonexistent_job(self):
        """
        run_matrix_load com job_id inexistente retorna sem processar.
        """
        from apps.core.tasks.ingestion_tasks import run_matrix_load

        fake_job_id = str(uuid.uuid4())
        fake_rust_mod = MagicMock()

        with patch.dict('sys.modules', {'rust_engine': fake_rust_mod}):
            result = run_matrix_load.run(fake_job_id, str(self.project_a.id))

        self.assertEqual(result.get('n_features'), 0)
        self.assertIn('job not found', result.get('errors', []))


# =============================================================================
# 5. run_matrix_load (task Celery) — fluxo completo mockado
# =============================================================================

class RunMatrixLoadTaskTests(_TmpStorageTestCase):
    """
    Testa a task run_matrix_load de ponta a ponta com rust_engine mockado.

    Como run_matrix_load é @shared_task(bind=True), o primeiro argumento
    posicional é `self` (a instância bound da task Celery). Para chamar a
    função diretamente em testes (sem worker), usamos task_mock como
    substituto de self.

    MatrixLoadService é importada localmente dentro da task (`from … import`),
    então o caminho correto de patch é o módulo onde a classe é *definida*:
    'apps.core.services.matrix_load_service.MatrixLoadService'.
    """

    def setUp(self):
        super().setUp()
        self.user = make_user('task_user')
        self.project = make_project(self.user, 'Task Test Project')

    def test_task_delegates_to_service_and_returns_result(self):
        """
        run_matrix_load com MatrixLoadService mockado: chama service.run(job=job)
        e retorna n_features, n_samples, errors=[].

        Invocamos via run_matrix_load.run(job_id, project_id) — método Python
        desembrulhado do @shared_task(bind=True), sem o `self` bound do Celery.
        MatrixLoadService é patchada no módulo onde é *definida*, pois a task
        importa com `from apps.core.services.matrix_load_service import ...`.
        """
        from apps.core.tasks.ingestion_tasks import run_matrix_load

        # Cria job PENDING (o que dispatch() faria)
        svc = MatrixLoadService(self.project)
        dataset = svc._get_or_create_dataset()
        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.MATRIX_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={'dataset_id': dataset.id},
        )

        fake_rust_mod = MagicMock()
        expected_result = {
            'job_id': str(job.id),
            # Namespace compartilhado público (M4): sem user_id nem project_id
            'storage_key': (
                f'omics/_shared/{CPTAC_CCRCC_ACCESSION}/{CPTAC_PARQUET_NAME}'
            ),
            'n_features': 3,
            'n_samples': 3,
            'checksum_md5': _FAKE_MD5,
            'genes_discarded': 0,
            'roles': {'tumor': 2, 'normal': 1},
        }

        with patch.dict('sys.modules', {'rust_engine': fake_rust_mod}), \
             patch(
                 'apps.core.services.matrix_load_service.MatrixLoadService',
             ) as MockServiceClass:
            mock_svc_instance = MagicMock()
            mock_svc_instance.run.return_value = expected_result
            MockServiceClass.return_value = mock_svc_instance

            result = run_matrix_load.run(str(job.id), str(self.project.id))

        # A task deve ter chamado service.run(job=job)
        mock_svc_instance.run.assert_called_once()

        self.assertEqual(result['n_features'], 3)
        self.assertEqual(result['n_samples'], 3)
        self.assertEqual(result.get('errors'), [])

    def test_task_marks_job_failed_on_exception(self):
        """
        Se MatrixLoadService.run() lança exceção, a task atualiza o job
        para FAILED e propaga o retry via self.retry.
        """
        from apps.core.tasks.ingestion_tasks import run_matrix_load

        svc = MatrixLoadService(self.project)
        dataset = svc._get_or_create_dataset()
        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.MATRIX_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={'dataset_id': dataset.id},
        )

        exc = RuntimeError('Teste de falha simulada')
        fake_rust_mod = MagicMock()

        with patch.dict('sys.modules', {'rust_engine': fake_rust_mod}), \
             patch(
                 'apps.core.services.matrix_load_service.MatrixLoadService',
             ) as MockServiceClass:
            mock_svc_instance = MagicMock()
            mock_svc_instance.run.side_effect = exc
            MockServiceClass.return_value = mock_svc_instance

            with self.assertRaises(RuntimeError):
                run_matrix_load.run(str(job.id), str(self.project.id))

        # Job deve ter sido marcado FAILED
        job.refresh_from_db()
        self.assertEqual(job.status, IngestionJob.JobStatus.FAILED)


# =============================================================================
# 6. management command load_cptac_matrix
# =============================================================================

class LoadCptacMatrixCommandTests(_TmpStorageTestCase):
    """
    Testa o management command load_cptac_matrix:
    - Modo síncrono com mock do rust_engine.
    - Modo --async com mock do dispatch().
    - Idempotência: segundo call reporta aviso.
    - Sem vazar caminho físico nem credencial no stdout.
    """

    def setUp(self):
        super().setUp()
        self.user = make_user('cmd_user')
        self.project = make_project(self.user, 'Command Test Project')

    def _call_command_sync(self) -> tuple[str, str]:
        """Chama load_cptac_matrix em modo síncrono com rust_engine mockado."""
        from django.core.management import call_command
        from io import StringIO

        stdout = StringIO()
        stderr = StringIO()

        with tempfile.TemporaryDirectory() as dest_dir:
            parquet_path = _create_dummy_parquet(dest_dir)
            fake_manifest = _make_fake_manifest(parquet_path)
            fake_rust = MagicMock()
            fake_rust.load_cptac_matrix.return_value = fake_manifest

            with patch('apps.core.services.matrix_load_service.default_storage', new=self._storage), \
                 patch.dict('sys.modules', {'rust_engine': fake_rust}):
                call_command(
                    'load_cptac_matrix',
                    project=str(self.project.id),
                    stdout=stdout,
                    stderr=stderr,
                )

        return stdout.getvalue(), stderr.getvalue()

    def test_command_sync_resolves_project_and_runs(self):
        """
        load_cptac_matrix --project <UUID> (modo síncrono) termina com sucesso
        e imprime storage_key, n_features, n_samples.
        """
        stdout_val, _stderr = self._call_command_sync()

        self.assertIn('storage_key', stdout_val.lower())
        self.assertIn('n_features', stdout_val.lower())
        self.assertIn('n_samples', stdout_val.lower())

    def test_command_output_does_not_leak_physical_path(self):
        """
        O stdout do command não vaza caminhos físicos absolutos.
        storage_key mostrado é a chave lógica, não /tmp/...
        """
        stdout_val, _stderr = self._call_command_sync()

        physical_prefixes = ['/tmp', '/var', '/home', '/Users', '/private']
        for prefix in physical_prefixes:
            self.assertNotIn(
                prefix,
                stdout_val,
                f'stdout não deve conter caminho físico "{prefix}": '
                f'{stdout_val[:300]!r}',
            )

    def test_command_idempotency_reports_warning_not_error(self):
        """
        Segundo call ao command (modo síncrono) reporta aviso de idempotência
        e não duplica OmicMatrix.
        """
        from django.core.management import call_command
        from io import StringIO

        # Primeiro run (cria OmicMatrix)
        self._call_command_sync()

        # Segundo run
        stdout2 = StringIO()
        with patch.dict('sys.modules', {'rust_engine': MagicMock()}):
            call_command(
                'load_cptac_matrix',
                project=str(self.project.id),
                stdout=stdout2,
            )

        # Não deve ter duplicado
        self.assertEqual(
            OmicMatrix.objects.filter(
                dataset__accession=CPTAC_CCRCC_ACCESSION
            ).count(),
            1,
        )

        # Deve ter reportado a idempotência
        stdout2_val = stdout2.getvalue().lower()
        # A mensagem pode conter 'idempotência', 'já existe', 'already' etc.
        self.assertTrue(
            any(kw in stdout2_val for kw in ['idempot', 'já existe', 'already', 'existente']),
            f'stdout do segundo run deve mencionar idempotência: {stdout2_val!r}',
        )

    def test_command_async_mode_dispatches_pending_job(self):
        """
        load_cptac_matrix --project <UUID> --async cria job PENDING sem processar.
        """
        from django.core.management import call_command
        from io import StringIO

        stdout_val = StringIO()

        with patch(
            'apps.core.tasks.ingestion_tasks.run_matrix_load.delay',
            return_value=None,
        ):
            call_command(
                'load_cptac_matrix',
                project=str(self.project.id),
                use_async=True,
                stdout=stdout_val,
            )

        job = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.MATRIX_LOAD,
        ).first()
        self.assertIsNotNone(job, 'Job deve ter sido criado em modo --async')
        self.assertEqual(job.status, IngestionJob.JobStatus.PENDING)

    def test_command_nonexistent_project_raises_command_error(self):
        """
        load_cptac_matrix --project com UUID inexistente levanta CommandError.
        """
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command(
                'load_cptac_matrix',
                project=str(uuid.uuid4()),
            )


# =============================================================================
# 7. Dispatch (MatrixLoadService.dispatch)
# =============================================================================

class MatrixLoadDispatchTests(TestCase):
    """
    Testa MatrixLoadService.dispatch():
    - Cria job PENDING.
    - Enfileira run_matrix_load.delay().
    - Idempotência: MatrixAlreadyLoadedError / MatrixJobActiveError.
    """

    def setUp(self):
        self.user = make_user('disp_user')
        self.project = make_project(self.user, 'Dispatch Project')

    @patch('apps.core.tasks.ingestion_tasks.run_matrix_load.delay', return_value=None)
    def test_dispatch_creates_pending_job(self, mock_delay):
        """dispatch() cria IngestionJob com status PENDING e job_type MATRIX_LOAD."""
        job = MatrixLoadService.dispatch(self.project)

        self.assertEqual(job.status, IngestionJob.JobStatus.PENDING)
        self.assertEqual(job.job_type, IngestionJob.JobType.MATRIX_LOAD)
        self.assertEqual(job.project_id, self.project.id)

    @patch('apps.core.tasks.ingestion_tasks.run_matrix_load.delay', return_value=None)
    def test_dispatch_calls_celery_delay(self, mock_delay):
        """dispatch() chama run_matrix_load.delay com job_id e project_id."""
        job = MatrixLoadService.dispatch(self.project)

        mock_delay.assert_called_once_with(str(job.id), str(self.project.id))

    @patch('apps.core.tasks.ingestion_tasks.run_matrix_load.delay', return_value=None)
    def test_dispatch_parameters_do_not_contain_db_url(self, mock_delay):
        """
        IngestionJob.parameters NÃO contém db_url nem credenciais
        (sensitive-data-handling).
        """
        job = MatrixLoadService.dispatch(self.project)

        params = job.parameters or {}
        self.assertNotIn('db_url', params)
        self.assertNotIn('password', params)
        self.assertNotIn('DATABASE_URL', params)

    @patch('apps.core.tasks.ingestion_tasks.run_matrix_load.delay', return_value=None)
    def test_dispatch_idempotency_matrix_already_loaded(self, mock_delay):
        """
        dispatch() quando OmicMatrix já existe → MatrixAlreadyLoadedError.
        Job não criado.
        """
        # Cria OmicMatrix diretamente (simula carga anterior)
        svc = MatrixLoadService(self.project)
        dataset = svc._get_or_create_dataset()
        OmicMatrix.objects.create(
            dataset=dataset,
            omics_layer='proteomic',
            feature_axis='gene',
            loader_version=LOADER_VERSION,
            data_format_level=OmicMatrix.DataFormatLevel.INTENSITIES,
            storage_key='omics/1/1/CPTAC-CCRCC-PROTEOME/test.parquet',
            n_features=3,
            n_samples=3,
            checksum_md5=_FAKE_MD5,
        )

        with self.assertRaises(MatrixAlreadyLoadedError):
            MatrixLoadService.dispatch(self.project)

        # Nenhum job deve ter sido criado
        self.assertEqual(
            IngestionJob.objects.filter(
                project=self.project,
                job_type=IngestionJob.JobType.MATRIX_LOAD,
            ).count(),
            0,
        )

    @patch('apps.core.tasks.ingestion_tasks.run_matrix_load.delay', return_value=None)
    def test_dispatch_idempotency_active_job(self, mock_delay):
        """
        dispatch() quando job PENDING ativo → MatrixJobActiveError.
        Segundo job não criado.
        """
        # Primeiro dispatch (cria job)
        MatrixLoadService.dispatch(self.project)
        jobs_after_first = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.MATRIX_LOAD,
        ).count()
        self.assertEqual(jobs_after_first, 1)

        # Segundo dispatch → MatrixJobActiveError
        with self.assertRaises(MatrixJobActiveError):
            MatrixLoadService.dispatch(self.project)

        # Ainda apenas 1 job
        jobs_after_second = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.MATRIX_LOAD,
        ).count()
        self.assertEqual(jobs_after_second, 1)

    @patch('apps.core.tasks.ingestion_tasks.run_matrix_load.delay', return_value=None)
    def test_dispatch_job_parameters_contain_loader_version(self, mock_delay):
        """
        IngestionJob.parameters contém loader_version e dataset_accession
        para rastreabilidade.
        """
        job = MatrixLoadService.dispatch(self.project)

        params = job.parameters or {}
        self.assertIn('loader_version', params)
        self.assertEqual(params['loader_version'], LOADER_VERSION)
        self.assertIn('dataset_accession', params)
        self.assertEqual(params['dataset_accession'], CPTAC_CCRCC_ACCESSION)


# =============================================================================
# 8. Testes de regressão — _normalize_role
# =============================================================================

class NormalizeRoleTests(TestCase):
    """
    Garante que _normalize_role mapeia corretamente strings do Rust
    para o vocabulário canônico de OmicMatrixSample.SampleRole.
    """

    def test_normalize_tumor(self):
        from apps.core.services.matrix_load_service import _normalize_role
        self.assertEqual(_normalize_role('tumor'), OmicMatrixSample.SampleRole.TUMOR)

    def test_normalize_normal(self):
        from apps.core.services.matrix_load_service import _normalize_role
        self.assertEqual(_normalize_role('normal'), OmicMatrixSample.SampleRole.NORMAL)

    def test_normalize_unknown_for_unexpected_value(self):
        from apps.core.services.matrix_load_service import _normalize_role
        self.assertEqual(_normalize_role(''), OmicMatrixSample.SampleRole.UNKNOWN)
        self.assertEqual(_normalize_role('other'), OmicMatrixSample.SampleRole.UNKNOWN)
        self.assertEqual(_normalize_role('Tumor'), OmicMatrixSample.SampleRole.UNKNOWN)  # case-sensitive

    def test_normalize_tumor_role_value_is_correct_string(self):
        """TUMOR = 'tumor', NORMAL = 'normal', UNKNOWN = 'unknown'."""
        self.assertEqual(OmicMatrixSample.SampleRole.TUMOR, 'tumor')
        self.assertEqual(OmicMatrixSample.SampleRole.NORMAL, 'normal')
        self.assertEqual(OmicMatrixSample.SampleRole.UNKNOWN, 'unknown')


# =============================================================================
# 9. Testes de _get_or_create_dataset
# =============================================================================

class GetOrCreateDatasetTests(TestCase):
    """
    Verifica _get_or_create_dataset: criação, idempotência e correção de
    source_db=tcga → cptac.
    """

    def setUp(self):
        self.user = make_user('ds_user')
        self.project = make_project(self.user, 'Dataset Create Project')

    def test_creates_omic_dataset_on_first_call(self):
        """Primeira chamada cria OmicDataset com source_db='cptac'."""
        svc = MatrixLoadService(self.project)
        dataset = svc._get_or_create_dataset()

        self.assertEqual(dataset.accession, CPTAC_CCRCC_ACCESSION)
        self.assertEqual(dataset.source_db, OmicDataset.SourceDB.CPTAC)
        self.assertEqual(dataset.access_type, OmicDataset.AccessType.PUBLIC)
        self.assertEqual(dataset.has_control_group, OmicDataset.ControlGroup.YES)

    def test_get_or_create_is_idempotent(self):
        """Duas chamadas não duplicam OmicDataset."""
        svc = MatrixLoadService(self.project)
        svc._get_or_create_dataset()
        svc._get_or_create_dataset()

        count = OmicDataset.objects.filter(accession=CPTAC_CCRCC_ACCESSION).count()
        self.assertEqual(count, 1)

    def test_corrects_stale_source_db_tcga_to_cptac(self):
        """
        OmicDataset pre-existente com source_db='tcga' é corrigido para 'cptac'
        pelo _get_or_create_dataset.
        """
        OmicDataset.objects.create(
            accession=CPTAC_CCRCC_ACCESSION,
            source_db='tcga',  # valor incorreto antigo
            title='Stale dataset',
        )

        svc = MatrixLoadService(self.project)
        dataset = svc._get_or_create_dataset()

        dataset.refresh_from_db()
        self.assertEqual(dataset.source_db, OmicDataset.SourceDB.CPTAC)

    def test_project_dataset_link_created_on_first_call(self):
        """ProjectDataset(project, dataset) criado na primeira chamada."""
        svc = MatrixLoadService(self.project)
        dataset = svc._get_or_create_dataset()

        self.assertTrue(
            ProjectDataset.objects.filter(
                project=self.project, dataset=dataset
            ).exists()
        )

    def test_project_dataset_link_idempotent(self):
        """Duas chamadas não duplicam ProjectDataset."""
        svc = MatrixLoadService(self.project)
        svc._get_or_create_dataset()
        svc._get_or_create_dataset()

        dataset = OmicDataset.objects.get(accession=CPTAC_CCRCC_ACCESSION)
        count = ProjectDataset.objects.filter(
            project=self.project, dataset=dataset
        ).count()
        self.assertEqual(count, 1)


# =============================================================================
# 10. Testes do ramo não-público de _upload_parquet (Hardening M4)
# =============================================================================

class MatrixLoadNonPublicStorageKeyTests(_TmpStorageTestCase):
    """
    Verifica que _upload_parquet usa namespace PROJECT-SCOPED quando
    dataset.access_type != 'public'.

    Prova que dados não-públicos (controlled / unknown) NUNCA vazam para o
    namespace omics/_shared/, que é compartilhado entre todos os projetos.

    Estratégia: instanciar OmicDataset diretamente com access_type desejado e
    chamar _upload_parquet() explicitamente, sem passar por _get_or_create_dataset()
    (que fixaria access_type=PUBLIC para o dataset CPTAC).  Isso isola o teste ao
    ramo de branching do upload sem alterar código de produção.
    """

    def setUp(self):
        super().setUp()
        self.user = make_user('nonpub_user')
        self.project = make_project(self.user, 'NonPublic Storage Project')

    def _make_controlled_dataset(self, accession: str = 'CONTROLLED-DATASET-001') -> OmicDataset:
        """Cria OmicDataset com access_type='controlled' para exercitar ramo não-público."""
        return OmicDataset.objects.create(
            accession=accession,
            source_db=OmicDataset.SourceDB.GEO,
            title='Controlled dataset para teste de storage_key',
            access_type=OmicDataset.AccessType.CONTROLLED,
        )

    def _make_unknown_access_dataset(self, accession: str = 'UNKNOWN-DATASET-001') -> OmicDataset:
        """Cria OmicDataset com access_type='unknown' para exercitar ramo não-público."""
        return OmicDataset.objects.create(
            accession=accession,
            source_db=OmicDataset.SourceDB.GEO,
            title='Unknown access dataset para teste de storage_key',
            access_type=OmicDataset.AccessType.UNKNOWN,
        )

    def _run_upload(self, dataset: OmicDataset) -> str:
        """
        Exercita _upload_parquet com o dataset fornecido.

        Cria um arquivo Parquet dummy, um IngestionJob RUNNING e chama
        _upload_parquet() diretamente no MatrixLoadService.  Retorna a
        storage_key produzida.
        """
        svc = MatrixLoadService(self.project)
        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.MATRIX_LOAD,
            status=IngestionJob.JobStatus.RUNNING,
            parameters={},
        )
        with tempfile.TemporaryDirectory(prefix='davinci_nonpub_') as dest_dir:
            parquet_path = _create_dummy_parquet(dest_dir)
            storage_key = svc._upload_parquet(parquet_path, dataset, job)
        return storage_key

    # ── controlled ────────────────────────────────────────────────────────────

    def test_controlled_dataset_uses_project_scoped_namespace(self):
        """
        access_type='controlled' → storage_key contém omics/{user_id}/{project_id}/.
        Prova que dado controlado não vaza para o namespace _shared.
        """
        dataset = self._make_controlled_dataset()
        storage_key = self._run_upload(dataset)

        expected_prefix = f'omics/{self.user.id}/{self.project.id}/'
        self.assertIn(
            expected_prefix,
            storage_key,
            f'storage_key de dado controlado deve conter "{expected_prefix}"; '
            f'obteve: {storage_key!r}',
        )

    def test_controlled_dataset_does_not_use_shared_namespace(self):
        """
        access_type='controlled' → storage_key NÃO contém 'omics/_shared/'.
        Garante que dado controlado NUNCA vaza para namespace público.
        """
        dataset = self._make_controlled_dataset()
        storage_key = self._run_upload(dataset)

        self.assertNotIn(
            'omics/_shared/',
            storage_key,
            f'Dado controlado NÃO deve usar namespace _shared: {storage_key!r}',
        )

    def test_controlled_dataset_storage_key_contains_accession(self):
        """storage_key do dado controlado contém o accession do dataset."""
        accession = 'CONTROLLED-DATASET-001'
        dataset = self._make_controlled_dataset(accession=accession)
        storage_key = self._run_upload(dataset)

        self.assertIn(
            accession,
            storage_key,
            f'storage_key deve conter accession "{accession}": {storage_key!r}',
        )

    # ── unknown ───────────────────────────────────────────────────────────────

    def test_unknown_access_dataset_uses_project_scoped_namespace(self):
        """
        access_type='unknown' → storage_key contém omics/{user_id}/{project_id}/.
        Qualquer tipo diferente de PUBLIC cai no ramo project-scoped.
        """
        dataset = self._make_unknown_access_dataset()
        storage_key = self._run_upload(dataset)

        expected_prefix = f'omics/{self.user.id}/{self.project.id}/'
        self.assertIn(
            expected_prefix,
            storage_key,
            f'storage_key de dado com access_type=unknown deve ser project-scoped; '
            f'obteve: {storage_key!r}',
        )

    def test_unknown_access_dataset_does_not_use_shared_namespace(self):
        """
        access_type='unknown' → storage_key NÃO contém 'omics/_shared/'.
        """
        dataset = self._make_unknown_access_dataset()
        storage_key = self._run_upload(dataset)

        self.assertNotIn(
            'omics/_shared/',
            storage_key,
            f'Dado com access_type=unknown NÃO deve usar namespace _shared: '
            f'{storage_key!r}',
        )

    # ── regressão cruzada: PUBLIC ainda usa _shared ────────────────────────────

    def test_public_dataset_still_uses_shared_namespace(self):
        """
        Regressão: ao mudar lógica de não-públicos, PUBLIC ainda usa _shared.

        Confirma que o fix M4 não quebrou o ramo positivo: OmicDataset com
        access_type=PUBLIC continua usando shared_omics_storage_key.
        """
        dataset = OmicDataset.objects.create(
            accession='PUBLIC-DATASET-FOR-REGRESSION',
            source_db=OmicDataset.SourceDB.GEO,
            title='Public dataset — regressão ramo _shared',
            access_type=OmicDataset.AccessType.PUBLIC,
        )
        svc = MatrixLoadService(self.project)
        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.MATRIX_LOAD,
            status=IngestionJob.JobStatus.RUNNING,
            parameters={},
        )
        with tempfile.TemporaryDirectory(prefix='davinci_pub_') as dest_dir:
            parquet_path = _create_dummy_parquet(dest_dir)
            storage_key = svc._upload_parquet(parquet_path, dataset, job)

        self.assertIn(
            'omics/_shared/',
            storage_key,
            f'Dataset PUBLIC deve continuar usando namespace _shared: {storage_key!r}',
        )
        self.assertNotIn(
            f'omics/{self.user.id}/',
            storage_key,
            f'Dataset PUBLIC não deve ter user_id na chave: {storage_key!r}',
        )
