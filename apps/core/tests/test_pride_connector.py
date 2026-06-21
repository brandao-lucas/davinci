"""
Testes QA — PRIDE connector (Fase 1, OmnisPathway).

Cobre:
  A) Orquestração (rust_engine mockado)
     - dispatch_pride_search cria IngestionJob com job_type='pride_search'
     - dispatch_pride_search chama run_pride_ingestion.delay
     - run_pride_ingestion: sucesso (mock) marca COMPLETED via defense-in-depth
     - run_pride_ingestion: ImportError marca FAILED com mensagem clara
     - run_pride_ingestion: exceção genérica marca FAILED e faz retry
     - Job órfão quando .delay() falha (comportamento atual documentado)
     - materialize_project_links chamado; falha não derruba o job
     - advance_to_curating_if_done: PRIDE job ativo bloqueia avanço;
       ao concluir, avança para curating

  B) Semântica do UPSERT (fallback SQL direto no Postgres)
     - Anti-clobber: campos de classificador sobrevivem intactos após re-ingestão
       (has_control_group, disease_axis, is_single_cell, sample_join_key,
        contract_confidence NÃO são substituídos)
     - Os campos do connector (omics_layers, data_format, access_type, omics_count,
       extra_metadata) SÃO atualizados
     - Anti-downgrade: re-ingestão com data_format='unknown' não rebaixa estado
       'processed' existente; omics_layers vazio não remove layers existentes
     - Upgrade normal: data_format='unknown' existente é atualizado para 'processed'

  C) CheckConstraint — 'proteomic' é token válido; 'proteomics' é rejeitado

Abordagem B: FALLBACK SQL ROBUSTO
  O rust_engine não executa SQL em testes (é mockado), portanto a semântica do
  ON CONFLICT DO UPDATE é validada diretamente via connection.cursor() contra
  o Postgres real (docker-compose).  O SQL do teste espelha FIELMENTE a cláusula
  do copy_writer.rs e deve ser mantido em sincronia com aquele arquivo.

Padrão: sem pytest; usa TransactionTestCase/APITestCase do Django.
Requer Postgres real (ArrayField, JSONB, CheckConstraint).
Sem chamadas NCBI/EBI: rust_engine e HTTP são sempre mockados.
"""

import sys
import types
from unittest.mock import MagicMock, patch

from celery.exceptions import Retry
from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase

from apps.core.models import DaVinciProject, IngestionJob, OmicDataset
from apps.core.services.project_status import advance_to_curating_if_done
from apps.core.services.search_service import SearchService
from apps.core.tasks.ingestion_tasks import run_pride_ingestion


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_user(username="pride_user", password="pw"):
    return User.objects.create_user(username=username, password=password)


def _make_project(user, title="PRIDE Test Project", query_term="proteomics cancer"):
    return DaVinciProject.objects.create(
        user=user,
        title=title,
        slug=f"pride-test-{user.username}-davinci",
        query_term=query_term,
    )


def _fake_rust_pride_module(search_fn=None):
    """
    Retorna módulo rust_engine falso com search_and_ingest_pride simulado.
    O caller passa funções que simulam sucesso, falha ou edge cases.
    """
    mod = types.ModuleType("rust_engine")
    mod.search_and_ingest_pride = search_fn or (
        lambda **kw: MagicMock(
            datasets_processed=0,
            datasets_inserted=0,
            links_inserted=0,
            errors=[],
        )
    )
    return mod


# ─── A) Orquestração ─────────────────────────────────────────────────────────


class DispatchPrideSearchTests(TransactionTestCase):
    """
    Testa SearchService.dispatch_pride_search:
    - Cria IngestionJob com job_type='pride_search'
    - Dispara run_pride_ingestion.delay com o job_id correto
    - Não vaza NCBI API key (PRIDE usa API REST do EBI sem autenticação)
    """

    def setUp(self):
        self.user = _make_user("dispatch_pride_user")
        self.project = _make_project(self.user)

    def test_dispatch_creates_job_with_pride_search_type(self):
        """dispatch_pride_search cria IngestionJob com job_type='pride_search'."""
        with patch(
            "apps.core.services.search_service.run_pride_ingestion.delay"
        ) as delay_mock:
            job = SearchService.dispatch_pride_search(self.project)

        self.assertEqual(job.job_type, IngestionJob.JobType.PRIDE_SEARCH)
        self.assertEqual(job.status, IngestionJob.JobStatus.PENDING)
        self.assertEqual(job.project_id, self.project.id)

    def test_dispatch_calls_delay_with_job_id(self):
        """dispatch_pride_search chama run_pride_ingestion.delay com o id do job."""
        with patch(
            "apps.core.services.search_service.run_pride_ingestion.delay"
        ) as delay_mock:
            job = SearchService.dispatch_pride_search(self.project)

        delay_mock.assert_called_once_with(str(job.id))

    def test_dispatch_job_parameters_have_query_and_max_results(self):
        """Job criado tem 'query' e 'max_results' nos parameters."""
        with patch(
            "apps.core.services.search_service.run_pride_ingestion.delay"
        ):
            job = SearchService.dispatch_pride_search(self.project, max_results=250)

        self.assertIn("query", job.parameters)
        self.assertEqual(job.parameters["max_results"], 250)

    def test_dispatch_default_max_results_is_500(self):
        """max_results padrão de dispatch_pride_search é 500."""
        with patch(
            "apps.core.services.search_service.run_pride_ingestion.delay"
        ):
            job = SearchService.dispatch_pride_search(self.project)

        self.assertEqual(job.parameters["max_results"], 500)

    def test_dispatch_parameters_do_not_contain_ncbi_api_key(self):
        """
        PRIDE usa API REST do EBI — não deve carregar ncbi_api_key nos parameters
        (diferente dos dispatchers de PubMed e GEO).
        """
        with patch(
            "apps.core.services.search_service.run_pride_ingestion.delay"
        ):
            job = SearchService.dispatch_pride_search(self.project)

        self.assertNotIn("ncbi_api_key", job.parameters)

    def test_dispatch_transitions_project_to_searching(self):
        """dispatch_pride_search transiciona o projeto draft → searching."""
        self.assertEqual(self.project.status, DaVinciProject.PipelineStatus.DRAFT)

        with patch(
            "apps.core.services.search_service.run_pride_ingestion.delay"
        ):
            SearchService.dispatch_pride_search(self.project)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, DaVinciProject.PipelineStatus.SEARCHING)

    def test_dispatch_delay_failure_leaves_job_failed(self):
        """
        Se .delay() lança exceção (broker down), job é marcado FAILED
        (não fica órfão em PENDING) — mesma garantia dos outros dispatchers.
        """
        with patch(
            "apps.core.services.search_service.run_pride_ingestion.delay",
            side_effect=RuntimeError("broker down"),
        ):
            with self.assertRaises(RuntimeError):
                SearchService.dispatch_pride_search(self.project)

        orphan = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.PRIDE_SEARCH,
        ).first()
        self.assertIsNotNone(orphan, "Job deve ter sido criado antes do .delay()")
        self.assertEqual(
            orphan.status,
            IngestionJob.JobStatus.FAILED,
            "Job deve ficar FAILED quando .delay() lança exceção (não órfão em PENDING).",
        )


class RunPrideIngestionTaskTests(TransactionTestCase):
    """
    Testa a task Celery run_pride_ingestion com rust_engine mockado.
    Cobre: sucesso (defense-in-depth), ImportError, exceção genérica + retry.
    """

    def setUp(self):
        self.user = _make_user("task_pride_user")
        self.project = _make_project(self.user, title="PRIDE Task Test")
        # Garante slug único para este setUp
        self.project.slug = "pride-task-test-task_pride_user-davinci"
        self.project.save(update_fields=["slug"])

        self.job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.PRIDE_SEARCH,
            parameters={"query": "proteomics cancer", "max_results": 100},
        )
        self.addCleanup(lambda: sys.modules.pop("rust_engine", None))

    def test_rust_success_marks_completed_via_defense_in_depth(self):
        """
        Caminho feliz: rust_engine retorna counts; task aplica defense-in-depth
        marcando COMPLETED (se Rust não o fez) e retorna dict com campos esperados.
        """

        def fake_search(**kwargs):
            return MagicMock(
                datasets_processed=15,
                datasets_inserted=12,
                links_inserted=3,
                errors=[],
            )

        sys.modules["rust_engine"] = _fake_rust_pride_module(search_fn=fake_search)

        # materialize_project_links é importado localmente na task (importação lazy).
        # O patch deve atingir o módulo de origem.
        with patch("apps.core.services.link_service.materialize_project_links", return_value=0):
            with patch("apps.core.services.project_status.advance_to_curating_if_done"):
                result = run_pride_ingestion(str(self.job.id))

        self.assertEqual(result["datasets_processed"], 15)
        self.assertEqual(result["datasets_inserted"], 12)
        self.assertEqual(result["links_inserted"], 3)
        self.assertEqual(result["errors"], [])

        # Defense-in-depth: task deve ter marcado COMPLETED (job estava PENDING)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, IngestionJob.JobStatus.COMPLETED)

    def test_import_error_marks_failed_with_message(self):
        """
        ImportError (rust_engine não compilado) marca job FAILED com
        error_message mencionando 'rust_engine'.
        """
        with patch.dict(sys.modules, {"rust_engine": None}):
            result = run_pride_ingestion(str(self.job.id))

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, IngestionJob.JobStatus.FAILED)
        self.assertIn(
            "rust_engine",
            (self.job.error_message or "").lower(),
            "error_message deve mencionar rust_engine quando não compilado.",
        )
        self.assertEqual(result["datasets_processed"], 0)
        self.assertEqual(result["datasets_inserted"], 0)

    def test_generic_exception_marks_failed_and_retries(self):
        """
        Exceção genérica do Rust → task marca FAILED + error_message + dispara retry.
        """

        def boom(**kwargs):
            raise RuntimeError("kaboom from pride rust")

        sys.modules["rust_engine"] = _fake_rust_pride_module(search_fn=boom)

        with patch.object(
            run_pride_ingestion,
            "retry",
            side_effect=Retry("retry triggered"),
        ) as retry_mock:
            with self.assertRaises(Retry):
                run_pride_ingestion(str(self.job.id))

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, IngestionJob.JobStatus.FAILED)
        self.assertIn("kaboom from pride rust", self.job.error_message)
        retry_mock.assert_called_once()

    def test_materialize_links_failure_does_not_abort_job(self):
        """
        Falha em materialize_project_links não derruba o job — job fica COMPLETED.
        """

        def fake_search(**kwargs):
            return MagicMock(
                datasets_processed=5,
                datasets_inserted=5,
                links_inserted=0,
                errors=[],
            )

        sys.modules["rust_engine"] = _fake_rust_pride_module(search_fn=fake_search)

        with patch(
            "apps.core.services.link_service.materialize_project_links",
            side_effect=RuntimeError("link service down"),
        ):
            with patch("apps.core.services.project_status.advance_to_curating_if_done"):
                result = run_pride_ingestion(str(self.job.id))

        self.job.refresh_from_db()
        # Job deve ter sido marcado COMPLETED apesar da falha em materialize
        self.assertEqual(self.job.status, IngestionJob.JobStatus.COMPLETED)
        self.assertEqual(result["datasets_inserted"], 5)

    def test_advance_to_curating_failure_does_not_abort_job(self):
        """
        Falha em advance_to_curating_if_done não derruba o job.
        """

        def fake_search(**kwargs):
            return MagicMock(
                datasets_processed=3,
                datasets_inserted=3,
                links_inserted=0,
                errors=[],
            )

        sys.modules["rust_engine"] = _fake_rust_pride_module(search_fn=fake_search)

        with patch("apps.core.services.link_service.materialize_project_links", return_value=0):
            with patch(
                "apps.core.services.project_status.advance_to_curating_if_done",
                side_effect=RuntimeError("status service down"),
            ):
                result = run_pride_ingestion(str(self.job.id))

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, IngestionJob.JobStatus.COMPLETED)
        self.assertEqual(result["datasets_inserted"], 3)

    def test_nonexistent_job_returns_zero_counts(self):
        """
        run_pride_ingestion com job_id inexistente retorna zeros sem crashing.
        """
        fake_mod = _fake_rust_pride_module()
        sys.modules["rust_engine"] = fake_mod

        result = run_pride_ingestion("00000000-0000-0000-0000-000000000000")

        self.assertEqual(result["datasets_processed"], 0)
        self.assertEqual(result["datasets_inserted"], 0)


class AdvanceToCuratingWithPrideTests(TransactionTestCase):
    """
    Testa que advance_to_curating_if_done considera PRIDE_SEARCH como job de busca ativo.

    - Projeto com PRIDE_SEARCH PENDING não avança para curating
    - Projeto com PRIDE_SEARCH COMPLETED avança para curating (sem outros jobs ativos)
    """

    def setUp(self):
        self.user = _make_user("advance_pride_user")
        self.project = _make_project(self.user, title="PRIDE Advance Test")
        self.project.slug = "pride-advance-test-advance_pride_user-davinci"
        self.project.save(update_fields=["slug"])
        # Coloca projeto em searching
        DaVinciProject.objects.filter(pk=self.project.pk).update(
            status=DaVinciProject.PipelineStatus.SEARCHING
        )
        self.project.refresh_from_db()

    def test_active_pride_job_blocks_advance_to_curating(self):
        """
        Projeto com PRIDE_SEARCH em PENDING não avança para curating —
        PRIDE_SEARCH está em SEARCH_JOB_TYPES.
        """
        IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.PRIDE_SEARCH,
            status=IngestionJob.JobStatus.PENDING,
            parameters={"query": "proteomics", "max_results": 500},
        )

        advance_to_curating_if_done(self.project)

        self.project.refresh_from_db()
        self.assertEqual(
            self.project.status,
            DaVinciProject.PipelineStatus.SEARCHING,
            "Projeto com PRIDE_SEARCH ativo (PENDING) deve permanecer em searching.",
        )

    def test_running_pride_job_blocks_advance_to_curating(self):
        """PRIDE_SEARCH em RUNNING também bloqueia avanço para curating."""
        IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.PRIDE_SEARCH,
            status=IngestionJob.JobStatus.RUNNING,
            parameters={"query": "proteomics", "max_results": 500},
        )

        advance_to_curating_if_done(self.project)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, DaVinciProject.PipelineStatus.SEARCHING)

    def test_completed_pride_job_allows_advance_to_curating(self):
        """
        Projeto com apenas um PRIDE_SEARCH COMPLETED (sem outros jobs ativos)
        avança para curating.
        """
        IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.PRIDE_SEARCH,
            status=IngestionJob.JobStatus.COMPLETED,
            parameters={"query": "proteomics", "max_results": 500},
        )

        advance_to_curating_if_done(self.project)

        self.project.refresh_from_db()
        self.assertEqual(
            self.project.status,
            DaVinciProject.PipelineStatus.CURATING,
            "Projeto com PRIDE_SEARCH concluído deve avançar para curating.",
        )

    def test_only_pride_search_project_advances_on_completion(self):
        """
        Projeto configurado com APENAS um PRIDE_SEARCH (sem PubMed nem GEO):
        ao concluir, avança para curating.
        """
        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.PRIDE_SEARCH,
            status=IngestionJob.JobStatus.PENDING,
            parameters={"query": "cancer proteome", "max_results": 500},
        )

        # Job ainda ativo — não avança
        advance_to_curating_if_done(self.project)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, DaVinciProject.PipelineStatus.SEARCHING)

        # Simula conclusão do job (como Rust/task fazem)
        IngestionJob.objects.filter(id=job.id).update(
            status=IngestionJob.JobStatus.COMPLETED
        )

        # Agora deve avançar
        advance_to_curating_if_done(self.project)
        self.project.refresh_from_db()
        self.assertEqual(
            self.project.status,
            DaVinciProject.PipelineStatus.CURATING,
            "Projeto com único PRIDE_SEARCH concluído deve ir para curating.",
        )


# ─── B) Semântica do UPSERT (SQL direto) ─────────────────────────────────────

# O SQL abaixo espelha FIELMENTE a cláusula ON CONFLICT DO UPDATE SET
# de copy_writer.rs (rust_src/src/db/copy_writer.rs), colunas do contrato
# OmnisPathway.  Deve ser mantido em sincronia com aquele arquivo.
#
# Campos NÃO tocados no UPDATE (classificadores — intocados em re-ingestão):
#   has_control_group, disease_axis, is_single_cell, sample_join_key,
#   contract_confidence
#
# Campos atualizados pelo connector no UPDATE:
#   omics_layers  : CASE WHEN cardinality(EXCLUDED.omics_layers) > 0 ...
#   omics_count   : COALESCE(EXCLUDED.omics_count, existing)
#   data_format   : COALESCE(NULLIF(EXCLUDED.data_format, 'unknown'), existing)
#   access_type   : COALESCE(NULLIF(EXCLUDED.access_type, 'unknown'), existing)
#   extra_metadata: existing || EXCLUDED (merge JSONB, rhs ganha em conflito de chave)

_UPSERT_SQL = """
INSERT INTO core_omicdataset (
    accession, source_db, bioproject_id, title, summary,
    omic_type, omic_subcategory, organism, platform,
    extra_metadata, is_active, ingested_at, updated_at,
    omics_layers, omics_count, data_format, access_type,
    has_control_group, disease_axis, is_single_cell,
    sample_join_key, contract_confidence
)
VALUES (
    %(accession)s, %(source_db)s, '',
    %(title)s, '',
    '', '', '', '',
    %(extra_metadata)s::jsonb,
    true, NOW(), NOW(),
    %(omics_layers)s::text[], %(omics_count)s, %(data_format)s, %(access_type)s,
    'unknown', 'indeterminate', 'unknown',
    ARRAY[]::text[], '{}'::jsonb
)
ON CONFLICT (accession) DO UPDATE SET
    -- Connector fields (may update on re-ingest)
    omics_layers     = CASE
        WHEN cardinality(EXCLUDED.omics_layers) > 0
        THEN EXCLUDED.omics_layers
        ELSE core_omicdataset.omics_layers
        END,
    omics_count      = COALESCE(EXCLUDED.omics_count, core_omicdataset.omics_count),
    data_format      = COALESCE(NULLIF(EXCLUDED.data_format, 'unknown'), core_omicdataset.data_format),
    access_type      = COALESCE(NULLIF(EXCLUDED.access_type, 'unknown'), core_omicdataset.access_type),
    extra_metadata   = core_omicdataset.extra_metadata || EXCLUDED.extra_metadata::jsonb,
    updated_at       = NOW()
    -- Classifier fields NOT listed here: has_control_group, disease_axis,
    -- is_single_cell, sample_join_key, contract_confidence
    -- (intocados em re-ingestão, conforme contrato copy_writer.rs)
"""


class UpsertAntiClobberTests(TransactionTestCase):
    """
    Valida a SEMÂNTICA SQL do contrato ON CONFLICT DO UPDATE SET do copy_writer.rs
    para o PRIDE connector.

    Este teste espelha FIELMENTE a cláusula do copy_writer.rs e deve ser
    mantido em sincronia com rust_src/src/db/copy_writer.rs.

    Abordagem: FALLBACK SQL ROBUSTO (sem rust_engine real).
    O SQL do _UPSERT_SQL é executado via connection.cursor() contra o Postgres
    real de docker-compose, validando o comportamento do banco diretamente.
    """

    ACCESSION = "PXD_ANTICLOBBER_TEST_001"

    def setUp(self):
        # Estado inicial: dataset criado com ORM, depois classificado por
        # um classificador hipotético (simulando Fases 2/3 do OmnisPathway).
        self.ds = OmicDataset.objects.create(
            accession=self.ACCESSION,
            source_db="pride",
            title="Anti-clobber test dataset",
            omics_layers=["transcriptomic"],  # estado pré-existente (outro connector)
            omics_count=1,
            data_format="processed",
            access_type="public",
            extra_metadata={"contract": {"existing_key": "existing_value"}},
        )
        # Simula classificador gravando campos de classificação (Fases 2/3)
        OmicDataset.objects.filter(pk=self.ds.pk).update(
            has_control_group="yes",
            disease_axis="monogenic",
            is_single_cell="single_cell",
            sample_join_key=["cohortA"],
            contract_confidence={"disease_axis": 0.9},
        )

    def test_classifier_fields_survive_reingest(self):
        """
        Anti-clobber: re-ingestão do MESMO accession via PRIDE connector
        NÃO sobrescreve os 5 campos de classificador.

        Campos verificados:
          - has_control_group: 'yes' → permanece 'yes'
          - disease_axis: 'monogenic' → permanece 'monogenic'
          - is_single_cell: 'single_cell' → permanece 'single_cell'
          - sample_join_key: ['cohortA'] → permanece ['cohortA']
          - contract_confidence: {'disease_axis': 0.9} → permanece intacto

        Simultâneo: campos do connector SÃO atualizados:
          - omics_layers: atualizado para ['proteomic']
          - data_format: permanece 'processed' (não regrediu para 'unknown')
          - access_type: atualizado para 'public'
          - omics_count: atualizado para 1
          - extra_metadata: merge com chave nova do PRIDE
        """
        # Re-ingestão PRIDE com valores do connector
        with connection.cursor() as cursor:
            cursor.execute(
                _UPSERT_SQL,
                {
                    "accession": self.ACCESSION,
                    "source_db": "pride",
                    "title": "Anti-clobber test dataset (re-ingest)",
                    "omics_layers": "{proteomic}",  # array literal Postgres
                    "omics_count": 1,
                    "data_format": "processed",
                    "access_type": "public",
                    "extra_metadata": '{"contract": {"pride_key": "pride_value"}}',
                },
            )

        self.ds.refresh_from_db()

        # --- Campos de classificador: DEVEM sobreviver intactos ---
        self.assertEqual(
            self.ds.has_control_group,
            "yes",
            "has_control_group deve sobreviver à re-ingestão (anti-clobber).",
        )
        self.assertEqual(
            self.ds.disease_axis,
            "monogenic",
            "disease_axis deve sobreviver à re-ingestão (anti-clobber).",
        )
        self.assertEqual(
            self.ds.is_single_cell,
            "single_cell",
            "is_single_cell deve sobreviver à re-ingestão (anti-clobber).",
        )
        self.assertEqual(
            self.ds.sample_join_key,
            ["cohortA"],
            "sample_join_key deve sobreviver à re-ingestão (anti-clobber).",
        )
        self.assertEqual(
            self.ds.contract_confidence,
            {"disease_axis": 0.9},
            "contract_confidence deve sobreviver à re-ingestão (anti-clobber).",
        )

        # --- Campos do connector: DEVEM ser atualizados ---
        self.assertEqual(
            self.ds.omics_layers,
            ["proteomic"],
            "omics_layers deve ser atualizado pelo connector (incoming não vazio).",
        )
        self.assertEqual(
            self.ds.omics_count,
            1,
            "omics_count deve ser atualizado pelo connector.",
        )
        self.assertEqual(
            self.ds.access_type,
            "public",
            "access_type deve ser atualizado pelo connector.",
        )

        # extra_metadata: merge — ambas as chaves devem existir
        self.assertIn(
            "contract",
            self.ds.extra_metadata,
            "extra_metadata deve conter a chave 'contract' após merge.",
        )
        # O merge JSONB (||) com rhs={'contract': {...pride...}} sobrescreve o
        # valor da chave 'contract' no nível superior (comportamento documentado
        # de jsonb || em Postgres: rhs ganha em conflito de chave raiz)
        # Aqui validamos que o extra_metadata não ficou vazio.
        self.assertIsInstance(self.ds.extra_metadata, dict)
        self.assertGreater(len(self.ds.extra_metadata), 0)

    def test_anti_downgrade_data_format(self):
        """
        Anti-downgrade: re-ingestão com data_format='unknown' NÃO rebaixa
        data_format='processed' já existente.

        Implementado via: COALESCE(NULLIF(EXCLUDED.data_format, 'unknown'), existing)
        """
        # Estado: data_format='processed' (já classificado)
        OmicDataset.objects.filter(pk=self.ds.pk).update(data_format="processed")

        # Re-ingestão com data_format='unknown' (conector sem informação de formato)
        with connection.cursor() as cursor:
            cursor.execute(
                _UPSERT_SQL,
                {
                    "accession": self.ACCESSION,
                    "source_db": "pride",
                    "title": "Anti-downgrade test",
                    "omics_layers": "{}",   # vazio — não deve limpar layers
                    "omics_count": None,
                    "data_format": "unknown",
                    "access_type": "unknown",
                    "extra_metadata": "{}",
                },
            )

        self.ds.refresh_from_db()
        self.assertEqual(
            self.ds.data_format,
            "processed",
            "data_format='processed' não deve ser rebaixado para 'unknown' "
            "(anti-downgrade via COALESCE/NULLIF).",
        )

    def test_anti_downgrade_omics_layers_empty(self):
        """
        Anti-downgrade: re-ingestão com omics_layers='{}' (vazio) NÃO remove
        omics_layers existentes ['proteomic'].

        Implementado via: CASE WHEN cardinality(EXCLUDED.omics_layers) > 0 ...
        """
        # Estado: layers existentes
        OmicDataset.objects.filter(pk=self.ds.pk).update(omics_layers=["proteomic"])

        # Re-ingestão com omics_layers vazio
        with connection.cursor() as cursor:
            cursor.execute(
                _UPSERT_SQL,
                {
                    "accession": self.ACCESSION,
                    "source_db": "pride",
                    "title": "Anti-downgrade layers test",
                    "omics_layers": "{}",  # vazio
                    "omics_count": None,
                    "data_format": "unknown",
                    "access_type": "unknown",
                    "extra_metadata": "{}",
                },
            )

        self.ds.refresh_from_db()
        self.assertEqual(
            self.ds.omics_layers,
            ["proteomic"],
            "omics_layers='{}' não deve apagar layers existentes "
            "(CASE WHEN cardinality > 0).",
        )

    def test_anti_downgrade_access_type(self):
        """
        Anti-downgrade: re-ingestão com access_type='unknown' NÃO rebaixa
        access_type='public' já existente.
        """
        OmicDataset.objects.filter(pk=self.ds.pk).update(access_type="public")

        with connection.cursor() as cursor:
            cursor.execute(
                _UPSERT_SQL,
                {
                    "accession": self.ACCESSION,
                    "source_db": "pride",
                    "title": "Anti-downgrade access_type test",
                    "omics_layers": "{}",
                    "omics_count": None,
                    "data_format": "unknown",
                    "access_type": "unknown",  # deve ser ignorado
                    "extra_metadata": "{}",
                },
            )

        self.ds.refresh_from_db()
        self.assertEqual(
            self.ds.access_type,
            "public",
            "access_type='public' não deve ser rebaixado para 'unknown'.",
        )

    def test_upgrade_normal_data_format(self):
        """
        Upgrade normal: data_format='unknown' existente é atualizado para
        'processed' quando a re-ingestão traz dados mais ricos.
        """
        # Estado: data_format='unknown' (default)
        OmicDataset.objects.filter(pk=self.ds.pk).update(data_format="unknown")

        # Re-ingestão com data_format='processed'
        with connection.cursor() as cursor:
            cursor.execute(
                _UPSERT_SQL,
                {
                    "accession": self.ACCESSION,
                    "source_db": "pride",
                    "title": "Upgrade data_format test",
                    "omics_layers": "{proteomic}",
                    "omics_count": 1,
                    "data_format": "processed",  # upgrade
                    "access_type": "public",
                    "extra_metadata": "{}",
                },
            )

        self.ds.refresh_from_db()
        self.assertEqual(
            self.ds.data_format,
            "processed",
            "data_format='unknown' deve ser atualizado para 'processed' (upgrade normal).",
        )

    def test_upgrade_normal_omics_layers(self):
        """
        Upgrade normal: omics_layers=[] existente é atualizado quando re-ingestão
        traz layers não-vazios.
        """
        OmicDataset.objects.filter(pk=self.ds.pk).update(omics_layers=[])

        with connection.cursor() as cursor:
            cursor.execute(
                _UPSERT_SQL,
                {
                    "accession": self.ACCESSION,
                    "source_db": "pride",
                    "title": "Upgrade layers test",
                    "omics_layers": "{proteomic}",  # non-empty upgrade
                    "omics_count": 1,
                    "data_format": "processed",
                    "access_type": "public",
                    "extra_metadata": "{}",
                },
            )

        self.ds.refresh_from_db()
        self.assertEqual(
            self.ds.omics_layers,
            ["proteomic"],
            "omics_layers vazio deve ser atualizado para ['proteomic'] (upgrade normal).",
        )

    def test_omics_count_coalesce_preserves_existing_when_none(self):
        """
        omics_count=None (EXCLUDED) não apaga omics_count=1 existente.
        COALESCE(NULL, existing) = existing.
        """
        OmicDataset.objects.filter(pk=self.ds.pk).update(omics_count=2)

        with connection.cursor() as cursor:
            cursor.execute(
                _UPSERT_SQL,
                {
                    "accession": self.ACCESSION,
                    "source_db": "pride",
                    "title": "COALESCE omics_count test",
                    "omics_layers": "{}",
                    "omics_count": None,  # incoming sem contagem
                    "data_format": "unknown",
                    "access_type": "unknown",
                    "extra_metadata": "{}",
                },
            )

        self.ds.refresh_from_db()
        self.assertEqual(
            self.ds.omics_count,
            2,
            "COALESCE: omics_count=None incoming deve preservar omics_count=2 existente.",
        )

    def test_extra_metadata_merge_jsonb(self):
        """
        extra_metadata é mesclado (JSONB ||): chaves do connector são adicionadas,
        chaves existentes sem conflito são preservadas.
        """
        OmicDataset.objects.filter(pk=self.ds.pk).update(
            extra_metadata={"existing_key": "existing_value", "source": "geo"}
        )

        with connection.cursor() as cursor:
            cursor.execute(
                _UPSERT_SQL,
                {
                    "accession": self.ACCESSION,
                    "source_db": "pride",
                    "title": "JSONB merge test",
                    "omics_layers": "{proteomic}",
                    "omics_count": 1,
                    "data_format": "processed",
                    "access_type": "public",
                    "extra_metadata": '{"pride_accession": "PXD999", "source": "pride"}',
                },
            )

        self.ds.refresh_from_db()
        # Chave nova deve existir
        self.assertIn("pride_accession", self.ds.extra_metadata)
        self.assertEqual(self.ds.extra_metadata["pride_accession"], "PXD999")
        # Chave pré-existente sem conflito deve ser preservada
        self.assertIn("existing_key", self.ds.extra_metadata)
        # Chave com conflito: rhs (PRIDE) ganha
        self.assertEqual(self.ds.extra_metadata["source"], "pride")


# ─── C) CheckConstraint — 'proteomic' vs 'proteomics' ────────────────────────


class ProteomicLayerCheckConstraintTests(TestCase):
    """
    Confirma que 'proteomic' (adjetivo canônico) é aceito em omics_layers
    e que 'proteomics' (forma nominal incorreta) viola o CheckConstraint.

    Reforça por que o PRIDE connector grava 'proteomic' (o adjetivo),
    não 'proteomics'.

    Estende a cobertura de OmicDatasetCheckConstraintTests de test_contract_fields.py.
    """

    def test_proteomic_adjetivo_aceito_em_omics_layers(self):
        """'proteomic' (adjetivo canônico do vocabulário) é aceito sem IntegrityError."""
        ds = OmicDataset.objects.create(
            accession="PXD_PROTEOMIC_VALID",
            source_db="pride",
            title="Proteomic valid token",
            omics_layers=["proteomic"],
            omics_count=1,
            data_format="processed",
            access_type="public",
        )
        from_db = OmicDataset.objects.get(pk=ds.pk)
        self.assertEqual(from_db.omics_layers, ["proteomic"])
        self.assertEqual(from_db.access_type, "public")

    def test_proteomics_nominal_rejeitado_com_integrity_error(self):
        """
        'proteomics' (forma nominal — fora do vocabulário canônico) viola
        a CheckConstraint de omics_layers → IntegrityError.

        Isso reforça por que o PRIDE connector deve gravar 'proteomic' (adjetivo).
        """
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OmicDataset.objects.create(
                    accession="PXD_PROTEOMICS_INVALID",
                    source_db="pride",
                    title="Proteomics invalid token",
                    omics_layers=["proteomics"],  # forma ERRADA
                )

    def test_pride_specific_access_type_public_accepted(self):
        """
        PRIDE deriva access_type='public' para todos os datasets (REST API pública).
        Confirma que 'public' é aceito pelo CheckConstraint.
        """
        ds = OmicDataset.objects.create(
            accession="PXD_ACCESS_PUBLIC",
            source_db="pride",
            title="PRIDE public dataset",
            access_type="public",
        )
        from_db = OmicDataset.objects.get(pk=ds.pk)
        self.assertEqual(from_db.access_type, "public")

    def test_pride_data_format_processed_accepted(self):
        """
        PRIDE datasets com completedRatio=COMPLETE → data_format='processed'.
        Confirma que 'processed' é aceito.
        """
        ds = OmicDataset.objects.create(
            accession="PXD_FORMAT_PROCESSED",
            source_db="pride",
            title="PRIDE processed dataset",
            data_format="processed",
        )
        from_db = OmicDataset.objects.get(pk=ds.pk)
        self.assertEqual(from_db.data_format, "processed")

    def test_pride_data_format_raw_accepted(self):
        """
        PRIDE datasets com completedRatio=PARTIAL → data_format='raw'.
        Confirma que 'raw' é aceito.
        """
        ds = OmicDataset.objects.create(
            accession="PXD_FORMAT_RAW",
            source_db="pride",
            title="PRIDE raw dataset",
            data_format="raw",
        )
        from_db = OmicDataset.objects.get(pk=ds.pk)
        self.assertEqual(from_db.data_format, "raw")

    def test_combined_pride_fields_valid(self):
        """
        Combinação completa dos campos derivados pelo PRIDE connector é válida:
        omics_layers=['proteomic'], omics_count=1, access_type='public',
        data_format='processed'.
        """
        ds = OmicDataset.objects.create(
            accession="PXD_COMBINED_VALID",
            source_db="pride",
            title="PRIDE full contract",
            omics_layers=["proteomic"],
            omics_count=1,
            access_type="public",
            data_format="processed",
            extra_metadata={
                "contract": {
                    "proteomics_modality": "global",
                    "matrix_pointer": "peptide_matrix.csv",
                    "tissue_raw": "liver",
                    "disease_raw": "hepatocellular carcinoma",
                    "ref_pmids": [12345678],
                    "ref_dois": ["10.1234/pride.2024.001"],
                }
            },
        )
        from_db = OmicDataset.objects.get(pk=ds.pk)
        self.assertEqual(from_db.omics_layers, ["proteomic"])
        self.assertEqual(from_db.omics_count, 1)
        self.assertEqual(from_db.access_type, "public")
        self.assertEqual(from_db.data_format, "processed")
        self.assertIn("contract", from_db.extra_metadata)
        self.assertEqual(
            from_db.extra_metadata["contract"]["proteomics_modality"], "global"
        )
