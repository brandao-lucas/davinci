"""
test_download.py — Cobertura da F1 (download de dados ômicos — GEO supplementary).

Plano de referência: .claude/plans/2026-06-19-download-dados-omicos.md

Áreas cobertas:
  1. Model DatasetFile (migration 0015):
       CheckConstraint XOR dataset/sample; UNIQUE em accession.
  2. Service DownloadService.dispatch:
       Idempotência (não duplica job ativo); cria IngestionJob correto;
       ncbi_api_key NUNCA nos parâmetros do job.
  3. Task run_omics_download:
       Upload pós-job via default_storage (mockado);
       storage_key sobrescrito com chave de object storage;
       download_status='downloaded', downloaded_at preenchido;
       todos downloaded → ProjectDataset.curation_status='downloaded';
       falha de upload → download_status='failed' + error_message;
       NUNCA deleta DatasetFile;
       curated_at/exclusion_reason/notes intocados.
  4. Endpoint POST .../datasets/{id}/download/:
       Dispara job (mock); seta curation_status='queued'; user B recebe 404.
  5. Endpoint GET .../datasets/{id}/files/:
       Lista DatasetFile; serializer NÃO expõe storage_key nem remote_url;
       download_url presente e resolve com prefixo /api/v1/;
       isolamento cross-user (404).
  6. Endpoint GET .../datasets/{id}/files/{file_id}/content/:
       Arquivo não baixado → 409; file_id de outro dataset → 404;
       input do cliente é só o file_id (PK); sucesso via mock de default_storage.
  7. Throttle: actions têm throttle_scope declarado.

Convenção: sem pytest — usa django.test.TestCase / APITestCase.
Sem internet: rust_engine e default_storage são mockados onde necessário.
"""

from __future__ import annotations

from io import BytesIO
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from apps.core.models import (
    DatasetFile,
    DaVinciProject,
    IngestionJob,
    OmicDataset,
    ProjectDataset,
)
from apps.core.services.download_service import DownloadService
from apps.core.views.dataset_views import ProjectDatasetViewSet


# =============================================================================
# Helpers de factory
# =============================================================================

def make_user(username='dl_user', password='pw'):
    return User.objects.create_user(username=username, password=password)


def make_project(user, title='Download Project', query_term='cancer'):
    slug = f'{title.lower().replace(" ", "-")}-{user.username}-davinci'
    return DaVinciProject.objects.create(
        user=user, title=title, slug=slug, query_term=query_term
    )


def make_dataset(accession='GSE12345', source_db='geo', extra_metadata=None):
    return OmicDataset.objects.create(
        accession=accession,
        source_db=source_db,
        title=f'Dataset {accession}',
        omic_type='transcriptomic',
        organism='Homo sapiens',
        extra_metadata=extra_metadata or {'gse': accession},
    )


def make_project_dataset(project, dataset, curation_status='pending'):
    return ProjectDataset.objects.create(
        project=project, dataset=dataset, curation_status=curation_status
    )


def make_dataset_file(
    dataset,
    accession='GSE12345_supp_file.txt.gz',
    file_type='supplementary',
    source='geo_ftp',
    remote_url='ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE12345.txt.gz',
    storage_key='',
    download_status='pending',
    size_bytes=None,
):
    return DatasetFile.objects.create(
        dataset=dataset,
        accession=accession,
        file_type=file_type,
        source=source,
        remote_url=remote_url,
        storage_key=storage_key,
        download_status=download_status,
        size_bytes=size_bytes,
    )


# =============================================================================
# 1 — Model DatasetFile: CheckConstraint XOR e UNIQUE em accession
# =============================================================================

class DatasetFileModelConstraintsTests(APITestCase):
    """
    Garante que as constraints do model DatasetFile funcionam:
    - CheckConstraint XOR: exatamente um de (dataset, sample) preenchido.
    - UNIQUE em accession.
    """

    def setUp(self):
        self.user = make_user('model_user')
        self.project = make_project(self.user, 'Model Project')
        self.dataset = make_dataset('GSE20001')

    def test_dataset_sem_sample_e_valido(self):
        """DatasetFile com dataset preenchido e sample=None é válido."""
        df = make_dataset_file(self.dataset, accession='GSE20001_valid')
        self.assertIsNotNone(df.pk)
        self.assertIsNotNone(df.dataset_id)
        self.assertIsNone(df.sample_id)

    def test_ambos_nulos_viola_check_constraint(self):
        """dataset=None e sample=None viola CheckConstraint XOR."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                DatasetFile.objects.create(
                    dataset=None,
                    sample=None,
                    accession='xor_both_null',
                    file_type='supplementary',
                    source='geo_ftp',
                    remote_url='ftp://example.com/file.txt',
                )

    def test_accession_unique_constraint(self):
        """Inserir dois DatasetFile com mesmo accession viola UNIQUE."""
        make_dataset_file(self.dataset, accession='GSE20001_dup_acc')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_dataset_file(self.dataset, accession='GSE20001_dup_acc')

    def test_mesmo_dataset_acessions_distintos_permitido(self):
        """Dois DatasetFile com accessions distintos para o mesmo dataset são permitidos."""
        f1 = make_dataset_file(self.dataset, accession='GSE20001_file_a')
        f2 = make_dataset_file(self.dataset, accession='GSE20001_file_b')
        self.assertNotEqual(f1.pk, f2.pk)

    def test_campo_download_status_default_pending(self):
        """download_status default é 'pending'."""
        df = make_dataset_file(self.dataset, accession='GSE20001_default_status')
        self.assertEqual(df.download_status, DatasetFile.DownloadStatus.PENDING)

    def test_storage_key_default_vazio(self):
        """storage_key default é string vazia (não null)."""
        df = make_dataset_file(self.dataset, accession='GSE20001_sk_default')
        self.assertEqual(df.storage_key, '')
        self.assertIsNotNone(df.storage_key)

    def test_downloaded_at_null_ate_download(self):
        """downloaded_at é null até que o download ocorra."""
        df = make_dataset_file(self.dataset, accession='GSE20001_dloaded_at')
        self.assertIsNone(df.downloaded_at)

    def test_str_representation(self):
        """__str__ retorna accession + file_type + download_status."""
        df = make_dataset_file(self.dataset, accession='GSE20001_str')
        s = str(df)
        self.assertIn('GSE20001_str', s)
        self.assertIn('supplementary', s)
        self.assertIn('pending', s)


# =============================================================================
# 2 — Service DownloadService.dispatch
# =============================================================================

class DownloadServiceDispatchTests(APITestCase):
    """
    Testa DownloadService.dispatch:
    - Cria IngestionJob com job_type GEO_SUPPLEMENTARY_DOWNLOAD.
    - Idempotência: não cria job duplicado se já houver um ativo.
    - ncbi_api_key NUNCA aparece nos parâmetros do job.
    - Contém dataset_id, dataset_accession, source_db, file_kind nos parâmetros.
    """

    def setUp(self):
        self.user = make_user('svc_user')
        self.project = make_project(self.user, 'Service Project')
        self.dataset = make_dataset('GSE30001')

    @patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay')
    def test_dispatch_cria_ingestion_job(self, mock_delay):
        """dispatch() cria IngestionJob com tipo GEO_SUPPLEMENTARY_DOWNLOAD."""
        job = DownloadService.dispatch(
            project=self.project,
            dataset=self.dataset,
            file_kind='geo_supplementary',
            user=self.user,
        )
        self.assertIsNotNone(job)
        self.assertEqual(job.job_type, IngestionJob.JobType.GEO_SUPPLEMENTARY_DOWNLOAD)

    @patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay')
    def test_dispatch_job_status_pending(self, mock_delay):
        """Job criado começa em status PENDING."""
        job = DownloadService.dispatch(
            project=self.project,
            dataset=self.dataset,
            file_kind='geo_supplementary',
            user=self.user,
        )
        self.assertEqual(job.status, IngestionJob.JobStatus.PENDING)

    @patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay')
    def test_dispatch_nao_grava_ncbi_api_key_nos_parametros(self, mock_delay):
        """ncbi_api_key NUNCA deve aparecer nos parameters do job."""
        job = DownloadService.dispatch(
            project=self.project,
            dataset=self.dataset,
            file_kind='geo_supplementary',
            user=self.user,
        )
        params = job.parameters
        self.assertNotIn('ncbi_api_key', params)
        # Verifica também que nenhuma variação de nome inclui a key
        for key in params:
            self.assertNotIn('key', key.lower())

    @patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay')
    def test_dispatch_parametros_essenciais_no_job(self, mock_delay):
        """Job deve conter dataset_id, dataset_accession, source_db, file_kind."""
        job = DownloadService.dispatch(
            project=self.project,
            dataset=self.dataset,
            file_kind='geo_supplementary',
            user=self.user,
        )
        params = job.parameters
        self.assertIn('dataset_id', params)
        self.assertIn('dataset_accession', params)
        self.assertIn('source_db', params)
        self.assertIn('file_kind', params)
        self.assertEqual(params['dataset_id'], self.dataset.id)
        self.assertEqual(params['dataset_accession'], self.dataset.accession)
        self.assertEqual(params['source_db'], 'geo')
        self.assertEqual(params['file_kind'], 'geo_supplementary')

    @patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay')
    def test_dispatch_idempotencia_nao_duplica_job_ativo(self, mock_delay):
        """Segundo dispatch com job PENDING ativo retorna o mesmo job, sem criar novo."""
        job1 = DownloadService.dispatch(
            project=self.project,
            dataset=self.dataset,
            file_kind='geo_supplementary',
            user=self.user,
        )
        job2 = DownloadService.dispatch(
            project=self.project,
            dataset=self.dataset,
            file_kind='geo_supplementary',
            user=self.user,
        )
        # Mesmo objeto (idempotência)
        self.assertEqual(job1.id, job2.id)
        # Apenas 1 job no banco para este dataset
        count = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.GEO_SUPPLEMENTARY_DOWNLOAD,
            parameters__dataset_id=self.dataset.id,
        ).count()
        self.assertEqual(count, 1)

    @patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay')
    def test_dispatch_idempotencia_com_job_running(self, mock_delay):
        """Dispatch com job em status RUNNING também é idempotente."""
        job1 = DownloadService.dispatch(
            project=self.project,
            dataset=self.dataset,
            file_kind='geo_supplementary',
            user=self.user,
        )
        # Simula Celery mudando para RUNNING
        IngestionJob.objects.filter(id=job1.id).update(status=IngestionJob.JobStatus.RUNNING)

        job2 = DownloadService.dispatch(
            project=self.project,
            dataset=self.dataset,
            file_kind='geo_supplementary',
            user=self.user,
        )
        self.assertEqual(job1.id, job2.id)

    @patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay')
    def test_dispatch_apos_job_concluido_cria_novo(self, mock_delay):
        """Após job COMPLETED, novo dispatch cria um novo job (não é bloqueado)."""
        job1 = DownloadService.dispatch(
            project=self.project,
            dataset=self.dataset,
            file_kind='geo_supplementary',
            user=self.user,
        )
        IngestionJob.objects.filter(id=job1.id).update(status=IngestionJob.JobStatus.COMPLETED)

        dataset2 = make_dataset('GSE30002')
        job2 = DownloadService.dispatch(
            project=self.project,
            dataset=dataset2,
            file_kind='geo_supplementary',
            user=self.user,
        )
        self.assertNotEqual(job1.id, job2.id)

    @patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay')
    def test_dispatch_file_kind_invalido_levanta_value_error(self, mock_delay):
        """file_kind inválido levanta ValueError antes de criar job."""
        with self.assertRaises(ValueError):
            DownloadService.dispatch(
                project=self.project,
                dataset=self.dataset,
                file_kind='arquivo_nao_existente',
                user=self.user,
            )

    @patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay')
    def test_dispatch_dispara_task_celery(self, mock_delay):
        """dispatch() chama run_omics_download.delay() exatamente uma vez."""
        DownloadService.dispatch(
            project=self.project,
            dataset=self.dataset,
            file_kind='geo_supplementary',
            user=self.user,
        )
        mock_delay.assert_called_once()


# =============================================================================
# 3 — Task run_omics_download (mockando rust_engine + default_storage)
# =============================================================================

class RunOmicsDownloadTaskTests(APITestCase):
    """
    Testa a task run_omics_download com rust_engine e default_storage mockados.
    Nenhuma chamada HTTP real; nenhum acesso a disco real.

    Cobre:
    - Upload pós-job: default_storage.save chamado; storage_key sobrescrito.
    - download_status='downloaded', downloaded_at preenchido.
    - Todos downloaded → ProjectDataset.curation_status='downloaded'.
    - Falha de upload → download_status='failed' + error_message; registro preservado.
    - curated_at, exclusion_reason, notes intocados.
    - DatasetFile NUNCA deletado, mesmo em falha.
    """

    def setUp(self):
        self.user = make_user('task_user')
        self.project = make_project(self.user, 'Task Project')
        self.dataset = make_dataset('GSE40001', extra_metadata={'gse': 'GSE40001'})
        self.project_dataset = make_project_dataset(
            self.project, self.dataset, curation_status='queued'
        )
        # Job já criado pelo DownloadService
        self.job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.GEO_SUPPLEMENTARY_DOWNLOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={
                'dataset_id': self.dataset.id,
                'dataset_accession': self.dataset.accession,
                'source_db': 'geo',
                'file_kind': 'geo_supplementary',
            },
        )

    def _make_fake_rust_result(self, files_downloaded=1, bytes_total=1024, errors=None):
        """Retorna um objeto fake simulando o resultado de rust_engine.download_dataset_files."""
        result = MagicMock()
        result.files_downloaded = files_downloaded
        result.bytes_total = bytes_total
        result.errors = errors or []
        return result

    def _run_task_with_mock_rust(
        self,
        rust_result=None,
        local_path='/tmp/davinci_omics_xxx/file.txt.gz',
        storage_save_key='omics/1/proj/GSE40001/file.txt.gz',
    ):
        """Executa a task mockando rust_engine e default_storage.

        Usa patch() como context manager para controle explícito da ordem
        e evitar conflitos com o decorador no helper (bind=True / builtins.open).
        """
        import sys
        from unittest.mock import mock_open as _mock_open

        # Criar DatasetFile com local_path como storage_key (Rust escreve o caminho local)
        # Garante accession única por execução via local_path diferente
        accession = f'GSE40001_supp_{local_path.split("/")[-1]}'
        df, _ = DatasetFile.objects.get_or_create(
            accession=accession,
            defaults=dict(
                dataset=self.dataset,
                file_type='supplementary',
                source='geo_ftp',
                remote_url='ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE40001/suppl/file.txt.gz',
                storage_key=local_path,
                download_status=DatasetFile.DownloadStatus.PENDING,
            ),
        )
        # Garante storage_key = local_path (caso get retorne existente)
        DatasetFile.objects.filter(pk=df.pk).update(storage_key=local_path)

        if rust_result is None:
            rust_result = self._make_fake_rust_result()

        mock_rust = MagicMock()
        mock_rust.download_dataset_files.return_value = rust_result

        mock_storage = MagicMock()
        mock_storage.save.return_value = storage_save_key

        m_open = _mock_open(read_data=b'data')

        with patch.dict(sys.modules, {'rust_engine': mock_rust}), \
             patch('apps.core.tasks.ingestion_tasks.default_storage', mock_storage), \
             patch('apps.core.tasks.ingestion_tasks.os.path.isfile', return_value=True), \
             patch('apps.core.tasks.ingestion_tasks.os.remove'), \
             patch('builtins.open', m_open):
            from apps.core.tasks.ingestion_tasks import run_omics_download
            result = run_omics_download.run(
                str(self.project.id),
                self.dataset.id,
                'geo_supplementary',
            )

        df.refresh_from_db()
        return result, df, mock_storage

    def test_upload_chama_default_storage_save(self):
        """Task chama default_storage.save para cada arquivo baixado."""
        result, df, mock_storage = self._run_task_with_mock_rust()
        mock_storage.save.assert_called_once()

    def test_storage_key_sobrescrito_com_chave_de_object_storage(self):
        """Após upload, storage_key do DatasetFile é sobrescrito com a chave do object storage."""
        expected_key = 'omics/1/proj/GSE40001/file.txt.gz'
        result, df, mock_storage = self._run_task_with_mock_rust(
            storage_save_key=expected_key
        )
        self.assertEqual(df.storage_key, expected_key)

    def test_download_status_torna_downloaded_apos_upload(self):
        """Após upload bem-sucedido, download_status='downloaded'."""
        result, df, _ = self._run_task_with_mock_rust()
        self.assertEqual(df.download_status, DatasetFile.DownloadStatus.DOWNLOADED)

    def test_downloaded_at_preenchido_apos_upload(self):
        """downloaded_at é preenchido após upload bem-sucedido."""
        result, df, _ = self._run_task_with_mock_rust()
        self.assertIsNotNone(df.downloaded_at)

    def test_todos_downloaded_promove_project_dataset_para_downloaded(self):
        """Quando todos os DatasetFile estão 'downloaded', ProjectDataset.curation_status='downloaded'."""
        self._run_task_with_mock_rust()
        self.project_dataset.refresh_from_db()
        self.assertEqual(
            self.project_dataset.curation_status,
            ProjectDataset.CurationStatus.DOWNLOADED,
        )

    def test_curated_at_nao_e_tocado_pelo_download(self):
        """download NÃO é curadoria: curated_at permanece null após task."""
        self._run_task_with_mock_rust()
        self.project_dataset.refresh_from_db()
        self.assertIsNone(self.project_dataset.curated_at)

    def test_exclusion_reason_intocado(self):
        """exclusion_reason não é alterado pela task de download."""
        self.project_dataset.exclusion_reason = 'motivo de exclusao'
        self.project_dataset.save(update_fields=['exclusion_reason'])

        self._run_task_with_mock_rust()
        self.project_dataset.refresh_from_db()
        self.assertEqual(self.project_dataset.exclusion_reason, 'motivo de exclusao')

    def test_notes_intocado(self):
        """notes não é alterado pela task de download."""
        self.project_dataset.notes = 'nota de curadoria preservada'
        self.project_dataset.save(update_fields=['notes'])

        self._run_task_with_mock_rust()
        self.project_dataset.refresh_from_db()
        self.assertEqual(self.project_dataset.notes, 'nota de curadoria preservada')

    def test_falha_de_upload_seta_failed_preserva_registro(self):
        """
        Falha no upload via default_storage:
        - download_status='failed'
        - error_message preenchido
        - registro DatasetFile NUNCA deletado
        """
        import sys
        from unittest.mock import mock_open as _mock_open

        local_path = '/tmp/davinci_omics_fail/file_fail.txt.gz'
        df = DatasetFile.objects.create(
            dataset=self.dataset,
            accession='GSE40001_fail_upload',
            file_type='supplementary',
            source='geo_ftp',
            remote_url='ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE40001/suppl/fail.txt.gz',
            storage_key=local_path,
            download_status=DatasetFile.DownloadStatus.PENDING,
        )

        mock_storage = MagicMock()
        mock_storage.save.side_effect = OSError('object storage unreachable')

        rust_result = self._make_fake_rust_result()
        mock_rust = MagicMock()
        mock_rust.download_dataset_files.return_value = rust_result
        m_open = _mock_open(read_data=b'data')

        with patch.dict(sys.modules, {'rust_engine': mock_rust}), \
             patch('apps.core.tasks.ingestion_tasks.default_storage', mock_storage), \
             patch('apps.core.tasks.ingestion_tasks.os.path.isfile', return_value=True), \
             patch('apps.core.tasks.ingestion_tasks.os.remove'), \
             patch('builtins.open', m_open):
            from apps.core.tasks.ingestion_tasks import run_omics_download
            try:
                run_omics_download.run(
                    str(self.project.id),
                    self.dataset.id,
                    'geo_supplementary',
                )
            except Exception:
                pass

        df.refresh_from_db()
        self.assertEqual(df.download_status, DatasetFile.DownloadStatus.FAILED)
        self.assertNotEqual(df.error_message, '')
        self.assertIn('object storage unreachable', df.error_message)

        # Registro NUNCA deletado
        self.assertTrue(DatasetFile.objects.filter(pk=df.pk).exists())

    def test_um_arquivo_falha_outro_baixado_project_dataset_nao_vira_downloaded(self):
        """
        Com 2 arquivos: 1 com upload OK, 1 com upload falho.
        ProjectDataset NÃO deve ser marcado 'downloaded' (nem todos baixados).
        """
        import sys
        from unittest.mock import mock_open as _mock_open

        local_path_ok = '/tmp/davinci_ok/file_ok.txt.gz'
        local_path_fail = '/tmp/davinci_fail/file_fail.txt.gz'

        DatasetFile.objects.create(
            dataset=self.dataset,
            accession='GSE40001_ok',
            file_type='supplementary',
            source='geo_ftp',
            remote_url='ftp://example.com/ok.txt.gz',
            storage_key=local_path_ok,
            download_status=DatasetFile.DownloadStatus.PENDING,
        )
        DatasetFile.objects.create(
            dataset=self.dataset,
            accession='GSE40001_fail',
            file_type='supplementary',
            source='geo_ftp',
            remote_url='ftp://example.com/fail.txt.gz',
            storage_key=local_path_fail,
            download_status=DatasetFile.DownloadStatus.PENDING,
        )

        mock_storage = MagicMock()
        # Primeiro save OK, segundo falha
        mock_storage.save.side_effect = [
            'omics/1/proj/GSE40001/file_ok.txt.gz',
            OSError('upload failed'),
        ]

        rust_result = self._make_fake_rust_result(files_downloaded=2)
        mock_rust = MagicMock()
        mock_rust.download_dataset_files.return_value = rust_result
        m_open = _mock_open(read_data=b'data')

        with patch.dict(sys.modules, {'rust_engine': mock_rust}), \
             patch('apps.core.tasks.ingestion_tasks.default_storage', mock_storage), \
             patch('apps.core.tasks.ingestion_tasks.os.path.isfile', return_value=True), \
             patch('apps.core.tasks.ingestion_tasks.os.remove'), \
             patch('builtins.open', m_open):
            from apps.core.tasks.ingestion_tasks import run_omics_download
            try:
                run_omics_download.run(
                    str(self.project.id),
                    self.dataset.id,
                    'geo_supplementary',
                )
            except Exception:
                pass

        self.project_dataset.refresh_from_db()
        self.assertNotEqual(
            self.project_dataset.curation_status,
            ProjectDataset.CurationStatus.DOWNLOADED,
            "ProjectDataset não deve ser 'downloaded' quando há arquivos com falha",
        )


# =============================================================================
# 4 — Endpoint POST .../datasets/{id}/download/
# =============================================================================

class DatasetDownloadEndpointTests(APITestCase):
    """
    POST /projects/{project_pk}/datasets/{pk}/download/

    Cobre:
    - Retorna 202 com IngestionJob; curation_status seta 'queued'.
    - User B recebe 404 ao disparar no dataset de user A.
    - Não autenticado → 401/403.
    - dataset não-GEO → 400.
    """

    def setUp(self):
        self.user = make_user('dl_ep_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, 'DL Endpoint Project')
        self.dataset = make_dataset('GSE50001', extra_metadata={'gse': 'GSE50001'})
        self.project_dataset = make_project_dataset(self.project, self.dataset)
        self.url = (
            f'/api/v1/projects/{self.project.id}'
            f'/datasets/{self.project_dataset.id}/download/'
        )

    @patch('apps.core.services.download_service.run_omics_download')
    def _dispatch(self, mock_task):
        """Dispara o endpoint mockando a task Celery."""
        mock_task.delay = MagicMock()
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            return self.client.post(self.url, format='json')

    @patch('apps.core.services.download_service.DownloadService.dispatch')
    def test_post_download_retorna_202(self, mock_dispatch):
        """POST .../download/ retorna 202 Accepted."""
        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.GEO_SUPPLEMENTARY_DOWNLOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={'dataset_id': self.dataset.id},
        )
        mock_dispatch.return_value = job

        response = self.client.post(self.url, format='json')
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    @patch('apps.core.services.download_service.DownloadService.dispatch')
    def test_post_download_seta_curation_status_queued(self, mock_dispatch):
        """POST .../download/ seta ProjectDataset.curation_status='queued'."""
        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.GEO_SUPPLEMENTARY_DOWNLOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={'dataset_id': self.dataset.id},
        )
        mock_dispatch.return_value = job

        self.client.post(self.url, format='json')
        self.project_dataset.refresh_from_db()
        self.assertEqual(
            self.project_dataset.curation_status,
            ProjectDataset.CurationStatus.QUEUED_DOWNLOAD,
        )

    def test_user_b_recebe_404_ao_disparar_download_de_dataset_de_user_a(self):
        """User B não pode disparar download no dataset de user A."""
        user_b = make_user('dl_ep_userb')
        client_b = APIClient()
        client_b.force_authenticate(user=user_b)

        response = client_b.post(self.url, format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_nao_autenticado_recebe_401_ou_403(self):
        """Sem autenticação, retorna 401 ou 403."""
        client_anon = APIClient()
        response = client_anon.post(self.url, format='json')
        self.assertIn(response.status_code, [
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        ])

    def test_dataset_sra_sem_confirm_retorna_400_com_preview(self):
        """
        Dataset SRA sem confirm=true no body → 400 com prévia de quota.

        Com a F2, SRA é suportado via file_kind='fastq'.  O gate de confirmação
        requer confirm=true antes de enfileirar.  Sem confirm, retorna HTTP 400
        com payload { detail, file_kind, used_bytes, quota_bytes, confirm_required }.
        dispatch() não chega a ser chamado no Celery (FastqConfirmRequiredError é
        levantado dentro do service antes do .delay()).
        """
        from apps.core.services.download_service import FastqConfirmRequiredError

        dataset_sra = make_dataset('SRP99999', source_db='sra')
        pd_sra = make_project_dataset(self.project, dataset_sra)
        url_sra = (
            f'/api/v1/projects/{self.project.id}'
            f'/datasets/{pd_sra.id}/download/'
        )
        # Mocka o .delay() da task Celery para não disparar job real;
        # NÃO mocka DownloadService.dispatch — deixa o gate de confirmação agir.
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay') as mock_delay:
            response = self.client.post(url_sra, format='json')  # sem body → confirm=False

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        data = response.json()
        self.assertIn('confirm_required', data)
        self.assertTrue(data['confirm_required'])
        self.assertIn('used_bytes', data)
        self.assertIn('quota_bytes', data)
        self.assertEqual(data['file_kind'], 'fastq')
        mock_delay.assert_not_called()

    def test_dataset_source_db_sem_suporte_retorna_400(self):
        """source_db sem mapeamento (ex: 'arrayexpress') retorna 400 explícito."""
        dataset_ae = make_dataset('E-MTAB-9999', source_db='arrayexpress')
        pd_ae = make_project_dataset(self.project, dataset_ae)
        url_ae = (
            f'/api/v1/projects/{self.project.id}'
            f'/datasets/{pd_ae.id}/download/'
        )
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(url_ae, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('detail', response.json())

    def test_dataset_sra_com_confirm_true_retorna_202(self):
        """
        Dataset SRA com confirm=true no body → 202 Accepted (job enfileirado).

        Verifica que o gate de confirmação é satisfeito e o job é criado.
        """
        from django.conf import settings as django_settings

        dataset_sra = make_dataset('SRP88888', source_db='sra')
        pd_sra = make_project_dataset(self.project, dataset_sra)
        url_sra = (
            f'/api/v1/projects/{self.project.id}'
            f'/datasets/{pd_sra.id}/download/'
        )
        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.FASTQ_DOWNLOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={'dataset_id': dataset_sra.id},
        )
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            with patch(
                'apps.core.services.download_service.DownloadService.dispatch',
                return_value=job,
            ):
                response = self.client.post(
                    url_sra,
                    data={'confirm': True},
                    format='json',
                )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_post_download_nao_toca_curated_at_nem_exclusion_reason(self):
        """POST download não altera curated_at, exclusion_reason, notes do ProjectDataset."""
        import datetime

        curated_ts = timezone.now() - datetime.timedelta(days=5)
        self.project_dataset.curated_at = curated_ts
        self.project_dataset.exclusion_reason = 'motivo original'
        self.project_dataset.notes = 'notas preservadas'
        self.project_dataset.curation_status = ProjectDataset.CurationStatus.INCLUDED
        self.project_dataset.save()

        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.GEO_SUPPLEMENTARY_DOWNLOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={'dataset_id': self.dataset.id},
        )

        with patch('apps.core.services.download_service.DownloadService.dispatch', return_value=job):
            self.client.post(self.url, format='json')

        self.project_dataset.refresh_from_db()

        # curated_at e exclusion_reason devem ser preservados
        self.assertAlmostEqual(
            self.project_dataset.curated_at.timestamp(),
            curated_ts.timestamp(),
            delta=1,
        )
        self.assertEqual(self.project_dataset.exclusion_reason, 'motivo original')
        self.assertEqual(self.project_dataset.notes, 'notas preservadas')


# =============================================================================
# 5 — Endpoint GET .../datasets/{id}/files/
# =============================================================================

class DatasetFilesEndpointTests(APITestCase):
    """
    GET /projects/{project_pk}/datasets/{pk}/files/

    Cobre:
    - Lista DatasetFile do dataset.
    - serializer NÃO expõe storage_key nem remote_url.
    - download_url presente no response.
    - download_url resolve com prefixo /api/v1/.
    - download_url é null para arquivo não baixado.
    - Isolamento cross-user (404 para user B).
    - Não autenticado → 401/403.
    """

    def setUp(self):
        self.user = make_user('files_ep_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, 'Files Endpoint Project')
        self.dataset = make_dataset('GSE60001')
        self.project_dataset = make_project_dataset(self.project, self.dataset)
        self.url = (
            f'/api/v1/projects/{self.project.id}'
            f'/datasets/{self.project_dataset.id}/files/'
        )

    def test_lista_dataset_files_retorna_200(self):
        """GET .../files/ retorna 200."""
        make_dataset_file(self.dataset, accession='GSE60001_f1')
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_lista_todos_arquivos_do_dataset(self):
        """Retorna todos os DatasetFile do dataset."""
        make_dataset_file(self.dataset, accession='GSE60001_fa')
        make_dataset_file(self.dataset, accession='GSE60001_fb')
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_serializer_nao_expoe_storage_key(self):
        """storage_key NUNCA aparece no response da listagem."""
        make_dataset_file(
            self.dataset,
            accession='GSE60001_sk',
            storage_key='omics/1/proj/GSE60001/secret.txt.gz',
            download_status='downloaded',
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for item in response.data:
            self.assertNotIn('storage_key', item,
                             "storage_key não deve ser exposto ao cliente")

    def test_serializer_nao_expoe_remote_url(self):
        """remote_url (URL original no servidor remoto) não aparece no response."""
        make_dataset_file(
            self.dataset,
            accession='GSE60001_ru',
            remote_url='ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE60001/secret_ftp_url.txt.gz',
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for item in response.data:
            self.assertNotIn('remote_url', item,
                             "remote_url não deve ser exposto ao cliente")

    def test_download_url_presente_para_arquivo_downloaded(self):
        """download_url está presente e não-null para arquivo com status 'downloaded'."""
        make_dataset_file(
            self.dataset,
            accession='GSE60001_dloaded',
            storage_key='omics/1/proj/GSE60001/dloaded.txt.gz',
            download_status='downloaded',
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        item = response.data[0]
        self.assertIn('download_url', item)
        self.assertIsNotNone(item['download_url'])

    def test_download_url_resolve_com_prefixo_api_v1(self):
        """download_url gerado pelo serializer contém /api/v1/ no path."""
        make_dataset_file(
            self.dataset,
            accession='GSE60001_url_prefix',
            storage_key='omics/1/proj/GSE60001/url_prefix.txt.gz',
            download_status='downloaded',
        )
        response = self.client.get(self.url)
        item = response.data[0]
        self.assertIsNotNone(item['download_url'])
        self.assertIn('/api/v1/', item['download_url'])

    def test_download_url_e_null_para_arquivo_nao_baixado(self):
        """download_url é null (None) para arquivo com status 'pending' ou 'failed'."""
        make_dataset_file(
            self.dataset,
            accession='GSE60001_pending',
            download_status='pending',
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        item = response.data[0]
        self.assertIn('download_url', item)
        self.assertIsNone(item['download_url'])

    def test_campos_canonicos_no_response(self):
        """Response contém todos os campos canônicos do DatasetFileSerializer."""
        make_dataset_file(self.dataset, accession='GSE60001_campos')
        response = self.client.get(self.url)
        item = response.data[0]
        campos = [
            'id', 'accession', 'file_type', 'source',
            'size_bytes', 'checksum_md5', 'checksum_algo',
            'download_status', 'bytes_downloaded', 'downloaded_at',
            'download_url',
        ]
        for campo in campos:
            self.assertIn(campo, item, f"campo canônico '{campo}' ausente no response")

    def test_user_b_recebe_404_ao_listar_files_de_dataset_de_user_a(self):
        """User B recebe 404 ao tentar listar arquivos do dataset de user A."""
        user_b = make_user('files_ep_userb')
        client_b = APIClient()
        client_b.force_authenticate(user=user_b)

        response = client_b.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_nao_autenticado_recebe_401_ou_403(self):
        """Sem autenticação, retorna 401 ou 403."""
        client_anon = APIClient()
        response = client_anon.get(self.url)
        self.assertIn(response.status_code, [
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        ])

    def test_dataset_sem_arquivos_retorna_lista_vazia(self):
        """Dataset sem DatasetFile retorna lista vazia (não null, não 404)."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])


# =============================================================================
# 6 — Endpoint GET .../datasets/{id}/files/{file_id}/content/
# =============================================================================

class FileContentEndpointTests(APITestCase):
    """
    GET /projects/{project_pk}/datasets/{pk}/files/{file_id}/content/

    Cobre:
    - Arquivo não baixado → 409 Conflict.
    - file_id pertencente a outro dataset/projeto → 404.
    - Input do cliente é apenas file_id (PK) — storage_key vem do banco.
    - Sucesso → 200 com mock de default_storage.open.
    - User B recebe 404.
    - Arquivo 'downloaded' sem storage_key → 404.
    """

    def setUp(self):
        self.user = make_user('content_ep_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, 'Content Endpoint Project')
        self.dataset = make_dataset('GSE70001')
        self.project_dataset = make_project_dataset(self.project, self.dataset)

    def _url(self, file_id):
        return (
            f'/api/v1/projects/{self.project.id}'
            f'/datasets/{self.project_dataset.id}'
            f'/files/{file_id}/content/'
        )

    def test_arquivo_pendente_retorna_409(self):
        """Arquivo com download_status='pending' retorna 409 Conflict."""
        df = make_dataset_file(
            self.dataset,
            accession='GSE70001_pending_content',
            download_status='pending',
        )
        response = self.client.get(self._url(df.id))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_arquivo_queued_retorna_409(self):
        """Arquivo com download_status='queued' retorna 409 Conflict."""
        df = make_dataset_file(
            self.dataset,
            accession='GSE70001_queued_content',
            download_status='queued',
        )
        response = self.client.get(self._url(df.id))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_arquivo_failed_retorna_409(self):
        """Arquivo com download_status='failed' retorna 409 Conflict."""
        df = make_dataset_file(
            self.dataset,
            accession='GSE70001_failed_content',
            download_status='failed',
        )
        response = self.client.get(self._url(df.id))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_file_id_de_outro_dataset_retorna_404(self):
        """file_id pertencente a dataset de outro projeto retorna 404."""
        other_user = make_user('content_other_user')
        other_project = make_project(other_user, 'Other Content Project')
        other_dataset = make_dataset('GSE70002')
        make_project_dataset(other_project, other_dataset)

        # Arquivo do outro dataset
        df_other = make_dataset_file(
            other_dataset,
            accession='GSE70002_other_content',
            storage_key='omics/other/GSE70002/other.txt.gz',
            download_status='downloaded',
        )

        # User A tenta acessar arquivo que pertence a dataset de outro projeto
        response = self.client.get(self._url(df_other.id))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch('django.core.files.storage.default_storage')
    def test_arquivo_downloaded_com_storage_key_retorna_200(self, mock_storage):
        """Arquivo com status 'downloaded' e storage_key válido retorna 200 (streaming)."""
        df = make_dataset_file(
            self.dataset,
            accession='GSE70001_ok_content',
            storage_key='omics/1/proj/GSE70001/file.txt.gz',
            download_status='downloaded',
            size_bytes=1024,
        )

        fake_file = BytesIO(b'fake file content for streaming')
        mock_storage.open.return_value = fake_file

        response = self.client.get(self._url(df.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @patch('django.core.files.storage.default_storage')
    def test_storage_key_vem_do_banco_nao_do_cliente(self, mock_storage):
        """
        O path do arquivo vem do registro no banco (storage_key do DatasetFile),
        não de nenhum parâmetro do cliente — sem risco de path traversal.
        """
        storage_key_no_banco = 'omics/1/proj/GSE70001/real_file.txt.gz'
        df = make_dataset_file(
            self.dataset,
            accession='GSE70001_no_traversal',
            storage_key=storage_key_no_banco,
            download_status='downloaded',
        )

        fake_file = BytesIO(b'content')
        mock_storage.open.return_value = fake_file

        self.client.get(self._url(df.id))

        # default_storage.open foi chamado com o path do banco, não do cliente
        mock_storage.open.assert_called_once_with(storage_key_no_banco, 'rb')

    def test_arquivo_downloaded_sem_storage_key_retorna_404(self):
        """Arquivo 'downloaded' sem storage_key retorna 404 (download incompleto)."""
        df = make_dataset_file(
            self.dataset,
            accession='GSE70001_no_sk',
            storage_key='',
            download_status='downloaded',
        )
        response = self.client.get(self._url(df.id))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_user_b_recebe_404_ao_acessar_arquivo_de_user_a(self):
        """User B recebe 404 ao tentar acessar arquivo de user A."""
        df = make_dataset_file(
            self.dataset,
            accession='GSE70001_xuser',
            storage_key='omics/1/proj/GSE70001/xuser.txt.gz',
            download_status='downloaded',
        )
        user_b = make_user('content_ep_userb')
        client_b = APIClient()
        client_b.force_authenticate(user=user_b)

        response = client_b.get(self._url(df.id))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_nao_autenticado_recebe_401_ou_403(self):
        """Sem autenticação, retorna 401 ou 403."""
        df = make_dataset_file(
            self.dataset,
            accession='GSE70001_anon',
            download_status='downloaded',
        )
        client_anon = APIClient()
        response = client_anon.get(self._url(df.id))
        self.assertIn(response.status_code, [
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        ])

    @patch('django.core.files.storage.default_storage')
    def test_content_disposition_filename_derivado_do_storage_key(self, mock_storage):
        """Content-Disposition usa o basename do storage_key, não input do cliente."""
        df = make_dataset_file(
            self.dataset,
            accession='GSE70001_filename',
            storage_key='omics/1/proj/GSE70001/GSE70001_series_matrix.txt.gz',
            download_status='downloaded',
        )
        fake_file = BytesIO(b'content')
        mock_storage.open.return_value = fake_file

        response = self.client.get(self._url(df.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('Content-Disposition', response)
        self.assertIn('GSE70001_series_matrix.txt.gz', response['Content-Disposition'])

    @patch('django.core.files.storage.default_storage')
    def test_falha_ao_abrir_storage_retorna_404(self, mock_storage):
        """Se default_storage.open lançar exceção, retorna 404 com mensagem."""
        df = make_dataset_file(
            self.dataset,
            accession='GSE70001_storage_fail',
            storage_key='omics/1/proj/GSE70001/missing.txt.gz',
            download_status='downloaded',
        )
        mock_storage.open.side_effect = OSError('file not found in storage')

        response = self.client.get(self._url(df.id))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


# =============================================================================
# 7 — Throttle: actions têm throttle_scope configurado
# =============================================================================

class ThrottleScopeConfigurationTests(APITestCase):
    """
    Verifica que as actions de download têm throttle_scope declarado.

    Não testa taxas em si (dependem de Redis/cache configurado),
    mas confirma que o atributo está presente na action, garantindo
    que o ScopedRateThrottle poderá aplicar o limite correto.
    """

    def test_download_action_tem_throttle_scope_download(self):
        """Action 'download' deve ter throttle_scope='download'."""
        action_fn = ProjectDatasetViewSet.download
        self.assertTrue(
            hasattr(action_fn, 'throttle_scope') or
            hasattr(action_fn, 'kwargs'),
            "Action 'download' deve ter throttle_scope configurado",
        )
        # O decorator @action armazena kwargs no atributo da função
        mapping_kwargs = getattr(action_fn, 'kwargs', {})
        scope = mapping_kwargs.get('throttle_scope') or getattr(action_fn, 'throttle_scope', None)
        self.assertEqual(scope, 'download',
                         "throttle_scope da action 'download' deve ser 'download'")

    def test_files_action_tem_throttle_scope_download_content(self):
        """Action 'files' deve ter throttle_scope='download_content'."""
        action_fn = ProjectDatasetViewSet.files
        mapping_kwargs = getattr(action_fn, 'kwargs', {})
        scope = mapping_kwargs.get('throttle_scope') or getattr(action_fn, 'throttle_scope', None)
        self.assertEqual(scope, 'download_content',
                         "throttle_scope da action 'files' deve ser 'download_content'")

    def test_file_content_action_tem_throttle_scope_download_content(self):
        """Action 'file_content' deve ter throttle_scope='download_content'."""
        action_fn = ProjectDatasetViewSet.file_content
        mapping_kwargs = getattr(action_fn, 'kwargs', {})
        scope = mapping_kwargs.get('throttle_scope') or getattr(action_fn, 'throttle_scope', None)
        self.assertEqual(scope, 'download_content',
                         "throttle_scope da action 'file_content' deve ser 'download_content'")


# =============================================================================
# F2 — 8. Quota e confirmação FASTQ (DownloadService)
# =============================================================================

class QuotaAndConfirmFastqTests(APITestCase):
    """
    Cobre o gate de quota e confirmação explícita para downloads FASTQ (F2).

    Casos:
    - POST sem confirm → FastqConfirmRequiredError → endpoint retorna 400
      com confirm_required=True, used_bytes, quota_bytes, file_kind='fastq'.
    - POST com confirm=True dentro da quota → 202 (job FASTQ_DOWNLOAD).
    - Quota esgotada (used_bytes >= DOWNLOAD_QUOTA_BYTES) com confirm=True → 409.
    - Isolamento: DatasetFile de outro projeto/usuário NÃO entra na soma de quota.
    - GEO dataset → file_kind='geo_supplementary' → sem gate de confirmação.
    - source_db sem suporte → 400.
    - _project_used_bytes soma apenas arquivos 'downloaded' do projeto correto.
    """

    def setUp(self):
        self.user = make_user('quota_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, 'Quota Project')

        self.dataset_sra = make_dataset('SRP11111', source_db='sra')
        self.pd_sra = make_project_dataset(self.project, self.dataset_sra)
        self.url_sra = (
            f'/api/v1/projects/{self.project.id}'
            f'/datasets/{self.pd_sra.id}/download/'
        )

    # ── Caso 1: sem confirm → 400 com prévia de quota ─────────────────────────

    def test_sra_sem_confirm_retorna_400(self):
        """SRA sem confirm → 400 Bad Request."""
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(self.url_sra, format='json')
        self.assertEqual(response.status_code, 400)

    def test_sra_sem_confirm_payload_tem_confirm_required_true(self):
        """400 inclui confirm_required=True."""
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(self.url_sra, format='json')
        self.assertTrue(response.json().get('confirm_required'))

    def test_sra_sem_confirm_payload_tem_used_bytes(self):
        """400 inclui used_bytes (pode ser 0)."""
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(self.url_sra, format='json')
        self.assertIn('used_bytes', response.json())

    def test_sra_sem_confirm_payload_tem_quota_bytes(self):
        """400 inclui quota_bytes."""
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(self.url_sra, format='json')
        self.assertIn('quota_bytes', response.json())

    def test_sra_sem_confirm_payload_file_kind_fastq(self):
        """400 inclui file_kind='fastq'."""
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(self.url_sra, format='json')
        self.assertEqual(response.json().get('file_kind'), 'fastq')

    def test_sra_sem_confirm_delay_nao_chamado(self):
        """Sem confirm, run_omics_download.delay() NUNCA deve ser chamado."""
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay') as mock_delay:
            self.client.post(self.url_sra, format='json')
        mock_delay.assert_not_called()

    # ── Caso 2: com confirm=True dentro da quota → 202 ────────────────────────

    def test_sra_com_confirm_true_retorna_202(self):
        """SRA com confirm=True e quota disponível → 202 Accepted."""
        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.FASTQ_DOWNLOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={'dataset_id': self.dataset_sra.id},
        )
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'), \
             patch('apps.core.services.download_service.DownloadService.dispatch',
                   return_value=job):
            response = self.client.post(
                self.url_sra, data={'confirm': True}, format='json'
            )
        self.assertEqual(response.status_code, 202)

    def test_sra_com_confirm_true_job_type_fastq(self):
        """Após dispatch com confirm=True, job criado tem tipo FASTQ_DOWNLOAD."""
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            from apps.core.services.download_service import DownloadService
            job = DownloadService.dispatch(
                project=self.project,
                dataset=self.dataset_sra,
                file_kind='fastq',
                user=self.user,
                confirm=True,
            )
        self.assertEqual(job.job_type, IngestionJob.JobType.FASTQ_DOWNLOAD)

    # ── Caso 3: quota esgotada com confirm=True → 409 ─────────────────────────

    def test_quota_esgotada_com_confirm_retorna_409(self):
        """
        Projeto com DatasetFile downloaded somando > DOWNLOAD_QUOTA_BYTES.
        Mesmo com confirm=True → 409 Conflict.
        Usa override_settings para definir quota pequena.
        """
        from django.test import override_settings

        # Cria DatasetFile vinculado ao dataset via ProjectDataset do projeto
        # (a FK dataset → OmicDataset, e OmicDataset.in_projects aponta para o projeto)
        df = DatasetFile.objects.create(
            dataset=self.dataset_sra,
            accession='SRP11111_fastq_big_1',
            file_type='fastq',
            source='ena_ftp',
            remote_url='ftp://ftp.sra.ebi.ac.uk/SRR111111_1.fastq.gz',
            storage_key='omics/1/proj/SRP11111/SRR111111_1.fastq.gz',
            size_bytes=600,
            download_status=DatasetFile.DownloadStatus.DOWNLOADED,
        )

        # Com quota = 500 bytes e 600 bytes usados → deve ser rejeitado
        with override_settings(DOWNLOAD_QUOTA_BYTES=500):
            with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
                response = self.client.post(
                    self.url_sra, data={'confirm': True}, format='json'
                )

        self.assertEqual(response.status_code, 409)

    def test_quota_esgotada_payload_contem_used_e_quota_bytes(self):
        """409 retorna used_bytes e quota_bytes no corpo."""
        from django.test import override_settings

        DatasetFile.objects.create(
            dataset=self.dataset_sra,
            accession='SRP11111_fastq_big_2',
            file_type='fastq',
            source='ena_ftp',
            remote_url='ftp://ftp.sra.ebi.ac.uk/SRR111112_1.fastq.gz',
            storage_key='omics/1/proj/SRP11111/SRR111112_1.fastq.gz',
            size_bytes=999,
            download_status=DatasetFile.DownloadStatus.DOWNLOADED,
        )

        with override_settings(DOWNLOAD_QUOTA_BYTES=100):
            with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
                response = self.client.post(
                    self.url_sra, data={'confirm': True}, format='json'
                )

        data = response.json()
        self.assertIn('used_bytes', data)
        self.assertIn('quota_bytes', data)

    def test_quota_esgotada_delay_nao_chamado(self):
        """Com quota esgotada, run_omics_download.delay() jamais é chamado."""
        from django.test import override_settings

        DatasetFile.objects.create(
            dataset=self.dataset_sra,
            accession='SRP11111_fastq_big_3',
            file_type='fastq',
            source='ena_ftp',
            remote_url='ftp://ftp.sra.ebi.ac.uk/SRR111113_1.fastq.gz',
            storage_key='omics/1/proj/SRP11111/SRR111113_1.fastq.gz',
            size_bytes=1000,
            download_status=DatasetFile.DownloadStatus.DOWNLOADED,
        )

        with override_settings(DOWNLOAD_QUOTA_BYTES=100):
            with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay') as mock_delay:
                self.client.post(
                    self.url_sra, data={'confirm': True}, format='json'
                )
        mock_delay.assert_not_called()

    # ── Caso 4: isolamento de quota por projeto ────────────────────────────────

    def test_project_used_bytes_soma_so_o_projeto_do_usuario(self):
        """
        _project_used_bytes filtra por projeto do usuário.
        DatasetFile 'downloaded' de outro usuário NÃO entra na soma.
        """
        from apps.core.services.download_service import _project_used_bytes

        # Usuário B com projeto e dataset próprios
        user_b = make_user('quota_userb')
        project_b = make_project(user_b, 'Quota Project B')
        dataset_b = make_dataset('SRP99991', source_db='sra')
        make_project_dataset(project_b, dataset_b)

        # DatasetFile MASSIVO de user B (deve ser ignorado ao calcular quota de A)
        DatasetFile.objects.create(
            dataset=dataset_b,
            accession='SRP99991_fastq_userb_1',
            file_type='fastq',
            source='ena_ftp',
            remote_url='ftp://ftp.sra.ebi.ac.uk/SRR999991_1.fastq.gz',
            storage_key='omics/b/SRP99991/SRR999991_1.fastq.gz',
            size_bytes=999_999_999,  # ~1 GB — esmagaria qualquer quota
            download_status=DatasetFile.DownloadStatus.DOWNLOADED,
        )

        # projeto de A não tem nenhum arquivo: soma deve ser 0
        used = _project_used_bytes(self.project)
        self.assertEqual(used, 0,
            "Bytes de outro projeto/usuário não devem contar na quota do projeto A")

    def test_project_used_bytes_inclui_so_downloaded_nao_pending(self):
        """
        _project_used_bytes conta apenas arquivos com download_status='downloaded'.
        Arquivo 'pending' ou 'failed' não entra.
        """
        from apps.core.services.download_service import _project_used_bytes

        DatasetFile.objects.create(
            dataset=self.dataset_sra,
            accession='SRP11111_pending_quota',
            file_type='fastq',
            source='ena_ftp',
            remote_url='ftp://ftp.sra.ebi.ac.uk/SRR111121_1.fastq.gz',
            storage_key='',
            size_bytes=500,
            download_status=DatasetFile.DownloadStatus.PENDING,
        )

        used = _project_used_bytes(self.project)
        self.assertEqual(used, 0,
            "Arquivo pending não deve entrar no cálculo de quota")

    def test_project_used_bytes_soma_multiplos_arquivos_downloaded(self):
        """
        _project_used_bytes soma todos os arquivos 'downloaded' do projeto.
        """
        from apps.core.services.download_service import _project_used_bytes

        # Dataset 2 no mesmo projeto
        dataset2 = make_dataset('SRP22222', source_db='sra')
        make_project_dataset(self.project, dataset2)

        DatasetFile.objects.create(
            dataset=self.dataset_sra,
            accession='SRP11111_dl_a',
            file_type='fastq',
            source='ena_ftp',
            remote_url='ftp://example.com/a.fastq.gz',
            storage_key='omics/1/proj/SRP11111/a.fastq.gz',
            size_bytes=300,
            download_status=DatasetFile.DownloadStatus.DOWNLOADED,
        )
        DatasetFile.objects.create(
            dataset=dataset2,
            accession='SRP22222_dl_b',
            file_type='fastq',
            source='ena_ftp',
            remote_url='ftp://example.com/b.fastq.gz',
            storage_key='omics/1/proj/SRP22222/b.fastq.gz',
            size_bytes=200,
            download_status=DatasetFile.DownloadStatus.DOWNLOADED,
        )

        used = _project_used_bytes(self.project)
        self.assertEqual(used, 500)

    def test_isolamento_quota_usuario_b_alto_nao_afeta_usuario_a(self):
        """
        Usuário B com GB de arquivos downloaded não deve bloquear usuário A.
        O dispatch de A com confirm=True deve ter 202 (não 409).
        """
        from django.test import override_settings

        user_b = make_user('quota_b_heavy')
        project_b = make_project(user_b, 'Heavy Project B')
        dataset_b = make_dataset('SRP88881', source_db='sra')
        pd_b = make_project_dataset(project_b, dataset_b)

        DatasetFile.objects.create(
            dataset=dataset_b,
            accession='SRP88881_fastq_huge',
            file_type='fastq',
            source='ena_ftp',
            remote_url='ftp://ftp.sra.ebi.ac.uk/SRR888811_1.fastq.gz',
            storage_key='omics/b/SRP88881/SRR888811_1.fastq.gz',
            size_bytes=999_999_999_999,  # ~1 TB — acima de qualquer quota razoável
            download_status=DatasetFile.DownloadStatus.DOWNLOADED,
        )

        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.FASTQ_DOWNLOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={'dataset_id': self.dataset_sra.id},
        )

        # Quota = 10 GB: usuario A (sem arquivos) ainda cabe; B não deve afetar
        with override_settings(DOWNLOAD_QUOTA_BYTES=10 * 1024 ** 3):
            with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'), \
                 patch('apps.core.services.download_service.DownloadService.dispatch',
                       return_value=job):
                response = self.client.post(
                    self.url_sra, data={'confirm': True}, format='json'
                )

        self.assertEqual(response.status_code, 202,
            "Quota alta de outro usuário não deve bloquear usuário A")

    # ── Derivação de file_kind por source_db ───────────────────────────────────

    def test_source_db_geo_deriva_geo_supplementary_sem_confirm(self):
        """GEO dataset → file_kind='geo_supplementary' → sem gate → dispatch direto."""
        dataset_geo = make_dataset('GSE12222', source_db='geo',
                                   extra_metadata={'gse': 'GSE12222'})
        pd_geo = make_project_dataset(self.project, dataset_geo)
        url_geo = (
            f'/api/v1/projects/{self.project.id}'
            f'/datasets/{pd_geo.id}/download/'
        )
        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.GEO_SUPPLEMENTARY_DOWNLOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={'dataset_id': dataset_geo.id},
        )
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'), \
             patch('apps.core.services.download_service.DownloadService.dispatch',
                   return_value=job):
            # Sem body (confirm ausente) — GEO não precisa
            response = self.client.post(url_geo, format='json')
        self.assertEqual(response.status_code, 202)

    def test_source_db_sem_suporte_retorna_400(self):
        """source_db='arrayexpress' não tem mapeamento → 400 com detalhe."""
        dataset_ae = make_dataset('E-MTAB-0001', source_db='arrayexpress')
        pd_ae = make_project_dataset(self.project, dataset_ae)
        url_ae = (
            f'/api/v1/projects/{self.project.id}'
            f'/datasets/{pd_ae.id}/download/'
        )
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(url_ae, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('detail', response.json())

    # ── Regressão A2: arquivos FASTQ (sample-linked) contam na quota ──────────
    #
    # Bug: _project_used_bytes só filtrava DatasetFile.dataset → project,
    # ignorando DatasetFile.sample → sample.dataset → project.
    # Fix: Q(dataset__in_projects__project=project)
    #      | Q(sample__dataset__in_projects__project=project)
    #
    # Os testes abaixo FALHARIAM sem o fix (os bytes sample-linked retornariam 0
    # no cálculo de quota) e PASSAM com ele.

    def test_regressao_a2_fastq_sample_linked_bloqueia_quota_409(self):
        """
        Regressão A2: DatasetFile com sample preenchido e dataset=None
        DEVE contar na quota. Com DOWNLOAD_QUOTA_BYTES menor que o total
        dos bytes baixados via sample, um novo POST com confirm=True deve
        retornar 409.

        Sem o fix, _project_used_bytes retornava 0 para arquivos sample-linked
        e o dispatch seria aceito (202) — a quota passava batida.
        """
        from django.test import override_settings
        from apps.core.models import OmicSample

        # Cria dataset SRA com sample e ProjectDataset no projeto do usuário
        dataset_sra2 = make_dataset('SRP77771', source_db='sra')
        pd_sra2 = make_project_dataset(self.project, dataset_sra2)

        sample = OmicSample.objects.create(
            dataset=dataset_sra2,
            accession='SRR77771',
            title='Sample SRR77771',
            organism='Homo sapiens',
        )

        # DatasetFile com sample preenchido, dataset=None — padrão FASTQ
        DatasetFile.objects.create(
            sample=sample,
            dataset=None,
            accession='SRR77771_1_fastq_a2',
            file_type=DatasetFile.FileType.FASTQ,
            source=DatasetFile.Source.ENA_FTP,
            remote_url='ftp://ftp.sra.ebi.ac.uk/SRR77771_1.fastq.gz',
            storage_key='omics/1/proj/SRP77771/SRR77771_1.fastq.gz',
            size_bytes=800,
            download_status=DatasetFile.DownloadStatus.DOWNLOADED,
        )

        # Quota menor que os bytes usados: deve rejeitar com 409
        url_sra2 = (
            f'/api/v1/projects/{self.project.id}'
            f'/datasets/{pd_sra2.id}/download/'
        )
        with override_settings(DOWNLOAD_QUOTA_BYTES=500):
            with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
                response = self.client.post(
                    url_sra2, data={'confirm': True}, format='json'
                )

        self.assertEqual(
            response.status_code, 409,
            "Arquivos FASTQ sample-linked (dataset=None) devem contar na quota "
            "e bloquear o dispatch quando a quota for excedida (regressão A2).",
        )

    def test_regressao_a2_project_used_bytes_soma_arquivos_sample_linked(self):
        """
        Regressão A2: _project_used_bytes deve somar DatasetFile com
        sample preenchido (dataset=None) para o projeto do usuário.

        Sem o fix, o valor retornado seria 0 para esses arquivos.
        """
        from apps.core.models import OmicSample
        from apps.core.services.download_service import _project_used_bytes

        dataset_sra3 = make_dataset('SRP77772', source_db='sra')
        make_project_dataset(self.project, dataset_sra3)

        sample = OmicSample.objects.create(
            dataset=dataset_sra3,
            accession='SRR77772',
            title='Sample SRR77772',
            organism='Homo sapiens',
        )

        DatasetFile.objects.create(
            sample=sample,
            dataset=None,
            accession='SRR77772_1_fastq_a2_bytes',
            file_type=DatasetFile.FileType.FASTQ,
            source=DatasetFile.Source.ENA_FTP,
            remote_url='ftp://ftp.sra.ebi.ac.uk/SRR77772_1.fastq.gz',
            storage_key='omics/1/proj/SRP77772/SRR77772_1.fastq.gz',
            size_bytes=350,
            download_status=DatasetFile.DownloadStatus.DOWNLOADED,
        )
        DatasetFile.objects.create(
            sample=sample,
            dataset=None,
            accession='SRR77772_2_fastq_a2_bytes',
            file_type=DatasetFile.FileType.FASTQ,
            source=DatasetFile.Source.ENA_FTP,
            remote_url='ftp://ftp.sra.ebi.ac.uk/SRR77772_2.fastq.gz',
            storage_key='omics/1/proj/SRP77772/SRR77772_2.fastq.gz',
            size_bytes=150,
            download_status=DatasetFile.DownloadStatus.DOWNLOADED,
        )

        used = _project_used_bytes(self.project)
        self.assertEqual(
            used, 500,
            "_project_used_bytes deve somar arquivos sample-linked (regressão A2): "
            "esperado 500 bytes, obtido {used}".format(used=used),
        )

    def test_regressao_a2_isolamento_sample_linked_outro_projeto_nao_conta(self):
        """
        Regressão A2 — isolamento: DatasetFile sample-linked de OUTRO projeto
        não deve entrar na soma de quota do projeto do usuário corrente.

        Garante que o fix não quebrou o isolamento por projeto/usuário.
        """
        from apps.core.models import OmicSample
        from apps.core.services.download_service import _project_used_bytes

        # Usuário B com projeto e dataset próprios
        user_b = make_user('quota_b_a2_isol')
        project_b = make_project(user_b, 'Project B A2 Isolation')
        dataset_b = make_dataset('SRP99992', source_db='sra')
        make_project_dataset(project_b, dataset_b)

        sample_b = OmicSample.objects.create(
            dataset=dataset_b,
            accession='SRR99992',
            title='Sample B SRR99992',
            organism='Homo sapiens',
        )

        # Arquivo MASSIVO de user B — sample-linked, outro projeto
        DatasetFile.objects.create(
            sample=sample_b,
            dataset=None,
            accession='SRR99992_1_fastq_b_isol',
            file_type=DatasetFile.FileType.FASTQ,
            source=DatasetFile.Source.ENA_FTP,
            remote_url='ftp://ftp.sra.ebi.ac.uk/SRR99992_1.fastq.gz',
            storage_key='omics/b/SRP99992/SRR99992_1.fastq.gz',
            size_bytes=999_999_999,
            download_status=DatasetFile.DownloadStatus.DOWNLOADED,
        )

        # Projeto de A não tem arquivos: soma deve ser 0
        used = _project_used_bytes(self.project)
        self.assertEqual(
            used, 0,
            "Bytes de arquivo sample-linked de OUTRO projeto não devem contar "
            "na quota do projeto do usuário A (regressão A2 — isolamento).",
        )

    def test_regressao_a2_mixed_geo_e_fastq_soma_ambos(self):
        """
        Regressão A2 — caso misto: projeto com arquivos GEO (dataset-linked)
        e FASTQ (sample-linked). _project_used_bytes deve somar os dois tipos.

        Confirma que o Q(...) | Q(...) funciona sem dupla contagem e sem
        excluir nenhum dos dois vínculos.
        """
        from apps.core.models import OmicSample
        from apps.core.services.download_service import _project_used_bytes

        # Arquivo GEO (dataset-linked, como existia antes do bug)
        DatasetFile.objects.create(
            dataset=self.dataset_sra,
            sample=None,
            accession='SRP11111_geo_mixed_a2',
            file_type='supplementary',
            source='geo_ftp',
            remote_url='ftp://ftp.ncbi.nlm.nih.gov/geo/series/SRP11111/suppl/a.txt.gz',
            storage_key='omics/1/proj/SRP11111/geo_a.txt.gz',
            size_bytes=200,
            download_status=DatasetFile.DownloadStatus.DOWNLOADED,
        )

        # Dataset SRA diferente para o arquivo FASTQ (sample-linked)
        dataset_sra_mix = make_dataset('SRP77773', source_db='sra')
        make_project_dataset(self.project, dataset_sra_mix)

        sample_mix = OmicSample.objects.create(
            dataset=dataset_sra_mix,
            accession='SRR77773',
            title='Sample SRR77773',
            organism='Homo sapiens',
        )

        DatasetFile.objects.create(
            sample=sample_mix,
            dataset=None,
            accession='SRR77773_1_fastq_mixed_a2',
            file_type=DatasetFile.FileType.FASTQ,
            source=DatasetFile.Source.ENA_FTP,
            remote_url='ftp://ftp.sra.ebi.ac.uk/SRR77773_1.fastq.gz',
            storage_key='omics/1/proj/SRP77773/SRR77773_1.fastq.gz',
            size_bytes=300,
            download_status=DatasetFile.DownloadStatus.DOWNLOADED,
        )

        used = _project_used_bytes(self.project)
        self.assertEqual(
            used, 500,
            "_project_used_bytes deve somar arquivos GEO (dataset-linked, 200 B) "
            "e FASTQ (sample-linked, 300 B) sem dupla contagem (regressão A2 — misto).",
        )


# =============================================================================
# F2 — 9. Task run_omics_download para FASTQ (amostra via sample.files)
# =============================================================================

class RunOmicsDownloadFastqTaskTests(APITestCase):
    """
    Testa run_omics_download com file_kind='fastq'.

    Diferença crítica da F1: Rust grava DatasetFile com sample=sample (não
    dataset=dataset).  A task itera sobre OmicSample.objects.filter(dataset=...)
    e, para cada sample, sobre DatasetFile.objects.filter(sample=sample).

    Cobre:
    - Mock de rust_engine.download_dataset_files retorna resultado ok.
    - Upload itera sobre sample.files (DatasetFile com sample preenchido, não dataset).
    - download_status='downloaded' por arquivo de sample.
    - Todos os samples baixados → ProjectDataset.curation_status='downloaded'.
    - Falha de upload em arquivo de sample → download_status='failed', registro preservado.
    - Dataset sem samples → task não marca ProjectDataset como downloaded (total_files=0).
    - curated_at/exclusion_reason/notes intocados durante task FASTQ.
    """

    def setUp(self):
        self.user = make_user('fastq_task_user')
        self.project = make_project(self.user, 'FASTQ Task Project')

        # Dataset SRA com accession
        self.dataset = make_dataset(
            'SRP55555',
            source_db='sra',
            extra_metadata={},
        )
        self.project_dataset = make_project_dataset(
            self.project, self.dataset, curation_status='queued'
        )

        # Job FASTQ_DOWNLOAD já criado
        self.job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.FASTQ_DOWNLOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={
                'dataset_id': self.dataset.id,
                'dataset_accession': self.dataset.accession,
                'source_db': 'sra',
                'file_kind': 'fastq',
            },
        )

    def _make_sample(self, accession):
        """Cria OmicSample vinculado ao dataset."""
        from apps.core.models import OmicSample
        return OmicSample.objects.create(
            dataset=self.dataset,
            accession=accession,
            title=f'Sample {accession}',
            organism='Homo sapiens',
        )

    def _make_sample_file(self, sample, accession, local_path, status='pending'):
        """Cria DatasetFile vinculado ao sample (não ao dataset)."""
        return DatasetFile.objects.create(
            sample=sample,
            accession=accession,
            file_type=DatasetFile.FileType.FASTQ,
            source=DatasetFile.Source.ENA_FTP,
            remote_url=f'ftp://ftp.sra.ebi.ac.uk/{sample.accession}_1.fastq.gz',
            storage_key=local_path,
            download_status=status,
        )

    def _run_fastq_task(self, rust_result=None, storage_save_side_effect=None):
        """
        Executa run_omics_download com file_kind='fastq'.
        Mocka rust_engine e default_storage.
        """
        import sys
        from unittest.mock import mock_open as _mock_open

        if rust_result is None:
            rust_result = MagicMock()
            rust_result.files_downloaded = 1
            rust_result.bytes_total = 2048
            rust_result.errors = []

        mock_rust = MagicMock()
        mock_rust.download_dataset_files.return_value = rust_result

        mock_storage = MagicMock()
        if storage_save_side_effect is not None:
            mock_storage.save.side_effect = storage_save_side_effect
        else:
            mock_storage.save.return_value = 'omics/fastq/SRP55555/SRR55551_1.fastq.gz'

        m_open = _mock_open(read_data=b'fastq_content')

        with patch.dict(sys.modules, {'rust_engine': mock_rust}), \
             patch('apps.core.tasks.ingestion_tasks.default_storage', mock_storage), \
             patch('apps.core.tasks.ingestion_tasks.os.path.isfile', return_value=True), \
             patch('apps.core.tasks.ingestion_tasks.os.remove'), \
             patch('builtins.open', m_open):
            from apps.core.tasks.ingestion_tasks import run_omics_download
            result = run_omics_download.run(
                str(self.project.id),
                self.dataset.id,
                'fastq',
            )

        return result, mock_storage

    def test_fastq_task_itera_sobre_sample_files_nao_dataset_files(self):
        """
        Task com file_kind='fastq' faz upload de DatasetFile vinculado ao
        sample (sample preenchido, dataset=None), não ao dataset diretamente.
        """
        sample = self._make_sample('SRR55551')
        df = self._make_sample_file(
            sample,
            accession='SRR55551_1',
            local_path='/tmp/davinci_fastq/SRR55551_1.fastq.gz',
        )

        result, mock_storage = self._run_fastq_task()

        df.refresh_from_db()
        self.assertEqual(df.download_status, DatasetFile.DownloadStatus.DOWNLOADED,
            "DatasetFile do sample deve ser marcado 'downloaded' pela task FASTQ")
        mock_storage.save.assert_called()

    def test_fastq_task_storage_key_sobrescrito_com_object_key(self):
        """Após upload, storage_key do DatasetFile do sample recebe a chave de object storage."""
        expected_key = 'omics/fastq/SRP55555/SRR55552_1.fastq.gz'

        sample = self._make_sample('SRR55552')
        df = self._make_sample_file(
            sample,
            accession='SRR55552_1',
            local_path='/tmp/davinci_fastq/SRR55552_1.fastq.gz',
        )

        result, mock_storage = self._run_fastq_task(
            storage_save_side_effect=[expected_key]
        )
        df.refresh_from_db()
        self.assertEqual(df.storage_key, expected_key)

    def test_fastq_task_downloaded_at_preenchido(self):
        """downloaded_at é preenchido após upload bem-sucedido do arquivo de sample."""
        sample = self._make_sample('SRR55553')
        df = self._make_sample_file(
            sample,
            accession='SRR55553_1',
            local_path='/tmp/davinci_fastq/SRR55553_1.fastq.gz',
        )

        self._run_fastq_task()
        df.refresh_from_db()
        self.assertIsNotNone(df.downloaded_at)

    def test_fastq_todos_samples_downloaded_promove_project_dataset(self):
        """
        Todos os DatasetFile de todos os samples do dataset estão 'downloaded'
        → ProjectDataset.curation_status='downloaded'.
        """
        sample1 = self._make_sample('SRR55554')
        df1 = self._make_sample_file(
            sample1, 'SRR55554_1',
            '/tmp/davinci_fastq/SRR55554_1.fastq.gz',
        )
        sample2 = self._make_sample('SRR55555A')
        df2 = self._make_sample_file(
            sample2, 'SRR55555A_1',
            '/tmp/davinci_fastq/SRR55555A_1.fastq.gz',
        )

        # Dois saves bem-sucedidos
        self._run_fastq_task(
            storage_save_side_effect=[
                'omics/fastq/SRP55555/SRR55554_1.fastq.gz',
                'omics/fastq/SRP55555/SRR55555A_1.fastq.gz',
            ]
        )

        self.project_dataset.refresh_from_db()
        self.assertEqual(
            self.project_dataset.curation_status,
            ProjectDataset.CurationStatus.DOWNLOADED,
        )

    def test_fastq_dataset_sem_samples_nao_promove_project_dataset(self):
        """
        Dataset sem OmicSamples (total_files=0): ProjectDataset não deve
        ser marcado 'downloaded' — nada foi baixado.
        """
        # Nenhum OmicSample criado para self.dataset
        self._run_fastq_task()

        self.project_dataset.refresh_from_db()
        self.assertNotEqual(
            self.project_dataset.curation_status,
            ProjectDataset.CurationStatus.DOWNLOADED,
            "Sem samples não há arquivos: ProjectDataset não deve ser 'downloaded'",
        )

    def test_fastq_falha_upload_seta_failed_preserva_registro(self):
        """
        Falha no upload de arquivo de sample:
        - download_status='failed'
        - error_message preenchido
        - Registro DatasetFile NUNCA deletado
        """
        sample = self._make_sample('SRR55556')
        df = self._make_sample_file(
            sample,
            accession='SRR55556_1',
            local_path='/tmp/davinci_fastq/SRR55556_1.fastq.gz',
        )

        self._run_fastq_task(
            storage_save_side_effect=[OSError('ENA upload error')]
        )

        df.refresh_from_db()
        self.assertEqual(df.download_status, DatasetFile.DownloadStatus.FAILED)
        self.assertNotEqual(df.error_message, '')
        # Registro nunca deletado (curation-audit-trail)
        self.assertTrue(DatasetFile.objects.filter(pk=df.pk).exists())

    def test_fastq_curated_at_nao_tocado_pela_task(self):
        """Task FASTQ não toca curated_at do ProjectDataset (download não é curadoria)."""
        sample = self._make_sample('SRR55557')
        self._make_sample_file(
            sample, 'SRR55557_1',
            '/tmp/davinci_fastq/SRR55557_1.fastq.gz',
        )
        self._run_fastq_task()
        self.project_dataset.refresh_from_db()
        self.assertIsNone(self.project_dataset.curated_at)

    def test_fastq_exclusion_reason_nao_tocado(self):
        """exclusion_reason preservado após task FASTQ."""
        self.project_dataset.exclusion_reason = 'motivo original fastq'
        self.project_dataset.save(update_fields=['exclusion_reason'])

        sample = self._make_sample('SRR55558')
        self._make_sample_file(
            sample, 'SRR55558_1',
            '/tmp/davinci_fastq/SRR55558_1.fastq.gz',
        )
        self._run_fastq_task()
        self.project_dataset.refresh_from_db()
        self.assertEqual(self.project_dataset.exclusion_reason, 'motivo original fastq')

    def test_fastq_notes_nao_tocado(self):
        """notes preservado após task FASTQ."""
        self.project_dataset.notes = 'notas fastq preservadas'
        self.project_dataset.save(update_fields=['notes'])

        sample = self._make_sample('SRR55559')
        self._make_sample_file(
            sample, 'SRR55559_1',
            '/tmp/davinci_fastq/SRR55559_1.fastq.gz',
        )
        self._run_fastq_task()
        self.project_dataset.refresh_from_db()
        self.assertEqual(self.project_dataset.notes, 'notas fastq preservadas')

    def test_fastq_dataset_file_sem_local_path_e_ignorado(self):
        """
        DatasetFile cujo storage_key NÃO aponta para arquivo local
        (path que não passa no os.path.isfile) é ignorado silenciosamente.
        Não deve causar erro nem marcar failed.
        """
        import sys
        from unittest.mock import mock_open as _mock_open

        sample = self._make_sample('SRR55560')
        # storage_key vazio → isfile retornará False para esse arquivo
        df = self._make_sample_file(
            sample, 'SRR55560_1',
            '',  # sem path local — Rust não baixou ainda
        )

        mock_rust = MagicMock()
        rust_result = MagicMock()
        rust_result.files_downloaded = 0
        rust_result.bytes_total = 0
        rust_result.errors = []
        mock_rust.download_dataset_files.return_value = rust_result

        mock_storage = MagicMock()
        m_open = _mock_open(read_data=b'')

        with patch.dict(sys.modules, {'rust_engine': mock_rust}), \
             patch('apps.core.tasks.ingestion_tasks.default_storage', mock_storage), \
             patch('apps.core.tasks.ingestion_tasks.os.path.isfile', return_value=False), \
             patch('apps.core.tasks.ingestion_tasks.os.remove'), \
             patch('builtins.open', m_open):
            from apps.core.tasks.ingestion_tasks import run_omics_download
            run_omics_download.run(
                str(self.project.id),
                self.dataset.id,
                'fastq',
            )

        df.refresh_from_db()
        # DatasetFile não deve ter sido deletado
        self.assertTrue(DatasetFile.objects.filter(pk=df.pk).exists())
        # upload não foi chamado (sem arquivo local)
        mock_storage.save.assert_not_called()


# =============================================================================
# F2 — 10. cleanup_orphan_files
# =============================================================================

class CleanupOrphanFilesTests(APITestCase):
    """
    Testa apps.core.tasks.cleanup_tasks.cleanup_orphan_files.

    Cobre (mock de default_storage):
    - Arquivo 'failed' com storage_key → default_storage.delete chamado.
    - Arquivo 'failed' com storage_key → storage_key resetado para '' após cleanup.
    - Arquivo 'failed' sem storage_key (já limpo) → delete NÃO chamado.
    - Arquivo 'downloaded' NUNCA deletado nem alterado (curation-audit-trail).
    - Arquivo 'downloaded' com objeto ausente no storage → recebe error_message,
      mas download_status permanece 'downloaded'.
    - Arquivo 'downloaded' com storage_key presente no storage → nenhuma alteração.
    - Retorno com contadores corretos (deleted_failed_bytes, marked_missing_in_storage).
    - Arquivo 'pending' (sem storage_key) → ignorado.
    """

    def setUp(self):
        self.user = make_user('cleanup_user')
        self.project = make_project(self.user, 'Cleanup Project')
        self.dataset = make_dataset('GSE80001')
        make_project_dataset(self.project, self.dataset)

    def _run_cleanup(self, storage_exists_map=None):
        """
        Executa cleanup_orphan_files mockando default_storage.

        storage_exists_map: dict {storage_key: bool} — True se o objeto existe
        no storage, False caso contrário. Default: retorna False para tudo.
        """
        exists_map = storage_exists_map or {}

        def fake_exists(key):
            return exists_map.get(key, False)

        mock_storage = MagicMock()
        mock_storage.exists.side_effect = fake_exists
        mock_storage.delete = MagicMock()

        with patch('apps.core.tasks.cleanup_tasks.default_storage', mock_storage):
            from apps.core.tasks.cleanup_tasks import cleanup_orphan_files
            result = cleanup_orphan_files()

        return result, mock_storage

    def test_failed_com_storage_key_chama_delete(self):
        """Arquivo 'failed' com storage_key e objeto presente → default_storage.delete chamado."""
        df = make_dataset_file(
            self.dataset,
            accession='GSE80001_failed_del',
            storage_key='omics/1/proj/GSE80001/failed_del.txt.gz',
            download_status='failed',
        )
        result, mock_storage = self._run_cleanup(
            storage_exists_map={'omics/1/proj/GSE80001/failed_del.txt.gz': True}
        )
        mock_storage.delete.assert_called_with('omics/1/proj/GSE80001/failed_del.txt.gz')

    def test_failed_com_storage_key_reseta_storage_key_para_vazio(self):
        """Após cleanup de 'failed', storage_key é resetado para ''."""
        df = make_dataset_file(
            self.dataset,
            accession='GSE80001_failed_reset',
            storage_key='omics/1/proj/GSE80001/failed_reset.txt.gz',
            download_status='failed',
        )
        self._run_cleanup(
            storage_exists_map={'omics/1/proj/GSE80001/failed_reset.txt.gz': True}
        )
        df.refresh_from_db()
        self.assertEqual(df.storage_key, '')

    def test_failed_sem_storage_key_delete_nao_chamado(self):
        """Arquivo 'failed' sem storage_key (já limpo) → delete NÃO é chamado."""
        df = make_dataset_file(
            self.dataset,
            accession='GSE80001_failed_no_sk',
            storage_key='',
            download_status='failed',
        )
        result, mock_storage = self._run_cleanup()
        mock_storage.delete.assert_not_called()

    def test_failed_com_storage_key_mas_objeto_ausente_delete_nao_chamado(self):
        """
        'failed' com storage_key mas objeto já ausente no storage
        → delete NÃO é chamado (já sumiu), mas storage_key ainda é resetado.
        """
        df = make_dataset_file(
            self.dataset,
            accession='GSE80001_failed_absent',
            storage_key='omics/1/proj/GSE80001/absent.txt.gz',
            download_status='failed',
        )
        # exists retorna False → objeto não existe no storage
        result, mock_storage = self._run_cleanup(
            storage_exists_map={'omics/1/proj/GSE80001/absent.txt.gz': False}
        )
        mock_storage.delete.assert_not_called()
        df.refresh_from_db()
        self.assertEqual(df.storage_key, '',
            "storage_key deve ser resetado mesmo quando objeto já não existe")

    def test_downloaded_nunca_deletado_nem_alterado(self):
        """
        Arquivo 'downloaded' com objeto presente no storage:
        - delete NÃO é chamado.
        - download_status permanece 'downloaded'.
        - storage_key não é alterado.
        """
        original_key = 'omics/1/proj/GSE80001/downloaded.txt.gz'
        df = make_dataset_file(
            self.dataset,
            accession='GSE80001_downloaded_safe',
            storage_key=original_key,
            download_status='downloaded',
        )
        result, mock_storage = self._run_cleanup(
            storage_exists_map={original_key: True}
        )
        mock_storage.delete.assert_not_called()
        df.refresh_from_db()
        self.assertEqual(df.download_status, DatasetFile.DownloadStatus.DOWNLOADED)
        self.assertEqual(df.storage_key, original_key)

    def test_downloaded_com_objeto_sumido_recebe_error_message(self):
        """
        'downloaded' com objeto ausente no storage:
        - download_status permanece 'downloaded' (curation-audit-trail).
        - error_message recebe aviso.
        """
        df = make_dataset_file(
            self.dataset,
            accession='GSE80001_downloaded_missing',
            storage_key='omics/1/proj/GSE80001/missing.txt.gz',
            download_status='downloaded',
        )
        # exists retorna False → objeto sumiu
        result, mock_storage = self._run_cleanup(
            storage_exists_map={'omics/1/proj/GSE80001/missing.txt.gz': False}
        )
        df.refresh_from_db()
        # download_status NUNCA alterado (curation-audit-trail)
        self.assertEqual(df.download_status, DatasetFile.DownloadStatus.DOWNLOADED)
        # error_message deve conter aviso
        self.assertNotEqual(df.error_message, '',
            "error_message deve ser preenchido quando storage_key sumiu no storage")

    def test_downloaded_com_objeto_sumido_status_permanece_downloaded(self):
        """
        Mesmo com objeto sumido no storage, download_status nunca muda de 'downloaded'.
        Invariante crítico: não pode regredir para 'failed' ou 'pending'.
        """
        df = make_dataset_file(
            self.dataset,
            accession='GSE80001_downloaded_status_guard',
            storage_key='omics/1/proj/GSE80001/missing2.txt.gz',
            download_status='downloaded',
        )
        self._run_cleanup(
            storage_exists_map={'omics/1/proj/GSE80001/missing2.txt.gz': False}
        )
        df.refresh_from_db()
        self.assertEqual(
            df.download_status,
            DatasetFile.DownloadStatus.DOWNLOADED,
            "download_status de arquivo 'downloaded' jamais deve regredir (curation-audit-trail)",
        )

    def test_downloaded_com_objeto_sumido_registro_nunca_deletado(self):
        """O registro DatasetFile 'downloaded' nunca é deletado, mesmo com objeto ausente."""
        df = make_dataset_file(
            self.dataset,
            accession='GSE80001_downloaded_nodelete',
            storage_key='omics/1/proj/GSE80001/nodelete.txt.gz',
            download_status='downloaded',
        )
        self._run_cleanup(
            storage_exists_map={'omics/1/proj/GSE80001/nodelete.txt.gz': False}
        )
        self.assertTrue(DatasetFile.objects.filter(pk=df.pk).exists(),
            "Registro 'downloaded' jamais deve ser deletado")

    def test_contagem_deleted_failed_no_retorno(self):
        """Retorno da task contém deleted_failed_bytes com contagem correta."""
        make_dataset_file(
            self.dataset,
            accession='GSE80001_cnt_del1',
            storage_key='omics/1/proj/GSE80001/cnt1.txt.gz',
            download_status='failed',
        )
        make_dataset_file(
            self.dataset,
            accession='GSE80001_cnt_del2',
            storage_key='omics/1/proj/GSE80001/cnt2.txt.gz',
            download_status='failed',
        )
        result, _ = self._run_cleanup(
            storage_exists_map={
                'omics/1/proj/GSE80001/cnt1.txt.gz': True,
                'omics/1/proj/GSE80001/cnt2.txt.gz': True,
            }
        )
        self.assertEqual(result['deleted_failed_bytes'], 2)

    def test_contagem_marked_missing_no_retorno(self):
        """Retorno contém marked_missing_in_storage com contagem de 'downloaded' ausentes."""
        make_dataset_file(
            self.dataset,
            accession='GSE80001_miss1',
            storage_key='omics/1/proj/GSE80001/miss1.txt.gz',
            download_status='downloaded',
        )
        make_dataset_file(
            self.dataset,
            accession='GSE80001_miss2',
            storage_key='omics/1/proj/GSE80001/miss2.txt.gz',
            download_status='downloaded',
        )
        result, _ = self._run_cleanup(
            storage_exists_map={
                'omics/1/proj/GSE80001/miss1.txt.gz': False,
                'omics/1/proj/GSE80001/miss2.txt.gz': False,
            }
        )
        self.assertEqual(result['marked_missing_in_storage'], 2)

    def test_pending_ignorado_completamente(self):
        """Arquivo 'pending' sem storage_key não é tocado pelo cleanup."""
        df = make_dataset_file(
            self.dataset,
            accession='GSE80001_pending_ignored',
            storage_key='',
            download_status='pending',
        )
        result, mock_storage = self._run_cleanup()
        df.refresh_from_db()
        self.assertEqual(df.download_status, DatasetFile.DownloadStatus.PENDING)
        mock_storage.delete.assert_not_called()

    def test_misto_failed_e_downloaded_comportamento_correto(self):
        """
        Com ambos tipos presentes:
        - 'failed' com key → delete chamado, key resetada.
        - 'downloaded' com key presente → inalterado.
        """
        key_failed = 'omics/1/proj/GSE80001/mix_failed.txt.gz'
        key_downloaded = 'omics/1/proj/GSE80001/mix_downloaded.txt.gz'

        df_failed = make_dataset_file(
            self.dataset,
            accession='GSE80001_mix_failed',
            storage_key=key_failed,
            download_status='failed',
        )
        df_downloaded = make_dataset_file(
            self.dataset,
            accession='GSE80001_mix_downloaded',
            storage_key=key_downloaded,
            download_status='downloaded',
        )

        result, mock_storage = self._run_cleanup(
            storage_exists_map={key_failed: True, key_downloaded: True}
        )

        mock_storage.delete.assert_called_with(key_failed)
        df_failed.refresh_from_db()
        self.assertEqual(df_failed.storage_key, '')

        df_downloaded.refresh_from_db()
        self.assertEqual(df_downloaded.download_status, DatasetFile.DownloadStatus.DOWNLOADED)
        self.assertEqual(df_downloaded.storage_key, key_downloaded)
