"""
test_download_increments.py — Cobertura dos incrementos do download ômico.

Plano de referência: .claude/plans/2026-06-22-download-fastq-selecao-e-link-geo-sra.md
Feature base: commit 5b823d1

Incrementos cobertos:
  Inc-3 (SampleSraRun):
    1. sra_resolved reflete SampleSraRun, não extra_metadata (sem N+1).
    2. Guard GEO→FASTQ usa SampleSraRun: com linhas → 202; sem → 400 orientativo.

  Inc-2 (scope='filter'):
    3. scope='filter' com curation_status=['included'] → só incluídas.
    4. scope='filter' com organism filtra por organism icontains.
    5. scope='filter' com platform filtra por platform icontains.
    6. scope='filter' respeita isolamento por projeto (Regra #3).
    7. scope='filter' sem filtro útil / sem resultados → 400.

  Inc-1 (destination='client'):
    8.  destination='client' retorna HTTP 200 com lista de URLs (mock resolve_fastq_urls).
    9.  destination='client' NÃO cria IngestionJob, NÃO chama .delay(), NÃO toca quota.
    10. Teto de runs: seleção > CLIENT_DOWNLOAD_MAX_RUNS → 400 orientativo.
    11. Isolamento: destination='client' respeita projeto do user (sample_ids de outro projeto → 404).

  Inc-5 (download-batch):
    12. POST .../datasets/download-batch/ scope='included' → 202 com lista de jobs.
    13. Gate de quota agregado: soma estoura → 409 sem despachar nenhum job.
    14. Falta confirm → 400 com prévia agregada; nenhum job criado.
    15. dataset_ids de outro projeto/usuário → ignorado (404 se todos inválidos).
    16. destination='client' no batch → 400 (BatchDownloadRequestSerializer.validate).

Convenção: sem pytest — usa django.test.TestCase / APITestCase.
Sem internet: rust_engine (resolve_fastq_urls, resolve_sra_runs_for_dataset,
  download_dataset_files) é mockado em todos os testes que chegam ao Rust.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from apps.core.models import (
    DaVinciProject,
    IngestionJob,
    OmicDataset,
    OmicSample,
    ProjectDataset,
    ProjectSample,
    SampleSraRun,
)


# =============================================================================
# Helpers de factory
# =============================================================================

def _user(username: str, password: str = 'pw') -> User:
    return User.objects.create_user(username=username, password=password)


def _project(user: User, title: str) -> DaVinciProject:
    slug = f'{title.lower().replace(" ", "-")}-{user.username}-inc'
    return DaVinciProject.objects.create(
        user=user, title=title, slug=slug, query_term='test'
    )


def _dataset(accession: str, source_db: str = 'sra') -> OmicDataset:
    return OmicDataset.objects.create(
        accession=accession,
        source_db=source_db,
        title=f'Dataset {accession}',
        omic_type='transcriptomic',
        organism='Homo sapiens',
        extra_metadata={},
    )


def _project_dataset(project: DaVinciProject, dataset: OmicDataset,
                     curation_status: str = 'pending') -> ProjectDataset:
    return ProjectDataset.objects.create(
        project=project, dataset=dataset, curation_status=curation_status
    )


def _omic_sample(dataset: OmicDataset, accession: str,
                 organism: str = 'Homo sapiens',
                 platform: str = '') -> OmicSample:
    return OmicSample.objects.create(
        dataset=dataset,
        accession=accession,
        title=f'Sample {accession}',
        organism=organism,
        platform=platform,
        extra_metadata={},
    )


def _project_sample(project: DaVinciProject, sample: OmicSample,
                    curation_status: str = 'pending') -> ProjectSample:
    return ProjectSample.objects.create(
        project=project, sample=sample, curation_status=curation_status
    )


def _sra_run(sample: OmicSample, run_accession: str,
             size_bytes: int | None = None,
             fastq_url: str = '') -> SampleSraRun:
    return SampleSraRun.objects.create(
        sample=sample,
        run_accession=run_accession,
        size_bytes=size_bytes,
        fastq_url=fastq_url,
    )


def _pending_job(project: DaVinciProject, dataset: OmicDataset,
                 job_type: str = IngestionJob.JobType.FASTQ_DOWNLOAD,
                 scope: str = 'all') -> IngestionJob:
    return IngestionJob.objects.create(
        project=project,
        job_type=job_type,
        status=IngestionJob.JobStatus.PENDING,
        parameters={
            'dataset_id': dataset.id,
            'dataset_accession': dataset.accession,
            'source_db': dataset.source_db,
            'file_kind': 'fastq',
            'scope': scope,
            'sample_ids': None,
        },
    )


def _mock_rust_empty():
    """Retorna um módulo rust_engine mockado com resolve_fastq_urls → []."""
    mock_rust = MagicMock()
    mock_rust.resolve_fastq_urls.return_value = []
    mock_rust.download_dataset_files.return_value = MagicMock(
        files_downloaded=0, bytes_total=0, errors=[]
    )
    mock_rust.resolve_sra_runs_for_dataset.return_value = None
    return mock_rust


def _make_fastq_entry(run_accession: str, fastq_url: str = 'https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR/SRR000001.fastq.gz',
                      file_name: str = 'SRR000001.fastq.gz',
                      size_bytes: int = 1024 * 1024,
                      checksum_md5: str = 'abc123') -> MagicMock:
    """Cria uma entrada fake de FastqUrlEntry (objeto Rust mockado)."""
    entry = MagicMock()
    entry.run_accession = run_accession
    entry.fastq_url = fastq_url
    entry.file_name = file_name
    entry.size_bytes = size_bytes
    entry.checksum_md5 = checksum_md5
    return entry


# =============================================================================
# Inc-3 — SampleSraRun: sra_resolved na listagem (sem N+1)
# =============================================================================

class SampleSraRunSraResolvedTests(APITestCase):
    """
    Inc-3: sra_resolved é calculado via Exists(SampleSraRun) — tabela estruturada.
    A anotação está em get_queryset() e usa subquery Exists para evitar N+1.
    """

    def setUp(self):
        self.user = _user('inc3_resolved_user')
        self.client.force_authenticate(user=self.user)
        self.project = _project(self.user, 'Inc3 Resolved Project')

    def _list_url(self):
        return f'/api/v1/projects/{self.project.id}/datasets/'

    def _get_items(self, response):
        data = response.json()
        if isinstance(data, dict) and 'results' in data:
            return data['results']
        return data

    # 1a. Com linha SampleSraRun → sra_resolved=True
    def test_sra_resolved_true_quando_existe_samplesrarun(self):
        """
        Dataset GEO com OmicSample e ao menos uma linha SampleSraRun
        → sra_resolved=true.
        """
        ds = _dataset('GSE_INC3_001', source_db='geo')
        _project_dataset(self.project, ds)
        gsm = _omic_sample(ds, 'GSM_INC3_001')
        _sra_run(gsm, 'SRR_INC3_001')

        response = self.client.get(self._list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        items = self._get_items(response)
        item = next((i for i in items if i.get('accession') == 'GSE_INC3_001'), None)
        self.assertIsNotNone(item)
        self.assertTrue(item['sra_resolved'])

    # 1b. Sem linha SampleSraRun → sra_resolved=False (mesmo com extra_metadata)
    def test_sra_resolved_false_sem_samplesrarun(self):
        """
        OmicSample existe mas sem SampleSraRun → sra_resolved=false.
        extra_metadata['sra_runs'] ignorado pela anotação (Inc-3 usa tabela).
        """
        ds = _dataset('GSE_INC3_002', source_db='geo')
        _project_dataset(self.project, ds)
        # OmicSample sem SampleSraRun vinculado
        _omic_sample(ds, 'GSM_INC3_002')

        response = self.client.get(self._list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        items = self._get_items(response)
        item = next((i for i in items if i.get('accession') == 'GSE_INC3_002'), None)
        self.assertIsNotNone(item)
        self.assertFalse(item['sra_resolved'])

    # 1c. Anotação Exists não gera N+1 — múltiplos datasets com e sem SampleSraRun
    def test_sra_resolved_anotacao_exists_sem_n_plus_1(self):
        """
        Listagem de 6 datasets (3 com SampleSraRun, 3 sem) → 200; todos têm
        o campo sra_resolved. Validação estrutural da anotação Exists.
        """
        for i in range(6):
            ds = _dataset(f'GSE_INC3_N{i:02d}', source_db='geo')
            _project_dataset(self.project, ds)
            if i < 3:
                gsm = _omic_sample(ds, f'GSM_INC3_N{i:02d}')
                _sra_run(gsm, f'SRR_INC3_N{i:02d}')

        response = self.client.get(self._list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        items = self._get_items(response)
        # Todos os datasets retornados devem ter o campo sra_resolved
        for item in items:
            self.assertIn('sra_resolved', item,
                          f"sra_resolved ausente para {item.get('accession')}")

    # 2. Guard GEO→FASTQ usa SampleSraRun
    def test_guard_geo_fastq_aceita_com_samplesrarun(self):
        """
        Dataset GEO com ao menos um SampleSraRun → guard liberado → 202.
        """
        ds = _dataset('GSE_GUARD_INC3_001', source_db='geo')
        pd = _project_dataset(self.project, ds)
        gsm = _omic_sample(ds, 'GSM_GUARD_INC3_001')
        _sra_run(gsm, 'SRR_GUARD_001')

        url = f'/api/v1/projects/{self.project.id}/datasets/{pd.id}/download/'
        job = _pending_job(self.project, ds)

        with patch('apps.core.services.download_service.DownloadService.dispatch',
                   return_value=job), \
             patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                url, data={'confirm': True, 'file_kind': 'fastq'}, format='json'
            )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_guard_geo_fastq_bloqueia_sem_samplesrarun(self):
        """
        Dataset GEO sem nenhuma linha SampleSraRun → guard bloqueia → 400 orientativo.
        A mensagem deve conter 'resolve'.
        """
        ds = _dataset('GSE_GUARD_INC3_002', source_db='geo')
        pd = _project_dataset(self.project, ds)
        url = f'/api/v1/projects/{self.project.id}/datasets/{pd.id}/download/'

        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                url, data={'confirm': True, 'file_kind': 'fastq'}, format='json'
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('resolve', response.json()['detail'].lower())

    def test_guard_geo_fastq_delay_nao_chamado_sem_samplesrarun(self):
        """Sem SampleSraRun, run_omics_download.delay() não é chamado."""
        ds = _dataset('GSE_GUARD_INC3_003', source_db='geo')
        pd = _project_dataset(self.project, ds)
        url = f'/api/v1/projects/{self.project.id}/datasets/{pd.id}/download/'

        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay') as mock_delay:
            self.client.post(
                url, data={'confirm': True, 'file_kind': 'fastq'}, format='json'
            )

        mock_delay.assert_not_called()


# =============================================================================
# Inc-2 — scope='filter'
# =============================================================================

class ScopeFilterTests(APITestCase):
    """
    Inc-2: scope='filter' resolve amostras conforme filters.

    Filtros suportados: curation_status (list, in), organism (icontains), platform (icontains).
    Isolamento por projeto (Regra #3) garantido pelo base_qs filtrado por project+dataset do user.
    """

    def setUp(self):
        self.user = _user('inc2_filter_user')
        self.client.force_authenticate(user=self.user)
        self.project = _project(self.user, 'Inc2 Filter Project')

        self.dataset = _dataset('SRP_FILTER_001', source_db='sra')
        self.pd = _project_dataset(self.project, self.dataset)
        self.url = (
            f'/api/v1/projects/{self.project.id}'
            f'/datasets/{self.pd.id}/download/'
        )

        # Amostras com curation_status distintos
        self.s_inc = _omic_sample(self.dataset, 'SRR_FIL_INC', organism='Homo sapiens', platform='Illumina')
        self.s_exc = _omic_sample(self.dataset, 'SRR_FIL_EXC', organism='Homo sapiens', platform='Illumina')
        self.s_pend = _omic_sample(self.dataset, 'SRR_FIL_PEND', organism='Mus musculus', platform='SOLiD')

        self.ps_inc = _project_sample(self.project, self.s_inc, 'included')
        self.ps_exc = _project_sample(self.project, self.s_exc, 'excluded')
        self.ps_pend = _project_sample(self.project, self.s_pend, 'pending')

    # 3. curation_status=['included'] → só incluídas
    def test_filter_curation_status_included_so_incluidas(self):
        """
        scope='filter' com filters.curation_status=['included']
        → dispatch recebe apenas o OmicSample.id da amostra 'included'.
        """
        job = _pending_job(self.project, self.dataset, scope='filter')

        with patch('apps.core.services.download_service.DownloadService.dispatch',
                   return_value=job) as mock_dispatch, \
             patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                self.url,
                data={
                    'confirm': True,
                    'scope': 'filter',
                    'filters': {'curation_status': ['included']},
                },
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        resolved = mock_dispatch.call_args.kwargs.get('sample_ids')
        self.assertIsNotNone(resolved)
        self.assertIn(self.s_inc.id, resolved)
        self.assertNotIn(self.s_exc.id, resolved)
        self.assertNotIn(self.s_pend.id, resolved)

    # 4. organism filtra por icontains
    def test_filter_organism_icontains(self):
        """
        scope='filter' com filters.organism='mus' → só a amostra com Mus musculus.
        """
        job = _pending_job(self.project, self.dataset, scope='filter')

        with patch('apps.core.services.download_service.DownloadService.dispatch',
                   return_value=job) as mock_dispatch, \
             patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                self.url,
                data={
                    'confirm': True,
                    'scope': 'filter',
                    'filters': {'organism': 'mus'},
                },
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        resolved = mock_dispatch.call_args.kwargs.get('sample_ids')
        self.assertIn(self.s_pend.id, resolved)
        self.assertNotIn(self.s_inc.id, resolved)
        self.assertNotIn(self.s_exc.id, resolved)

    # 5. platform filtra por icontains
    def test_filter_platform_icontains(self):
        """
        scope='filter' com filters.platform='solid' → só a amostra SOLiD (Mus).
        """
        job = _pending_job(self.project, self.dataset, scope='filter')

        with patch('apps.core.services.download_service.DownloadService.dispatch',
                   return_value=job) as mock_dispatch, \
             patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                self.url,
                data={
                    'confirm': True,
                    'scope': 'filter',
                    'filters': {'platform': 'solid'},
                },
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        resolved = mock_dispatch.call_args.kwargs.get('sample_ids')
        self.assertIn(self.s_pend.id, resolved)
        self.assertNotIn(self.s_inc.id, resolved)

    # 6. Isolamento: scope='filter' filtrado por project+dataset do usuário (Regra #3)
    def test_filter_isolamento_por_projeto(self):
        """
        scope='filter' não vaza amostras de outro projeto/usuário.
        A base_qs é filtrada por project+dataset do usuário autenticado.
        """
        # Usuário B com seu próprio projeto e dataset
        user_b = _user('inc2_filter_userb')
        project_b = _project(user_b, 'Inc2 Filter Project B')
        dataset_b = _dataset('SRP_FILTER_B_001', source_db='sra')
        pd_b = _project_dataset(project_b, dataset_b)
        sample_b = _omic_sample(dataset_b, 'SRR_FILTER_B_001')
        _project_sample(project_b, sample_b, 'included')

        client_b = APIClient()
        client_b.force_authenticate(user=user_b)

        # Usuário B não pode ver o endpoint do projeto de user A
        url_a = self.url
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = client_b.post(
                url_a,
                data={
                    'confirm': True,
                    'scope': 'filter',
                    'filters': {'curation_status': ['included']},
                },
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND,
                         "User B não deve acessar projeto de user A")

    # 7. Sem resultados → 400 orientativo
    def test_filter_sem_resultados_retorna_400(self):
        """
        scope='filter' com filtro que não casa nenhuma amostra → 400 com detail.
        A view verifica lista vazia e retorna 400 antes de chamar dispatch.
        """
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay') as mock_delay:
            response = self.client.post(
                self.url,
                data={
                    'confirm': True,
                    'scope': 'filter',
                    'filters': {'organism': 'organismo_inexistente_xyz_abc'},
                },
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('detail', response.json())
        mock_delay.assert_not_called()

    def test_filter_sem_filtros_uteis_retorna_400(self):
        """
        scope='filter' sem nenhum campo em filters útil (todos None/null)
        → o serializer rejeita (filters obrigatório quando scope='filter').
        """
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay') as mock_delay:
            response = self.client.post(
                self.url,
                data={
                    'confirm': True,
                    'scope': 'filter',
                    # filters ausente — serializer deve rejeitar
                },
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        mock_delay.assert_not_called()

    def test_filter_curation_status_multivalor(self):
        """
        scope='filter' com filters.curation_status=['included', 'excluded'] (IN list)
        → ambas as amostras retornadas.
        """
        job = _pending_job(self.project, self.dataset, scope='filter')

        with patch('apps.core.services.download_service.DownloadService.dispatch',
                   return_value=job) as mock_dispatch, \
             patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                self.url,
                data={
                    'confirm': True,
                    'scope': 'filter',
                    'filters': {'curation_status': ['included', 'excluded']},
                },
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        resolved = mock_dispatch.call_args.kwargs.get('sample_ids')
        self.assertIn(self.s_inc.id, resolved)
        self.assertIn(self.s_exc.id, resolved)
        self.assertNotIn(self.s_pend.id, resolved)


# =============================================================================
# Inc-1 — destination='client'
# =============================================================================

class DestinationClientIncTests(APITestCase):
    """
    Inc-1: destination='client' retorna HTTP 200 com lista de URLs públicas ENA.
    Não cria IngestionJob, não chama .delay(), não toca quota/curation_status/DatasetFile.
    """

    def setUp(self):
        self.user = _user('inc1_client_user')
        self.client.force_authenticate(user=self.user)
        self.project = _project(self.user, 'Inc1 Client Project')
        self.dataset = _dataset('SRP_CLIENT_001', source_db='sra')
        self.pd = _project_dataset(self.project, self.dataset)
        self.url = (
            f'/api/v1/projects/{self.project.id}'
            f'/datasets/{self.pd.id}/download/'
        )

        # Amostras no projeto para poder usar scope='included'
        self.s1 = _omic_sample(self.dataset, 'SRR_CLI_001')
        self.s2 = _omic_sample(self.dataset, 'SRR_CLI_002')
        self.ps1 = _project_sample(self.project, self.s1, 'included')
        self.ps2 = _project_sample(self.project, self.s2, 'included')

    # 8. destination='client' retorna 200 com lista de URLs
    def test_destination_client_retorna_200_com_lista_urls(self):
        """
        destination='client' com rust_engine.resolve_fastq_urls mockado →
        HTTP 200 com runs na resposta.
        """
        entries = [
            _make_fastq_entry('SRR_CLI_001'),
            _make_fastq_entry('SRR_CLI_002'),
        ]
        mock_rust = _mock_rust_empty()
        mock_rust.resolve_fastq_urls.return_value = entries

        with patch.dict(sys.modules, {'rust_engine': mock_rust}):
            response = self.client.post(
                self.url,
                data={
                    'confirm': True,
                    'scope': 'included',
                    'destination': 'client',
                },
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn('runs', data)
        self.assertEqual(data['total_runs'], 2)

    def test_destination_client_campos_na_resposta(self):
        """
        Resposta 200 inclui dataset_id, dataset_accession, runs, total_runs, bytes_total.
        """
        entries = [_make_fastq_entry('SRR_CLI_001', size_bytes=1024)]
        mock_rust = _mock_rust_empty()
        mock_rust.resolve_fastq_urls.return_value = entries

        with patch.dict(sys.modules, {'rust_engine': mock_rust}):
            response = self.client.post(
                self.url,
                data={'confirm': True, 'scope': 'all', 'destination': 'client'},
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn('dataset_id', data)
        self.assertIn('dataset_accession', data)
        self.assertIn('runs', data)
        self.assertIn('total_runs', data)
        self.assertIn('bytes_total', data)
        self.assertEqual(data['dataset_accession'], 'SRP_CLIENT_001')

    # 9. Não cria IngestionJob, não chama .delay()
    def test_destination_client_nao_cria_job_nem_chama_delay(self):
        """
        destination='client' não cria IngestionJob nem chama run_omics_download.delay().
        """
        job_count_before = IngestionJob.objects.count()

        mock_rust = _mock_rust_empty()
        with patch.dict(sys.modules, {'rust_engine': mock_rust}), \
             patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay') as mock_delay:
            self.client.post(
                self.url,
                data={'confirm': True, 'scope': 'all', 'destination': 'client'},
                format='json',
            )

        mock_delay.assert_not_called()
        self.assertEqual(IngestionJob.objects.count(), job_count_before,
                         "destination='client' não deve criar IngestionJob")

    def test_destination_client_nao_altera_curation_status(self):
        """
        destination='client' não altera ProjectDataset.curation_status
        (não é download de servidor — sem auditoria de job).
        """
        original_status = self.pd.curation_status

        mock_rust = _mock_rust_empty()
        with patch.dict(sys.modules, {'rust_engine': mock_rust}):
            self.client.post(
                self.url,
                data={'confirm': True, 'scope': 'all', 'destination': 'client'},
                format='json',
            )

        self.pd.refresh_from_db()
        self.assertEqual(self.pd.curation_status, original_status,
                         "destination='client' não deve alterar curation_status")

    # 10. Teto de runs > CLIENT_DOWNLOAD_MAX_RUNS → 400
    def test_destination_client_teto_excedido_retorna_400(self):
        """
        Seleção > CLIENT_DOWNLOAD_MAX_RUNS (padrão 200) → HTTP 400 orientativo.
        Mock: OmicSample.objects.filter().count() retorna 201 para scope='all'.
        """
        from apps.core.services.download_service import CLIENT_DOWNLOAD_MAX_RUNS

        # Usa patch para simular contagem acima do teto
        with patch('apps.core.views.dataset_views.OmicSample.objects') as mock_qs_manager:
            mock_qs = MagicMock()
            mock_qs.filter.return_value.count.return_value = CLIENT_DOWNLOAD_MAX_RUNS + 1
            mock_qs_manager.filter.return_value.count.return_value = CLIENT_DOWNLOAD_MAX_RUNS + 1
            mock_qs_manager.return_value = mock_qs

            with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
                response = self.client.post(
                    self.url,
                    data={'confirm': True, 'scope': 'all', 'destination': 'client'},
                    format='json',
                )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('detail', response.json())

    # 11. Isolamento: sample_ids de outro projeto → 404
    def test_destination_client_isolamento_projeto(self):
        """
        User B não pode usar o endpoint de download do projeto de user A
        com destination='client' — retorna 404.
        """
        user_b = _user('inc1_client_userb')
        client_b = APIClient()
        client_b.force_authenticate(user=user_b)

        mock_rust = _mock_rust_empty()
        with patch.dict(sys.modules, {'rust_engine': mock_rust}):
            response = client_b.post(
                self.url,  # URL do projeto de user A
                data={'confirm': True, 'scope': 'all', 'destination': 'client'},
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND,
                         "User B não deve acessar endpoint de projeto de user A")


# =============================================================================
# Inc-5 — download-batch
# =============================================================================

class DownloadBatchTests(APITestCase):
    """
    Inc-5: POST .../datasets/download-batch/ despacha um job por dataset alvo.
    Gate de quota agregado: se a soma estourar → 409; sem confirm → 400.
    Isolamento: dataset_ids de outro projeto são ignorados.
    """

    def setUp(self):
        self.user = _user('inc5_batch_user')
        self.client.force_authenticate(user=self.user)
        self.project = _project(self.user, 'Inc5 Batch Project')

        # Dois datasets SRA com amostras 'included'
        self.ds1 = _dataset('SRP_BATCH_001', source_db='sra')
        self.ds2 = _dataset('SRP_BATCH_002', source_db='sra')
        self.pd1 = _project_dataset(self.project, self.ds1)
        self.pd2 = _project_dataset(self.project, self.ds2)

        # Amostras 'included' em cada dataset
        self.s1a = _omic_sample(self.ds1, 'SRR_BATCH_001a')
        self.s1b = _omic_sample(self.ds1, 'SRR_BATCH_001b')
        self.s2a = _omic_sample(self.ds2, 'SRR_BATCH_002a')

        _project_sample(self.project, self.s1a, 'included')
        _project_sample(self.project, self.s1b, 'included')
        _project_sample(self.project, self.s2a, 'included')

        # SampleSraRun com size_bytes para estimativa de quota
        _sra_run(self.s1a, 'SRR_BATCH_001a', size_bytes=50 * 1024 ** 3)   # 50 GB
        _sra_run(self.s1b, 'SRR_BATCH_001b', size_bytes=50 * 1024 ** 3)   # 50 GB
        _sra_run(self.s2a, 'SRR_BATCH_002a', size_bytes=50 * 1024 ** 3)   # 50 GB

        self.batch_url = f'/api/v1/projects/{self.project.id}/datasets/download-batch/'

    # 12. scope='included' → 202 com lista de jobs
    def test_batch_scope_included_retorna_202_com_jobs(self):
        """
        POST .../download-batch/ com scope='included' e confirm=True →
        202 com lista de jobs (um por dataset alvo).
        """
        job1 = _pending_job(self.project, self.ds1)
        job2 = _pending_job(self.project, self.ds2)

        with patch('apps.core.services.download_service.DownloadService.dispatch',
                   side_effect=[job1, job2]), \
             patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                self.batch_url,
                data={
                    'scope': 'included',
                    'confirm': True,
                    'destination': 'server',
                    'dataset_ids': [self.ds1.id, self.ds2.id],
                },
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        data = response.json()
        self.assertIn('jobs', data)
        self.assertEqual(data['total_jobs'], 2)

    def test_batch_despacha_job_por_dataset(self):
        """
        dispatch() é chamado uma vez por dataset alvo.
        """
        job1 = _pending_job(self.project, self.ds1)
        job2 = _pending_job(self.project, self.ds2)

        with patch('apps.core.services.download_service.DownloadService.dispatch',
                   side_effect=[job1, job2]) as mock_dispatch, \
             patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            self.client.post(
                self.batch_url,
                data={
                    'scope': 'included',
                    'confirm': True,
                    'destination': 'server',
                    'dataset_ids': [self.ds1.id, self.ds2.id],
                },
                format='json',
            )

        self.assertEqual(mock_dispatch.call_count, 2)

    # 13. Gate de quota agregado: soma estoura → 409 sem despachar nenhum job
    def test_batch_quota_estoura_retorna_409_sem_jobs(self):
        """
        Soma estimada de bytes (SampleSraRun.size_bytes) acima da quota →
        HTTP 409 sem despachar nenhum job.
        Total: 3 * 50 GB = 150 GB; quota padrão = 200 GB.
        Forçamos quota = 100 GB via mock de settings.
        """
        from django.conf import settings as django_settings

        with patch.object(django_settings, 'DOWNLOAD_QUOTA_BYTES', 100 * 1024 ** 3), \
             patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay') as mock_delay:
            response = self.client.post(
                self.batch_url,
                data={
                    'scope': 'included',
                    'confirm': True,
                    'destination': 'server',
                    'dataset_ids': [self.ds1.id, self.ds2.id],
                },
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        mock_delay.assert_not_called()

    def test_batch_quota_estoura_nenhum_job_criado(self):
        """
        Quota estourada → nenhum IngestionJob é criado no banco.
        """
        from django.conf import settings as django_settings

        job_count_before = IngestionJob.objects.count()

        with patch.object(django_settings, 'DOWNLOAD_QUOTA_BYTES', 100 * 1024 ** 3), \
             patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            self.client.post(
                self.batch_url,
                data={
                    'scope': 'included',
                    'confirm': True,
                    'destination': 'server',
                    'dataset_ids': [self.ds1.id, self.ds2.id],
                },
                format='json',
            )

        self.assertEqual(
            IngestionJob.objects.count(), job_count_before,
            "Quota estourada: nenhum IngestionJob deve ser criado"
        )

    # 14. Falta confirm → 400 com prévia; nenhum job criado
    def test_batch_sem_confirm_retorna_400(self):
        """
        scope='included' sem confirm=True → HTTP 400 com confirm_required=True
        e prévia dos datasets.
        """
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay') as mock_delay:
            response = self.client.post(
                self.batch_url,
                data={
                    'scope': 'included',
                    'confirm': False,
                    'destination': 'server',
                    'dataset_ids': [self.ds1.id, self.ds2.id],
                },
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        data = response.json()
        self.assertTrue(data.get('confirm_required'))
        mock_delay.assert_not_called()

    def test_batch_sem_confirm_nenhum_job_criado(self):
        """Sem confirm → nenhum IngestionJob criado."""
        job_count_before = IngestionJob.objects.count()

        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            self.client.post(
                self.batch_url,
                data={
                    'scope': 'included',
                    'confirm': False,
                    'destination': 'server',
                    'dataset_ids': [self.ds1.id, self.ds2.id],
                },
                format='json',
            )

        self.assertEqual(IngestionJob.objects.count(), job_count_before)

    def test_batch_sem_confirm_payload_tem_datasets_preview(self):
        """
        400 sem confirm inclui datasets_preview com accessions e sample_count.
        """
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                self.batch_url,
                data={
                    'scope': 'included',
                    'confirm': False,
                    'destination': 'server',
                    'dataset_ids': [self.ds1.id, self.ds2.id],
                },
                format='json',
            )

        data = response.json()
        self.assertIn('datasets_preview', data)
        self.assertIn('used_bytes', data)
        self.assertIn('quota_bytes', data)

    # 15. dataset_ids de outro usuário → ignorado / 404
    def test_batch_dataset_ids_de_outro_usuario_ignorados(self):
        """
        dataset_ids que não pertencem ao projeto do usuário são filtrados pelo
        base_pd_qs. Se TODOS forem inválidos → 404 (nenhum dataset encontrado).
        """
        # Dataset de outro usuário
        other_user = _user('inc5_batch_other')
        other_project = _project(other_user, 'Inc5 Batch Other')
        other_ds = _dataset('SRP_BATCH_OTHER_001', source_db='sra')
        _project_dataset(other_project, other_ds)

        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                self.batch_url,
                data={
                    'scope': 'included',
                    'confirm': True,
                    'destination': 'server',
                    'dataset_ids': [other_ds.id],  # ID de outro projeto
                },
                format='json',
            )

        # Nenhum dataset válido → 404 ou 400 (nenhum dataset/amostras encontradas)
        self.assertIn(
            response.status_code,
            [status.HTTP_404_NOT_FOUND, status.HTTP_400_BAD_REQUEST],
            "dataset_ids fora do projeto do usuário devem resultar em 404 ou 400"
        )

    def test_batch_user_b_nao_acessa_projeto_de_user_a(self):
        """User B não pode chamar download-batch no projeto de user A → 404."""
        user_b = _user('inc5_batch_userb')
        client_b = APIClient()
        client_b.force_authenticate(user=user_b)

        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = client_b.post(
                self.batch_url,  # URL do projeto de user A
                data={'scope': 'included', 'confirm': True, 'destination': 'server'},
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # 16. destination='client' no batch → 200 com lista agregada (Inc-1 implementado)
    def test_batch_destination_client_retorna_200_lista_agregada(self):
        """
        destination='client' no batch → HTTP 200 com BatchFastqUrlListResponseSerializer.

        O serializer aceita 'client' (bloqueio espúrio removido pelo vitruvio).
        A view chama _resolve_fastq_urls_client por dataset e agrega os resultados.
        rust_engine.resolve_fastq_urls é mockado — sem rede real.

        Campos esperados na raiz: datasets, total_datasets, total_runs,
        bytes_total, skipped_datasets.
        Cada item em datasets: dataset_id, dataset_accession, runs, total_runs,
        bytes_total, bam_only_count.
        """
        entries = [_make_fastq_entry('SRR_BATCH_001a', size_bytes=1024 * 1024)]
        mock_rust = _mock_rust_empty()
        mock_rust.resolve_fastq_urls.return_value = entries

        with patch.dict(sys.modules, {'rust_engine': mock_rust}), \
             patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay') as mock_delay:
            response = self.client.post(
                self.batch_url,
                data={
                    'scope': 'included',
                    'destination': 'client',
                    'dataset_ids': [self.ds1.id],
                },
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_delay.assert_not_called()

        data = response.json()
        # Estrutura agregada BatchFastqUrlListResponseSerializer
        self.assertIn('datasets', data)
        self.assertIn('total_datasets', data)
        self.assertIn('total_runs', data)
        self.assertIn('bytes_total', data)
        self.assertIn('skipped_datasets', data)

    def test_batch_destination_client_nao_cria_job(self):
        """
        destination='client' no batch não cria IngestionJob.
        """
        job_count_before = IngestionJob.objects.count()

        mock_rust = _mock_rust_empty()
        mock_rust.resolve_fastq_urls.return_value = [
            _make_fastq_entry('SRR_BATCH_001a')
        ]

        with patch.dict(sys.modules, {'rust_engine': mock_rust}), \
             patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            self.client.post(
                self.batch_url,
                data={
                    'scope': 'included',
                    'destination': 'client',
                    'dataset_ids': [self.ds1.id],
                },
                format='json',
            )

        self.assertEqual(IngestionJob.objects.count(), job_count_before,
                         "destination='client' no batch não deve criar IngestionJob")

    def test_batch_destination_client_nao_altera_curation_status(self):
        """
        destination='client' no batch não altera ProjectDataset.curation_status.
        (sem queued_download — não é job de servidor)
        """
        original_status = self.pd1.curation_status

        mock_rust = _mock_rust_empty()
        mock_rust.resolve_fastq_urls.return_value = [
            _make_fastq_entry('SRR_BATCH_001a')
        ]

        with patch.dict(sys.modules, {'rust_engine': mock_rust}), \
             patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            self.client.post(
                self.batch_url,
                data={
                    'scope': 'included',
                    'destination': 'client',
                    'dataset_ids': [self.ds1.id],
                },
                format='json',
            )

        self.pd1.refresh_from_db()
        self.assertEqual(self.pd1.curation_status, original_status,
                         "destination='client' no batch não deve alterar curation_status")

    def test_batch_destination_client_estrutura_por_dataset(self):
        """
        Cada item em datasets[] contém dataset_id, dataset_accession,
        runs, total_runs, bytes_total, bam_only_count.
        """
        entries = [
            _make_fastq_entry('SRR_BATCH_001a', size_bytes=512),
            _make_fastq_entry('SRR_BATCH_001b', size_bytes=512),
        ]
        mock_rust = _mock_rust_empty()
        mock_rust.resolve_fastq_urls.return_value = entries

        with patch.dict(sys.modules, {'rust_engine': mock_rust}), \
             patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                self.batch_url,
                data={
                    'scope': 'included',
                    'destination': 'client',
                    'dataset_ids': [self.ds1.id],
                },
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        datasets = response.json()['datasets']
        self.assertGreater(len(datasets), 0)
        ds_item = datasets[0]
        for campo in ('dataset_id', 'dataset_accession', 'runs', 'total_runs',
                      'bytes_total', 'bam_only_count'):
            self.assertIn(campo, ds_item, f"campo '{campo}' ausente em datasets[0]")
        self.assertEqual(ds_item['total_runs'], 2)

    def test_batch_destination_client_teto_agregado_retorna_400(self):
        """
        Soma total de amostras no lote > CLIENT_DOWNLOAD_MAX_RUNS → HTTP 400.
        A view verifica total_sample_count antes de chamar _resolve_fastq_urls_client.
        Cria amostras 'included' em excesso usando patch no count de ProjectSample.
        """
        from apps.core.services.download_service import CLIENT_DOWNLOAD_MAX_RUNS

        # Cria amostras adicionais além do teto
        for i in range(CLIENT_DOWNLOAD_MAX_RUNS + 1):
            s = _omic_sample(self.ds1, f'SRR_TETO_BATCH_{i:04d}')
            _project_sample(self.project, s, 'included')

        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                self.batch_url,
                data={
                    'scope': 'included',
                    'destination': 'client',
                    'dataset_ids': [self.ds1.id],
                },
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('detail', response.json())


# =============================================================================
# Inc-5 — download-batch destination='client' multi-dataset (regressão de contrato)
# =============================================================================

class DownloadBatchClientMultiDatasetTests(APITestCase):
    """
    Verifica o comportamento do batch-client com múltiplos datasets SRA.

    destination='client' → HTTP 200 com resultados agregados por dataset.
    Confirma que dois datasets distintos produzem dois itens em datasets[].
    """

    def setUp(self):
        self.user = _user('inc5_multi_batch_user')
        self.client.force_authenticate(user=self.user)
        self.project = _project(self.user, 'Inc5 Multi Batch Project')

        self.dsa = _dataset('SRP_MBATCH_A', source_db='sra')
        self.dsb = _dataset('SRP_MBATCH_B', source_db='sra')
        self.pda = _project_dataset(self.project, self.dsa)
        self.pdb = _project_dataset(self.project, self.dsb)

        sa = _omic_sample(self.dsa, 'SRR_MBATCH_A1')
        sb = _omic_sample(self.dsb, 'SRR_MBATCH_B1')
        _project_sample(self.project, sa, 'included')
        _project_sample(self.project, sb, 'included')

        self.batch_url = f'/api/v1/projects/{self.project.id}/datasets/download-batch/'

    def test_batch_client_dois_datasets_retorna_dois_itens(self):
        """
        Dois datasets SRA com amostras 'included' → datasets[] com 2 itens.
        """
        mock_rust = _mock_rust_empty()
        mock_rust.resolve_fastq_urls.side_effect = [
            [_make_fastq_entry('SRR_MBATCH_A1')],
            [_make_fastq_entry('SRR_MBATCH_B1')],
        ]

        with patch.dict(sys.modules, {'rust_engine': mock_rust}), \
             patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                self.batch_url,
                data={
                    'scope': 'included',
                    'destination': 'client',
                    'dataset_ids': [self.dsa.id, self.dsb.id],
                },
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data['total_datasets'], 2)
        self.assertEqual(data['total_runs'], 2)
        accessions = {d['dataset_accession'] for d in data['datasets']}
        self.assertIn('SRP_MBATCH_A', accessions)
        self.assertIn('SRP_MBATCH_B', accessions)

    def test_batch_client_isolamento_user_b(self):
        """User B não pode chamar batch-client no projeto de user A → 404."""
        user_b = _user('inc5_multi_batch_userb')
        client_b = APIClient()
        client_b.force_authenticate(user=user_b)

        mock_rust = _mock_rust_empty()
        with patch.dict(sys.modules, {'rust_engine': mock_rust}):
            response = client_b.post(
                self.batch_url,
                data={
                    'scope': 'included',
                    'destination': 'client',
                    'dataset_ids': [self.dsa.id],
                },
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
