"""
test_download_scope_sra.py — Cobertura da feature de seleção de amostras no
download ômico + resolução GEO→SRA (MVP-A e MVP-B).

Plano de referência: .claude/plans/2026-06-22-download-fastq-selecao-e-link-geo-sra.md
Aprovação de segurança: 007 (toca apps/core/views/** — Regra #3, firebase-auth-guard).

Casos cobertos:
  1.  scope='all'           → sample_ids=None repassado ao dispatch (comportamento original)
  2.  scope='included'      → só ProjectSample com curation_status='included'
  3.  scope='included' vazio → 400
  4.  scope='manual' válido  → resolve ProjectSample.id → OmicSample.id corretos
  5a. scope='manual' com ProjectSample.id de outro projeto do mesmo usuário → 404
  5b. scope='manual' com ProjectSample.id de outro usuário → 404
  6.  scope='manual' sem sample_ids → 400
  7.  destination='client'  → 400 (Inc-1 bloqueado)
  8a. GEO sem sra_runs + file_kind='fastq' → 400 orientativo
  8b. GEO com sra_runs em OmicSample → 202 (aceita)
  9a. resolve-sra em dataset GEO → 202 com job SRA_RESOLUTION
  9b. resolve-sra em dataset não-GEO → 400
  9c. resolve-sra de dataset de outro usuário → 404
  10a. sra_resolved=True quando OmicSample tem extra_metadata['sra_runs']
  10b. sra_resolved=False quando nenhum OmicSample tem 'sra_runs'
  11. Idempotência por escopo: dois dispatches com scope diferentes geram jobs distintos

Convenção: sem pytest — usa django.test.TestCase / APITestCase.
Sem internet: rust_engine.download_dataset_files / resolve_sra_runs_for_dataset
  são mockados; nenhuma chamada real à ENA/NCBI.
"""

from __future__ import annotations

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
    slug = f'{title.lower().replace(" ", "-")}-{user.username}'
    return DaVinciProject.objects.create(
        user=user, title=title, slug=slug, query_term='test'
    )


def _dataset(accession: str, source_db: str = 'sra', extra_metadata: dict | None = None) -> OmicDataset:
    return OmicDataset.objects.create(
        accession=accession,
        source_db=source_db,
        title=f'Dataset {accession}',
        omic_type='transcriptomic',
        organism='Homo sapiens',
        extra_metadata=extra_metadata or {},
    )


def _project_dataset(project: DaVinciProject, dataset: OmicDataset, curation_status: str = 'pending') -> ProjectDataset:
    return ProjectDataset.objects.create(
        project=project, dataset=dataset, curation_status=curation_status
    )


def _omic_sample(dataset: OmicDataset, accession: str, extra_metadata: dict | None = None) -> OmicSample:
    return OmicSample.objects.create(
        dataset=dataset,
        accession=accession,
        title=f'Sample {accession}',
        organism='Homo sapiens',
        extra_metadata=extra_metadata or {},
    )


def _project_sample(project: DaVinciProject, sample: OmicSample, curation_status: str = 'pending') -> ProjectSample:
    return ProjectSample.objects.create(
        project=project,
        sample=sample,
        curation_status=curation_status,
    )


def _sra_run(sample: OmicSample, run_accession: str, size_bytes: int | None = None) -> SampleSraRun:
    """Cria uma linha SampleSraRun para o sample (aresta GSM→SRR estruturada, Inc-3)."""
    return SampleSraRun.objects.create(
        sample=sample,
        run_accession=run_accession,
        size_bytes=size_bytes,
    )


def _pending_job(project: DaVinciProject, dataset: OmicDataset,
                 job_type: str = IngestionJob.JobType.FASTQ_DOWNLOAD,
                 scope: str = 'all', sample_ids=None) -> IngestionJob:
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
            'sample_ids': sample_ids,
        },
    )


# =============================================================================
# Mixin: autenticação + dataset SRA + URL base
# =============================================================================

class _SraDownloadMixin:
    """
    Configura usuário, projeto, dataset SRA e URL de download.
    Subclasses chamam super().setUp() para herdar.
    """

    def setUp(self):
        self.user = _user('scope_user')
        self.client.force_authenticate(user=self.user)
        self.project = _project(self.user, 'Scope Project')
        self.dataset = _dataset('SRP_SCOPE_001', source_db='sra')
        self.pd = _project_dataset(self.project, self.dataset)
        self.url = (
            f'/api/v1/projects/{self.project.id}'
            f'/datasets/{self.pd.id}/download/'
        )


# =============================================================================
# 1 — scope='all' → sample_ids=None ao dispatch (comportamento original mantido)
# =============================================================================

class ScopeAllTests(_SraDownloadMixin, APITestCase):
    """
    scope='all' mantém comportamento original: sample_ids=None é repassado ao
    dispatch e, por extensão, ao Rust (que usa WHERE dataset_id internamente).
    """

    def test_scope_all_dispatch_recebe_sample_ids_none(self):
        """
        scope='all' (explícito no body) → DownloadService.dispatch chamado com
        sample_ids=None — o Rust baixa todas as runs sem filtro por amostra.
        """
        job = _pending_job(self.project, self.dataset, scope='all')

        with patch(
            'apps.core.services.download_service.DownloadService.dispatch',
            return_value=job,
        ) as mock_dispatch, \
             patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                self.url,
                data={'confirm': True, 'scope': 'all'},
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        # sample_ids=None indica "todas as runs" ao Rust
        call_kwargs = mock_dispatch.call_args.kwargs
        self.assertIsNone(
            call_kwargs.get('sample_ids'),
            "scope='all' deve passar sample_ids=None ao dispatch"
        )

    def test_scope_omitido_equivale_a_all(self):
        """
        scope omitido no body (padrão 'all') → mesmo comportamento de sample_ids=None.
        """
        job = _pending_job(self.project, self.dataset, scope='all')

        with patch(
            'apps.core.services.download_service.DownloadService.dispatch',
            return_value=job,
        ) as mock_dispatch, \
             patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                self.url,
                data={'confirm': True},
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        call_kwargs = mock_dispatch.call_args.kwargs
        self.assertIsNone(call_kwargs.get('sample_ids'))


# =============================================================================
# 2 & 3 — scope='included'
# =============================================================================

class ScopeIncludedTests(_SraDownloadMixin, APITestCase):
    """
    scope='included' filtra OmicSample pelas ProjectSample com
    curation_status='included' no projeto do usuário autenticado.
    """

    def setUp(self):
        super().setUp()
        # Cria amostras: 1 included, 1 excluded, 1 pending
        self.sample_included = _omic_sample(self.dataset, 'SRR_INC_001')
        self.sample_excluded = _omic_sample(self.dataset, 'SRR_INC_002')
        self.sample_pending = _omic_sample(self.dataset, 'SRR_INC_003')

        self.ps_included = _project_sample(self.project, self.sample_included, 'included')
        self.ps_excluded = _project_sample(self.project, self.sample_excluded, 'excluded')
        self.ps_pending = _project_sample(self.project, self.sample_pending, 'pending')

    def test_scope_included_passa_apenas_omic_ids_incluidos(self):
        """
        scope='included' resolve para os OmicSample.id de status 'included'
        — exclui 'excluded' e 'pending'.
        """
        job = _pending_job(self.project, self.dataset, scope='included',
                           sample_ids=[self.sample_included.id])

        with patch(
            'apps.core.services.download_service.DownloadService.dispatch',
            return_value=job,
        ) as mock_dispatch, \
             patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                self.url,
                data={'confirm': True, 'scope': 'included'},
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        call_kwargs = mock_dispatch.call_args.kwargs
        resolved = call_kwargs.get('sample_ids')

        self.assertIsNotNone(resolved, "sample_ids não pode ser None para scope='included'")
        self.assertIn(
            self.sample_included.id, resolved,
            "OmicSample.id da amostra 'included' deve estar em sample_ids"
        )
        self.assertNotIn(
            self.sample_excluded.id, resolved,
            "OmicSample.id de amostra 'excluded' NÃO deve estar em sample_ids"
        )
        self.assertNotIn(
            self.sample_pending.id, resolved,
            "OmicSample.id de amostra 'pending' NÃO deve estar em sample_ids"
        )

    def test_scope_included_sem_amostras_incluidas_retorna_400(self):
        """
        scope='included' quando nenhuma ProjectSample do dataset tem
        curation_status='included' → 400 orientativo.
        """
        # Garante que todas as amostras estão excluded/pending
        ProjectSample.objects.filter(project=self.project).update(
            curation_status='pending'
        )

        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                self.url,
                data={'confirm': True, 'scope': 'included'},
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        data = response.json()
        self.assertIn('detail', data)

    def test_scope_included_delay_nao_chamado_quando_lista_vazia(self):
        """
        Sem amostras 'included', run_omics_download.delay() NUNCA deve ser chamado.
        """
        ProjectSample.objects.filter(project=self.project).update(
            curation_status='pending'
        )

        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay') as mock_delay:
            self.client.post(
                self.url,
                data={'confirm': True, 'scope': 'included'},
                format='json',
            )

        mock_delay.assert_not_called()


# =============================================================================
# 4 — scope='manual' com sample_ids válidos (ProjectSample.id do projeto)
# =============================================================================

class ScopeManualValidTests(_SraDownloadMixin, APITestCase):
    """
    scope='manual' com ProjectSample.id válidos do projeto resolve
    corretamente para os OmicSample.id correspondentes.
    """

    def setUp(self):
        super().setUp()
        self.sample_a = _omic_sample(self.dataset, 'SRR_MAN_001')
        self.sample_b = _omic_sample(self.dataset, 'SRR_MAN_002')
        self.sample_c = _omic_sample(self.dataset, 'SRR_MAN_003')

        self.ps_a = _project_sample(self.project, self.sample_a, 'included')
        self.ps_b = _project_sample(self.project, self.sample_b, 'pending')
        self.ps_c = _project_sample(self.project, self.sample_c, 'excluded')

    def test_scope_manual_resolve_project_sample_ids_para_omic_sample_ids(self):
        """
        Envia ProjectSample.id [ps_a.id, ps_b.id].
        Dispatch deve receber OmicSample.id [sample_a.id, sample_b.id].
        """
        job = _pending_job(
            self.project, self.dataset, scope='manual',
            sample_ids=sorted([self.sample_a.id, self.sample_b.id])
        )

        with patch(
            'apps.core.services.download_service.DownloadService.dispatch',
            return_value=job,
        ) as mock_dispatch, \
             patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                self.url,
                data={
                    'confirm': True,
                    'scope': 'manual',
                    'sample_ids': [self.ps_a.id, self.ps_b.id],
                },
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        call_kwargs = mock_dispatch.call_args.kwargs
        resolved = call_kwargs.get('sample_ids')

        self.assertIsNotNone(resolved)
        self.assertIn(self.sample_a.id, resolved)
        self.assertIn(self.sample_b.id, resolved)
        # sample_c não foi solicitado
        self.assertNotIn(self.sample_c.id, resolved)

    def test_scope_manual_retorna_202(self):
        """POST com scope='manual' e sample_ids válidos retorna 202."""
        job = _pending_job(
            self.project, self.dataset, scope='manual',
            sample_ids=[self.sample_a.id]
        )

        with patch(
            'apps.core.services.download_service.DownloadService.dispatch',
            return_value=job,
        ), patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                self.url,
                data={
                    'confirm': True,
                    'scope': 'manual',
                    'sample_ids': [self.ps_a.id],
                },
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)


# =============================================================================
# 5 — Isolamento (Regra #3) — caso pedido pelo 007
# =============================================================================

class ScopeManualIsolationTests(_SraDownloadMixin, APITestCase):
    """
    Garante que scope='manual' com ProjectSample.id de outro projeto/usuário
    resulta em 404 — NUNCA vaza existência de amostras alheias.

    Dois cenários cobertos:
    5a. ProjectSample.id que pertence a outro projeto do MESMO usuário → 404
    5b. ProjectSample.id que pertence a projeto de OUTRO usuário → 404
    """

    def setUp(self):
        super().setUp()
        # Amostra válida no projeto do usuário
        self.sample_mine = _omic_sample(self.dataset, 'SRR_ISO_001')
        self.ps_mine = _project_sample(self.project, self.sample_mine, 'included')

    def test_5a_project_sample_de_outro_projeto_mesmo_usuario_retorna_404(self):
        """
        5a: ProjectSample.id pertence a outro projeto do mesmo usuário.
        → 404 (não vaza existência de ids alheios — firebase-auth-guard / Regra #3).
        """
        # Segundo projeto do MESMO usuário com dataset e amostra distintos
        other_project = _project(self.user, 'Other Project Same User')
        other_dataset = _dataset('SRP_ISO_OTHER_001', source_db='sra')
        other_sample = _omic_sample(other_dataset, 'SRR_ISO_OTHER_001')
        _project_dataset(other_project, other_dataset)
        ps_other = _project_sample(other_project, other_sample, 'included')

        # Tenta usar ProjectSample.id do outro projeto na URL do projeto correto
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                self.url,  # URL do projeto original (self.project)
                data={
                    'confirm': True,
                    'scope': 'manual',
                    'sample_ids': [ps_other.id],  # id de OUTRO projeto do mesmo usuário
                },
                format='json',
            )

        self.assertEqual(
            response.status_code, status.HTTP_404_NOT_FOUND,
            "ProjectSample.id de outro projeto do mesmo usuário deve retornar 404"
        )

    def test_5b_project_sample_de_outro_usuario_retorna_404(self):
        """
        5b: ProjectSample.id pertence a projeto de OUTRO usuário.
        → 404 (sem vazar bytes de outro projeto — isolamento total).
        """
        # Usuário B com projeto, dataset e amostra próprios
        user_b = _user('iso_user_b')
        project_b = _project(user_b, 'Project User B')
        dataset_b = _dataset('SRP_ISO_B_001', source_db='sra')
        sample_b = _omic_sample(dataset_b, 'SRR_ISO_B_001')
        _project_dataset(project_b, dataset_b)
        ps_b = _project_sample(project_b, sample_b, 'included')

        # Usuário A (autenticado) tenta usar o ProjectSample.id do usuário B
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                self.url,
                data={
                    'confirm': True,
                    'scope': 'manual',
                    'sample_ids': [ps_b.id],  # id do usuário B
                },
                format='json',
            )

        self.assertEqual(
            response.status_code, status.HTTP_404_NOT_FOUND,
            "ProjectSample.id de outro usuário deve retornar 404 (isolamento total)"
        )

    def test_5c_user_b_nao_acessa_projeto_de_user_a(self):
        """
        User B autenticado não pode disparar download no endpoint do projeto de user A.
        → 404 (projeto não pertence ao usuário — _get_project()).
        """
        user_b = _user('iso_user_b_cross')
        client_b = APIClient()
        client_b.force_authenticate(user=user_b)

        response = client_b.post(
            self.url,  # URL do projeto de self.user (user A)
            data={'confirm': True},
            format='json',
        )

        self.assertEqual(
            response.status_code, status.HTTP_404_NOT_FOUND,
            "User B não deve acessar projeto de user A"
        )


# =============================================================================
# 6 — scope='manual' sem sample_ids → 400
# =============================================================================

class ScopeManualWithoutIdsTests(_SraDownloadMixin, APITestCase):
    """scope='manual' sem sample_ids deve ser rejeitado com 400."""

    def test_scope_manual_sem_sample_ids_retorna_400(self):
        """scope='manual' sem sample_ids → 400 com detalhe orientativo."""
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                self.url,
                data={'confirm': True, 'scope': 'manual'},
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_scope_manual_sample_ids_nulo_retorna_400(self):
        """scope='manual' com sample_ids=null → 400."""
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                self.url,
                data={'confirm': True, 'scope': 'manual', 'sample_ids': None},
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_scope_manual_sample_ids_lista_vazia_retorna_400(self):
        """scope='manual' com sample_ids=[] → 400."""
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                self.url,
                data={'confirm': True, 'scope': 'manual', 'sample_ids': []},
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


# =============================================================================
# 7 — destination='client' (Inc-1 implementado)
# =============================================================================

class DestinationClientTests(_SraDownloadMixin, APITestCase):
    """
    destination='client' está implementado (Inc-1).
    Retorna HTTP 200 com lista de URLs (sem criar IngestionJob, sem quota).
    """

    def test_destination_client_nao_cria_job(self):
        """destination='client' não deve criar IngestionJob nem chamar .delay()."""
        job_count_before = IngestionJob.objects.count()

        import sys
        mock_rust = MagicMock()
        mock_rust.resolve_fastq_urls.return_value = []

        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay') as mock_delay, \
             patch.dict(sys.modules, {'rust_engine': mock_rust}):
            self.client.post(
                self.url,
                data={
                    'confirm': True,
                    'scope': 'all',
                    'destination': 'client',
                },
                format='json',
            )

        mock_delay.assert_not_called()
        self.assertEqual(IngestionJob.objects.count(), job_count_before)

    def test_destination_server_e_aceito(self):
        """destination='server' (padrão explícito) → não é rejeitado pelo serializer."""
        job = _pending_job(self.project, self.dataset, scope='all')

        with patch(
            'apps.core.services.download_service.DownloadService.dispatch',
            return_value=job,
        ), patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                self.url,
                data={
                    'confirm': True,
                    'scope': 'all',
                    'destination': 'server',
                },
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)


# =============================================================================
# 8 — Guard GEO→FASTQ
# =============================================================================

class GeoFastqGuardTests(APITestCase):
    """
    8a. Dataset GEO sem sra_runs + file_kind='fastq' → 400 orientativo.
    8b. Dataset GEO com extra_metadata['sra_runs'] em OmicSample → 202.
    """

    def setUp(self):
        self.user = _user('geo_guard_user')
        self.client.force_authenticate(user=self.user)
        self.project = _project(self.user, 'GEO Guard Project')
        self.geo_dataset = _dataset('GSE_GUARD_001', source_db='geo')
        self.pd = _project_dataset(self.project, self.geo_dataset)
        self.url = (
            f'/api/v1/projects/{self.project.id}'
            f'/datasets/{self.pd.id}/download/'
        )

    def test_8a_geo_sem_sra_runs_fastq_retorna_400(self):
        """
        Dataset GEO sem nenhum OmicSample com 'sra_runs' em extra_metadata.
        Pedido file_kind='fastq' → 400 com instrução de resolver SRA antes.
        """
        # Nenhuma OmicSample com sra_runs — guard deve bloquear
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                self.url,
                data={'confirm': True, 'file_kind': 'fastq'},
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        data = response.json()
        self.assertIn('detail', data)
        # Mensagem deve orientar o usuário a executar resolve-sra antes
        self.assertIn('resolve', data['detail'].lower())

    def test_8a_geo_sem_sra_runs_delay_nao_chamado(self):
        """GEO sem sra_runs → run_omics_download.delay() não deve ser chamado."""
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay') as mock_delay:
            self.client.post(
                self.url,
                data={'confirm': True, 'file_kind': 'fastq'},
                format='json',
            )

        mock_delay.assert_not_called()

    def test_8b_geo_com_sra_runs_resolvidos_aceita_fastq(self):
        """
        Dataset GEO com ao menos um SampleSraRun resolvido (Inc-3: tabela estruturada).
        Pedido file_kind='fastq' → 202 (guard liberado).

        A implementação (Inc-3) usa SampleSraRun, não extra_metadata['sra_runs'].
        O guard na view executa:
          SampleSraRun.objects.filter(sample__dataset=dataset).exists()
        """
        # Cria OmicSample GSM e liga via SampleSraRun (estrutura Inc-3)
        gsm = _omic_sample(self.geo_dataset, 'GSM_WITH_SRA_001')
        _sra_run(gsm, 'SRR000001')
        _sra_run(gsm, 'SRR000002')

        job = _pending_job(self.project, self.geo_dataset, scope='all')

        with patch(
            'apps.core.services.download_service.DownloadService.dispatch',
            return_value=job,
        ), patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                self.url,
                data={'confirm': True, 'file_kind': 'fastq'},
                format='json',
            )

        self.assertEqual(
            response.status_code, status.HTTP_202_ACCEPTED,
            "Dataset GEO com SampleSraRun resolvidos deve aceitar file_kind='fastq'"
        )

    def test_8a_geo_sem_samplesrarun_bloqueia(self):
        """
        Dataset GEO sem nenhuma linha SampleSraRun → guard bloqueia → 400.
        (OmicSample com extra_metadata qualquer não libera o guard — Inc-3.)
        """
        # OmicSample sem SampleSraRun vinculado — mesmo com extra_metadata preenchido
        _omic_sample(
            self.geo_dataset,
            'GSM_NO_SRA_RUN_001',
            extra_metadata={'relation': 'SRA: https://www.ncbi.nlm.nih.gov/sra?term=SRX999'},
        )

        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            response = self.client.post(
                self.url,
                data={'confirm': True, 'file_kind': 'fastq'},
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


# =============================================================================
# 9 — Action resolve_sra
# =============================================================================

class ResolveSraActionTests(APITestCase):
    """
    9a. POST .../resolve-sra/ em dataset GEO → 202 com job SRA_RESOLUTION.
    9b. POST .../resolve-sra/ em dataset não-GEO → 400.
    9c. POST .../resolve-sra/ de dataset de outro usuário → 404.
    """

    def setUp(self):
        self.user = _user('resolve_sra_user')
        self.client.force_authenticate(user=self.user)
        self.project = _project(self.user, 'Resolve SRA Project')

        # Dataset GEO
        self.geo_dataset = _dataset('GSE_RESOLVE_001', source_db='geo')
        self.pd_geo = _project_dataset(self.project, self.geo_dataset)
        self.url_geo = (
            f'/api/v1/projects/{self.project.id}'
            f'/datasets/{self.pd_geo.id}/resolve-sra/'
        )

        # Dataset SRA (não-GEO)
        self.sra_dataset = _dataset('SRP_RESOLVE_001', source_db='sra')
        self.pd_sra = _project_dataset(self.project, self.sra_dataset)
        self.url_sra = (
            f'/api/v1/projects/{self.project.id}'
            f'/datasets/{self.pd_sra.id}/resolve-sra/'
        )

    def test_9a_resolve_sra_geo_retorna_202(self):
        """POST resolve-sra/ em dataset GEO → 202 com job SRA_RESOLUTION."""
        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.SRA_RESOLUTION,
            status=IngestionJob.JobStatus.PENDING,
            parameters={
                'dataset_id': self.geo_dataset.id,
                'dataset_accession': self.geo_dataset.accession,
                'source_db': 'geo',
            },
        )

        with patch(
            'apps.core.services.download_service.SraResolutionService.dispatch_resolution',
            return_value=job,
        ):
            response = self.client.post(self.url_geo, format='json')

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_9a_resolve_sra_response_contem_job_type_sra_resolution(self):
        """Resposta do resolve-sra/ inclui job_type=sra_resolution."""
        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.SRA_RESOLUTION,
            status=IngestionJob.JobStatus.PENDING,
            parameters={
                'dataset_id': self.geo_dataset.id,
                'dataset_accession': self.geo_dataset.accession,
                'source_db': 'geo',
            },
        )

        with patch(
            'apps.core.services.download_service.SraResolutionService.dispatch_resolution',
            return_value=job,
        ):
            response = self.client.post(self.url_geo, format='json')

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        data = response.json()
        self.assertIn('job_type', data)
        self.assertEqual(data['job_type'], IngestionJob.JobType.SRA_RESOLUTION)

    def test_9b_resolve_sra_nao_geo_retorna_400(self):
        """POST resolve-sra/ em dataset SRA (não-GEO) → 400 com detalhe."""
        with patch('apps.core.tasks.ingestion_tasks.run_sra_resolution.delay'):
            response = self.client.post(self.url_sra, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        data = response.json()
        self.assertIn('detail', data)

    def test_9b_resolve_sra_nao_geo_nao_cria_job(self):
        """Não-GEO → nenhum IngestionJob SRA_RESOLUTION é criado."""
        count_before = IngestionJob.objects.filter(
            job_type=IngestionJob.JobType.SRA_RESOLUTION
        ).count()

        with patch('apps.core.tasks.ingestion_tasks.run_sra_resolution.delay'):
            self.client.post(self.url_sra, format='json')

        count_after = IngestionJob.objects.filter(
            job_type=IngestionJob.JobType.SRA_RESOLUTION
        ).count()
        self.assertEqual(count_before, count_after)

    def test_9c_resolve_sra_de_outro_usuario_retorna_404(self):
        """User B não pode disparar resolve-sra/ no projeto de user A."""
        user_b = _user('resolve_sra_userb')
        client_b = APIClient()
        client_b.force_authenticate(user=user_b)

        response = client_b.post(self.url_geo, format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_9a_resolve_sra_nao_autenticado_retorna_401_ou_403(self):
        """Sem autenticação → 401/403."""
        client_anon = APIClient()
        response = client_anon.post(self.url_geo, format='json')
        self.assertIn(response.status_code, [
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        ])


# =============================================================================
# 10 — sra_resolved no serializer
# =============================================================================

class SraResolvedSerializerTests(APITestCase):
    """
    sra_resolved=True quando OmicSample tem extra_metadata['sra_runs'];
    sra_resolved=False quando nenhum OmicSample tem a chave.

    Usa GET /projects/{pk}/datasets/ para verificar o campo no response
    (view anota com Exists subquery — sem N+1).
    """

    def setUp(self):
        self.user = _user('sra_resolved_user')
        self.client.force_authenticate(user=self.user)
        self.project = _project(self.user, 'SRA Resolved Project')

    def _list_url(self):
        return f'/api/v1/projects/{self.project.id}/datasets/'

    def _get_items(self, response):
        """
        Extrai a lista de datasets do response, independente de paginação.
        A view usa PageNumberPagination: response.data = {'count': N, 'results': [...]}.
        """
        data = response.json()
        if isinstance(data, dict) and 'results' in data:
            return data['results']
        return data  # fallback para resposta sem paginação

    def test_10a_sra_resolved_true_quando_ha_sra_runs(self):
        """
        Dataset GEO com ao menos um SampleSraRun resolvido (Inc-3: tabela estruturada)
        → sra_resolved=true na listagem.

        A anotação na view usa Exists(SampleSraRun.filter(sample__dataset_id=...)),
        não extra_metadata['sra_runs'].
        """
        geo_dataset = _dataset('GSE_RESOLVED_001', source_db='geo')
        _project_dataset(self.project, geo_dataset)

        # Cria OmicSample e materializa a aresta via SampleSraRun (Inc-3)
        gsm = _omic_sample(geo_dataset, 'GSM_RESOLVED_001')
        _sra_run(gsm, 'SRR100001')

        response = self.client.get(self._list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        items = self._get_items(response)
        # Filtra pelo accession do dataset criado
        item = next(
            (i for i in items if i.get('accession') == 'GSE_RESOLVED_001'),
            None
        )
        self.assertIsNotNone(item, "Dataset não aparece na listagem")
        self.assertIn('sra_resolved', item, "Campo sra_resolved ausente no serializer")
        self.assertTrue(item['sra_resolved'], "sra_resolved deve ser True")

    def test_10b_sra_resolved_false_quando_sem_sra_runs(self):
        """
        Dataset GEO com OmicSample mas sem nenhuma linha SampleSraRun
        → sra_resolved=false na listagem.
        (extra_metadata['relation'] preenchido não é suficiente — Inc-3 usa SampleSraRun.)
        """
        geo_dataset = _dataset('GSE_NOT_RESOLVED_001', source_db='geo')
        _project_dataset(self.project, geo_dataset)

        # OmicSample sem SampleSraRun vinculado (relation presente mas não resolvido)
        _omic_sample(
            geo_dataset,
            'GSM_NOT_RESOLVED_001',
            extra_metadata={'relation': 'SRA: https://www.ncbi.nlm.nih.gov/sra?term=SRX000001'},
        )

        response = self.client.get(self._list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        items = self._get_items(response)
        item = next(
            (i for i in items if i.get('accession') == 'GSE_NOT_RESOLVED_001'),
            None
        )
        self.assertIsNotNone(item, "Dataset não aparece na listagem")
        self.assertFalse(item.get('sra_resolved'), "sra_resolved deve ser False")

    def test_10c_sra_resolved_false_sem_nenhuma_omic_sample(self):
        """
        Dataset sem nenhuma OmicSample → sra_resolved=false.
        """
        geo_dataset = _dataset('GSE_NO_SAMPLES_001', source_db='geo')
        _project_dataset(self.project, geo_dataset)

        response = self.client.get(self._list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        items = self._get_items(response)
        item = next(
            (i for i in items if i.get('accession') == 'GSE_NO_SAMPLES_001'),
            None
        )
        self.assertIsNotNone(item)
        self.assertFalse(item.get('sra_resolved'), "sra_resolved deve ser False sem amostras")

    def test_10d_sra_resolved_presente_para_dataset_sra(self):
        """
        Dataset SRA (não-GEO) também expõe sra_resolved=false
        (campo presente, mas sem sra_runs relevantes).
        """
        sra_dataset = _dataset('SRP_RESOLVED_TEST_001', source_db='sra')
        _project_dataset(self.project, sra_dataset)

        response = self.client.get(self._list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        items = self._get_items(response)
        item = next(
            (i for i in items if i.get('accession') == 'SRP_RESOLVED_TEST_001'),
            None
        )
        self.assertIsNotNone(item)
        self.assertIn('sra_resolved', item, "Campo sra_resolved deve estar presente")
        self.assertFalse(item['sra_resolved'])

    def test_10e_sem_n_plus_1_listagem_multiplos_datasets(self):
        """
        Listagem com múltiplos datasets GEO usa a anotação Exists (sem N+1).
        Verificação indireta: listagem de 5 datasets retorna 200 e todos têm
        o campo sra_resolved (garantindo que a anotação foi aplicada a todos).

        Datasets pares têm SampleSraRun → sra_resolved=True.
        Datasets ímpares não têm → sra_resolved=False.
        """
        for i in range(5):
            ds = _dataset(f'GSE_MULTI_{i:03d}', source_db='geo')
            _project_dataset(self.project, ds)
            if i % 2 == 0:
                gsm = _omic_sample(ds, f'GSM_MULTI_{i:03d}')
                _sra_run(gsm, f'SRR9{i:05d}')

        response = self.client.get(self._list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        items = self._get_items(response)

        for item in items:
            self.assertIn('sra_resolved', item,
                          f"sra_resolved ausente para dataset {item.get('accession')}")


# =============================================================================
# 11 — Idempotência por escopo
# =============================================================================

class IdempotenciaPorEscopoTests(_SraDownloadMixin, APITestCase):
    """
    Dois dispatches com scope diferentes para o mesmo dataset geram jobs distintos
    (não reusa silenciosamente — cada scope é rastreável no histórico de jobs).

    BUG CONHECIDO (reportado para vitruvio):
    DownloadService.dispatch usa `parameters__sample_ids__isnull=True` para
    detectar jobs com scope='all' (sample_ids=None). Porém o job é persistido
    com `{'sample_ids': None}` no JSONB, o que armazena {"sample_ids": null}.
    A query `(parameters->'sample_ids') IS NULL` retorna FALSE para valor JSON null
    (pois a chave existe — `->` devolve o literal JSON `null`, não SQL NULL).
    Resultado: a idempotência de scope='all' não funciona — cada dispatch cria
    um job novo em vez de reutilizar o existente.
    Fix correto em DownloadService.dispatch linha 182:
        existing_filter.filter(parameters__sample_ids__isnull=True)
        → existing_filter.filter(parameters__sample_ids=None)
    Ticket: bug de produção em apps/core/services/download_service.py:182.
    """

    def setUp(self):
        super().setUp()
        self.sample_a = _omic_sample(self.dataset, 'SRR_IDEM_001')
        self.sample_b = _omic_sample(self.dataset, 'SRR_IDEM_002')
        self.ps_a = _project_sample(self.project, self.sample_a, 'included')
        self.ps_b = _project_sample(self.project, self.sample_b, 'included')

    def test_scope_all_e_included_geram_jobs_distintos(self):
        """
        Dispatch com scope='all' e depois scope='included' para o mesmo dataset
        → dois jobs distintos (não reusa o job de 'all' para 'included').
        """
        from apps.core.services.download_service import DownloadService

        # Dispatch 1: scope='all'
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            job_all = DownloadService.dispatch(
                project=self.project,
                dataset=self.dataset,
                file_kind='fastq',
                user=self.user,
                confirm=True,
                scope='all',
                sample_ids=None,
            )

        # Dispatch 2: scope='included' com lista de ids
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            job_included = DownloadService.dispatch(
                project=self.project,
                dataset=self.dataset,
                file_kind='fastq',
                user=self.user,
                confirm=True,
                scope='included',
                sample_ids=sorted([self.sample_a.id, self.sample_b.id]),
            )

        self.assertNotEqual(
            job_all.id, job_included.id,
            "Escopos distintos devem gerar jobs distintos (rastreabilidade)"
        )

    def test_scope_identico_all_idempotencia_bug_regression(self):
        """
        Regressão: dois dispatches com scope='all' (sample_ids=None) para o mesmo
        dataset deveriam retornar o mesmo job (idempotência), mas atualmente criam
        dois jobs distintos por bug no filtro isnull em JSONField.

        BUG: DownloadService.dispatch linha 182 usa __isnull=True em vez de =None.
        `parameters__sample_ids__isnull=True` gera SQL `(parameters->'sample_ids') IS NULL`,
        que é FALSE quando o valor armazenado é o literal JSON null (chave existe).
        Fix: substituir por `parameters__sample_ids=None` (gera `= 'null'::jsonb`).
        Agente responsável: vitruvio (apps/core/services/download_service.py).
        """
        from apps.core.services.download_service import DownloadService

        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            job_1 = DownloadService.dispatch(
                project=self.project,
                dataset=self.dataset,
                file_kind='fastq',
                user=self.user,
                confirm=True,
                scope='all',
                sample_ids=None,
            )
            job_2 = DownloadService.dispatch(
                project=self.project,
                dataset=self.dataset,
                file_kind='fastq',
                user=self.user,
                confirm=True,
                scope='all',
                sample_ids=None,
            )

        # Este assert FALHA com o bug atual — registra a regressão esperada.
        # Quando vitruvio corrigir download_service.py:182, este teste passará.
        self.assertEqual(
            job_1.id, job_2.id,
            "REGRESSÃO (bug vitruvio): scope='all' não é idempotente. "
            "Fix: download_service.py:182 — trocar __isnull=True por =None"
        )

    def test_scope_manual_identico_e_idempotente(self):
        """
        scope='manual' com os mesmos sample_ids → retorna o mesmo job.
        (O filtro `parameters__sample_ids=[...]` funciona corretamente
        para listas — não tem o bug do isnull que afeta scope='all'.)
        """
        from apps.core.services.download_service import DownloadService

        ids = sorted([self.sample_a.id, self.sample_b.id])
        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            job_1 = DownloadService.dispatch(
                project=self.project,
                dataset=self.dataset,
                file_kind='fastq',
                user=self.user,
                confirm=True,
                scope='manual',
                sample_ids=ids,
            )
            job_2 = DownloadService.dispatch(
                project=self.project,
                dataset=self.dataset,
                file_kind='fastq',
                user=self.user,
                confirm=True,
                scope='manual',
                sample_ids=ids,
            )

        self.assertEqual(
            job_1.id, job_2.id,
            "scope='manual' com mesmos sample_ids deve ser idempotente"
        )

    def test_scope_manual_com_ids_distintos_gera_jobs_distintos(self):
        """
        scope='manual' com conjuntos de sample_ids diferentes para o mesmo dataset
        → jobs distintos (rastreabilidade de cada seleção).
        """
        from apps.core.services.download_service import DownloadService

        with patch('apps.core.tasks.ingestion_tasks.run_omics_download.delay'):
            job_subset_a = DownloadService.dispatch(
                project=self.project,
                dataset=self.dataset,
                file_kind='fastq',
                user=self.user,
                confirm=True,
                scope='manual',
                sample_ids=[self.sample_a.id],
            )
            job_subset_ab = DownloadService.dispatch(
                project=self.project,
                dataset=self.dataset,
                file_kind='fastq',
                user=self.user,
                confirm=True,
                scope='manual',
                sample_ids=sorted([self.sample_a.id, self.sample_b.id]),
            )

        self.assertNotEqual(
            job_subset_a.id, job_subset_ab.id,
            "sample_ids distintos devem gerar jobs distintos"
        )
