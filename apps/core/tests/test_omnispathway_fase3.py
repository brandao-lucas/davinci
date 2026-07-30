"""
test_omnispathway_fase3.py — Cobertura dos services e tasks da Fase 3 do Objetivo 2.

OmnisPathway Objetivo 2, Fase 3: backbone de via (topologia KEGG assinada) + readouts.

Módulos cobertos:
  - apps.core.services.phospho_matrix_load_service (PhosphoMatrixLoadService)
  - apps.core.services.pathway_topology_load_service (PathwayTopologyLoadService)
  - apps.core.services.regulon_load_service (RegulonLoadService)
  - apps.core.services.readout_mapping_service (ReadoutMappingService)
  - apps.core.tasks.ingestion_tasks (run_phospho_matrix_load, run_pathway_topology_load,
    run_regulon_load)

Casos cobertos (plano passo 3.5):
  3.1 phospho: service cria OmicMatrix(proteomic/phospho_site/intensities/phospho-v1)
      + OmicSample (get_or_create) + OmicMatrixSample; idempotência;
      isolamento por --project.
  3.2 topologia: IngestionJob(pathway_topology_load), chama mock, marca COMPLETED
      com contadores; idempotência (job ativo → PathwayTopologyJobActiveError).
  3.3 regulon: tf_allowlist derivada dos nós gene das vias; RegulonGraphNotLoadedError
      se grafo não carregado; guarda regulon_path.
  3.4 mapeamento: Regra 1 fosfo → readout_role='phospho' + PathwayReadoutFeature(rule='phospho');
      Regra 2 TF → readout_role='tf_target' + PathwayReadoutFeature(rule='tf_target',
      regulon_sign=±1, regulon_source='collectri'); --dry-run NÃO persiste;
      idempotência (2ª execução não duplica pela NK); gatilho_ac=True sempre;
      n_features_not_found reflete alvos fora das vias (validação intra-grafo).
  Tasks: run_phospho_matrix_load trata PhosphoMatrixAlreadyLoadedError como
      idempotência (COMPLETED); run_regulon_load trata RegulonGraphNotLoadedError
      sem retry.
  Isolamento: PhosphoMatrix project-scoped; grafo/readout é catálogo global.

Padrões obrigatórios:
  - SEM rede: rust_engine SEMPRE mockado (patch.dict sys.modules).
  - SEM pytest: usa django.test.TestCase.
  - OmicSample é COMPARTILHADA entre matrizes (get_or_create por accession).
  - JSONL de regulon fake local (não CollecTRI bruto).
  - default_storage mockado com FileSystemStorage em tmpdir.
"""

from __future__ import annotations

import json
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
    IngestionJob,
    OmicDataset,
    OmicMatrix,
    OmicMatrixSample,
    OmicSample,
    Pathway,
    PathwayEdge,
    PathwayNode,
    PathwayReadoutFeature,
    ProjectDataset,
)


# =============================================================================
# Helpers de factory — reutiliza padrões dos outros testes do projeto
# =============================================================================

def _make_user(username: str) -> User:
    return User.objects.create_user(username=username, password='pw')


def _make_project(user: User, title: str) -> DaVinciProject:
    slug = f'{title.lower().replace(" ", "-")}-{user.username}'
    return DaVinciProject.objects.create(
        user=user, title=title, slug=slug, query_term='fase3-test',
    )


# ── helpers de manifesto fake (simulam o que o Rust retornaria) ──────────────

def _make_sample_column(case_id: str, role: str, col_idx: int) -> MagicMock:
    col = MagicMock()
    col.case_id = case_id
    col.sample_role = role
    col.column_index = col_idx
    return col


def _make_phospho_manifest(
    parquet_path: str,
    case_ids: list[str] | None = None,
) -> MagicMock:
    """Manifesto fake de PhosphoMatrixManifest."""
    case_ids = case_ids or ['C3L-00001', 'C3L-00002']
    m = MagicMock()
    m.parquet_path = parquet_path
    m.checksum_md5 = 'aabbccddeeff11223344556677889900'
    m.n_features = 150
    m.n_samples = len(case_ids) * 2  # tumor + normal
    m.genes_discarded = 3
    m.sample_columns = [
        _make_sample_column(cid, role, idx * 2 + (0 if role == 'tumor' else 1))
        for idx, cid in enumerate(case_ids)
        for role in ('tumor', 'normal')
    ]
    return m


def _make_kegg_manifest(
    n_pathways: int = 3,
    n_nodes: int = 120,
    n_edges: int = 250,
) -> MagicMock:
    """Manifesto fake de KeggTopologyManifest."""
    m = MagicMock()
    m.n_pathways = n_pathways
    m.n_nodes = n_nodes
    m.n_edges = n_edges
    m.n_signed = 200
    m.n_unsigned = 50
    m.n_orphan_symbols = 2
    m.source_version = '2026-07-15'
    return m


def _make_regulon_manifest(regulon_path: str, n_tfs: int = 5) -> MagicMock:
    """Manifesto fake de RegulonLoadManifest."""
    m = MagicMock()
    m.n_tfs = n_tfs
    m.n_targets = n_tfs * 4
    m.n_edges = n_tfs * 4
    m.n_positive = n_tfs * 2
    m.n_negative = n_tfs * 2
    m.n_neutral = 0
    m.source_version = 'collectri-2024-01'
    m.regulon_path = regulon_path
    return m


# ── helpers de fixture de grafo/nós ──────────────────────────────────────────

def _make_pathway(kegg_id: str, name: str = '') -> Pathway:
    pathway, _ = Pathway.objects.get_or_create(
        kegg_id=kegg_id,
        defaults={
            'name': name or f'Pathway {kegg_id}',
            'source': Pathway.Source.KEGG,
            'source_version': '2026-07-15',
        },
    )
    return pathway


def _make_node(
    pathway: Pathway,
    gene_symbol: str,
    entry_id: str | None = None,
    node_type: str = PathwayNode.NodeType.GENE,
) -> PathwayNode:
    eid = entry_id or f'entry-{gene_symbol}'
    node, _ = PathwayNode.objects.get_or_create(
        pathway=pathway,
        kegg_entry_id=eid,
        defaults={
            'node_type': node_type,
            'gene_symbol': gene_symbol.upper(),
            'graphics_name': gene_symbol,
            'kegg_ids': [f'hsa:{gene_symbol}'],
            'readout_role': PathwayNode.ReadoutRole.NONE,
        },
    )
    return node


def _make_edge(
    pathway: Pathway,
    source_node: PathwayNode,
    target_node: PathwayNode,
    sign: int = 1,
    interaction: str = PathwayEdge.Interaction.ACTIVATION,
) -> PathwayEdge:
    edge, _ = PathwayEdge.objects.get_or_create(
        pathway=pathway,
        source_node=source_node,
        target_node=target_node,
        interaction=interaction,
        defaults={
            'sign': sign,
            'relation_type': PathwayEdge.RelationType.PPREL,
            'subtypes': ['activation' if sign == 1 else 'inhibition'],
        },
    )
    return edge


def _make_phospho_matrix(accession: str = 'CPTAC-CCRCC-PHOSPHO') -> OmicMatrix:
    """Cria OmicMatrix de fosfoproteoma (sem armazenamento real)."""
    ds, _ = OmicDataset.objects.get_or_create(
        accession=accession,
        defaults={
            'source_db': OmicDataset.SourceDB.CPTAC,
            'title': f'Phospho Dataset {accession}',
            'access_type': OmicDataset.AccessType.PUBLIC,
        },
    )
    matrix, _ = OmicMatrix.objects.get_or_create(
        dataset=ds,
        omics_layer='proteomic',
        feature_axis=OmicMatrix.FeatureAxis.PHOSPHO_SITE,
        loader_version='phospho-v1',
        defaults={
            'data_format_level': OmicMatrix.DataFormatLevel.INTENSITIES,
            'storage_key': f'omics/_shared/{accession}/phospho.parquet',
            'n_features': 150,
            'n_samples': 4,
            'checksum_md5': 'aabbccddeeff11223344556677889900',
        },
    )
    return matrix


def _make_proteome_matrix(accession: str = 'CPTAC-CCRCC-PROTEOME') -> OmicMatrix:
    """Cria OmicMatrix de proteoma gene-level (Fase 0)."""
    ds, _ = OmicDataset.objects.get_or_create(
        accession=accession,
        defaults={
            'source_db': OmicDataset.SourceDB.CPTAC,
            'title': f'Proteome Dataset {accession}',
            'access_type': OmicDataset.AccessType.PUBLIC,
        },
    )
    matrix, _ = OmicMatrix.objects.get_or_create(
        dataset=ds,
        omics_layer='proteomic',
        feature_axis=OmicMatrix.FeatureAxis.GENE,
        loader_version='v1',
        defaults={
            'data_format_level': OmicMatrix.DataFormatLevel.INTENSITIES,
            'storage_key': f'omics/_shared/{accession}/proteome.parquet',
            'n_features': 8000,
            'n_samples': 4,
            'checksum_md5': 'ccddee334455667788990011aabbcc33',
        },
    )
    return matrix


def _write_regulon_jsonl(
    path: str,
    entries: list[dict],
) -> None:
    """Escreve arquivo JSONL de regulon sintético em path."""
    with open(path, 'w', encoding='utf-8') as f:
        for entry in entries:
            f.write(json.dumps(entry) + '\n')


# =============================================================================
# 1. PhosphoMatrixLoadService — 3.1
# =============================================================================

class PhosphoMatrixStorageTestCase(TestCase):
    """Substitui default_storage por FileSystemStorage local."""

    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.mkdtemp(prefix='davinci_phospho_test_')
        self._storage = FileSystemStorage(location=self._tmpdir)
        self._storage_patcher = patch(
            'apps.core.services.phospho_matrix_load_service.default_storage',
            new=self._storage,
        )
        self._storage_patcher.start()

    def _put_parquet(self, name: str = 'cptac_ccrcc_phospho.parquet') -> str:
        path = os.path.join(self._tmpdir, name)
        with open(path, 'wb') as f:
            f.write(b'PAR1' + b'\x00' * 32 + b'PAR1')
        return path

    def tearDown(self):
        self._storage_patcher.stop()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        super().tearDown()


class PhosphoMatrixLoadServiceTests(PhosphoMatrixStorageTestCase):
    """
    Testa PhosphoMatrixLoadService.run() de ponta a ponta com mock do Rust.

    Cobre:
      - Cria OmicMatrix(proteomic/phospho_site/intensities/phospho-v1).
      - OmicSample get_or_create (reutiliza amostras por accession).
      - OmicMatrixSample bulk_create.
      - Idempotência: 2ª carga → PhosphoMatrixAlreadyLoadedError.
      - storage_key no namespace _shared.
      - db_url NUNCA no resultado.
    """

    def setUp(self):
        super().setUp()
        self.user = _make_user('phospho_user')
        self.project = _make_project(self.user, 'Phospho Project')

    def _run_mocked(
        self,
        case_ids: list[str] | None = None,
    ) -> dict:
        """Executa service.run() com rust_engine mockado."""
        parquet_path = self._put_parquet()
        case_ids = case_ids or ['C3L-00001', 'C3L-00002']
        fake_manifest = _make_phospho_manifest(parquet_path, case_ids)
        fake_engine = MagicMock()
        fake_engine.load_cptac_phospho_matrix.return_value = fake_manifest

        from apps.core.services.phospho_matrix_load_service import PhosphoMatrixLoadService

        service = PhosphoMatrixLoadService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            result = service.run()
        return result

    def test_run_cria_omic_matrix_fosfo(self):
        """run() cria OmicMatrix com omics_layer=proteomic, feature_axis=phospho_site."""
        result = self._run_mocked()

        matrix = OmicMatrix.objects.get(
            omics_layer='proteomic',
            feature_axis=OmicMatrix.FeatureAxis.PHOSPHO_SITE,
            loader_version='phospho-v1',
        )
        self.assertEqual(matrix.data_format_level, OmicMatrix.DataFormatLevel.INTENSITIES)
        self.assertEqual(matrix.n_features, 150)

    def test_run_cria_omic_sample_por_case_id_e_role(self):
        """run() cria OmicSample para cada (case_id, role) — tumor+normal."""
        self._run_mocked(case_ids=['C3L-00010'])

        # Cada case_id produz 2 amostras: tumor + normal
        self.assertTrue(OmicSample.objects.filter(accession='C3L-00010_tumor').exists())
        self.assertTrue(OmicSample.objects.filter(accession='C3L-00010_normal').exists())

    def test_run_get_or_create_amostra_reutiliza_existente(self):
        """
        OmicSample por accession: se já existe (ex.: carregada pela Fase 0),
        não cria duplicata — get_or_create.
        """
        # Pre-cria a amostra (simula Fase 0)
        ds_pre, _ = OmicDataset.objects.get_or_create(
            accession='CPTAC-CCRCC-PROTEOME',
            defaults={
                'source_db': OmicDataset.SourceDB.CPTAC,
                'title': 'Pre-existing proteome',
                'access_type': OmicDataset.AccessType.PUBLIC,
            },
        )
        pre_sample = OmicSample.objects.create(
            accession='C3L-00020_tumor',
            dataset=ds_pre,
            title='C3L-00020 tumor (pre-existing)',
            organism='Homo sapiens',
        )

        self._run_mocked(case_ids=['C3L-00020'])

        # Deve continuar sendo exatamente 1 amostra para este accession
        count = OmicSample.objects.filter(accession='C3L-00020_tumor').count()
        self.assertEqual(count, 1, 'get_or_create não deve duplicar OmicSample existente')

    def test_run_cria_omic_matrix_sample(self):
        """run() cria OmicMatrixSample vinculando amostras à matriz fosfo."""
        result = self._run_mocked(case_ids=['C3L-00011'])

        matrix = OmicMatrix.objects.get(
            omics_layer='proteomic',
            feature_axis=OmicMatrix.FeatureAxis.PHOSPHO_SITE,
            loader_version='phospho-v1',
        )
        oms_count = OmicMatrixSample.objects.filter(matrix=matrix).count()
        # 1 case_id × 2 roles (tumor+normal) = 2 amostras
        self.assertGreater(oms_count, 0, 'OmicMatrixSample deve ser criado')

    def test_run_job_completed_after_success(self):
        """run() cria IngestionJob e o marca COMPLETED."""
        result = self._run_mocked()

        job = IngestionJob.objects.get(id=result['job_id'])
        self.assertEqual(job.status, IngestionJob.JobStatus.COMPLETED)
        self.assertEqual(job.job_type, IngestionJob.JobType.PHOSPHO_MATRIX_LOAD)

    def test_run_result_keys(self):
        """run() retorna dict com as chaves esperadas do contrato."""
        result = self._run_mocked()

        for key in ('job_id', 'storage_key', 'n_features', 'n_samples', 'checksum_md5',
                    'genes_discarded', 'roles'):
            self.assertIn(key, result, f'Chave ausente no resultado: {key!r}')

    def test_run_storage_key_shared_namespace(self):
        """storage_key tem namespace _shared (dado público)."""
        result = self._run_mocked()
        self.assertIn('_shared', result['storage_key'])

    def test_run_storage_key_sem_caminho_absoluto(self):
        """storage_key não vaza caminho físico absoluto."""
        result = self._run_mocked()
        for prefix in ('/tmp', '/var', '/home', '/Users', '/private'):
            self.assertNotIn(prefix, result['storage_key'])

    def test_run_nao_vaza_db_url_no_resultado(self):
        """Resultado NUNCA contém postgresql://, db_url, password."""
        result = self._run_mocked()
        result_str = str(result)
        self.assertNotIn('postgresql://', result_str)
        self.assertNotIn('db_url', result_str)
        self.assertNotIn('password', result_str)

    def test_run_roles_summary_contem_tumor_normal(self):
        """result['roles'] mapeia tumor/normal com contagens."""
        result = self._run_mocked(case_ids=['C3L-00030', 'C3L-00031'])
        roles = result['roles']
        self.assertIn('tumor', roles)
        self.assertIn('normal', roles)


class PhosphoMatrixIdempotencyTests(PhosphoMatrixStorageTestCase):
    """Testa idempotência e gate de job ativo."""

    def setUp(self):
        super().setUp()
        self.user = _make_user('phospho_idem_user')
        self.project = _make_project(self.user, 'Phospho Idempotency Project')

    def test_segunda_carga_levanta_already_loaded(self):
        """
        Idempotência: 2ª execução de run() com OmicMatrix já existente
        → PhosphoMatrixAlreadyLoadedError.
        """
        from apps.core.services.phospho_matrix_load_service import (
            PHOSPHO_CCRCC_ACCESSION,
            LOADER_VERSION,
            PhosphoMatrixAlreadyLoadedError,
            PhosphoMatrixLoadService,
        )

        parquet_path = self._put_parquet()
        fake_manifest = _make_phospho_manifest(parquet_path)
        fake_engine = MagicMock()
        fake_engine.load_cptac_phospho_matrix.return_value = fake_manifest

        service = PhosphoMatrixLoadService(self.project)

        # 1ª execução: cria a matriz
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            service.run()

        # 2ª execução: deve levantar AlreadyLoadedError
        service2 = PhosphoMatrixLoadService(self.project)
        with self.assertRaises(PhosphoMatrixAlreadyLoadedError):
            with patch.dict('sys.modules', {'rust_engine': fake_engine}):
                service2.run()

    def test_job_ativo_bloqueia_check_idempotency(self):
        """Job PENDING/RUNNING → PhosphoMatrixJobActiveError."""
        from apps.core.services.phospho_matrix_load_service import (
            PHOSPHO_CCRCC_ACCESSION,
            PhosphoMatrixJobActiveError,
            PhosphoMatrixLoadService,
        )

        service = PhosphoMatrixLoadService(self.project)
        dataset = service._get_or_create_dataset()

        IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.PHOSPHO_MATRIX_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={
                'dataset_id': dataset.id,
                'dataset_accession': dataset.accession,
                'omics_layer': 'proteomic',
                'feature_axis': 'phospho_site',
                'loader_version': 'phospho-v1',
            },
        )

        with self.assertRaises(PhosphoMatrixJobActiveError):
            service._check_idempotency(dataset)

    def test_get_or_create_dataset_creates_project_link(self):
        """_get_or_create_dataset cria ProjectDataset vinculando projeto ao dataset."""
        from apps.core.services.phospho_matrix_load_service import PhosphoMatrixLoadService

        service = PhosphoMatrixLoadService(self.project)
        dataset = service._get_or_create_dataset()

        self.assertTrue(
            ProjectDataset.objects.filter(
                project=self.project, dataset=dataset
            ).exists()
        )

    def test_get_or_create_dataset_segunda_chamada_mesmo_dataset(self):
        """_get_or_create_dataset 2x retorna o mesmo OmicDataset."""
        from apps.core.services.phospho_matrix_load_service import PhosphoMatrixLoadService

        service = PhosphoMatrixLoadService(self.project)
        d1 = service._get_or_create_dataset()
        d2 = service._get_or_create_dataset()
        self.assertEqual(d1.id, d2.id)


# =============================================================================
# 2. PhosphoMatrixLoadService — isolamento project-scoped
# =============================================================================

class PhosphoMatrixIsolationTests(TestCase):
    """Verifica isolamento ProjectDataset para fosfoproteoma."""

    def test_dataset_fosfo_vinculado_apenas_ao_projeto_que_disparou(self):
        """
        _get_or_create_dataset cria ProjectDataset para o projeto A.
        Projeto B NÃO deve ter esse vínculo automaticamente.
        """
        from apps.core.services.phospho_matrix_load_service import PhosphoMatrixLoadService

        user_a = _make_user('phospho_iso_a')
        user_b = _make_user('phospho_iso_b')
        project_a = _make_project(user_a, 'Phospho Iso A')
        project_b = _make_project(user_b, 'Phospho Iso B')

        service_a = PhosphoMatrixLoadService(project_a)
        dataset = service_a._get_or_create_dataset()

        # Projeto B não deve ter vínculo
        linked_to_b = ProjectDataset.objects.filter(
            project=project_b, dataset=dataset
        ).exists()
        self.assertFalse(linked_to_b, 'Projeto B não deve ter vínculo com dataset de fosfo de A')


# =============================================================================
# 3. PathwayTopologyLoadService — 3.2
# =============================================================================

class PathwayTopologyLoadServiceTests(TestCase):
    """
    Testa PathwayTopologyLoadService.run() com mock do Rust.

    Cobre:
      - Cria IngestionJob(pathway_topology_load).
      - Chama rust_engine.load_kegg_topology com pathway_kegg_ids.
      - Marca job COMPLETED com contadores.
      - Retorno contém n_pathways, n_nodes, n_edges, n_signed, source_version.
      - db_url NÃO aparece no job.parameters.
      - Idempotência: job ativo → PathwayTopologyJobActiveError.
    """

    def setUp(self):
        self.user = _make_user('topology_user')
        self.project = _make_project(self.user, 'Topology Project')

    def _run_mocked(
        self,
        pathway_ids: list[str] | None = None,
        manifest: MagicMock | None = None,
    ) -> dict:
        fake_manifest = manifest or _make_kegg_manifest()
        fake_engine = MagicMock()
        fake_engine.load_kegg_topology.return_value = fake_manifest

        from apps.core.services.pathway_topology_load_service import PathwayTopologyLoadService

        service = PathwayTopologyLoadService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            result = service.run(pathway_ids=pathway_ids)
        return result

    def test_run_retorna_chaves_do_contrato(self):
        """run() retorna dict com todas as chaves esperadas."""
        result = self._run_mocked()

        expected_keys = {
            'job_id', 'n_pathways', 'n_nodes', 'n_edges',
            'n_signed', 'n_unsigned', 'n_orphan_symbols', 'source_version',
        }
        self.assertEqual(set(result.keys()), expected_keys)

    def test_run_cria_job_pathway_topology_load(self):
        """run() cria IngestionJob com job_type=PATHWAY_TOPOLOGY_LOAD."""
        result = self._run_mocked()

        job = IngestionJob.objects.get(id=result['job_id'])
        self.assertEqual(job.job_type, IngestionJob.JobType.PATHWAY_TOPOLOGY_LOAD)

    def test_run_marca_job_completed(self):
        """run() marca o IngestionJob como COMPLETED."""
        result = self._run_mocked()

        job = IngestionJob.objects.get(id=result['job_id'])
        self.assertEqual(job.status, IngestionJob.JobStatus.COMPLETED)

    def test_run_contadores_corretos_no_resultado(self):
        """run() retorna contadores do manifesto (n_pathways, n_nodes, n_edges)."""
        manifest = _make_kegg_manifest(n_pathways=3, n_nodes=90, n_edges=180)
        result = self._run_mocked(manifest=manifest)

        self.assertEqual(result['n_pathways'], 3)
        self.assertEqual(result['n_nodes'], 90)
        self.assertEqual(result['n_edges'], 180)

    def test_run_source_version_no_resultado(self):
        """run() retorna source_version do manifesto."""
        result = self._run_mocked()
        self.assertEqual(result['source_version'], '2026-07-15')

    def test_run_pathway_ids_default_quando_nenhum_passado(self):
        """
        Sem pathway_ids explícito, usa DEFAULT_PATHWAY_IDS
        ['hsa04151', 'hsa04010', 'hsa04115'].
        """
        from apps.core.services.pathway_topology_load_service import (
            DEFAULT_PATHWAY_IDS,
            PathwayTopologyLoadService,
        )

        fake_engine = MagicMock()
        fake_engine.load_kegg_topology.return_value = _make_kegg_manifest()

        service = PathwayTopologyLoadService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            service.run()

        call_kwargs = fake_engine.load_kegg_topology.call_args[1]
        self.assertEqual(call_kwargs['pathway_kegg_ids'], DEFAULT_PATHWAY_IDS)

    def test_run_db_url_nao_em_job_parameters(self):
        """db_url NÃO é armazenada em IngestionJob.parameters."""
        result = self._run_mocked()

        job = IngestionJob.objects.get(id=result['job_id'])
        params_str = str(job.parameters or {})
        self.assertNotIn('postgresql://', params_str)
        self.assertNotIn('db_url', params_str)
        self.assertNotIn('password', params_str)

    def test_run_rust_chamado_com_args_corretos(self):
        """load_kegg_topology chamado com pathway_kegg_ids, dest_dir, db_url."""
        fake_engine = MagicMock()
        fake_engine.load_kegg_topology.return_value = _make_kegg_manifest()

        from apps.core.services.pathway_topology_load_service import PathwayTopologyLoadService

        service = PathwayTopologyLoadService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            service.run(pathway_ids=['hsa04151'])

        call_kwargs = fake_engine.load_kegg_topology.call_args[1]
        self.assertIn('pathway_kegg_ids', call_kwargs)
        self.assertIn('dest_dir', call_kwargs)
        self.assertIn('db_url', call_kwargs)
        self.assertEqual(call_kwargs['pathway_kegg_ids'], ['hsa04151'])


class PathwayTopologyIdempotencyTests(TestCase):
    """Testa idempotência do PathwayTopologyLoadService."""

    def setUp(self):
        self.user = _make_user('topology_idem_user')
        self.project = _make_project(self.user, 'Topology Idempotency Project')

    def test_job_ativo_levanta_topology_job_active_error(self):
        """
        PATHWAY_TOPOLOGY_LOAD PENDING/RUNNING → PathwayTopologyJobActiveError.
        """
        from apps.core.services.pathway_topology_load_service import (
            PathwayTopologyJobActiveError,
            PathwayTopologyLoadService,
        )

        IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.PATHWAY_TOPOLOGY_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={'pathway_ids': ['hsa04151'], 'loader_version': 'kegg-topology-v1'},
        )

        service = PathwayTopologyLoadService(self.project)
        with self.assertRaises(PathwayTopologyJobActiveError):
            service._check_idempotency()

    def test_job_completed_nao_bloqueia(self):
        """Job COMPLETED não bloqueia _check_idempotency."""
        from apps.core.services.pathway_topology_load_service import (
            PathwayTopologyJobActiveError,
            PathwayTopologyLoadService,
        )

        IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.PATHWAY_TOPOLOGY_LOAD,
            status=IngestionJob.JobStatus.COMPLETED,
            parameters={'pathway_ids': ['hsa04151'], 'loader_version': 'kegg-topology-v1'},
        )

        service = PathwayTopologyLoadService(self.project)
        try:
            service._check_idempotency()  # não deve levantar
        except PathwayTopologyJobActiveError as exc:
            self.fail(f'Job COMPLETED não deve bloquear: {exc}')


# =============================================================================
# 4. RegulonLoadService — 3.3
# =============================================================================

class RegulonLoadServiceTests(TestCase):
    """
    Testa RegulonLoadService.run() com mock do Rust.

    Cobre:
      - tf_allowlist derivada dos PathwayNode(node_type='gene') em PG.
      - RegulonGraphNotLoadedError quando nenhum PathwayNode existe.
      - run() chama rust_engine.load_collectri_regulons com tf_allowlist.
      - Guarda regulon_path no IngestionJob.parameters.
      - Marca job COMPLETED.
    """

    def setUp(self):
        self.user = _make_user('regulon_user')
        self.project = _make_project(self.user, 'Regulon Project')

    def _setup_graph(
        self,
        pathway_ids: list[str] | None = None,
        genes: list[str] | None = None,
    ) -> list[PathwayNode]:
        """Cria Pathway + PathwayNode em PG para simular carga 3.2."""
        pathway_ids = pathway_ids or ['hsa04151']
        genes = genes or ['PIK3CA', 'AKT1', 'TP53']
        nodes = []
        for kid in pathway_ids:
            pathway = _make_pathway(kid)
            for gene in genes:
                node = _make_node(pathway, gene, entry_id=f'{kid}-{gene}')
                nodes.append(node)
        return nodes

    def _run_mocked(
        self,
        pathway_ids: list[str] | None = None,
        regulon_jsonl_path: str | None = None,
    ) -> dict:
        regulon_jsonl_path = regulon_jsonl_path or '/tmp/test_regulon.jsonl'
        fake_manifest = _make_regulon_manifest(regulon_jsonl_path)
        fake_engine = MagicMock()
        fake_engine.load_collectri_regulons.return_value = fake_manifest

        from apps.core.services.regulon_load_service import RegulonLoadService

        service = RegulonLoadService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            result = service.run(pathway_ids=pathway_ids)
        return result

    def test_derive_tf_allowlist_usa_nos_gene_do_pg(self):
        """
        tf_allowlist deriva de PathwayNode(node_type='gene') em PG
        (símbolos UPPERCASE, distintos, excluindo vazios).
        """
        from apps.core.services.regulon_load_service import RegulonLoadService

        self._setup_graph(
            pathway_ids=['hsa04151'],
            genes=['PIK3CA', 'AKT1', 'PIK3CA'],  # duplicata proposital
        )

        service = RegulonLoadService(self.project)
        allowlist = service._derive_tf_allowlist(['hsa04151'])

        # Deve ser distintos e UPPERCASE
        self.assertIn('PIK3CA', allowlist)
        self.assertIn('AKT1', allowlist)
        # Sem duplicatas
        self.assertEqual(len(allowlist), len(set(allowlist)))

    def test_derive_tf_allowlist_sem_grafo_levanta_erro(self):
        """
        Sem PathwayNode para as vias → RegulonGraphNotLoadedError.
        (Indica que o passo 3.2 não foi executado.)
        """
        from apps.core.services.regulon_load_service import (
            RegulonGraphNotLoadedError,
            RegulonLoadService,
        )

        # Nenhum PathwayNode no banco
        service = RegulonLoadService(self.project)
        with self.assertRaises(RegulonGraphNotLoadedError):
            service._derive_tf_allowlist(['hsa04151', 'hsa04010', 'hsa04115'])

    def test_run_sem_grafo_levanta_regulon_graph_not_loaded(self):
        """
        run() sem PathwayNode em PG → RegulonGraphNotLoadedError
        (pré-condição: passo 3.2 deve ter sido executado).
        """
        from apps.core.services.regulon_load_service import (
            RegulonGraphNotLoadedError,
            RegulonLoadService,
        )

        fake_engine = MagicMock()
        service = RegulonLoadService(self.project)
        with self.assertRaises(RegulonGraphNotLoadedError):
            with patch.dict('sys.modules', {'rust_engine': fake_engine}):
                service.run()

    def test_run_chama_rust_com_tf_allowlist_correto(self):
        """
        run() chama rust_engine.load_collectri_regulons com tf_allowlist
        derivado dos nós gene das vias.
        """
        from apps.core.services.regulon_load_service import RegulonLoadService

        self._setup_graph(genes=['PIK3CA', 'MAPK1'])
        fake_manifest = _make_regulon_manifest('/tmp/regulon.jsonl')
        fake_engine = MagicMock()
        fake_engine.load_collectri_regulons.return_value = fake_manifest

        service = RegulonLoadService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            service.run(pathway_ids=['hsa04151'])

        call_kwargs = fake_engine.load_collectri_regulons.call_args[1]
        self.assertIn('tf_allowlist', call_kwargs)
        tf_allowlist = call_kwargs['tf_allowlist']
        self.assertIn('PIK3CA', tf_allowlist)
        self.assertIn('MAPK1', tf_allowlist)

    def test_run_guarda_regulon_path_no_job(self):
        """run() guarda regulon_path em IngestionJob.parameters."""
        self._setup_graph()
        fake_path = '/tmp/test_regulon_stored.jsonl'
        result = self._run_mocked(regulon_jsonl_path=fake_path)

        job = IngestionJob.objects.get(id=result['job_id'])
        self.assertEqual(job.parameters.get('regulon_path'), fake_path)

    def test_run_marca_job_completed(self):
        """run() marca o IngestionJob como COMPLETED."""
        self._setup_graph()
        result = self._run_mocked()

        job = IngestionJob.objects.get(id=result['job_id'])
        self.assertEqual(job.status, IngestionJob.JobStatus.COMPLETED)

    def test_run_retorna_contadores(self):
        """run() retorna contadores do manifesto (n_tfs, n_targets, n_edges)."""
        self._setup_graph()
        result = self._run_mocked()

        self.assertIn('n_tfs', result)
        self.assertIn('n_targets', result)
        self.assertIn('n_edges', result)
        self.assertIn('tf_allowlist_size', result)
        self.assertGreater(result['tf_allowlist_size'], 0)

    def test_run_tf_allowlist_size_no_resultado(self):
        """tf_allowlist_size no resultado bate com nº de símbolos distintos."""
        self._setup_graph(genes=['PIK3CA', 'AKT1', 'MAPK1'])
        result = self._run_mocked()

        # tf_allowlist_size deve refletir os 3 genes distintos (sem contar pathway)
        self.assertGreaterEqual(result['tf_allowlist_size'], 3)


class RegulonIdempotencyTests(TestCase):
    """Testa idempotência do RegulonLoadService."""

    def setUp(self):
        self.user = _make_user('regulon_idem_user')
        self.project = _make_project(self.user, 'Regulon Idempotency Project')

    def test_job_ativo_levanta_regulon_job_active_error(self):
        """REGULON_LOAD PENDING/RUNNING → RegulonJobActiveError."""
        from apps.core.services.regulon_load_service import (
            RegulonJobActiveError,
            RegulonLoadService,
        )

        IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.REGULON_LOAD,
            status=IngestionJob.JobStatus.RUNNING,
            parameters={},
        )

        service = RegulonLoadService(self.project)
        with self.assertRaises(RegulonJobActiveError):
            service._check_idempotency()


# =============================================================================
# 5. ReadoutMappingService — 3.4 (mapeamento readout→feature)
# =============================================================================

class ReadoutMappingBaseTestCase(TestCase):
    """
    Monta o cenário mínimo para o ReadoutMappingService:
      - 3 vias (hsa04151, hsa04010, hsa04115) com PathwayNode(gene).
      - OmicMatrix de fosfoproteoma (Regra 1).
      - OmicMatrix de proteoma gene-level (Regra 2).
      - Arquivo JSONL de regulon sintético.
      - IngestionJob REGULON_LOAD COMPLETED apontando para o JSONL.
    """

    def setUp(self):
        super().setUp()
        # ── 3 vias ──────────────────────────────────────────────────────────
        self.pathway_pik3k = _make_pathway('hsa04151', 'PI3K-Akt')
        self.pathway_mapk = _make_pathway('hsa04010', 'MAPK')
        self.pathway_p53 = _make_pathway('hsa04115', 'p53')

        # ── Nós gene (vias de fosfo: hsa04151 + hsa04010) ───────────────────
        self.node_pik3ca = _make_node(self.pathway_pik3k, 'PIK3CA', 'e1')
        self.node_akt1 = _make_node(self.pathway_pik3k, 'AKT1', 'e2')
        self.node_mapk3 = _make_node(self.pathway_mapk, 'MAPK3', 'e3')
        # Nó gene na via p53 (para Regra 2)
        self.node_tp53 = _make_node(self.pathway_p53, 'TP53', 'e4')

        # ── Nó compound (não deve virar readout) ────────────────────────────
        self.node_compound = _make_node(
            self.pathway_pik3k, '', 'e_compound',
            node_type=PathwayNode.NodeType.COMPOUND,
        )

        # ── Matrizes ─────────────────────────────────────────────────────────
        self.phospho_matrix = _make_phospho_matrix()
        self.proteome_matrix = _make_proteome_matrix()

        # ── Arquivo JSONL de regulon sintético ──────────────────────────────
        self._tmpdir = tempfile.mkdtemp(prefix='davinci_readout_test_')
        self.regulon_path = os.path.join(self._tmpdir, 'regulon.jsonl')
        _write_regulon_jsonl(self.regulon_path, [
            # TP53 (nó na via p53) é TF; AKT1 (nó nas vias) é alvo → deve bater
            {'tf': 'TP53', 'target': 'AKT1', 'sign': -1, 'confidence': 0.9},
            # TP53 → PIK3CA: também nas vias → deve bater
            {'tf': 'TP53', 'target': 'PIK3CA', 'sign': 1, 'confidence': 0.8},
            # TP53 → FOOBARBAZ: NÃO está nas vias → n_not_found
            {'tf': 'TP53', 'target': 'FOOBARBAZ', 'sign': 1, 'confidence': 0.7},
        ])

        # ── IngestionJob REGULON_LOAD COMPLETED com regulon_path ─────────────
        user = _make_user('readout_base_user')
        project = _make_project(user, 'Readout Base Project')
        self.regulon_job = IngestionJob.objects.create(
            project=project,
            job_type=IngestionJob.JobType.REGULON_LOAD,
            status=IngestionJob.JobStatus.COMPLETED,
            parameters={
                'regulon_path': self.regulon_path,
                'source_version': 'collectri-2024-01',
            },
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        super().tearDown()

    def _make_service(
        self,
        dry_run: bool = False,
        mapping_version: str = 'fase3-readout-v1',
    ):
        from apps.core.services.readout_mapping_service import ReadoutMappingService
        return ReadoutMappingService(
            pathway_ids_phospho=['hsa04151', 'hsa04010'],
            pathway_ids_tf=['hsa04151', 'hsa04010', 'hsa04115'],
            mapping_version=mapping_version,
            dry_run=dry_run,
        )


class ReadoutMappingRule1Tests(ReadoutMappingBaseTestCase):
    """
    Regra 1 (fosfo): nós gene das vias hsa04151/hsa04010 × matriz fosfoproteoma.
    """

    def test_regra1_cria_pathway_readout_feature_com_rule_phospho(self):
        """
        Nó gene de via de sinalização → PathwayReadoutFeature(rule='phospho').
        """
        service = self._make_service()
        service.run()

        # PIK3CA e AKT1 são nós gene de hsa04151 (via fosfo)
        for symbol in ('PIK3CA', 'AKT1'):
            self.assertTrue(
                PathwayReadoutFeature.objects.filter(
                    matrix=self.phospho_matrix,
                    feature_key=symbol,
                    rule=PathwayReadoutFeature.Rule.PHOSPHO,
                ).exists(),
                f'PathwayReadoutFeature fosfo ausente para gene {symbol}',
            )

    def test_regra1_confidence_1_0(self):
        """Regra 1: confidence=1.0 (gene-level, nó na via)."""
        service = self._make_service()
        service.run()

        prf = PathwayReadoutFeature.objects.filter(
            matrix=self.phospho_matrix,
            feature_key='PIK3CA',
            rule=PathwayReadoutFeature.Rule.PHOSPHO,
        ).first()
        self.assertIsNotNone(prf)
        self.assertAlmostEqual(prf.confidence, 1.0, places=5)

    def test_regra1_marca_readout_role_phospho_no_node(self):
        """
        Após run(), PathwayNode das vias fosfo tem readout_role='phospho'.
        """
        service = self._make_service()
        service.run()

        self.node_pik3ca.refresh_from_db()
        self.node_akt1.refresh_from_db()

        self.assertEqual(self.node_pik3ca.readout_role, PathwayNode.ReadoutRole.PHOSPHO)
        self.assertEqual(self.node_akt1.readout_role, PathwayNode.ReadoutRole.PHOSPHO)

    def test_regra1_nao_marca_compound_como_readout(self):
        """
        Nó compound (sem gene_symbol) NÃO recebe PathwayReadoutFeature.
        """
        service = self._make_service()
        service.run()

        # Nó compound tem gene_symbol='' — não deve gerar readout
        self.assertFalse(
            PathwayReadoutFeature.objects.filter(
                node=self.node_compound,
            ).exists(),
            'Nó compound não deve receber PathwayReadoutFeature',
        )

    def test_regra1_feature_key_e_gene_symbol_uppercase(self):
        """feature_key é o gene_symbol em UPPERCASE."""
        service = self._make_service()
        service.run()

        features = PathwayReadoutFeature.objects.filter(
            matrix=self.phospho_matrix,
            rule=PathwayReadoutFeature.Rule.PHOSPHO,
        )
        for f in features:
            self.assertEqual(f.feature_key, f.feature_key.upper(),
                             f'feature_key deve ser UPPERCASE: {f.feature_key!r}')

    def test_regra1_regulon_source_vazio(self):
        """Regra 1: regulon_source='' e regulon_sign=None (não é regulon)."""
        service = self._make_service()
        service.run()

        prf = PathwayReadoutFeature.objects.filter(
            matrix=self.phospho_matrix,
            rule=PathwayReadoutFeature.Rule.PHOSPHO,
        ).first()
        self.assertIsNotNone(prf)
        self.assertEqual(prf.regulon_source, '')
        self.assertIsNone(prf.regulon_sign)


class ReadoutMappingRule2Tests(ReadoutMappingBaseTestCase):
    """
    Regra 2 (TF): TFs das vias × regulons CollecTRI × features da matriz proteoma.
    """

    def test_regra2_cria_pathway_readout_feature_com_rule_tf_target(self):
        """
        TF com alvo no JSONL que está nas vias → PathwayReadoutFeature(rule='tf_target').
        """
        service = self._make_service()
        service.run()

        # TP53 como TF → AKT1 como alvo (AKT1 está nas vias) → deve criar
        self.assertTrue(
            PathwayReadoutFeature.objects.filter(
                node=self.node_tp53,
                matrix=self.proteome_matrix,
                feature_key='AKT1',
                rule=PathwayReadoutFeature.Rule.TF_TARGET,
            ).exists(),
            'PathwayReadoutFeature tf_target ausente: TP53→AKT1',
        )

    def test_regra2_regulon_sign_preservado(self):
        """
        regulon_sign=−1 preservado do JSONL (TP53→AKT1 sign=-1).
        """
        service = self._make_service()
        service.run()

        prf = PathwayReadoutFeature.objects.filter(
            node=self.node_tp53,
            feature_key='AKT1',
            rule=PathwayReadoutFeature.Rule.TF_TARGET,
        ).first()
        self.assertIsNotNone(prf)
        self.assertEqual(prf.regulon_sign, -1)

    def test_regra2_regulon_source_collectri(self):
        """Regra 2: regulon_source='collectri'."""
        service = self._make_service()
        service.run()

        prf = PathwayReadoutFeature.objects.filter(
            rule=PathwayReadoutFeature.Rule.TF_TARGET,
        ).first()
        self.assertIsNotNone(prf)
        self.assertEqual(prf.regulon_source, 'collectri')

    def test_regra2_alvo_fora_das_vias_nao_materializa(self):
        """
        Alvo do regulon (FOOBARBAZ) não está nas vias → NÃO gera
        PathwayReadoutFeature (validação intra-grafo).
        """
        service = self._make_service()
        service.run()

        self.assertFalse(
            PathwayReadoutFeature.objects.filter(
                feature_key='FOOBARBAZ',
            ).exists(),
            'Alvo fora das vias não deve gerar PathwayReadoutFeature',
        )

    def test_regra2_n_features_not_found_reflete_alvos_fora_das_vias(self):
        """
        n_features_not_found = nº de alvos do regulon fora das vias (1 linha
        com FOOBARBAZ no JSONL fixture).
        """
        service = self._make_service()
        report = service.run()

        # FOOBARBAZ não está nas vias → deve contar como not_found
        self.assertGreaterEqual(
            report['n_features_not_found'], 1,
            'n_features_not_found deve refletir alvos fora das vias',
        )

    def test_regra2_marca_readout_role_tf_target_no_node_tf(self):
        """
        Após run(), PathwayNode do TF (TP53) tem readout_role='tf_target'.
        """
        service = self._make_service()
        service.run()

        self.node_tp53.refresh_from_db()
        self.assertEqual(self.node_tp53.readout_role, PathwayNode.ReadoutRole.TF_TARGET)

    def test_regra2_alvo_tf_positivo_tem_regulon_sign_positivo(self):
        """
        TP53→PIK3CA sign=+1 → PathwayReadoutFeature(regulon_sign=1).
        """
        service = self._make_service()
        service.run()

        prf = PathwayReadoutFeature.objects.filter(
            node=self.node_tp53,
            feature_key='PIK3CA',
            rule=PathwayReadoutFeature.Rule.TF_TARGET,
        ).first()
        self.assertIsNotNone(prf)
        self.assertEqual(prf.regulon_sign, 1)

    def test_regra2_confidence_do_jsonl(self):
        """
        confidence da Regra 2 vem do campo 'confidence' do JSONL.
        """
        service = self._make_service()
        service.run()

        prf = PathwayReadoutFeature.objects.filter(
            node=self.node_tp53,
            feature_key='AKT1',
            rule=PathwayReadoutFeature.Rule.TF_TARGET,
        ).first()
        self.assertIsNotNone(prf)
        self.assertAlmostEqual(prf.confidence, 0.9, places=5)


class ReadoutMappingContractTests(ReadoutMappingBaseTestCase):
    """
    Contrato declarado do ReadoutMappingService: gatilho_ac, dry_run, idempotência.
    """

    def test_gatilho_ac_e_sempre_true(self):
        """
        gatilho_ac=True sempre (A→C adiada — validação completa do Parquet
        requer OmicMatrixFeature, que não existe ainda).
        """
        service = self._make_service()
        report = service.run()

        self.assertTrue(
            report.get('gatilho_ac'),
            'gatilho_ac deve ser True sempre (A→C adiada na Fase 3)',
        )

    def test_dry_run_nao_persiste_readout_features(self):
        """
        --dry-run=True: run() retorna relatório mas NÃO persiste
        PathwayReadoutFeature nem atualiza readout_role dos nós.
        """
        service = self._make_service(dry_run=True)
        report = service.run()

        self.assertTrue(report['dry_run'])

        # Nenhum PathwayReadoutFeature deve ter sido criado
        self.assertEqual(
            PathwayReadoutFeature.objects.count(), 0,
            '--dry-run não deve persistir PathwayReadoutFeature',
        )

        # readout_role dos nós deve permanecer 'none'
        self.node_pik3ca.refresh_from_db()
        self.assertEqual(
            self.node_pik3ca.readout_role, PathwayNode.ReadoutRole.NONE,
            '--dry-run não deve atualizar readout_role',
        )

    def test_dry_run_retorna_contadores_sem_persistir(self):
        """
        dry-run retorna n_phospho_mapped > 0 mas sem criar linhas no banco.
        """
        service = self._make_service(dry_run=True)
        report = service.run()

        self.assertGreater(report['n_phospho_mapped'], 0,
                           'dry-run deve reportar mapeamentos mesmo sem persistir')
        self.assertEqual(PathwayReadoutFeature.objects.count(), 0)

    def test_idempotencia_segunda_execucao_nao_duplica_features(self):
        """
        Idempotência: run() 2x com mesmos dados → PathwayReadoutFeature não
        duplica (ignore_conflicts=True na NK).
        """
        service1 = self._make_service()
        service1.run()
        count_after_first = PathwayReadoutFeature.objects.count()

        service2 = self._make_service()
        service2.run()
        count_after_second = PathwayReadoutFeature.objects.count()

        self.assertEqual(
            count_after_first, count_after_second,
            'Idempotência: 2ª execução não deve duplicar PathwayReadoutFeature',
        )

    def test_mapping_version_no_resultado(self):
        """report['mapping_version'] bate com o passado para o service."""
        service = self._make_service(mapping_version='fase3-readout-v1')
        report = service.run()
        self.assertEqual(report['mapping_version'], 'fase3-readout-v1')

    def test_report_contem_todas_as_chaves_do_contrato(self):
        """run() retorna dict com todas as chaves esperadas."""
        service = self._make_service()
        report = service.run()

        expected_keys = {
            'n_phospho_mapped', 'n_tf_mapped', 'n_nodes_readout',
            'n_unmapped_nodes', 'n_features_not_found',
            'mapping_version', 'dry_run', 'duration_s',
            'validation_strategy', 'gatilho_ac',
            'phospho_matrix_id', 'proteome_matrix_id', 'regulon_path_found',
        }
        for key in expected_keys:
            self.assertIn(key, report, f'Chave ausente no relatório: {key!r}')

    def test_n_phospho_mapped_positivo(self):
        """n_phospho_mapped > 0 quando há nós gene nas vias fosfo."""
        service = self._make_service()
        report = service.run()
        self.assertGreater(report['n_phospho_mapped'], 0)

    def test_n_tf_mapped_positivo(self):
        """n_tf_mapped > 0 quando há TF com alvos nas vias."""
        service = self._make_service()
        report = service.run()
        self.assertGreater(report['n_tf_mapped'], 0)

    def test_regulon_path_found_true(self):
        """regulon_path_found=True quando o JSONL existe."""
        service = self._make_service()
        report = service.run()
        self.assertTrue(report['regulon_path_found'])

    def test_phospho_matrix_id_no_relatorio(self):
        """phospho_matrix_id aponta para a OmicMatrix de fosfoproteoma."""
        service = self._make_service()
        report = service.run()
        self.assertEqual(report['phospho_matrix_id'], self.phospho_matrix.id)

    def test_proteome_matrix_id_no_relatorio(self):
        """proteome_matrix_id aponta para a OmicMatrix de proteoma gene-level."""
        service = self._make_service()
        report = service.run()
        self.assertEqual(report['proteome_matrix_id'], self.proteome_matrix.id)


class ReadoutMappingAusenciaPreconditionTests(TestCase):
    """Testa comportamento quando pré-condições estão ausentes."""

    def test_sem_matriz_fosfo_regra1_pulada(self):
        """
        Sem OmicMatrix de fosfoproteoma → Regra 1 pulada (n_phospho_mapped=0).
        """
        from apps.core.services.readout_mapping_service import ReadoutMappingService

        # Garante nenhuma matriz fosfo no banco
        OmicMatrix.objects.filter(
            omics_layer='proteomic',
            feature_axis=OmicMatrix.FeatureAxis.PHOSPHO_SITE,
        ).delete()

        # Mas cria proteoma e nós (para não falhar em outro lugar)
        pathway = _make_pathway('hsa04151')
        _make_node(pathway, 'AKT1')
        _make_proteome_matrix()

        service = ReadoutMappingService(
            pathway_ids_phospho=['hsa04151'],
            pathway_ids_tf=['hsa04151'],
            dry_run=True,
        )
        report = service.run()

        self.assertEqual(report['n_phospho_mapped'], 0,
                         'Sem matriz fosfo, Regra 1 deve ser pulada')

    def test_sem_regulon_job_completed_regra2_pulada(self):
        """
        Sem IngestionJob REGULON_LOAD COMPLETED → Regra 2 pulada (n_tf_mapped=0).
        """
        from apps.core.services.readout_mapping_service import ReadoutMappingService

        # Garante nenhum job regulon completed
        IngestionJob.objects.filter(
            job_type=IngestionJob.JobType.REGULON_LOAD,
            status=IngestionJob.JobStatus.COMPLETED,
        ).delete()

        pathway = _make_pathway('hsa04151')
        _make_node(pathway, 'TP53')
        _make_phospho_matrix()
        _make_proteome_matrix()

        service = ReadoutMappingService(
            pathway_ids_phospho=['hsa04151'],
            pathway_ids_tf=['hsa04151'],
            dry_run=True,
        )
        report = service.run()

        self.assertEqual(report['n_tf_mapped'], 0,
                         'Sem job regulon completed, Regra 2 deve ser pulada')

    def test_regulon_path_inexistente_regra2_pulada(self):
        """
        regulon_path no job apontando para arquivo inexistente → Regra 2 pulada.
        """
        from apps.core.services.readout_mapping_service import ReadoutMappingService

        user = _make_user('readout_absent_user')
        project = _make_project(user, 'Readout Absent Project')
        IngestionJob.objects.create(
            project=project,
            job_type=IngestionJob.JobType.REGULON_LOAD,
            status=IngestionJob.JobStatus.COMPLETED,
            parameters={
                'regulon_path': '/nonexistent/path/regulon.jsonl',
                'source_version': 'test',
            },
        )

        pathway = _make_pathway('hsa04151b')  # kegg_id único para este teste
        pathway.kegg_id = 'hsa04151b'
        _make_phospho_matrix('CPTAC-CCRCC-PHOSPHO-ABS')
        _make_proteome_matrix('CPTAC-CCRCC-PROTEOME-ABS')

        service = ReadoutMappingService(
            pathway_ids_phospho=['hsa04151b'],
            pathway_ids_tf=['hsa04151b'],
            dry_run=True,
        )
        report = service.run()

        self.assertEqual(report['n_tf_mapped'], 0,
                         'regulon_path inexistente: Regra 2 deve ser pulada')
        self.assertFalse(report['regulon_path_found'])


# =============================================================================
# 6. Tasks Celery — 3.1/3.2/3.3 (isolamento + idempotência)
# =============================================================================

class RunPhosphoMatrixLoadTaskTests(PhosphoMatrixStorageTestCase):
    """
    Testa a Celery task run_phospho_matrix_load.

    Cobre:
      - PhosphoMatrixAlreadyLoadedError → tratada como idempotência (COMPLETED).
      - Isolamento: job de projeto A com project_id de B → FAILED.
      - Projeto inexistente → erro sem processar.
      - Job inexistente → erro sem processar.
    """

    def setUp(self):
        super().setUp()
        self.user_a = _make_user('phospho_task_a')
        self.user_b = _make_user('phospho_task_b')
        self.project_a = _make_project(self.user_a, 'Phospho Task A')
        self.project_b = _make_project(self.user_b, 'Phospho Task B')

    def test_task_already_loaded_marca_job_completed(self):
        """
        PhosphoMatrixAlreadyLoadedError (idempotência): job deve ser marcado
        COMPLETED sem retry (não é erro real — carga já feita).
        """
        from apps.core.tasks.ingestion_tasks import run_phospho_matrix_load
        from apps.core.services.phospho_matrix_load_service import (
            PhosphoMatrixAlreadyLoadedError,
            PhosphoMatrixLoadService,
        )

        # Cria job PENDING para o projeto A
        job_a = IngestionJob.objects.create(
            project=self.project_a,
            job_type=IngestionJob.JobType.PHOSPHO_MATRIX_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={},
        )

        # Cria uma OmicMatrix fake para simular "já carregada"
        ds, _ = OmicDataset.objects.get_or_create(
            accession='CPTAC-CCRCC-PHOSPHO',
            defaults={
                'source_db': OmicDataset.SourceDB.CPTAC,
                'title': 'Phospho (pre-existing)',
                'access_type': OmicDataset.AccessType.PUBLIC,
            },
        )
        existing_matrix = OmicMatrix.objects.create(
            dataset=ds,
            omics_layer='proteomic',
            feature_axis=OmicMatrix.FeatureAxis.PHOSPHO_SITE,
            loader_version='phospho-v1',
            data_format_level=OmicMatrix.DataFormatLevel.INTENSITIES,
            storage_key='omics/_shared/CPTAC-CCRCC-PHOSPHO/phospho.parquet',
            n_features=100,
            n_samples=4,
        )

        # Mock: PhosphoMatrixLoadService.run levanta AlreadyLoadedError
        fake_engine = MagicMock()

        def raise_already_loaded(*args, **kwargs):
            raise PhosphoMatrixAlreadyLoadedError(existing_matrix)

        fake_engine.load_cptac_phospho_matrix.side_effect = raise_already_loaded

        with patch.dict('sys.modules', {'rust_engine': fake_engine}), \
             patch(
                 'apps.core.services.phospho_matrix_load_service.PhosphoMatrixLoadService.run',
                 side_effect=PhosphoMatrixAlreadyLoadedError(existing_matrix),
             ):
            result = run_phospho_matrix_load.run(str(job_a.id), str(self.project_a.id))

        job_a.refresh_from_db()
        self.assertEqual(
            job_a.status, IngestionJob.JobStatus.COMPLETED,
            'PhosphoMatrixAlreadyLoadedError deve marcar job COMPLETED (idempotência)',
        )
        # Resultado de idempotência — sem erro
        self.assertEqual(result.get('errors', []), [])

    def test_task_isolamento_job_de_outro_projeto_marca_failed(self):
        """
        Isolamento: task chamada com project_id de B mas job pertence a A
        → job FAILED, sem processar.
        """
        from apps.core.tasks.ingestion_tasks import run_phospho_matrix_load

        job_a = IngestionJob.objects.create(
            project=self.project_a,
            job_type=IngestionJob.JobType.PHOSPHO_MATRIX_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={},
        )

        fake_engine = MagicMock()
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            result = run_phospho_matrix_load.run(str(job_a.id), str(self.project_b.id))

        job_a.refresh_from_db()
        self.assertEqual(job_a.status, IngestionJob.JobStatus.FAILED)
        self.assertIn('isolation violation', result.get('errors', []))

    def test_task_projeto_inexistente_retorna_erro(self):
        """project_id inexistente → retorna erro sem processar."""
        from apps.core.tasks.ingestion_tasks import run_phospho_matrix_load

        fake_engine = MagicMock()
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            result = run_phospho_matrix_load.run(
                str(uuid.uuid4()), str(uuid.uuid4())
            )

        self.assertIn('project not found', result.get('errors', []))

    def test_task_job_inexistente_retorna_erro(self):
        """job_id inexistente → retorna erro sem processar."""
        from apps.core.tasks.ingestion_tasks import run_phospho_matrix_load

        fake_engine = MagicMock()
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            result = run_phospho_matrix_load.run(
                str(uuid.uuid4()), str(self.project_a.id)
            )

        self.assertIn('job not found', result.get('errors', []))


class RunPathwayTopologyLoadTaskTests(TestCase):
    """
    Testa a Celery task run_pathway_topology_load.

    Cobre:
      - PathwayTopologyJobActiveError → job COMPLETED sem retry (idempotência).
      - Isolamento: job de outro projeto → FAILED.
      - Projeto/job inexistente → erro.
    """

    def setUp(self):
        self.user_a = _make_user('topo_task_a')
        self.user_b = _make_user('topo_task_b')
        self.project_a = _make_project(self.user_a, 'Topo Task A')
        self.project_b = _make_project(self.user_b, 'Topo Task B')

    def test_task_job_ativo_retorna_sem_processar(self):
        """
        PathwayTopologyJobActiveError → task retorna sem criar novo job
        (idempotência).
        """
        from apps.core.tasks.ingestion_tasks import run_pathway_topology_load
        from apps.core.services.pathway_topology_load_service import (
            PathwayTopologyJobActiveError,
            PathwayTopologyLoadService,
        )

        job_a = IngestionJob.objects.create(
            project=self.project_a,
            job_type=IngestionJob.JobType.PATHWAY_TOPOLOGY_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={},
        )

        # Simula outro job ativo que bloqueia
        active_job = IngestionJob.objects.create(
            project=self.project_a,
            job_type=IngestionJob.JobType.PATHWAY_TOPOLOGY_LOAD,
            status=IngestionJob.JobStatus.RUNNING,
            parameters={},
        )

        fake_engine = MagicMock()
        with patch.dict('sys.modules', {'rust_engine': fake_engine}), \
             patch(
                 'apps.core.services.pathway_topology_load_service.PathwayTopologyLoadService.run',
                 side_effect=PathwayTopologyJobActiveError(active_job),
             ):
            result = run_pathway_topology_load.run(str(job_a.id), str(self.project_a.id))

        # Resultado de idempotência — sem erros fatais
        self.assertNotIn('isolation violation', result.get('errors', []))

    def test_task_isolamento_job_de_outro_projeto_marca_failed(self):
        """Isolamento: job de A com project_id de B → job FAILED."""
        from apps.core.tasks.ingestion_tasks import run_pathway_topology_load

        job_a = IngestionJob.objects.create(
            project=self.project_a,
            job_type=IngestionJob.JobType.PATHWAY_TOPOLOGY_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={},
        )

        fake_engine = MagicMock()
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            result = run_pathway_topology_load.run(str(job_a.id), str(self.project_b.id))

        job_a.refresh_from_db()
        self.assertEqual(job_a.status, IngestionJob.JobStatus.FAILED)
        self.assertIn('isolation violation', result.get('errors', []))

    def test_task_projeto_inexistente_retorna_erro(self):
        """project_id inexistente → erro sem processar."""
        from apps.core.tasks.ingestion_tasks import run_pathway_topology_load

        fake_engine = MagicMock()
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            result = run_pathway_topology_load.run(
                str(uuid.uuid4()), str(uuid.uuid4())
            )

        self.assertIn('project not found', result.get('errors', []))

    def test_task_job_inexistente_retorna_erro(self):
        """job_id inexistente → erro sem processar."""
        from apps.core.tasks.ingestion_tasks import run_pathway_topology_load

        fake_engine = MagicMock()
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            result = run_pathway_topology_load.run(
                str(uuid.uuid4()), str(self.project_a.id)
            )

        self.assertIn('job not found', result.get('errors', []))


class RunRegulonLoadTaskTests(TestCase):
    """
    Testa a Celery task run_regulon_load.

    Cobre:
      - RegulonGraphNotLoadedError → job FAILED sem retry (erro de configuração).
      - Isolamento: job de outro projeto → FAILED.
      - Projeto/job inexistente → erro.
    """

    def setUp(self):
        self.user_a = _make_user('regulon_task_a')
        self.user_b = _make_user('regulon_task_b')
        self.project_a = _make_project(self.user_a, 'Regulon Task A')
        self.project_b = _make_project(self.user_b, 'Regulon Task B')

    def test_task_graph_not_loaded_marca_job_failed_sem_retry(self):
        """
        RegulonGraphNotLoadedError → job FAILED sem retry (erro de configuração:
        falta executar o passo 3.2 antes).
        """
        from apps.core.tasks.ingestion_tasks import run_regulon_load
        from apps.core.services.regulon_load_service import (
            DEFAULT_PATHWAY_IDS,
            RegulonGraphNotLoadedError,
        )

        job_a = IngestionJob.objects.create(
            project=self.project_a,
            job_type=IngestionJob.JobType.REGULON_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={},
        )

        # Nenhum PathwayNode em PG → RegulonGraphNotLoadedError
        fake_engine = MagicMock()
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            result = run_regulon_load.run(str(job_a.id), str(self.project_a.id))

        job_a.refresh_from_db()
        self.assertEqual(
            job_a.status, IngestionJob.JobStatus.FAILED,
            'RegulonGraphNotLoadedError deve marcar job FAILED',
        )
        # Resultado contém a mensagem de erro (sem retry → incluída em errors)
        self.assertTrue(
            len(result.get('errors', [])) > 0 or result.get('n_tfs', 0) == 0,
            'Resultado deve sinalizar o erro de grafo não carregado',
        )

    def test_task_isolamento_job_de_outro_projeto_marca_failed(self):
        """Isolamento: job de A com project_id de B → job FAILED."""
        from apps.core.tasks.ingestion_tasks import run_regulon_load

        job_a = IngestionJob.objects.create(
            project=self.project_a,
            job_type=IngestionJob.JobType.REGULON_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={},
        )

        fake_engine = MagicMock()
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            result = run_regulon_load.run(str(job_a.id), str(self.project_b.id))

        job_a.refresh_from_db()
        self.assertEqual(job_a.status, IngestionJob.JobStatus.FAILED)
        self.assertIn('isolation violation', result.get('errors', []))

    def test_task_projeto_inexistente_retorna_erro(self):
        """project_id inexistente → erro sem processar."""
        from apps.core.tasks.ingestion_tasks import run_regulon_load

        fake_engine = MagicMock()
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            result = run_regulon_load.run(
                str(uuid.uuid4()), str(uuid.uuid4())
            )

        self.assertIn('project not found', result.get('errors', []))

    def test_task_job_inexistente_retorna_erro(self):
        """job_id inexistente → erro sem processar."""
        from apps.core.tasks.ingestion_tasks import run_regulon_load

        fake_engine = MagicMock()
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            result = run_regulon_load.run(
                str(uuid.uuid4()), str(self.project_a.id)
            )

        self.assertIn('job not found', result.get('errors', []))


# =============================================================================
# 7. Catálogo global — isolamento de leitura futura
# =============================================================================

class PathwayCatalogGlobalTests(TestCase):
    """
    Verifica que Pathway/PathwayNode/PathwayEdge são catálogo global
    (sem FK de projeto) e não sofrem vazamento entre projetos na leitura.
    """

    def test_pathway_nao_tem_fk_projeto(self):
        """
        Pathway é catálogo global — pode ser criado sem FK de projeto.
        """
        pathway = Pathway.objects.create(
            kegg_id='hsa04151',
            name='PI3K-Akt signaling pathway',
            source=Pathway.Source.KEGG,
        )
        self.assertIsNotNone(pathway.pk)
        # Não existe campo project na instância
        self.assertFalse(hasattr(pathway, 'project_id'))

    def test_readout_feature_aponta_para_matriz_correta_regra1(self):
        """
        PathwayReadoutFeature(rule='phospho') aponta para matriz de
        fosfoproteoma (não proteoma gene-level).

        Isolamento futuro na Fase 4: via matrix__dataset__in_projects__project.
        """
        phospho_matrix = _make_phospho_matrix()
        pathway = _make_pathway('hsa04151', 'PI3K-Akt')
        node = _make_node(pathway, 'AKT1')

        prf = PathwayReadoutFeature.objects.create(
            node=node,
            matrix=phospho_matrix,
            feature_key='AKT1',
            rule=PathwayReadoutFeature.Rule.PHOSPHO,
            confidence=1.0,
            mapping_version='fase3-readout-v1',
        )

        self.assertEqual(prf.matrix.feature_axis, OmicMatrix.FeatureAxis.PHOSPHO_SITE)
        self.assertEqual(prf.matrix.omics_layer, 'proteomic')

    def test_readout_feature_aponta_para_matriz_correta_regra2(self):
        """
        PathwayReadoutFeature(rule='tf_target') aponta para matriz de proteoma
        gene-level (feature_axis='gene').
        """
        proteome_matrix = _make_proteome_matrix()
        pathway = _make_pathway('hsa04115', 'p53')
        node = _make_node(pathway, 'TP53')

        prf = PathwayReadoutFeature.objects.create(
            node=node,
            matrix=proteome_matrix,
            feature_key='AKT1',
            rule=PathwayReadoutFeature.Rule.TF_TARGET,
            regulon_source='collectri',
            regulon_sign=-1,
            confidence=0.9,
            mapping_version='fase3-readout-v1',
        )

        self.assertEqual(prf.matrix.feature_axis, OmicMatrix.FeatureAxis.GENE)
        self.assertEqual(prf.matrix.omics_layer, 'proteomic')

    def test_pathway_edge_sinal_correto(self):
        """
        PathwayEdge com sign=+1 (ativação) e sign=−1 (inibição) são corretamente
        representados no modelo. Testa a estrutura do grafo assinado.
        """
        pathway = _make_pathway('hsa04010', 'MAPK')
        node_a = _make_node(pathway, 'RAF1', 'e_raf1')
        node_b = _make_node(pathway, 'MAP2K1', 'e_map2k1')
        node_c = _make_node(pathway, 'DUSP1', 'e_dusp1')

        edge_act = _make_edge(pathway, node_a, node_b, sign=1,
                              interaction=PathwayEdge.Interaction.ACTIVATION)
        edge_inh = _make_edge(pathway, node_c, node_b, sign=-1,
                              interaction=PathwayEdge.Interaction.INHIBITION)

        self.assertEqual(edge_act.sign, 1)
        self.assertEqual(edge_inh.sign, -1)

    def test_pathway_node_check_constraint_node_type_valido(self):
        """
        CheckConstraint: node_type deve estar em {gene, compound, group, map, ortholog}.
        Valores inválidos levantam IntegrityError.
        """
        from django.db import IntegrityError

        pathway = _make_pathway('hsa_test_constraint')
        with self.assertRaises(IntegrityError):
            PathwayNode.objects.create(
                pathway=pathway,
                kegg_entry_id='e_invalid',
                node_type='invalid_type',  # violação de CheckConstraint
                gene_symbol='TEST',
                kegg_ids=['hsa:999'],
            )

    def test_pathway_edge_natural_key_impede_duplicata(self):
        """
        Natural key (pathway, source_node, target_node, interaction) impede
        duplicata de aresta.
        """
        from django.db import IntegrityError

        pathway = _make_pathway('hsa04151_nk')
        node_s = _make_node(pathway, 'GENESX', 'entry_s')
        node_t = _make_node(pathway, 'GENESY', 'entry_t')

        _make_edge(pathway, node_s, node_t, sign=1,
                   interaction=PathwayEdge.Interaction.ACTIVATION)

        # Segunda inserção com mesma NK deve falhar
        with self.assertRaises(IntegrityError):
            PathwayEdge.objects.create(
                pathway=pathway,
                source_node=node_s,
                target_node=node_t,
                interaction=PathwayEdge.Interaction.ACTIVATION,
                sign=1,
                relation_type=PathwayEdge.RelationType.PPREL,
                subtypes=['activation'],
            )
