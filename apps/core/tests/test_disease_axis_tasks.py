"""
Testes de QA — recompute_disease_axis_for_job (Fase 3, gap R4).

Cobre:
  1. Critério A (ingestão nova): dataset com ProjectDataset.ingestion_job=job é classificado.
  2. Critério B (re-ingestão R4): dataset re-ingerido (updated_at bumpado, ingestion_job
     aponta pro job antigo) é capturado pelo critério temporal e tem monogenic_gene_hit
     restaurado.
  3. Isolamento de projeto: task do projeto X não reclassifica datasets do projeto Y.
  4. Idempotência: rodar duas vezes não causa efeito espúrio.
  5. Tolerância a falha: exception em dataset individual → registra em errors, não derruba.
  6. Hook de enfileiramento: run_omics_ingestion e run_pride_ingestion enfileiram a task
     ao concluir (via .delay()).

Padrão: TestCase Django (sem pytest). Requer Postgres real (JSONB, updated_at).
Sem chamadas externas: NCBI/EBI mockado; rust_engine forçado ausente onde necessário.
"""

import sys
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from apps.core.models import (
    DaVinciProject,
    DatasetGene,
    IngestionJob,
    MendelianGene,
    OmicDataset,
    ProjectDataset,
)
from apps.core.tasks.disease_axis_tasks import recompute_disease_axis_for_job

# ---------------------------------------------------------------------------
# Helpers reutilizáveis
# ---------------------------------------------------------------------------

def make_user(username):
    return User.objects.create_user(username=username, password='pw')


def make_project(user, slug_suffix=''):
    slug = f'proj-{user.username}{slug_suffix}'
    return DaVinciProject.objects.create(
        user=user,
        title=f'Proj {slug}',
        slug=slug,
        query_term='test',
    )


def make_job(project, job_type=None, created_at=None):
    job = IngestionJob.objects.create(
        project=project,
        job_type=job_type or IngestionJob.JobType.GEO_SEARCH,
        parameters={},
    )
    if created_at is not None:
        # auto_now_add ignora assignment; usamos update() para forçar o valor
        IngestionJob.objects.filter(pk=job.pk).update(created_at=created_at)
        job.refresh_from_db()
    return job


def make_dataset(accession, source_db='geo', extra_metadata=None, updated_at=None):
    ds = OmicDataset.objects.create(
        accession=accession,
        source_db=source_db,
        title='Test Dataset',
        summary='Test summary',
        omic_type='transcriptomic',
        extra_metadata=extra_metadata or {},
    )
    if updated_at is not None:
        # auto_now ignora assignment; usamos update() para forçar o valor
        OmicDataset.objects.filter(pk=ds.pk).update(updated_at=updated_at)
        ds.refresh_from_db()
    return ds


def make_project_dataset(project, dataset, job=None):
    return ProjectDataset.objects.create(
        project=project,
        dataset=dataset,
        ingestion_job=job,
    )


def make_mendelian_gene(symbol, confidence='assessed'):
    return MendelianGene.objects.create(
        gene_symbol=symbol.upper(),
        disease_name=f'Disease of {symbol}',
        source='orphanet',
        confidence=confidence,
        confidence_raw=confidence,
        source_version='test-v1',
        loaded_at=timezone.now(),
    )


def make_dataset_gene(dataset, symbol, source='paper_link'):
    return DatasetGene.objects.create(
        dataset=dataset,
        gene_symbol=symbol.upper(),
        source=source,
        mention_count=1,
    )


# ---------------------------------------------------------------------------
# Caso 1 — Critério A: datasets novos (ProjectDataset.ingestion_job == job)
# ---------------------------------------------------------------------------

class CriterioAIngestionNovaTests(TestCase):
    """
    Critério A: dataset vinculado via ProjectDataset.ingestion_job=job
    é capturado e classificado pela task.
    """

    def setUp(self):
        self.user = make_user('user_a')
        self.project = make_project(self.user, '-a')
        self.job = make_job(self.project)
        make_mendelian_gene('CFTR')

    def test_criterio_a_dataset_novo_classificado(self):
        """
        Dataset cujo ProjectDataset.ingestion_job aponta para o job →
        recompute_disease_axis_for_job classifica esse dataset.
        """
        ds = make_dataset(
            'TASK_A_001',
            extra_metadata={'contract': {'disease_raw': ['Cystic fibrosis']}},
        )
        make_dataset_gene(ds, 'CFTR')
        make_project_dataset(self.project, ds, job=self.job)

        result = recompute_disease_axis_for_job(str(self.job.id))

        self.assertEqual(result['datasets_classified'], 1)
        self.assertEqual(result['datasets_skipped'], 0)
        self.assertEqual(result['errors'], [])

    def test_criterio_a_sem_datasets_retorna_zero(self):
        """
        Job sem datasets vinculados → result com zeros, sem erros.
        """
        result = recompute_disease_axis_for_job(str(self.job.id))

        self.assertEqual(result['datasets_classified'], 0)
        self.assertEqual(result['datasets_skipped'], 0)
        self.assertEqual(result['errors'], [])

    def test_criterio_a_job_inexistente_retorna_erro(self):
        """
        Job com UUID inválido → result com 'job not found' em errors.
        """
        fake_id = str(uuid.uuid4())
        result = recompute_disease_axis_for_job(fake_id)

        self.assertEqual(result['datasets_classified'], 0)
        self.assertIn('job not found', result['errors'])

    def test_criterio_a_multiplos_datasets(self):
        """
        Múltiplos datasets ligados ao mesmo job → todos classificados.
        """
        make_mendelian_gene('BRCA1', 'definitive')
        for i, (acc, gene) in enumerate([
            ('TASK_A_M01', 'CFTR'),
            ('TASK_A_M02', 'BRCA1'),
        ]):
            ds = make_dataset(acc)
            make_dataset_gene(ds, gene)
            make_project_dataset(self.project, ds, job=self.job)

        result = recompute_disease_axis_for_job(str(self.job.id))

        self.assertEqual(result['datasets_classified'], 2)
        self.assertEqual(result['errors'], [])


# ---------------------------------------------------------------------------
# Caso 2 — Critério B (R4): re-ingestão sem novo ProjectDataset
# ---------------------------------------------------------------------------

class CriterioBReingestionR4Tests(TestCase):
    """
    Critério B — o cenário central R4:
    dataset re-ingerido (UPSERT bumpou updated_at) mas ingestion_job aponta
    para o job antigo. O critério temporal do new_job captura o dataset.

    Verifica que monogenic_gene_hit é RESTAURADO pelo recompute.
    """

    def setUp(self):
        self.user = make_user('user_r4')
        self.project = make_project(self.user, '-r4')
        make_mendelian_gene('CFTR')

    def test_r4_cenario_central_gene_hit_restaurado(self):
        """
        CENÁRIO R4 CENTRAL:

        1. Dataset com monogenic_gene_hit no extra_metadata['contract'].
        2. ProjectDataset.ingestion_job = old_job (vínculo antigo).
        3. Re-ingestão simulada: extra_metadata.contract perde monogenic_gene_hit
           (comportamento documentado do copy_writer.rs), updated_at bumpado.
        4. new_job criado com created_at < ds.updated_at, sem novo ProjectDataset.
        5. recompute_disease_axis_for_job(new_job) → critério B captura o dataset.
        6. Asserts: datasets_classified == 1 (A daria 0) e monogenic_gene_hit restaurado.
        """
        # --- Preparação: dataset com gene_hit ---
        ds = make_dataset(
            'TASK_R4_CENTRAL',
            extra_metadata={
                'contract': {
                    'disease_raw': ['Cystic fibrosis'],
                    'monogenic_gene_hit': {
                        'genes': ['CFTR'],
                        'confidence': 0.48,
                        'gene_details': [],
                    },
                }
            },
        )
        make_dataset_gene(ds, 'CFTR')

        # old_job criado antes do dataset (para que created_at < ds.updated_at seja factível)
        past = timezone.now() - timedelta(hours=2)
        old_job = make_job(self.project, created_at=past)
        make_project_dataset(self.project, ds, job=old_job)

        # --- Simular re-ingestão: apaga monogenic_gene_hit (clobber R4) ---
        # e bumpa updated_at para simular o ON CONFLICT DO UPDATE SET updated_at=NOW()
        reingest_time = timezone.now() - timedelta(minutes=30)
        OmicDataset.objects.filter(pk=ds.pk).update(
            extra_metadata={
                'contract': {
                    'disease_raw': ['Cystic fibrosis'],
                    # monogenic_gene_hit AUSENTE: apagado pelo merge RHS-vence
                }
            },
            updated_at=reingest_time,
        )
        ds.refresh_from_db()

        # Confirma que gene_hit foi apagado (pré-condição do teste)
        contract_before = (ds.extra_metadata or {}).get('contract') or {}
        self.assertNotIn(
            'monogenic_gene_hit', contract_before,
            'Pré-condição: gene_hit deve estar ausente antes do recompute'
        )

        # --- new_job: criado APÓS a re-ingestão, sem ProjectDataset ---
        # created_at < ds.updated_at para o critério B capturar
        new_job_created = reingest_time - timedelta(minutes=5)
        new_job = make_job(self.project, created_at=new_job_created)
        # Confirma que não há ProjectDataset para new_job (critério A = 0)
        count_a = ProjectDataset.objects.filter(ingestion_job=new_job).count()
        self.assertEqual(count_a, 0, 'Critério A deve ser vazio para new_job')

        # --- Chamar a task ---
        result = recompute_disease_axis_for_job(str(new_job.id))

        # --- Asserts principais ---
        # Critério B deve ter capturado 1 dataset
        self.assertEqual(
            result['datasets_classified'], 1,
            f'Critério B deve capturar 1 dataset re-ingerido. '
            f'Obtido: {result}'
        )
        self.assertEqual(
            result['errors'], [],
            f'Nenhum erro esperado. Erros: {result["errors"]}'
        )

        # monogenic_gene_hit deve ter sido restaurado
        ds.refresh_from_db()
        contract_after = (ds.extra_metadata or {}).get('contract') or {}
        self.assertIn(
            'monogenic_gene_hit', contract_after,
            'monogenic_gene_hit deve ser restaurado pelo recompute pós-ingestão (R4).'
        )
        gene_hit = contract_after['monogenic_gene_hit']
        self.assertIn('CFTR', gene_hit['genes'],
                      'CFTR deve aparecer em genes do hit restaurado')
        self.assertGreater(gene_hit['confidence'], 0.0,
                           'Confidence deve ser positiva após restauração')

    def test_r4_criterio_b_nao_captura_dataset_sem_updated_at_recente(self):
        """
        Dataset atualizado ANTES do job.created_at não é capturado pelo critério B.
        Garante que o escopo temporal é respeitado.
        """
        # Dataset "antigo": updated_at = 2 horas atrás
        old_updated = timezone.now() - timedelta(hours=2)
        ds = make_dataset('TASK_R4_OLD', updated_at=old_updated)
        make_dataset_gene(ds, 'CFTR')

        old_job = make_job(self.project)
        make_project_dataset(self.project, ds, job=old_job)

        # new_job criado 1 hora atrás (DEPOIS do updated_at do dataset)
        new_job_created = timezone.now() - timedelta(hours=1)
        new_job = make_job(self.project, created_at=new_job_created)

        result = recompute_disease_axis_for_job(str(new_job.id))

        # Critério A: 0 (sem ProjectDataset para new_job)
        # Critério B: 0 (ds.updated_at < new_job.created_at)
        self.assertEqual(
            result['datasets_classified'], 0,
            'Dataset antigo (updated_at < job.created_at) não deve ser capturado'
        )


# ---------------------------------------------------------------------------
# Caso 3 — Isolamento de projeto
# ---------------------------------------------------------------------------

class IsolamentoPorProjetoTests(TestCase):
    """
    A task de um job do projeto X não reclassifica datasets do projeto Y.
    Critério B é bounded a job.project.
    """

    def setUp(self):
        self.user = make_user('user_iso')
        self.project_x = make_project(self.user, '-x')
        self.project_y = make_project(self.user, '-y')
        make_mendelian_gene('CFTR')

    def test_isolamento_projeto_x_nao_afeta_projeto_y(self):
        """
        job pertence ao projeto X. Dataset do projeto Y (mesmo updated_at recente)
        NÃO deve ser classificado — critério B filtra por job.project.
        """
        # Dataset do projeto Y com updated_at recente
        recent_time = timezone.now() - timedelta(minutes=10)
        ds_y = make_dataset('TASK_ISO_Y01', updated_at=recent_time)
        make_dataset_gene(ds_y, 'CFTR')

        old_job_y = make_job(self.project_y)
        make_project_dataset(self.project_y, ds_y, job=old_job_y)

        # job pertence ao projeto X (nenhum dataset vinculado)
        past = timezone.now() - timedelta(minutes=30)
        job_x = make_job(self.project_x, created_at=past)

        result = recompute_disease_axis_for_job(str(job_x.id))

        # Critério A: 0 (job_x não tem ProjectDatasets)
        # Critério B: 0 (ds_y está no projeto Y, não no projeto X)
        self.assertEqual(
            result['datasets_classified'], 0,
            'Dataset do projeto Y não deve ser classificado por job do projeto X'
        )
        self.assertEqual(result['errors'], [])

    def test_isolamento_ambos_projetos_classificam_apenas_os_proprios(self):
        """
        job_x classifica somente dataset_x; job_y classifica somente dataset_y.
        Cada um vê apenas seu próprio escopo.
        """
        make_mendelian_gene('BRCA1', 'definitive')
        past = timezone.now() - timedelta(hours=1)

        # Dataset do projeto X com updated_at recente
        ds_x = make_dataset('TASK_ISO_X02', updated_at=timezone.now() - timedelta(minutes=5))
        make_dataset_gene(ds_x, 'CFTR')
        old_x = make_job(self.project_x, created_at=past)
        make_project_dataset(self.project_x, ds_x, job=old_x)

        # Dataset do projeto Y com updated_at recente
        ds_y = make_dataset('TASK_ISO_Y02', updated_at=timezone.now() - timedelta(minutes=5))
        make_dataset_gene(ds_y, 'BRCA1')
        old_y = make_job(self.project_y, created_at=past)
        make_project_dataset(self.project_y, ds_y, job=old_y)

        # Jobs novos para cada projeto
        anchor = timezone.now() - timedelta(minutes=20)
        job_x = make_job(self.project_x, created_at=anchor)
        job_y = make_job(self.project_y, created_at=anchor)

        result_x = recompute_disease_axis_for_job(str(job_x.id))
        result_y = recompute_disease_axis_for_job(str(job_y.id))

        # Cada job classifica exatamente 1 dataset (o seu)
        self.assertEqual(result_x['datasets_classified'], 1,
                         'job_x deve classificar apenas dataset_x')
        self.assertEqual(result_y['datasets_classified'], 1,
                         'job_y deve classificar apenas dataset_y')


# ---------------------------------------------------------------------------
# Caso 4 — Idempotência
# ---------------------------------------------------------------------------

class IdempotenciaTests(TestCase):
    """
    Rodar recompute_disease_axis_for_job duas vezes não causa efeito espúrio.
    O resultado da segunda chamada é idêntico ao da primeira.
    """

    def setUp(self):
        self.user = make_user('user_idemp')
        self.project = make_project(self.user, '-idemp')
        make_mendelian_gene('CFTR')

    def test_idempotencia_criterio_a(self):
        """
        Rodar a task duas vezes com critério A → mesmo resultado (1 classificado).
        """
        ds = make_dataset(
            'TASK_IDEMP_A01',
            extra_metadata={'contract': {'disease_raw': ['Cystic fibrosis']}},
        )
        make_dataset_gene(ds, 'CFTR')
        job = make_job(self.project)
        make_project_dataset(self.project, ds, job=job)

        result1 = recompute_disease_axis_for_job(str(job.id))
        result2 = recompute_disease_axis_for_job(str(job.id))

        self.assertEqual(result1['datasets_classified'], 1)
        self.assertEqual(result2['datasets_classified'], 1)
        self.assertEqual(result1['errors'], [])
        self.assertEqual(result2['errors'], [])

        # Estado final do DB deve ser consistente após segunda rodada
        ds.refresh_from_db()
        contract = (ds.extra_metadata or {}).get('contract') or {}
        self.assertIn('monogenic_gene_hit', contract)

    def test_idempotencia_criterio_b(self):
        """
        Rodar a task duas vezes com critério B → dataset re-ingerido classificado
        e gene_hit presente em ambas as rodadas.
        """
        past = timezone.now() - timedelta(hours=1)
        reingest_time = timezone.now() - timedelta(minutes=20)

        ds = make_dataset(
            'TASK_IDEMP_B01',
            extra_metadata={'contract': {'disease_raw': ['Cystic fibrosis']}},
            updated_at=reingest_time,
        )
        make_dataset_gene(ds, 'CFTR')

        old_job = make_job(self.project, created_at=past)
        make_project_dataset(self.project, ds, job=old_job)

        anchor = reingest_time - timedelta(minutes=5)
        new_job = make_job(self.project, created_at=anchor)

        result1 = recompute_disease_axis_for_job(str(new_job.id))
        result2 = recompute_disease_axis_for_job(str(new_job.id))

        self.assertEqual(result1['datasets_classified'], 1)
        self.assertEqual(result2['datasets_classified'], 1)
        self.assertEqual(result1['errors'], [])
        self.assertEqual(result2['errors'], [])

        ds.refresh_from_db()
        contract = (ds.extra_metadata or {}).get('contract') or {}
        self.assertIn('monogenic_gene_hit', contract)


# ---------------------------------------------------------------------------
# Caso 5 — Tolerância a falha por dataset
# ---------------------------------------------------------------------------

class ToleranciaFalhaTests(TestCase):
    """
    Se a classificação de um dataset falhar, a task:
    - Registra o dataset em errors (formato '<accession>: <exc>').
    - Incrementa datasets_skipped.
    - Continua para os demais datasets (não derruba o loop).
    """

    def setUp(self):
        self.user = make_user('user_tol')
        self.project = make_project(self.user, '-tol')
        make_mendelian_gene('CFTR')

    def test_falha_em_dataset_individual_nao_derruba_outros(self):
        """
        Dataset1 falha, dataset2 tem sucesso →
        errors=[dataset1], datasets_classified=1, datasets_skipped=1.
        """
        # Dataset que vai ser classificado com sucesso
        ds_ok = make_dataset(
            'TASK_TOL_OK',
            extra_metadata={'contract': {'disease_raw': ['Cystic fibrosis']}},
        )
        make_dataset_gene(ds_ok, 'CFTR')
        job = make_job(self.project)
        make_project_dataset(self.project, ds_ok, job=job)

        # Dataset que vai causar exceção na classificação
        ds_fail = make_dataset('TASK_TOL_FAIL')
        make_project_dataset(self.project, ds_fail, job=job)

        # Patchamos classify_disease_axis_for_dataset para falhar apenas no ds_fail
        from apps.core.services import disease_axis_classifier_service as svc

        original_classify = svc.classify_disease_axis_for_dataset

        def classify_side_effect(dataset, **kwargs):
            if dataset.accession == 'TASK_TOL_FAIL':
                raise RuntimeError('Erro simulado de classificação')
            return original_classify(dataset, **kwargs)

        with patch.object(svc, 'classify_disease_axis_for_dataset',
                          side_effect=classify_side_effect):
            result = recompute_disease_axis_for_job(str(job.id))

        # ds_fail → skipped + error registrado
        self.assertEqual(result['datasets_skipped'], 1)
        self.assertTrue(
            any('TASK_TOL_FAIL' in e for e in result['errors']),
            f'Esperado erro de TASK_TOL_FAIL em errors. Obtido: {result["errors"]}'
        )
        # ds_ok → classificado com sucesso
        self.assertEqual(result['datasets_classified'], 1)

    def test_falha_total_todos_datasets_falham(self):
        """
        Todos os datasets falham → datasets_classified=0, errors preenchido,
        datasets_skipped = total.
        """
        for acc in ['TASK_TOL_FAIL_A', 'TASK_TOL_FAIL_B']:
            ds = make_dataset(acc)
            job = make_job(self.project) if acc == 'TASK_TOL_FAIL_A' else None
            if acc == 'TASK_TOL_FAIL_A':
                job_ref = job
            make_project_dataset(self.project, ds, job=job_ref)

        from apps.core.services import disease_axis_classifier_service as svc

        with patch.object(svc, 'classify_disease_axis_for_dataset',
                          side_effect=RuntimeError('Falha total simulada')):
            result = recompute_disease_axis_for_job(str(job_ref.id))

        self.assertEqual(result['datasets_classified'], 0)
        self.assertGreater(len(result['errors']), 0)
        self.assertGreater(result['datasets_skipped'], 0)


# ---------------------------------------------------------------------------
# Caso 6 — Hook de enfileiramento em run_omics_ingestion / run_pride_ingestion
# ---------------------------------------------------------------------------

class HookEnfileiramentoTests(TestCase):
    """
    Verifica que run_omics_ingestion e run_pride_ingestion enfileiram
    recompute_disease_axis_for_job.delay() ao concluir (path de sucesso).

    Como o rust_engine não está compilado em CI, forçamos a ausência do módulo
    para testar o path de sucesso via mock do próprio rust_engine.
    """

    def setUp(self):
        self.user = make_user('user_hook')
        self.project = make_project(self.user, '-hook')

    def _make_omics_job(self):
        return IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.GEO_SEARCH,
            parameters={'query': 'test', 'sources': ['geo']},
        )

    def _make_pride_job(self):
        return IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.PRIDE_SEARCH,
            parameters={'query': 'test'},
        )

    def _make_fake_result(self):
        """
        Constrói um objeto resultado mínimo compatível com o que o rust_engine retorna.

        O ORM usa `result.datasets_processed` diretamente em `.update()`, portanto
        os valores precisam ser inteiros reais — não MagicMock (que vira expressão
        de aggregate e quebra o SQL).
        """
        class FakeResult:
            datasets_processed = 1
            datasets_inserted = 1
            links_inserted = 0
            errors = []
        return FakeResult()

    def test_run_omics_ingestion_enfileira_recompute(self):
        """
        run_omics_ingestion bem-sucedido → recompute_disease_axis_for_job.delay
        é chamado com str(job_id).

        Todos os imports dentro de run_omics_ingestion são locais. Patches:
        - rust_engine substituído por MagicMock cujo search_and_ingest_omics retorna FakeResult.
        - recompute_disease_axis_for_job.delay patchado no módulo de origem.
        - materialize_project_links e advance_to_curating_if_done patchados nos
          respectivos módulos de origem (link_service, project_status).
        """
        from apps.core.tasks.ingestion_tasks import run_omics_ingestion

        job = self._make_omics_job()
        fake_result = self._make_fake_result()

        fake_engine = MagicMock()
        fake_engine.search_and_ingest_omics.return_value = fake_result

        with patch.dict(sys.modules, {'rust_engine': fake_engine}), \
             patch('apps.core.tasks.disease_axis_tasks.recompute_disease_axis_for_job.delay') as mock_delay, \
             patch('apps.core.services.link_service.materialize_project_links',
                   return_value=0), \
             patch('apps.core.services.project_status.advance_to_curating_if_done'):

            run_omics_ingestion(str(job.id))

        mock_delay.assert_called_once_with(str(job.id))

    def test_run_pride_ingestion_enfileira_recompute(self):
        """
        run_pride_ingestion bem-sucedido → recompute_disease_axis_for_job.delay
        é chamado com str(job_id).
        """
        from apps.core.tasks.ingestion_tasks import run_pride_ingestion

        job = self._make_pride_job()
        fake_result = self._make_fake_result()

        fake_engine = MagicMock()
        fake_engine.search_and_ingest_pride.return_value = fake_result

        with patch.dict(sys.modules, {'rust_engine': fake_engine}), \
             patch('apps.core.tasks.disease_axis_tasks.recompute_disease_axis_for_job.delay') as mock_delay, \
             patch('apps.core.services.link_service.materialize_project_links',
                   return_value=0), \
             patch('apps.core.services.project_status.advance_to_curating_if_done'):

            run_pride_ingestion(str(job.id))

        mock_delay.assert_called_once_with(str(job.id))

    def test_run_omics_ingestion_sem_rust_engine_nao_enfileira(self):
        """
        Quando rust_engine não está disponível (ImportError), a task NÃO é
        enfileirada — o job é marcado FAILED antes de chegar no hook.
        """
        from apps.core.tasks.ingestion_tasks import run_omics_ingestion

        job = self._make_omics_job()

        with patch.dict(sys.modules, {'rust_engine': None}), \
             patch('apps.core.tasks.disease_axis_tasks.recompute_disease_axis_for_job') as mock_task:

            mock_task.delay = MagicMock()
            run_omics_ingestion(str(job.id))

        mock_task.delay.assert_not_called()

        job.refresh_from_db()
        self.assertEqual(job.status, IngestionJob.JobStatus.FAILED)


# ---------------------------------------------------------------------------
# Caso extra — UNION entre critério A e critério B (sem duplicatas)
# ---------------------------------------------------------------------------

class UnionCriteriosTests(TestCase):
    """
    Um dataset que satisfaça AMBOS os critérios (A e B) deve ser
    contado apenas uma vez (set union).
    """

    def setUp(self):
        self.user = make_user('user_union')
        self.project = make_project(self.user, '-union')
        make_mendelian_gene('CFTR')

    def test_union_sem_duplicata_quando_ambos_criterios_capturam(self):
        """
        Dataset com ProjectDataset.ingestion_job=job E updated_at >= job.created_at
        → contado exatamente 1 vez em datasets_classified.
        """
        past = timezone.now() - timedelta(hours=1)
        job = make_job(self.project, created_at=past)

        # updated_at = agora (>= job.created_at) → critério B
        ds = make_dataset(
            'TASK_UNION_01',
            extra_metadata={'contract': {'disease_raw': ['Cystic fibrosis']}},
            updated_at=timezone.now(),
        )
        make_dataset_gene(ds, 'CFTR')

        # ProjectDataset.ingestion_job = job → critério A também captura
        make_project_dataset(self.project, ds, job=job)

        result = recompute_disease_axis_for_job(str(job.id))

        # Deve classificar 1, não 2 (sem duplicata)
        self.assertEqual(
            result['datasets_classified'], 1,
            f'Dataset em A ∩ B deve ser contado uma vez. Obtido: {result}'
        )
        self.assertEqual(result['errors'], [])
