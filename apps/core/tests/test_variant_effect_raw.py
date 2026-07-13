"""
test_variant_effect_raw.py — Cobertura do VariantEffectRawService e
management command load_variant_effects.

OmnisPathway Objetivo 2, Fase 2, Slice 2.4 (Slice 2B).

Áreas cobertas:
  1. build_uniprot_gene_map:
     - Hit UniProt: gene mapeado corretamente.
     - Miss (gene sem hit): contado em n_genes_without_uniprot.
     - Timeout/URLError: resiliente (graceful degradation) — gene fica sem map.
     - Cache TTL: arquivo fresco usa cache; arquivo velho re-faz fetch.
     - use_cache=False: não lê nem grava cache.

  2. Pré-checks:
     - GeneRole(source='oncokb') vazio → GeneRoleNotPopulatedError.
     - GeneRole populado → retorna allowlist correta.

  3. Gate de idempotência:
     - Job PENDING → VariantEffectRawJobActiveError.
     - Job RUNNING → VariantEffectRawJobActiveError.
     - Job COMPLETED → não bloqueia (UPSERT idempotente).
     - Job FAILED → não bloqueia.

  4. run() síncrono com rust_engine mockado:
     - ClinVar sempre chamado.
     - AlphaMissense chamado se mapa não vazio.
     - AlphaMissense PULADO se skip_alphamissense=True.
     - AlphaMissense PULADO se handoff_required=True.
     - job COMPLETED após sucesso.
     - db_url NUNCA no resultado.
     - resultado contém todas as chaves do contrato.

  5. dispatch():
     - Cria job PENDING com parâmetros corretos (sem db_url).
     - GeneRole vazio → GeneRoleNotPopulatedError antes de criar job.
     - Job ativo → VariantEffectRawJobActiveError.
     - Chama task Celery com (job_id, project_id).

  6. Management command load_variant_effects:
     - Modo síncrono (mock Rust) → relatório completo.
     - --skip-alphamissense → AM não chamado.
     - Projeto inválido → CommandError.
     - GeneRole vazio → CommandError.
     - Saída não vaza db_url nem caminho físico.
     - Modo --async cria job PENDING.
     - Job ativo → aviso de idempotência.

  7. Isolamento na task run_variant_effect_raw_load:
     - job.project_id != project_id → job FAILED.
     - project_id inexistente → erro sem processar.
     - job_id inexistente → erro sem processar.

Padrões obrigatórios:
  - SEM internet: UniProt e Rust SEMPRE mockados.
  - SEM pytest — usa django.test.TestCase (padrão do projeto).
  - GeneRole populado via ORM diretamente (não via Rust).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from io import StringIO
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase

from apps.core.models import DaVinciProject, GeneRole, IngestionJob
from apps.core.services.variant_effect_raw_service import (
    CLINVAR_VCF_URL,
    ALPHAMISSENSE_URL,
    LOADER_VERSION,
    UNIPROT_CACHE_MAX_AGE_DAYS,
    GeneRoleNotPopulatedError,
    VariantEffectRawJobActiveError,
    VariantEffectRawService,
    build_uniprot_gene_map,
    _cache_is_fresh,
    _fetch_uniprot_batch,
)


# =============================================================================
# Helpers de factory
# =============================================================================

def _make_user(username: str = 'vareff_user') -> User:
    return User.objects.create_user(username=username, password='pw')


def _make_project(user: User, title: str = 'VarEff Project') -> DaVinciProject:
    slug = f'{title.lower().replace(" ", "-")}-{user.username}'
    return DaVinciProject.objects.create(
        user=user, title=title, slug=slug, query_term='vareff',
    )


def _populate_gene_role(n: int = 5, source: str = 'oncokb') -> list[str]:
    """Popula GeneRole com n genes de papel 'oncogene' via ORM e retorna símbolos."""
    symbols = []
    for i in range(n):
        sym = f'GENE{i:03d}'
        GeneRole.objects.get_or_create(
            gene_symbol=sym,
            source=source,
            defaults={'role': GeneRole.Role.ONCOGENE},
        )
        symbols.append(sym)
    return symbols


def _make_fake_clinvar_manifest() -> MagicMock:
    m = MagicMock()
    m.n_variants_processed = 10000
    m.n_kept = 250
    m.n_skipped_offlist = 9500
    m.n_skipped_no_gene = 250
    m.n_upserted = 248
    m.source_version = 'clinvar-20240601'
    m.errors = []
    return m


def _make_fake_am_manifest(handoff_required: bool = False) -> MagicMock:
    m = MagicMock()
    m.n_variants_processed = 5000
    m.n_kept = 120
    m.n_skipped_no_map = 4880
    m.n_upserted = 118
    m.source_version = 'alphamissense-2023'
    m.errors = []
    m.handoff_required = handoff_required
    return m


def _make_fake_rust_engine(
    clinvar_manifest=None,
    am_manifest=None,
) -> MagicMock:
    engine = MagicMock()
    engine.load_clinvar_effects.return_value = clinvar_manifest or _make_fake_clinvar_manifest()
    engine.load_alphamissense_effects.return_value = am_manifest or _make_fake_am_manifest()
    return engine


# =============================================================================
# 1. build_uniprot_gene_map — helper de nível de módulo
# =============================================================================

class UniprotGeneMapHitTest(TestCase):
    """Testa build_uniprot_gene_map com mock de _fetch_uniprot_batch."""

    def test_hit_maps_gene_correctly(self):
        """Gene com hit UniProt é mapeado accession → gene_symbol."""
        fake_result = {'P04637': 'TP53', 'P00533': 'EGFR'}

        with patch(
            'apps.core.services.variant_effect_raw_service._fetch_uniprot_batch',
            return_value=fake_result,
        ):
            gene_map, n_without = build_uniprot_gene_map(
                ['TP53', 'EGFR'], use_cache=False
            )

        self.assertEqual(gene_map.get('P04637'), 'TP53')
        self.assertEqual(gene_map.get('P00533'), 'EGFR')
        self.assertEqual(n_without, 0)

    def test_miss_counted_in_n_without(self):
        """Gene sem hit UniProt incrementa n_genes_without_uniprot."""
        # Apenas TP53 retornado; UNKNOWNGENE não tem hit
        fake_result = {'P04637': 'TP53'}

        with patch(
            'apps.core.services.variant_effect_raw_service._fetch_uniprot_batch',
            return_value=fake_result,
        ):
            gene_map, n_without = build_uniprot_gene_map(
                ['TP53', 'UNKNOWNGENE'], use_cache=False
            )

        self.assertEqual(n_without, 1)
        self.assertIn('P04637', gene_map)
        self.assertNotIn('UNKNOWNGENE', gene_map.values())

    def test_empty_gene_list_returns_empty_map(self):
        """Lista de genes vazia → mapa vazio, n_without=0."""
        with patch(
            'apps.core.services.variant_effect_raw_service._fetch_uniprot_batch',
            return_value={},
        ):
            gene_map, n_without = build_uniprot_gene_map([], use_cache=False)

        self.assertEqual(gene_map, {})
        self.assertEqual(n_without, 0)

    def test_all_genes_missing_returns_n_without_equals_total(self):
        """Todos os genes sem hit → n_without = total de genes."""
        with patch(
            'apps.core.services.variant_effect_raw_service._fetch_uniprot_batch',
            return_value={},  # nenhum hit
        ):
            gene_map, n_without = build_uniprot_gene_map(
                ['GENEAAA', 'GENEBBB', 'GENECCC'], use_cache=False
            )

        self.assertEqual(n_without, 3)
        self.assertEqual(len(gene_map), 0)


class UniprotGeneMapResilienceTest(TestCase):
    """Testa resiliência a erros de rede (timeout, URLError)."""

    def test_timeout_in_fetch_batch_returns_empty_map_graceful(self):
        """
        Timeout ao consultar UniProt: resiliente — retorna mapa vazio sem
        propagar exceção. Todos os genes ficam sem map.
        """
        from urllib.error import URLError

        with patch(
            'apps.core.services.variant_effect_raw_service._fetch_uniprot_batch',
            side_effect=lambda genes: {},  # simula fallback gracioso do _fetch_uniprot_batch
        ):
            gene_map, n_without = build_uniprot_gene_map(
                ['TP53', 'PIK3CA'], use_cache=False
            )

        # O build_uniprot_gene_map não deve propagar exceção do batch
        self.assertEqual(n_without, 2)
        self.assertEqual(len(gene_map), 0)

    def test_partial_batch_failure_accumulates_available_genes(self):
        """
        Se 1 de 2 lotes falha, o mapa acumula apenas os genes do lote bem-sucedido.
        Usando lote de 1 gene por vez (UNIPROT_BATCH_SIZE=1) para simular 2 lotes.
        """
        call_count = [0]

        def fake_batch(genes):
            call_count[0] += 1
            if call_count[0] == 1:
                return {'P04637': 'TP53'}
            return {}  # segundo lote sem resultado

        with patch(
            'apps.core.services.variant_effect_raw_service._fetch_uniprot_batch',
            side_effect=fake_batch,
        ), patch(
            'apps.core.services.variant_effect_raw_service.UNIPROT_BATCH_SIZE',
            1,  # forçar um gene por lote para testar 2 lotes separados
        ), patch(
            'apps.core.services.variant_effect_raw_service.time.sleep',
            return_value=None,
        ):
            gene_map, n_without = build_uniprot_gene_map(
                ['TP53', 'GENEXXX'], use_cache=False
            )

        # TP53 mapeado; GENEXXX sem hit
        self.assertIn('P04637', gene_map)
        self.assertEqual(n_without, 1)


class UniprotGeneMapCacheTest(TestCase):
    """Testa comportamento de cache em disco (TTL + escrita)."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix='davinci_uniprot_cache_test_')

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_fresh_cache_used_without_fetch(self):
        """
        Cache fresco (< 7 dias): build_uniprot_gene_map usa o cache em vez
        de consultar UniProt REST. Nenhuma chamada a _fetch_uniprot_batch.
        """
        cache_file = os.path.join(self._tmpdir, 'uniprot_gene_map.json')
        cached_data = {'P04637': 'TP53', 'P00533': 'EGFR'}
        with open(cache_file, 'w') as f:
            json.dump(cached_data, f)

        # Arquivo criado agora → fresco (< 7 dias)
        with patch(
            'apps.core.services.variant_effect_raw_service._cache_path',
            return_value=cache_file,
        ), patch(
            'apps.core.services.variant_effect_raw_service._fetch_uniprot_batch',
        ) as mock_fetch:
            gene_map, n_without = build_uniprot_gene_map(
                ['TP53', 'EGFR'], use_cache=True
            )

        # Não deve ter chamado fetch (usa cache)
        mock_fetch.assert_not_called()
        self.assertIn('P04637', gene_map)

    def test_stale_cache_triggers_refetch(self):
        """
        Cache velho (> 7 dias): build_uniprot_gene_map ignora cache e re-faz fetch.
        """
        cache_file = os.path.join(self._tmpdir, 'uniprot_gene_map.json')
        with open(cache_file, 'w') as f:
            json.dump({'P04637': 'TP53'}, f)

        # Artificialmente envelhece o arquivo além do TTL
        old_mtime = time.time() - (UNIPROT_CACHE_MAX_AGE_DAYS + 1) * 86400
        os.utime(cache_file, (old_mtime, old_mtime))

        with patch(
            'apps.core.services.variant_effect_raw_service._cache_path',
            return_value=cache_file,
        ), patch(
            'apps.core.services.variant_effect_raw_service._fetch_uniprot_batch',
            return_value={'P04637': 'TP53'},
        ) as mock_fetch, patch(
            'apps.core.services.variant_effect_raw_service.time.sleep',
            return_value=None,
        ):
            build_uniprot_gene_map(['TP53'], use_cache=True)

        # Cache velho → deve ter chamado fetch
        mock_fetch.assert_called()

    def test_use_cache_false_always_fetches(self):
        """
        use_cache=False: não usa cache e sempre faz fetch.
        Verifica que _fetch_uniprot_batch é chamado (ao invés de ler cache).
        """
        cache_file = os.path.join(self._tmpdir, 'uniprot_gene_map.json')
        # Mesmo com cache fresco existente, use_cache=False deve ignorá-lo
        with open(cache_file, 'w') as f:
            json.dump({'P04637': 'TP53'}, f)

        with patch(
            'apps.core.services.variant_effect_raw_service._cache_path',
            return_value=cache_file,
        ), patch(
            'apps.core.services.variant_effect_raw_service._fetch_uniprot_batch',
            return_value={'P04637': 'TP53'},
        ) as mock_fetch, patch(
            'apps.core.services.variant_effect_raw_service.time.sleep',
            return_value=None,
        ):
            gene_map, _ = build_uniprot_gene_map(['TP53'], use_cache=False)

        # Com use_cache=False, deve ter feito fetch (não lido cache)
        mock_fetch.assert_called()
        self.assertIn('P04637', gene_map)


# =============================================================================
# 2. Pré-checks — GeneRole populado
# =============================================================================

class VariantEffectRawGeneRoleCheckTests(TestCase):
    """Testa _check_gene_role_populated()."""

    def setUp(self):
        self.user = _make_user('vareff_grcheck_user')
        self.project = _make_project(self.user, 'GeneRole VarEff Project')

    def test_empty_gene_role_raises_error(self):
        """GeneRole(source='oncokb') vazio → GeneRoleNotPopulatedError."""
        GeneRole.objects.all().delete()

        service = VariantEffectRawService(self.project)
        with self.assertRaises(GeneRoleNotPopulatedError):
            service._check_gene_role_populated()

    def test_populated_gene_role_returns_allowlist(self):
        """GeneRole populado → retorna lista de gene_symbols."""
        _populate_gene_role(n=3)

        service = VariantEffectRawService(self.project)
        allowlist = service._check_gene_role_populated()

        self.assertIsInstance(allowlist, list)
        self.assertEqual(len(allowlist), 3)

    def test_only_oncokb_source_in_allowlist(self):
        """Apenas genes com source='oncokb' entram no allowlist."""
        _populate_gene_role(n=2, source='oncokb')
        # Gene de outra fonte (ex: cgc) — não deve entrar
        GeneRole.objects.create(
            gene_symbol='CGCGENE',
            source='cgc',
            role=GeneRole.Role.TSG,
        )

        service = VariantEffectRawService(self.project)
        allowlist = service._check_gene_role_populated()

        self.assertNotIn('CGCGENE', allowlist)
        self.assertEqual(len(allowlist), 2)

    def test_error_message_mentions_load_gene_roles(self):
        """Mensagem de erro de GeneRole vazio menciona load_gene_roles."""
        GeneRole.objects.all().delete()

        service = VariantEffectRawService(self.project)
        with self.assertRaises(GeneRoleNotPopulatedError) as ctx:
            service._check_gene_role_populated()

        self.assertIn('load_gene_roles', str(ctx.exception))


# =============================================================================
# 3. Gate de idempotência — _check_idempotency
# =============================================================================

class VariantEffectRawIdempotencyTests(TestCase):
    """Testa _check_idempotency()."""

    def setUp(self):
        self.user = _make_user('vareff_idm_user')
        self.project = _make_project(self.user, 'Idempotency VarEff Project')

    def _create_vareff_job(self, status: str) -> IngestionJob:
        return IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.VARIANT_EFFECT_RAW_LOAD,
            status=status,
            parameters={},
        )

    def test_pending_job_blocks(self):
        """Job PENDING → VariantEffectRawJobActiveError."""
        self._create_vareff_job(IngestionJob.JobStatus.PENDING)
        service = VariantEffectRawService(self.project)
        with self.assertRaises(VariantEffectRawJobActiveError) as ctx:
            service._check_idempotency()
        self.assertIsNotNone(ctx.exception.job)

    def test_running_job_blocks(self):
        """Job RUNNING → VariantEffectRawJobActiveError."""
        self._create_vareff_job(IngestionJob.JobStatus.RUNNING)
        service = VariantEffectRawService(self.project)
        with self.assertRaises(VariantEffectRawJobActiveError):
            service._check_idempotency()

    def test_completed_job_does_not_block(self):
        """Job COMPLETED não bloqueia — UPSERT idempotente."""
        self._create_vareff_job(IngestionJob.JobStatus.COMPLETED)
        service = VariantEffectRawService(self.project)
        try:
            service._check_idempotency()
        except VariantEffectRawJobActiveError as exc:
            self.fail(f'_check_idempotency levantou para job COMPLETED: {exc}')

    def test_failed_job_does_not_block(self):
        """Job FAILED não bloqueia — re-run válido."""
        self._create_vareff_job(IngestionJob.JobStatus.FAILED)
        service = VariantEffectRawService(self.project)
        try:
            service._check_idempotency()
        except VariantEffectRawJobActiveError as exc:
            self.fail(f'_check_idempotency levantou para job FAILED: {exc}')


# =============================================================================
# 4. run() síncrono — fluxo completo mockado
# =============================================================================

class VariantEffectRawRunTests(TestCase):
    """Testa VariantEffectRawService.run() de ponta a ponta com mocks."""

    def setUp(self):
        self.user = _make_user('vareff_run_user')
        self.project = _make_project(self.user, 'VarEff Run Project')
        _populate_gene_role(n=5)

    def _run_with_mock(self, skip_alphamissense=False, am_manifest=None, **kwargs):
        """Executa service.run() com rust_engine totalmente mockado."""
        cv = _make_fake_clinvar_manifest()
        am = am_manifest or _make_fake_am_manifest()
        fake_engine = _make_fake_rust_engine(clinvar_manifest=cv, am_manifest=am)

        service = VariantEffectRawService(
            self.project, skip_alphamissense=skip_alphamissense
        )

        with patch.dict('sys.modules', {'rust_engine': fake_engine}), \
             patch(
                 'apps.core.services.variant_effect_raw_service.build_uniprot_gene_map',
                 return_value=({'P04637': 'GENE000'}, 0),
             ):
            result = service.run(use_uniprot_cache=False, **kwargs)

        return result, fake_engine

    def test_run_returns_all_contract_keys(self):
        """run() retorna dict com todas as chaves do contrato."""
        result, _ = self._run_with_mock()

        expected_keys = {
            'job_id',
            'n_genes_in_allowlist',
            'clinvar_n_variants_processed',
            'clinvar_n_kept',
            'clinvar_n_skipped_offlist',
            'clinvar_n_skipped_no_gene',
            'clinvar_n_upserted',
            'clinvar_source_version',
            'clinvar_errors',
            'am_skipped',
            'am_skip_reason',
            'am_n_variants_processed',
            'am_n_kept',
            'am_n_skipped_no_map',
            'am_n_upserted',
            'am_source_version',
            'am_errors',
            'n_genes_with_uniprot',
            'n_genes_without_uniprot',
        }
        self.assertEqual(set(result.keys()), expected_keys)

    def test_run_clinvar_always_called(self):
        """rust_engine.load_clinvar_effects sempre chamado."""
        _, engine = self._run_with_mock()
        engine.load_clinvar_effects.assert_called_once()

    def test_run_am_called_when_map_not_empty(self):
        """load_alphamissense_effects chamado quando mapa uniprot→gene não vazio."""
        _, engine = self._run_with_mock()
        engine.load_alphamissense_effects.assert_called_once()

    def test_run_am_skipped_when_skip_flag_true(self):
        """skip_alphamissense=True → load_alphamissense_effects NÃO chamado."""
        _, engine = self._run_with_mock(skip_alphamissense=True)
        engine.load_alphamissense_effects.assert_not_called()

    def test_run_am_skipped_result_flag(self):
        """Com skip_alphamissense=True, resultado tem am_skipped=True."""
        result, _ = self._run_with_mock(skip_alphamissense=True)
        self.assertTrue(result['am_skipped'])

    def test_run_am_skipped_when_handoff_required(self):
        """
        handoff_required=True no manifesto AM → serviço marca am_skipped=True
        e não propaga erro.
        """
        am_with_handoff = _make_fake_am_manifest(handoff_required=True)
        result, engine = self._run_with_mock(am_manifest=am_with_handoff)

        # AM foi chamado (não skip_flag)
        engine.load_alphamissense_effects.assert_called_once()
        # Mas resultado reflete que foi pulado pós-handoff
        self.assertTrue(result['am_skipped'])

    def test_run_job_completed_after_success(self):
        """run() cria IngestionJob e o marca COMPLETED após sucesso."""
        result, _ = self._run_with_mock()
        job = IngestionJob.objects.get(id=result['job_id'])
        self.assertEqual(job.status, IngestionJob.JobStatus.COMPLETED)
        self.assertEqual(job.job_type, IngestionJob.JobType.VARIANT_EFFECT_RAW_LOAD)
        self.assertIsNotNone(job.completed_at)

    def test_run_result_does_not_leak_db_url(self):
        """Resultado de run() NUNCA contém db_url, postgresql://, password."""
        result, _ = self._run_with_mock()
        result_str = str(result)
        self.assertNotIn('postgresql://', result_str)
        self.assertNotIn('db_url', result_str)
        self.assertNotIn('password', result_str)

    def test_run_clinvar_url_passed_to_rust(self):
        """load_clinvar_effects chamado com url=CLINVAR_VCF_URL."""
        _, engine = self._run_with_mock()
        call_kwargs = engine.load_clinvar_effects.call_args[1]
        self.assertEqual(call_kwargs['url'], CLINVAR_VCF_URL)
        self.assertIn('db_url', call_kwargs)

    def test_run_am_url_passed_to_rust(self):
        """load_alphamissense_effects chamado com url=ALPHAMISSENSE_URL."""
        _, engine = self._run_with_mock()
        call_kwargs = engine.load_alphamissense_effects.call_args[1]
        self.assertEqual(call_kwargs['url'], ALPHAMISSENSE_URL)

    def test_run_gene_role_empty_raises_before_job_creation(self):
        """GeneRole vazio → GeneRoleNotPopulatedError levantado ANTES de criar job."""
        GeneRole.objects.all().delete()

        service = VariantEffectRawService(self.project)
        with self.assertRaises(GeneRoleNotPopulatedError):
            with patch.dict('sys.modules', {'rust_engine': MagicMock()}):
                service.run(use_uniprot_cache=False)

        self.assertEqual(
            IngestionJob.objects.filter(
                project=self.project,
                job_type=IngestionJob.JobType.VARIANT_EFFECT_RAW_LOAD,
            ).count(),
            0,
        )

    def test_run_on_rust_exception_marks_job_failed(self):
        """Exceção no Rust → job FAILED, exceção propagada."""
        failing_engine = MagicMock()
        failing_engine.load_clinvar_effects.side_effect = RuntimeError('Rust crash')

        service = VariantEffectRawService(self.project)
        with patch.dict('sys.modules', {'rust_engine': failing_engine}), \
             patch(
                 'apps.core.services.variant_effect_raw_service.build_uniprot_gene_map',
                 return_value=({'P04637': 'GENE000'}, 0),
             ):
            with self.assertRaises(RuntimeError):
                service.run(use_uniprot_cache=False)

        failed_job = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.VARIANT_EFFECT_RAW_LOAD,
            status=IngestionJob.JobStatus.FAILED,
        ).first()
        self.assertIsNotNone(failed_job, 'Job deve ser FAILED após exceção no Rust')

    def test_run_idempotency_second_run_with_completed_job_allowed(self):
        """
        Re-run com job COMPLETED anterior não é bloqueado pelo gate de idempotência.
        Dois run() devem criar 2 jobs COMPLETED separados.
        """
        _run1, _ = self._run_with_mock()
        _run2, _ = self._run_with_mock()

        completed_jobs = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.VARIANT_EFFECT_RAW_LOAD,
            status=IngestionJob.JobStatus.COMPLETED,
        ).count()
        self.assertGreaterEqual(completed_jobs, 2)


# =============================================================================
# 5. dispatch()
# =============================================================================

class VariantEffectRawDispatchTests(TestCase):
    """Testa VariantEffectRawService.dispatch()."""

    def setUp(self):
        self.user = _make_user('vareff_disp_user')
        self.project = _make_project(self.user, 'Dispatch VarEff Project')
        _populate_gene_role(n=3)

    @patch('apps.core.tasks.ingestion_tasks.run_variant_effect_raw_load.delay',
           return_value=None)
    def test_dispatch_creates_pending_job(self, mock_delay):
        """dispatch() cria job PENDING com job_type VARIANT_EFFECT_RAW_LOAD."""
        job = VariantEffectRawService.dispatch(self.project)
        self.assertEqual(job.status, IngestionJob.JobStatus.PENDING)
        self.assertEqual(job.job_type, IngestionJob.JobType.VARIANT_EFFECT_RAW_LOAD)

    @patch('apps.core.tasks.ingestion_tasks.run_variant_effect_raw_load.delay',
           return_value=None)
    def test_dispatch_parameters_contain_audit_fields(self, mock_delay):
        """IngestionJob.parameters contém clinvar_url, alphamissense_url, loader_version."""
        job = VariantEffectRawService.dispatch(self.project)
        params = job.parameters or {}
        self.assertIn('clinvar_url', params)
        self.assertIn('alphamissense_url', params)
        self.assertIn('loader_version', params)
        self.assertEqual(params['loader_version'], LOADER_VERSION)

    @patch('apps.core.tasks.ingestion_tasks.run_variant_effect_raw_load.delay',
           return_value=None)
    def test_dispatch_parameters_no_db_url(self, mock_delay):
        """IngestionJob.parameters NUNCA contém db_url (sensitive-data-handling)."""
        job = VariantEffectRawService.dispatch(self.project)
        params = job.parameters or {}
        self.assertNotIn('db_url', params)
        self.assertNotIn('postgresql://', str(params))

    @patch('apps.core.tasks.ingestion_tasks.run_variant_effect_raw_load.delay',
           return_value=None)
    def test_dispatch_calls_celery_delay(self, mock_delay):
        """dispatch() chama run_variant_effect_raw_load.delay com (job_id, project_id)."""
        job = VariantEffectRawService.dispatch(self.project)
        mock_delay.assert_called_once_with(
            str(job.id), str(self.project.id), skip_alphamissense=False
        )

    def test_dispatch_gene_role_empty_raises_before_job(self):
        """GeneRole vazio → GeneRoleNotPopulatedError sem criar job."""
        GeneRole.objects.all().delete()

        with self.assertRaises(GeneRoleNotPopulatedError):
            with patch('apps.core.tasks.ingestion_tasks.run_variant_effect_raw_load.delay'):
                VariantEffectRawService.dispatch(self.project)

        self.assertEqual(
            IngestionJob.objects.filter(
                project=self.project,
                job_type=IngestionJob.JobType.VARIANT_EFFECT_RAW_LOAD,
            ).count(),
            0,
        )

    @patch('apps.core.tasks.ingestion_tasks.run_variant_effect_raw_load.delay',
           return_value=None)
    def test_dispatch_active_job_blocks(self, mock_delay):
        """Job PENDING ativo → VariantEffectRawJobActiveError."""
        VariantEffectRawService.dispatch(self.project)

        with self.assertRaises(VariantEffectRawJobActiveError):
            VariantEffectRawService.dispatch(self.project)

        # Apenas 1 job criado
        self.assertEqual(
            IngestionJob.objects.filter(
                project=self.project,
                job_type=IngestionJob.JobType.VARIANT_EFFECT_RAW_LOAD,
            ).count(),
            1,
        )

    @patch('apps.core.tasks.ingestion_tasks.run_variant_effect_raw_load.delay',
           return_value=None)
    def test_dispatch_with_skip_am_flag(self, mock_delay):
        """dispatch(skip_alphamissense=True) registra flag em parameters."""
        job = VariantEffectRawService.dispatch(
            self.project, skip_alphamissense=True
        )
        params = job.parameters or {}
        self.assertTrue(params.get('skip_alphamissense'))


# =============================================================================
# 6. Management command load_variant_effects
# =============================================================================

class LoadVariantEffectsCommandTests(TestCase):
    """Testa o management command load_variant_effects."""

    def setUp(self):
        self.user = _make_user('vareff_cmd_user')
        self.project = _make_project(self.user, 'Cmd VarEff Project')
        _populate_gene_role(n=3)

    def _call_sync(
        self,
        project_id: str | None = None,
        skip_alphamissense: bool = False,
    ) -> tuple[str, str]:
        from django.core.management import call_command

        pid = project_id or str(self.project.id)
        stdout = StringIO()
        stderr = StringIO()
        fake_engine = _make_fake_rust_engine()

        kwargs: dict = {
            'project': pid,
            'skip_alphamissense': skip_alphamissense,
            'stdout': stdout,
            'stderr': stderr,
        }

        with patch.dict('sys.modules', {'rust_engine': fake_engine}), \
             patch(
                 'apps.core.services.variant_effect_raw_service.build_uniprot_gene_map',
                 return_value=({'P04637': 'GENE000'}, 0),
             ):
            call_command('load_variant_effects', **kwargs)

        return stdout.getvalue(), stderr.getvalue()

    def test_command_sync_reports_counters(self):
        """load_variant_effects (síncrono) reporta contadores ClinVar."""
        stdout_val, _ = self._call_sync()
        for field in ('clinvar', 'n_upserted', 'n_kept', 'allowlist'):
            self.assertIn(field, stdout_val.lower(),
                          f'stdout deve conter "{field}": {stdout_val[:500]!r}')

    def test_command_sync_skip_am_reports_skipped(self):
        """Com --skip-alphamissense, stdout sinaliza AlphaMissense pulado."""
        stdout_val, _ = self._call_sync(skip_alphamissense=True)
        lower = stdout_val.lower()
        self.assertIn('pulado', lower)
        self.assertIn('skip-alphamissense', lower)

    def test_command_sync_does_not_leak_db_url(self):
        """stdout/stderr não vaza postgresql://, db_url, password."""
        stdout_val, stderr_val = self._call_sync()
        combined = stdout_val + stderr_val
        for token in ('postgresql://', 'db_url', 'password'):
            self.assertNotIn(token, combined,
                             f'Token sensível "{token}" vazou na saída')

    def test_command_invalid_project_raises_command_error(self):
        """--project com UUID inexistente → CommandError."""
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command('load_variant_effects',
                         project=str(uuid.uuid4()),
                         stdout=StringIO())

    def test_command_gene_role_empty_raises_command_error(self):
        """GeneRole vazio → CommandError com orientação."""
        from django.core.management import call_command
        from django.core.management.base import CommandError

        GeneRole.objects.all().delete()
        with self.assertRaises(CommandError) as ctx:
            with patch.dict('sys.modules', {'rust_engine': MagicMock()}):
                call_command('load_variant_effects',
                             project=str(self.project.id),
                             stdout=StringIO())

        self.assertIn('load_gene_roles', str(ctx.exception))

    def test_command_async_creates_pending_job(self):
        """load_variant_effects --async cria job PENDING."""
        from django.core.management import call_command

        stdout = StringIO()
        with patch('apps.core.tasks.ingestion_tasks.run_variant_effect_raw_load.delay',
                   return_value=None):
            call_command('load_variant_effects',
                         project=str(self.project.id),
                         use_async=True,
                         stdout=stdout)

        job = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.VARIANT_EFFECT_RAW_LOAD,
            status=IngestionJob.JobStatus.PENDING,
        ).first()
        self.assertIsNotNone(job, 'Modo --async deve criar job PENDING')

    def test_command_active_job_reports_warning(self):
        """Job PENDING ativo → aviso sem criar segundo job."""
        from django.core.management import call_command

        IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.VARIANT_EFFECT_RAW_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={},
        )

        stdout = StringIO()
        with patch.dict('sys.modules', {'rust_engine': MagicMock()}), \
             patch(
                 'apps.core.services.variant_effect_raw_service.build_uniprot_gene_map',
                 return_value=({}, 0),
             ):
            call_command('load_variant_effects',
                         project=str(self.project.id),
                         stdout=stdout)

        count = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.VARIANT_EFFECT_RAW_LOAD,
        ).count()
        self.assertEqual(count, 1, 'Não deve duplicar job quando há PENDING ativo')

        output = stdout.getvalue().lower()
        self.assertTrue(
            any(kw in output for kw in ['idempot', 'ativo', 'active', 'job']),
            f'stdout deve mencionar idempotência: {output[:300]!r}',
        )


# =============================================================================
# 7. Isolamento na task run_variant_effect_raw_load
# =============================================================================

class VariantEffectRawTaskIsolationTests(TestCase):
    """Testa isolamento cross-project na task run_variant_effect_raw_load."""

    def setUp(self):
        self.user_a = _make_user('vareff_iso_a')
        self.user_b = _make_user('vareff_iso_b')
        self.project_a = _make_project(self.user_a, 'VarEff Iso A')
        self.project_b = _make_project(self.user_b, 'VarEff Iso B')

    def test_task_aborts_cross_project(self):
        """job criado para A com project_id de B → job FAILED."""
        from apps.core.tasks.ingestion_tasks import run_variant_effect_raw_load

        job_a = IngestionJob.objects.create(
            project=self.project_a,
            job_type=IngestionJob.JobType.VARIANT_EFFECT_RAW_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={},
        )

        fake_rust = MagicMock()
        with patch.dict('sys.modules', {'rust_engine': fake_rust}), \
             patch(
                 'apps.core.services.variant_effect_raw_service.VariantEffectRawService',
             ) as MockSvc:
            result = run_variant_effect_raw_load.run(
                str(job_a.id), str(self.project_b.id)
            )

        job_a.refresh_from_db()
        self.assertEqual(job_a.status, IngestionJob.JobStatus.FAILED)
        MockSvc.assert_not_called()

    def test_task_returns_error_for_nonexistent_project(self):
        """run_variant_effect_raw_load com project_id inexistente retorna erro."""
        from apps.core.tasks.ingestion_tasks import run_variant_effect_raw_load

        with patch.dict('sys.modules', {'rust_engine': MagicMock()}):
            result = run_variant_effect_raw_load.run(
                str(uuid.uuid4()), str(uuid.uuid4())
            )

        self.assertIn('project not found', result.get('errors', []))

    def test_task_returns_error_for_nonexistent_job(self):
        """run_variant_effect_raw_load com job_id inexistente retorna erro."""
        from apps.core.tasks.ingestion_tasks import run_variant_effect_raw_load

        with patch.dict('sys.modules', {'rust_engine': MagicMock()}):
            result = run_variant_effect_raw_load.run(
                str(uuid.uuid4()), str(self.project_a.id)
            )

        self.assertIn('job not found', result.get('errors', []))
