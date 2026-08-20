"""
test_omnispathway_fase4_pfs.py — Cobertura do motor PFS v1 (orquestração
Django) — OmnisPathway Objetivo 2, Fase 4.

Módulos cobertos:
  - apps.core.services.pathway_scoring_service (PathwayScoringService)
  - apps.core.tasks.ingestion_tasks (run_pathway_scoring)
  - apps.core.models.PathwayActivityScore

O NÚCLEO Rust (RWR assinado / ULM / permutação) já tem cobertura própria
(370 testes `#[cfg(test)]`) — este arquivo testa SÓ a órbita Django:
pré-checagens, gates, orquestração de job, isolamento e o contrato do
relatório. `rust_engine.run_pfs_scoring` é SEMPRE mockado (patch.dict
sys.modules) — nunca chamado de verdade, sem rede, sem Postgres via Rust.

Casos cobertos (ver prompt do plano da Fase 4):
  1. Pré-checagens falham ALTO, uma por vez, cada mensagem cita o command:
     grafo ausente; readouts não mapeados; OmicMatrixFeature vazia em CADA
     matriz separadamente; pares ausentes — com destaque para o caso
     "par validado SÓ em uma das duas matrizes" (a NK de OmicSamplePairing
     é (matrix, case_id); pareamento é POR MATRIZ).
  2. Zero seeds NÃO é erro — deliberadamente sem gate de existência de
     VariantEffectSeed. O run completa, grava escore, sinaliza
     n_seeds_on_graph=0.
  3. Gate de reprodutibilidade (D3): parâmetros divergentes sob o MESMO
     method_version → PfsReproducibilityGateError; mesmos parâmetros passa;
     --force passa mesmo divergindo; method_version novo nunca dispara;
     scores sem job de proveniência → reason='no_provenance'; dry_run nunca
     dispara o gate.
  4. Isolamento (Regra #3): matriz de outro projeto não resolve;
     PathwayActivityScore é catálogo global (sem FK de projeto).
  5. Idempotência: rodar 2x com os mesmos parâmetros não duplica (NK) e não
     falha (o 2º run passa pelo próprio gate de reprodutibilidade, que
     reconhece "mesmos parâmetros" como não-divergência).
  6. --dry-run: NÃO persiste PathwayActivityScore. Comportamento REAL
     confirmado no código-fonte antes de assertar: o job AINDA é marcado
     COMPLETED (não há ramo especial para dry_run em _execute) — só a
     escrita em PathwayActivityScore é condicional (no fake engine, que
     espelha o que o Rust faz de verdade com o parâmetro dry_run).
  7. Task Celery run_pathway_scoring: projeto/job inexistente, isolamento
     cross-project (mesmo padrão de run_cnv_seed_load).
  8. Contrato dos contadores: n_readouts_mapped == n_readouts_with_value +
     n_readouts_missing no relatório; readout_coverage_ratio =
     with_value/mapped (não mapped/with_value — bug real corrigido: a razão
     chegou a 6664% antes da correção, hoje dá 79,3%).
  9. Modelo PathwayActivityScore: NK rejeita duplicata; pairing aceita NULL;
     method_version diferente coexiste (D10).

Padrões obrigatórios:
  - SEM rede: rust_engine SEMPRE mockado (patch.dict sys.modules).
  - SEM pytest: django.test.TestCase.
  - default_storage mockado com FileSystemStorage em tmpdir (mesmo padrão
    de test_cnv_seed.py / test_omnispathway_fase3.py).
  - Fake engine que PERSISTE de verdade em PathwayActivityScore via ORM
    quando isso torna o teste mais honesto (idempotência via NK, dry-run
    condicionado) — não confia cegamente no manifesto para o que a ORM
    consegue verificar por si.
"""

from __future__ import annotations

import itertools
import os
import shutil
import tempfile
import uuid
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.files.storage import FileSystemStorage
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.core.models import (
    DaVinciProject,
    IngestionJob,
    OmicDataset,
    OmicMatrix,
    OmicMatrixFeature,
    OmicSample,
    OmicSamplePairing,
    Pathway,
    PathwayActivityScore,
    PathwayEdge,
    PathwayNode,
    PathwayReadoutFeature,
    ProjectDataset,
)
from apps.core.services.pathway_scoring_service import (
    DEFAULT_MIN_EDGES,
    DEFAULT_MIN_REGULON_TARGETS,
    DEFAULT_MIN_SIGNED_FRACTION,
    DEFAULT_N_PERMUTATIONS,
    DEFAULT_PATHWAY_KEGG_IDS,
    DEFAULT_RESTART,
    DEFAULT_RNG_SEED,
    DEFAULT_SEED_METHOD_VERSIONS,
    DEFAULT_UNSIGNED_WEIGHT,
    RECOMMENDED_MIN_EDGES,
    RECOMMENDED_MIN_SIGNED_FRACTION,
    TOPOLOGY_EXCLUSION_LOW_SIGNED_FRACTION,
    TOPOLOGY_EXCLUSION_NO_EDGES,
    TOPOLOGY_EXCLUSION_TOO_FEW_EDGES,
    PathwayScoringService,
    PfsFeaturesNotCataloguedError,
    PfsGraphNotLoadedError,
    PfsJobActiveError,
    PfsMatrixNotFoundError,
    PfsPairingMissingError,
    PfsReadoutsNotMappedError,
    PfsReproducibilityGateError,
    _resolve_default_pathway_ids,
    _topology_quality_reason,
)

MAPPING_VERSION = 'fase3-readout-v1'


# =============================================================================
# Helpers de factory — reutiliza padrões dos outros testes do projeto
# =============================================================================

def _make_user(username: str) -> User:
    return User.objects.create_user(username=username, password='pw')


def _make_project(user: User, title: str) -> DaVinciProject:
    slug = f'{title.lower().replace(" ", "-")}-{user.username}'
    return DaVinciProject.objects.create(
        user=user, title=title, slug=slug, query_term='fase4-pfs-test',
    )


def _link_dataset(
    project: DaVinciProject,
    dataset: OmicDataset,
    status: str = ProjectDataset.CurationStatus.INCLUDED,
) -> ProjectDataset:
    return ProjectDataset.objects.create(
        project=project, dataset=dataset, curation_status=status,
    )


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
    source: PathwayNode,
    target: PathwayNode,
    sign: int,
    interaction: str = PathwayEdge.Interaction.BINDING,
    relation_type: str = PathwayEdge.RelationType.PPREL,
) -> PathwayEdge:
    """Factory de PathwayEdge — usada pelos testes do filtro de qualidade
    de topologia (D-2). `sign` é o que a agregação Count(filter=Q(sign__in
    =[1,-1])) do filtro conta como 'assinada'."""
    return PathwayEdge.objects.create(
        pathway=pathway,
        source_node=source,
        target_node=target,
        sign=sign,
        interaction=interaction,
        relation_type=relation_type,
        subtypes=[],
    )


def _make_pathway_topology(pathway: Pathway, n_edges: int, n_signed: int) -> None:
    """
    Cria `n_edges` PathwayEdge DISTINTAS na via, das quais as `n_signed`
    primeiras têm `sign=1` (assinada) e o resto `sign=0` (sem sinal) — usado
    pelos testes do filtro de qualidade de topologia (D-2) para simular
    perfis de grafo (poucas arestas / fração baixa / aprovado) sem laço
    manual por par (nós suficientes + `itertools.permutations` garante
    pares (source, target) nunca repetidos, respeitando a natural key de
    PathwayEdge).
    """
    assert n_signed <= n_edges
    n_nodes = 2
    while n_nodes * (n_nodes - 1) < n_edges:
        n_nodes += 1
    nodes = [
        _make_node(pathway, f'TOPOGENE{i}', f'topo-{pathway.kegg_id}-{i}')
        for i in range(n_nodes)
    ]
    pairs = list(itertools.permutations(nodes, 2))[:n_edges]
    for idx, (source, target) in enumerate(pairs):
        _make_edge(pathway, source, target, sign=1 if idx < n_signed else 0)


def _make_phospho_matrix(accession: str = 'CPTAC-PFS-PHOSPHO') -> OmicMatrix:
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
            'n_samples': 2,
            'checksum_md5': 'aabbccddeeff11223344556677889900',
        },
    )
    return matrix


def _make_proteome_matrix(accession: str = 'CPTAC-PFS-PROTEOME') -> OmicMatrix:
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
            'n_samples': 2,
            'checksum_md5': 'ccddee334455667788990011aabbcc33',
        },
    )
    return matrix


def _make_matrix_features(matrix: OmicMatrix, feature_keys: list[str]) -> None:
    OmicMatrixFeature.objects.bulk_create([
        OmicMatrixFeature(matrix=matrix, feature_key=key.upper(), row_index=idx)
        for idx, key in enumerate(feature_keys)
    ])


def _make_pair_samples(case_id: str) -> tuple[OmicSample, OmicSample]:
    ds, _ = OmicDataset.objects.get_or_create(
        accession='PFS-SHARED-SAMPLES',
        defaults={
            'source_db': OmicDataset.SourceDB.CPTAC,
            'title': 'PFS shared samples',
            'access_type': OmicDataset.AccessType.PUBLIC,
        },
    )
    tumor, _ = OmicSample.objects.get_or_create(
        accession=f'{case_id}_tumor',
        defaults={
            'dataset': ds,
            'title': f'{case_id} tumor',
            'organism': 'Homo sapiens',
            'characteristics': {'case_id': case_id},
        },
    )
    normal, _ = OmicSample.objects.get_or_create(
        accession=f'{case_id}_normal',
        defaults={
            'dataset': ds,
            'title': f'{case_id} normal',
            'organism': 'Homo sapiens',
            'characteristics': {'case_id': case_id},
        },
    )
    return tumor, normal


def _make_pairing(
    matrix: OmicMatrix,
    case_id: str,
    tumor: OmicSample,
    normal: OmicSample,
    validated: bool = True,
) -> OmicSamplePairing:
    return OmicSamplePairing.objects.create(
        matrix=matrix,
        tumor_sample=tumor,
        normal_sample=normal,
        case_id=case_id,
        validated_overlap=validated,
    )


# ── manifesto fake (PfsRunManifest) e engine que persiste de verdade ────────

def _make_pfs_manifest(
    *,
    n_pathways: int = 3,
    n_pathways_no_readout: int = 0,
    n_cases_scored: int = 1,
    n_rows_written: int = 1,
    n_seeds_total: int = 0,
    n_seeds_on_graph: int = 0,
    n_seeds_off_graph: int = 0,
    n_seeds_masked_by_neutral_v2: int = 0,
    n_readouts_mapped: int = 2,
    n_readouts_with_value: int = 2,
    n_readouts_missing: int = 0,
    n_readouts_phospho: int = 1,
    n_readouts_tf: int = 1,
    n_tf_below_min_targets: int = 0,
    n_tumor_unpaired_skipped: int = 0,
    n_zero_sd_null: int = 0,
    n_signed_edges: int = 10,
    n_unsigned_edges: int = 2,
) -> MagicMock:
    """Manifesto fake de PfsRunManifest — shape do contrato Rust real."""
    m = MagicMock()
    m.n_pathways = n_pathways
    m.n_pathways_no_readout = n_pathways_no_readout
    m.n_cases_scored = n_cases_scored
    m.n_rows_written = n_rows_written
    m.n_seeds_total = n_seeds_total
    m.n_seeds_on_graph = n_seeds_on_graph
    m.n_seeds_off_graph = n_seeds_off_graph
    m.n_seeds_masked_by_neutral_v2 = n_seeds_masked_by_neutral_v2
    m.n_readouts_mapped = n_readouts_mapped
    m.n_readouts_with_value = n_readouts_with_value
    m.n_readouts_missing = n_readouts_missing
    m.n_readouts_phospho = n_readouts_phospho
    m.n_readouts_tf = n_readouts_tf
    m.n_tf_below_min_targets = n_tf_below_min_targets
    m.n_tumor_unpaired_skipped = n_tumor_unpaired_skipped
    m.n_zero_sd_null = n_zero_sd_null
    m.n_signed_edges = n_signed_edges
    m.n_unsigned_edges = n_unsigned_edges
    m.elapsed_ms_load = 10
    m.elapsed_ms_graph_precompute = 20
    m.elapsed_ms_readout_and_permutation = 30
    m.elapsed_ms_copy = 5
    m.elapsed_ms_total = 65
    return m


def _make_persisting_engine(
    pathway: Pathway,
    sample: OmicSample,
    pairing: OmicSamplePairing,
    manifest_kwargs: dict | None = None,
) -> MagicMock:
    """
    Fake rust_engine cujo run_pfs_scoring GRAVA de verdade um
    PathwayActivityScore via ORM (mimetiza o COPY UPSERT do motor Rust)
    quando dry_run=False, e devolve um PfsRunManifest fake consistente.
    Usado quando o teste fica mais honesto verificando o comportamento
    real do Django em cima do que a ORM garante (idempotência via NK,
    dry-run condicionado) em vez de confiar cegamente no manifesto.
    """
    manifest_kwargs = dict(manifest_kwargs or {})
    engine = MagicMock()

    def _run_pfs_scoring(**kwargs):
        dry_run = kwargs['dry_run']
        method_version = kwargs['method_version']
        n_written = 0
        if not dry_run:
            PathwayActivityScore.objects.update_or_create(
                pathway=pathway,
                sample=sample,
                method_version=method_version,
                defaults=dict(
                    score=1.5,
                    null_mean=0.0,
                    null_sd=1.0,
                    z_score=1.5,
                    p_empirical=0.1,
                    n_permutations=kwargs.get('n_permutations', DEFAULT_N_PERMUTATIONS),
                    n_seeds_on_graph=manifest_kwargs.get('n_seeds_on_graph', 0),
                    n_seeds_off_graph=manifest_kwargs.get('n_seeds_off_graph', 0),
                    n_readouts_mapped=manifest_kwargs.get('n_readouts_mapped', 2),
                    n_readouts_with_value=manifest_kwargs.get('n_readouts_with_value', 2),
                    n_readouts_missing=manifest_kwargs.get('n_readouts_missing', 0),
                    n_readouts_phospho=manifest_kwargs.get('n_readouts_phospho', 1),
                    n_readouts_tf=manifest_kwargs.get('n_readouts_tf', 1),
                    pairing=pairing,
                ),
            )
            n_written = 1
        return _make_pfs_manifest(n_rows_written=n_written, **manifest_kwargs)

    engine.run_pfs_scoring.side_effect = _run_pfs_scoring
    return engine


def _make_prior_run(
    project: DaVinciProject,
    phospho_matrix: OmicMatrix,
    proteome_matrix: OmicMatrix,
    method_version: str,
    **config_overrides,
) -> tuple[IngestionJob, dict]:
    """
    Cria o IngestionJob(PATHWAY_SCORE_RUN, COMPLETED, dry_run=False) de
    PROVENIÊNCIA que o gate de reprodutibilidade usa como referência — os
    defaults espelham exatamente o que PathwayScoringService._build_config
    produziria para um run com os parâmetros default.
    """
    config = dict(
        pathway_kegg_ids=sorted(DEFAULT_PATHWAY_KEGG_IDS),
        phospho_matrix_id=phospho_matrix.id,
        proteome_matrix_id=proteome_matrix.id,
        seed_method_versions=sorted(DEFAULT_SEED_METHOD_VERSIONS),
        mapping_version=MAPPING_VERSION,
        restart=DEFAULT_RESTART,
        unsigned_weight=DEFAULT_UNSIGNED_WEIGHT,
        n_permutations=DEFAULT_N_PERMUTATIONS,
        rng_seed=DEFAULT_RNG_SEED,
        min_regulon_targets=DEFAULT_MIN_REGULON_TARGETS,
        min_edges=DEFAULT_MIN_EDGES,
        min_signed_fraction=DEFAULT_MIN_SIGNED_FRACTION,
    )
    config.update(config_overrides)
    job = IngestionJob.objects.create(
        project=project,
        job_type=IngestionJob.JobType.PATHWAY_SCORE_RUN,
        status=IngestionJob.JobStatus.COMPLETED,
        parameters={
            **config,
            'method_version': method_version,
            'force': False,
            'dry_run': False,
        },
        completed_at=timezone.now(),
    )
    return job, config


# =============================================================================
# Base — cenário PASSANDO em todas as pré-checagens
# =============================================================================

class PfsStorageTestCase(TestCase):
    """Substitui default_storage por FileSystemStorage local em tmpdir."""

    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.mkdtemp(prefix='davinci_pfs_test_')
        self._storage = FileSystemStorage(location=self._tmpdir)
        self._storage_patcher = patch(
            'apps.core.services.pathway_scoring_service.default_storage',
            new=self._storage,
        )
        self._storage_patcher.start()

    def _put_parquet(self, storage_key: str) -> None:
        full_path = os.path.join(self._tmpdir, storage_key)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, 'wb') as f:
            f.write(b'PAR1' + b'\x00' * 32 + b'PAR1')

    def tearDown(self):
        self._storage_patcher.stop()
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        super().tearDown()


class PfsScoringBaseTestCase(PfsStorageTestCase):
    """
    Monta um cenário que PASSA em TODAS as pré-checagens do motor PFS v1:
      - Pathway/PathwayNode(gene) para as 3 vias default.
      - OmicMatrix fosfo + proteoma vinculadas ao projeto (ProjectDataset
        ativo) com Parquet dummy no storage.
      - OmicMatrixFeature catalogado nas duas matrizes.
      - PathwayReadoutFeature(mapping_version='fase3-readout-v1') para pelo
        menos um nó de cada regra.
      - OmicSamplePairing(validated_overlap=True) NAS DUAS matrizes para o
        mesmo case_id.
    """

    def setUp(self):
        super().setUp()
        self.user = _make_user('pfs_user')
        self.project = _make_project(self.user, 'PFS Project')

        self.pathway_pik3k = _make_pathway('hsa04151', 'PI3K-Akt')
        self.pathway_mapk = _make_pathway('hsa04010', 'MAPK')
        self.pathway_p53 = _make_pathway('hsa04115', 'p53')
        self.node_pik3ca = _make_node(self.pathway_pik3k, 'PIK3CA', 'e1')
        self.node_mapk3 = _make_node(self.pathway_mapk, 'MAPK3', 'e2')
        self.node_tp53 = _make_node(self.pathway_p53, 'TP53', 'e3')

        self.phospho_matrix = _make_phospho_matrix()
        self.proteome_matrix = _make_proteome_matrix()
        _link_dataset(self.project, self.phospho_matrix.dataset)
        _link_dataset(self.project, self.proteome_matrix.dataset)
        self._put_parquet(self.phospho_matrix.storage_key)
        self._put_parquet(self.proteome_matrix.storage_key)

        _make_matrix_features(self.phospho_matrix, ['PIK3CA', 'MAPK3'])
        _make_matrix_features(self.proteome_matrix, ['PIK3CA', 'MAPK3', 'TP53'])

        PathwayReadoutFeature.objects.create(
            node=self.node_pik3ca,
            matrix=self.phospho_matrix,
            feature_key='PIK3CA',
            rule=PathwayReadoutFeature.Rule.PHOSPHO,
            mapping_version=MAPPING_VERSION,
            confidence=1.0,
        )
        PathwayReadoutFeature.objects.create(
            node=self.node_tp53,
            matrix=self.proteome_matrix,
            feature_key='TP53',
            rule=PathwayReadoutFeature.Rule.TF_TARGET,
            mapping_version=MAPPING_VERSION,
            confidence=1.0,
            regulon_source='collectri',
            regulon_sign=1,
        )

        self.tumor_sample, self.normal_sample = _make_pair_samples('C3L-00001')
        self.pairing_phospho = _make_pairing(
            self.phospho_matrix, 'C3L-00001', self.tumor_sample, self.normal_sample,
        )
        self.pairing_proteome = _make_pairing(
            self.proteome_matrix, 'C3L-00001', self.tumor_sample, self.normal_sample,
        )

    def _run(self, **kwargs) -> dict:
        """Executa PathwayScoringService.run() com rust_engine mockado (MagicMock plano)."""
        fake_engine = MagicMock()
        service = PathwayScoringService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            return service.run(**kwargs)


# =============================================================================
# 1. Pré-checagens — uma por vez
# =============================================================================

class PfsPreCheckFailuresTests(PfsScoringBaseTestCase):
    """
    Cada pré-checagem falha ISOLADAMENTE (o resto do cenário permanece
    válido) e a mensagem cita o management command a rodar.
    """

    def test_grafo_ausente_levanta_erro_com_command(self):
        """Via sem Pathway carregado → PfsGraphNotLoadedError cita load_pathway_topology."""
        with self.assertRaises(PfsGraphNotLoadedError) as ctx:
            self._run(pathway_kegg_ids=['hsa04151', 'hsa99999'])
        self.assertIn('load_pathway_topology', str(ctx.exception))
        self.assertIn('hsa99999', str(ctx.exception))
        self.assertEqual(ctx.exception.missing_kegg_ids, {'hsa99999'})

    def test_readouts_nao_mapeados_levanta_erro_com_command(self):
        """mapping_version sem PathwayReadoutFeature → PfsReadoutsNotMappedError cita map_readouts."""
        with self.assertRaises(PfsReadoutsNotMappedError) as ctx:
            self._run(mapping_version='mapping-inexistente')
        self.assertIn('map_readouts', str(ctx.exception))
        self.assertIn('mapping-inexistente', str(ctx.exception))

    def test_features_nao_catalogadas_fosfo_levanta_erro_isolado(self):
        """OmicMatrixFeature vazio SÓ na matriz de fosfo → PfsFeaturesNotCataloguedError(fosfoproteoma)."""
        OmicMatrixFeature.objects.filter(matrix=self.phospho_matrix).delete()

        with self.assertRaises(PfsFeaturesNotCataloguedError) as ctx:
            self._run()
        self.assertEqual(ctx.exception.label, 'fosfoproteoma')
        self.assertIn('backfill_matrix_features', str(ctx.exception))
        self.assertIn(str(self.phospho_matrix.id), str(ctx.exception))

    def test_features_nao_catalogadas_proteoma_levanta_erro_isolado(self):
        """OmicMatrixFeature vazio SÓ na matriz de proteoma → PfsFeaturesNotCataloguedError(proteoma)."""
        OmicMatrixFeature.objects.filter(matrix=self.proteome_matrix).delete()

        with self.assertRaises(PfsFeaturesNotCataloguedError) as ctx:
            self._run()
        self.assertEqual(ctx.exception.label, 'proteoma')

    def test_pares_ausentes_em_ambas_matrizes_levanta_erro(self):
        """Sem OmicSamplePairing validado em NENHUMA matriz → PfsPairingMissingError."""
        OmicSamplePairing.objects.all().delete()

        with self.assertRaises(PfsPairingMissingError):
            self._run()

    def test_pares_so_no_proteoma_nao_vale_para_fosfo(self):
        """
        CASO DECISIVO: par validado SÓ na matriz de proteoma. A natural key
        de OmicSamplePairing é (matrix, case_id) — pareamento é POR MATRIZ.
        Pareear só o proteoma zeraria a Regra 1 silenciosamente se a
        pré-checagem não pegasse isso — ela deve pegar.
        """
        self.pairing_phospho.delete()

        with self.assertRaises(PfsPairingMissingError) as ctx:
            self._run()
        self.assertEqual(ctx.exception.label, 'fosfoproteoma')
        self.assertIn('POR MATRIZ', str(ctx.exception))
        # Ainda existe par validado no proteoma — prova que o teste isola
        # exatamente a matriz de fosfo, não zera tudo por acidente.
        self.assertTrue(
            OmicSamplePairing.objects.filter(
                matrix=self.proteome_matrix, validated_overlap=True,
            ).exists()
        )

    def test_pares_so_no_fosfo_nao_vale_para_proteoma(self):
        """Espelho do caso decisivo: par validado SÓ no fosfo → falha para 'proteoma'."""
        self.pairing_proteome.delete()

        with self.assertRaises(PfsPairingMissingError) as ctx:
            self._run()
        self.assertEqual(ctx.exception.label, 'proteoma')

    def test_par_nao_validado_nao_satisfaz_pre_checagem(self):
        """OmicSamplePairing com validated_overlap=False não conta como par válido."""
        self.pairing_phospho.validated_overlap = False
        self.pairing_phospho.save(update_fields=['validated_overlap'])

        with self.assertRaises(PfsPairingMissingError) as ctx:
            self._run()
        self.assertEqual(ctx.exception.label, 'fosfoproteoma')

    def test_cenario_base_passa_todas_as_pre_checagens(self):
        """Confirma que o cenário base (sem quebrar nada) chega ao Rust — sanity check."""
        fake_engine = _make_persisting_engine(
            self.pathway_pik3k, self.tumor_sample, self.pairing_phospho,
        )
        service = PathwayScoringService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            report = service.run(method_version='fase4-pfs-v1-sanity')
        self.assertEqual(report['method_version'], 'fase4-pfs-v1-sanity')
        fake_engine.run_pfs_scoring.assert_called_once()


# =============================================================================
# 2. Zero seeds NÃO é erro
# =============================================================================

class PfsZeroSeedsTests(PfsScoringBaseTestCase):
    """
    Deliberadamente NÃO há gate de existência de VariantEffectSeed. Com
    zero sementes, o run COMPLETA, grava PathwayActivityScore e sinaliza
    n_seeds_on_graph=0 — é o estado real da bancada em 2026-07-30.
    """

    def test_zero_seeds_completa_sem_erro_e_grava_escore(self):
        fake_engine = _make_persisting_engine(
            self.pathway_pik3k, self.tumor_sample, self.pairing_phospho,
            manifest_kwargs=dict(n_seeds_total=0, n_seeds_on_graph=0, n_seeds_off_graph=0),
        )
        service = PathwayScoringService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            report = service.run(method_version='fase4-pfs-v1-zeroseed')

        self.assertEqual(report['n_seeds_on_graph'], 0)
        self.assertTrue(report['zero_seeds_on_graph'])
        self.assertIsNone(report['seed_coverage_ratio'])

        job = IngestionJob.objects.get(id=report['job_id'])
        self.assertEqual(job.status, IngestionJob.JobStatus.COMPLETED)
        self.assertTrue(
            PathwayActivityScore.objects.filter(
                pathway=self.pathway_pik3k,
                sample=self.tumor_sample,
                method_version='fase4-pfs-v1-zeroseed',
            ).exists(),
            'Zero seeds deve produzir escore gravado (degenerado), não crash',
        )


# =============================================================================
# 3. Gate de reprodutibilidade (D3)
# =============================================================================

class PfsReproducibilityGateTests(PfsScoringBaseTestCase):

    def setUp(self):
        super().setUp()
        self.method_version = 'fase4-pfs-v1-gate'

    def _seed_prior_score_and_job(self, **config_overrides):
        job, config = _make_prior_run(
            self.project, self.phospho_matrix, self.proteome_matrix,
            self.method_version, **config_overrides,
        )
        PathwayActivityScore.objects.create(
            pathway=self.pathway_pik3k,
            sample=self.tumor_sample,
            method_version=self.method_version,
            score=0.1, null_mean=0.0, null_sd=1.0, z_score=0.1, p_empirical=0.5,
        )
        return job, config

    def test_parametros_divergentes_levanta_gate_error(self):
        self._seed_prior_score_and_job(restart=0.3)
        fake_engine = MagicMock()

        service = PathwayScoringService(self.project)
        with self.assertRaises(PfsReproducibilityGateError) as ctx:
            with patch.dict('sys.modules', {'rust_engine': fake_engine}):
                service.run(method_version=self.method_version, restart=0.9)

        self.assertEqual(ctx.exception.reason, 'mismatch')
        fake_engine.run_pfs_scoring.assert_not_called()

    def test_mesmos_parametros_passa_sem_gate(self):
        self._seed_prior_score_and_job()  # defaults == defaults do run() abaixo
        fake_engine = _make_persisting_engine(
            self.pathway_pik3k, self.tumor_sample, self.pairing_phospho,
        )

        service = PathwayScoringService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            report = service.run(method_version=self.method_version)

        self.assertEqual(report['method_version'], self.method_version)
        fake_engine.run_pfs_scoring.assert_called_once()

    def test_force_ignora_gate_mesmo_divergindo(self):
        self._seed_prior_score_and_job(restart=0.3)
        fake_engine = _make_persisting_engine(
            self.pathway_pik3k, self.tumor_sample, self.pairing_phospho,
        )

        service = PathwayScoringService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            report = service.run(
                method_version=self.method_version, restart=0.9, force=True,
            )

        self.assertEqual(report['method_version'], self.method_version)
        fake_engine.run_pfs_scoring.assert_called_once()

    def test_method_version_novo_nunca_dispara_gate(self):
        """Divergindo tudo, mas sob um --method-version NOVO: nenhum gate a checar."""
        self._seed_prior_score_and_job(restart=0.3)
        fake_engine = _make_persisting_engine(
            self.pathway_pik3k, self.tumor_sample, self.pairing_phospho,
        )

        service = PathwayScoringService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            report = service.run(method_version='fase4-pfs-v1-novo', restart=0.9)

        self.assertEqual(report['method_version'], 'fase4-pfs-v1-novo')

    def test_scores_sem_job_de_provenance_levanta_no_provenance(self):
        """
        PathwayActivityScore existe mas SEM IngestionJob(COMPLETED,
        dry_run=False) correspondente → reason='no_provenance' (postura
        conservadora: "não consigo provar que é seguro" é tratado como
        "não é seguro").
        """
        PathwayActivityScore.objects.create(
            pathway=self.pathway_pik3k,
            sample=self.tumor_sample,
            method_version=self.method_version,
            score=0.1, null_mean=0.0, null_sd=1.0, z_score=0.1, p_empirical=0.5,
        )

        service = PathwayScoringService(self.project)
        with self.assertRaises(PfsReproducibilityGateError) as ctx:
            with patch.dict('sys.modules', {'rust_engine': MagicMock()}):
                service.run(method_version=self.method_version)

        self.assertEqual(ctx.exception.reason, 'no_provenance')

    def test_no_provenance_com_force_passa(self):
        PathwayActivityScore.objects.create(
            pathway=self.pathway_pik3k,
            sample=self.tumor_sample,
            method_version=self.method_version,
            score=0.1, null_mean=0.0, null_sd=1.0, z_score=0.1, p_empirical=0.5,
        )
        fake_engine = _make_persisting_engine(
            self.pathway_pik3k, self.tumor_sample, self.pairing_phospho,
        )

        service = PathwayScoringService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            report = service.run(method_version=self.method_version, force=True)

        self.assertEqual(report['method_version'], self.method_version)

    def test_dry_run_nunca_dispara_o_gate(self):
        """
        dry_run=True pula o gate inteiro (`if not dry_run:` em run()) — nem
        mismatch nem no_provenance bloqueiam um dry-run, mesmo com
        parâmetros divergentes.
        """
        self._seed_prior_score_and_job(restart=0.3)
        fake_engine = _make_persisting_engine(
            self.pathway_pik3k, self.tumor_sample, self.pairing_phospho,
        )

        service = PathwayScoringService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            report = service.run(
                method_version=self.method_version, restart=0.9, dry_run=True,
            )

        self.assertTrue(report['dry_run'])


# =============================================================================
# 4. Isolamento (Regra #3)
# =============================================================================

class PfsIsolationTests(PfsStorageTestCase):

    def test_matriz_de_outro_projeto_nao_resolve(self):
        """
        Matriz vinculada (ProjectDataset) a um projeto NÃO resolve para
        outro projeto — PfsMatrixNotFoundError.
        """
        user_a = _make_user('pfs_iso_a')
        user_b = _make_user('pfs_iso_b')
        project_a = _make_project(user_a, 'PFS Iso A')
        project_b = _make_project(user_b, 'PFS Iso B')

        phospho_matrix = _make_phospho_matrix('CPTAC-ISO-PHOSPHO')
        _link_dataset(project_a, phospho_matrix.dataset)

        # project_a resolve normalmente
        resolved = PathwayScoringService.resolve_phospho_matrix(project_a)
        self.assertEqual(resolved.id, phospho_matrix.id)

        # project_b (sem vínculo) NÃO resolve
        with self.assertRaises(PfsMatrixNotFoundError):
            PathwayScoringService.resolve_phospho_matrix(project_b)

    def test_pathway_activity_score_e_catalogo_global_sem_fk_projeto(self):
        """
        PathwayActivityScore NÃO tem FK de projeto — é catálogo global
        (espelha VariantEffectSeed/OmicSamplePairing/Pathway). Isolamento
        de leitura passa por ProjectDataset via sample__dataset.
        """
        field_names = {f.name for f in PathwayActivityScore._meta.get_fields()}
        self.assertNotIn('project', field_names)
        self.assertIn('pathway', field_names)
        self.assertIn('sample', field_names)


# =============================================================================
# 5. Idempotência
# =============================================================================

class PfsIdempotencyTests(PfsScoringBaseTestCase):

    def test_rodar_duas_vezes_mesmos_parametros_nao_duplica_e_nao_falha(self):
        fake_engine = _make_persisting_engine(
            self.pathway_pik3k, self.tumor_sample, self.pairing_phospho,
        )
        method_version = 'fase4-pfs-v1-idem'

        service1 = PathwayScoringService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            service1.run(method_version=method_version)

        service2 = PathwayScoringService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            report2 = service2.run(method_version=method_version)  # não deve levantar

        self.assertEqual(report2['method_version'], method_version)
        self.assertEqual(
            PathwayActivityScore.objects.filter(
                pathway=self.pathway_pik3k,
                sample=self.tumor_sample,
                method_version=method_version,
            ).count(),
            1,
            'NK (pathway, sample, method_version) não deve duplicar',
        )
        self.assertEqual(
            IngestionJob.objects.filter(
                project=self.project,
                job_type=IngestionJob.JobType.PATHWAY_SCORE_RUN,
                status=IngestionJob.JobStatus.COMPLETED,
                parameters__method_version=method_version,
            ).count(),
            2,
            'Cada run gera seu próprio IngestionJob (histórico auditável)',
        )


# =============================================================================
# 6. --dry-run
# =============================================================================

class PfsDryRunTests(PfsScoringBaseTestCase):

    def test_dry_run_nao_persiste_pathway_activity_score(self):
        fake_engine = _make_persisting_engine(
            self.pathway_pik3k, self.tumor_sample, self.pairing_phospho,
        )
        service = PathwayScoringService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            report = service.run(method_version='fase4-pfs-v1-dryrun', dry_run=True)

        self.assertTrue(report['dry_run'])
        self.assertFalse(
            PathwayActivityScore.objects.filter(
                method_version='fase4-pfs-v1-dryrun',
            ).exists(),
            'dry_run=True NÃO deve persistir PathwayActivityScore',
        )

    def test_dry_run_ainda_assim_marca_job_completed(self):
        """
        Comportamento REAL confirmado em pathway_scoring_service._execute:
        o job é marcado COMPLETED INCONDICIONALMENTE após a chamada ao
        Rust, independente de dry_run (não há ramo especial). Documentado
        aqui em vez de assumido — records_inserted reflete
        manifest.n_rows_written, que é 0 quando o motor (real) não grava.
        """
        fake_engine = _make_persisting_engine(
            self.pathway_pik3k, self.tumor_sample, self.pairing_phospho,
        )
        service = PathwayScoringService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            report = service.run(method_version='fase4-pfs-v1-dryrun2', dry_run=True)

        job = IngestionJob.objects.get(id=report['job_id'])
        self.assertEqual(job.status, IngestionJob.JobStatus.COMPLETED)
        self.assertEqual(job.records_inserted, 0)


# =============================================================================
# 7. Task Celery run_pathway_scoring
# =============================================================================

class PfsScoringTaskTests(PfsScoringBaseTestCase):

    def test_task_projeto_inexistente_retorna_erro(self):
        from apps.core.tasks.ingestion_tasks import run_pathway_scoring

        with patch.dict('sys.modules', {'rust_engine': MagicMock()}):
            result = run_pathway_scoring.run(str(uuid.uuid4()), str(uuid.uuid4()))

        self.assertIn('project not found', result.get('errors', []))
        self.assertEqual(result.get('n_rows_written'), 0)

    def test_task_job_inexistente_retorna_erro(self):
        from apps.core.tasks.ingestion_tasks import run_pathway_scoring

        with patch.dict('sys.modules', {'rust_engine': MagicMock()}):
            result = run_pathway_scoring.run(str(uuid.uuid4()), str(self.project.id))

        self.assertIn('job not found', result.get('errors', []))

    def test_task_isolamento_cross_project(self):
        """
        job.project_id != project_id recebido → job FAILED,
        PathwayScoringService NÃO instanciado (mesmo padrão de
        run_cnv_seed_load).
        """
        user_b = _make_user('pfs_task_iso_b')
        project_b = _make_project(user_b, 'PFS Task Iso B')

        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.PATHWAY_SCORE_RUN,
            status=IngestionJob.JobStatus.PENDING,
            parameters={'method_version': 'fase4-pfs-v1'},
        )

        from apps.core.tasks.ingestion_tasks import run_pathway_scoring

        with patch.dict('sys.modules', {'rust_engine': MagicMock()}), \
             patch('apps.core.services.pathway_scoring_service.PathwayScoringService') as MockSvc:
            run_pathway_scoring.run(str(job.id), str(project_b.id))

        job.refresh_from_db()
        self.assertEqual(job.status, IngestionJob.JobStatus.FAILED)
        self.assertIn('isolamento', (job.error_message or '').lower())
        MockSvc.assert_not_called()

    def test_task_delega_ao_service_com_job_precriado(self):
        """Fluxo feliz: task resolve project/job e delega a PathwayScoringService.run(job=job)."""
        job = IngestionJob.objects.create(
            project=self.project,
            job_type=IngestionJob.JobType.PATHWAY_SCORE_RUN,
            status=IngestionJob.JobStatus.PENDING,
            parameters={
                'pathway_kegg_ids': sorted(DEFAULT_PATHWAY_KEGG_IDS),
                'method_version': 'fase4-pfs-v1-task',
                'seed_method_versions': sorted(DEFAULT_SEED_METHOD_VERSIONS),
                'mapping_version': MAPPING_VERSION,
                'restart': DEFAULT_RESTART,
                'unsigned_weight': DEFAULT_UNSIGNED_WEIGHT,
                'n_permutations': DEFAULT_N_PERMUTATIONS,
                'rng_seed': DEFAULT_RNG_SEED,
                'min_regulon_targets': DEFAULT_MIN_REGULON_TARGETS,
                'phospho_matrix_id': self.phospho_matrix.id,
                'proteome_matrix_id': self.proteome_matrix.id,
                'force': False,
                'dry_run': False,
            },
        )
        fake_engine = _make_persisting_engine(
            self.pathway_pik3k, self.tumor_sample, self.pairing_phospho,
        )

        from apps.core.tasks.ingestion_tasks import run_pathway_scoring
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            result = run_pathway_scoring.run(str(job.id), str(self.project.id))

        self.assertEqual(result['errors'], [])
        job.refresh_from_db()
        self.assertEqual(job.status, IngestionJob.JobStatus.COMPLETED)


# =============================================================================
# 7b. dispatch() — criação de job + Celery
# =============================================================================

class PfsDispatchTests(PfsScoringBaseTestCase):

    @patch('apps.core.tasks.ingestion_tasks.run_pathway_scoring.delay', return_value=None)
    def test_dispatch_cria_job_pending_e_chama_celery(self, mock_delay):
        job = PathwayScoringService.dispatch(
            self.project, method_version='fase4-pfs-v1-dispatch',
        )

        self.assertEqual(job.status, IngestionJob.JobStatus.PENDING)
        self.assertEqual(job.job_type, IngestionJob.JobType.PATHWAY_SCORE_RUN)
        mock_delay.assert_called_once_with(str(job.id), str(self.project.id))

    @patch('apps.core.tasks.ingestion_tasks.run_pathway_scoring.delay', return_value=None)
    def test_dispatch_sem_db_url_nos_parametros(self, mock_delay):
        job = PathwayScoringService.dispatch(
            self.project, method_version='fase4-pfs-v1-dispatch2',
        )
        params_str = str(job.parameters or {})
        self.assertNotIn('postgresql://', params_str)
        self.assertNotIn('db_url', params_str)
        self.assertNotIn('password', params_str)

    @patch('apps.core.tasks.ingestion_tasks.run_pathway_scoring.delay', return_value=None)
    def test_dispatch_job_ativo_bloqueia(self, mock_delay):
        PathwayScoringService.dispatch(
            self.project, method_version='fase4-pfs-v1-dispatch3',
        )
        with self.assertRaises(PfsJobActiveError):
            PathwayScoringService.dispatch(
                self.project, method_version='fase4-pfs-v1-dispatch3',
            )

        self.assertEqual(
            IngestionJob.objects.filter(
                project=self.project,
                job_type=IngestionJob.JobType.PATHWAY_SCORE_RUN,
                parameters__method_version='fase4-pfs-v1-dispatch3',
            ).count(),
            1,
        )


# =============================================================================
# 8. Contrato dos contadores no relatório
# =============================================================================

class PfsReadoutCounterContractTests(PfsScoringBaseTestCase):
    """
    Trava a identidade n_readouts_mapped == n_readouts_with_value +
    n_readouts_missing no relatório, e a fórmula CORRETA de
    readout_coverage_ratio = with_value / mapped. Regressão real: o
    manifesto já misturou total-por-via com soma-por-linha e produziu razão
    de 6664%; hoje corrigido, dá 79,3%.
    """

    def test_identidade_mapped_igual_with_value_mais_missing(self):
        fake_engine = _make_persisting_engine(
            self.pathway_pik3k, self.tumor_sample, self.pairing_phospho,
            manifest_kwargs=dict(
                n_readouts_mapped=1000, n_readouts_with_value=793, n_readouts_missing=207,
            ),
        )
        service = PathwayScoringService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            report = service.run(method_version='fase4-pfs-v1-counters')

        self.assertEqual(
            report['n_readouts_mapped'],
            report['n_readouts_with_value'] + report['n_readouts_missing'],
        )

    def test_readout_coverage_ratio_e_with_value_sobre_mapped(self):
        fake_engine = _make_persisting_engine(
            self.pathway_pik3k, self.tumor_sample, self.pairing_phospho,
            manifest_kwargs=dict(
                n_readouts_mapped=1000, n_readouts_with_value=793, n_readouts_missing=207,
            ),
        )
        service = PathwayScoringService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            report = service.run(method_version='fase4-pfs-v1-ratio')

        self.assertAlmostEqual(report['readout_coverage_ratio'], 0.793, places=3)
        # Regressão: a fórmula errada (mapped/with_value ou total-por-via)
        # produziria algo muito > 1 (a medição live deu 66.64).
        self.assertLessEqual(report['readout_coverage_ratio'], 1.0)

    def test_readout_coverage_ratio_none_quando_mapped_zero(self):
        fake_engine = _make_persisting_engine(
            self.pathway_pik3k, self.tumor_sample, self.pairing_phospho,
            manifest_kwargs=dict(
                n_readouts_mapped=0, n_readouts_with_value=0, n_readouts_missing=0,
            ),
        )
        service = PathwayScoringService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            report = service.run(method_version='fase4-pfs-v1-ratio-zero')

        self.assertIsNone(report['readout_coverage_ratio'])


# =============================================================================
# 8b. Universo de vias — default deixou de ser 3 fixas (quarto/último lugar)
# =============================================================================

class PfsPathwayUniverseDefaultTests(PfsScoringBaseTestCase):
    """
    Sem `pathway_kegg_ids`, `run()`/`dispatch()` usam TODAS as `Pathway` em
    PG no momento da execução — não o preset legado de 3 vias fixas
    (LEGACY_V1_PATHWAY_KEGG_IDS/DEFAULT_PATHWAY_KEGG_IDS). Mesmo padrão de
    prova já usado em RegulonLoadService/ReadoutMappingService
    (test_omnispathway_fase3.py): cria uma via FORA do preset legado e
    confirma que ela É passada ao Rust — o preset legado antigo jamais a
    incluiria. Espelha o bug real que este ajuste fecha: sem isso, o motor
    PFS pontuaria só 3 das 372 vias, silenciosamente.
    """

    def test_default_usa_todas_as_pathway_em_pg_nao_o_preset_legado(self):
        _make_pathway('hsa04150', 'mTOR signaling pathway')  # fora do preset legado

        fake_engine = MagicMock()
        fake_engine.run_pfs_scoring.return_value = _make_pfs_manifest()
        service = PathwayScoringService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            service.run(method_version='fase4-pfs-v1-universe-default')

        call_kwargs = fake_engine.run_pfs_scoring.call_args.kwargs
        self.assertEqual(
            sorted(call_kwargs['pathway_kegg_ids']),
            sorted(DEFAULT_PATHWAY_KEGG_IDS + ['hsa04150']),
        )

    def test_dispatch_default_usa_todas_as_pathway_em_pg(self):
        _make_pathway('hsa04150', 'mTOR signaling pathway')  # fora do preset legado

        with patch(
            'apps.core.tasks.ingestion_tasks.run_pathway_scoring.delay',
            return_value=None,
        ):
            job = PathwayScoringService.dispatch(
                self.project, method_version='fase4-pfs-v1-universe-dispatch',
            )

        self.assertEqual(
            sorted(job.parameters['pathway_kegg_ids']),
            sorted(DEFAULT_PATHWAY_KEGG_IDS + ['hsa04150']),
        )

    def test_lista_explicita_vazia_e_respeitada_nao_cai_no_default(self):
        """
        `pathway_kegg_ids=[]` explícito NÃO cai para o universo default
        (todas as `Pathway`) nem para o preset legado — é respeitado tal
        como passado. Prova indireta: com vias=[], a pré-checagem de
        readouts mapeados falha citando exatamente `[]` na mensagem — se
        `[]` tivesse sido silenciosamente substituído pelo default, a
        pré-checagem passaria (as 3 vias de setUp têm readout mapeado) e o
        motor seria chamado.
        """
        fake_engine = MagicMock()
        service = PathwayScoringService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            with self.assertRaises(PfsReadoutsNotMappedError) as ctx:
                service.run(pathway_kegg_ids=[], method_version='fase4-pfs-v1-empty')

        self.assertIn('nas vias []', str(ctx.exception))
        fake_engine.run_pfs_scoring.assert_not_called()


# =============================================================================
# 8c. Filtro de qualidade de topologia (D-2)
# =============================================================================

class PfsTopologyQualityFilterPureTests(PfsScoringBaseTestCase):
    """
    `_topology_quality_reason` — classificação pura de UMA via já agregada
    (n_edges, n_signed_edges), sem tocar o banco.
    """

    def test_zero_arestas_reprovada_como_no_edges_nao_low_signed_fraction(self):
        """
        Via com 0 arestas tem fração 0/0 INDEFINIDA. Reprovada
        explicitamente como `no_edges` — NUNCA `low_signed_fraction` (que
        seria o resultado se 0/0 virasse silenciosamente 0.0 e fosse
        comparada contra o limiar, mascarando a causa real da reprovação —
        exatamente a confusão que mascarou o achado na primeira medição).
        """
        self.assertEqual(
            _topology_quality_reason(0, 0, min_edges=0, min_signed_fraction=0.5),
            TOPOLOGY_EXCLUSION_NO_EDGES,
        )
        self.assertEqual(
            _topology_quality_reason(
                0, 0,
                min_edges=RECOMMENDED_MIN_EDGES,
                min_signed_fraction=RECOMMENDED_MIN_SIGNED_FRACTION,
            ),
            TOPOLOGY_EXCLUSION_NO_EDGES,
        )

    def test_poucas_arestas_reprovada_como_too_few_edges(self):
        """Via com arestas < min_edges reprovada — mesmo com 100% assinada."""
        self.assertEqual(
            _topology_quality_reason(5, 5, min_edges=10, min_signed_fraction=0.5),
            TOPOLOGY_EXCLUSION_TOO_FEW_EDGES,
        )

    def test_fracao_abaixo_do_limiar_reprovada(self):
        """Via com arestas suficientes mas fração assinada abaixo do limiar."""
        self.assertEqual(
            _topology_quality_reason(10, 3, min_edges=10, min_signed_fraction=0.5),
            TOPOLOGY_EXCLUSION_LOW_SIGNED_FRACTION,
        )

    def test_via_aprovada_retorna_none(self):
        self.assertIsNone(
            _topology_quality_reason(20, 15, min_edges=10, min_signed_fraction=0.5)
        )

    def test_fracao_exatamente_no_limiar_aprovada(self):
        """Fração == limiar passa (comparação é `<`, não `<=`)."""
        self.assertIsNone(
            _topology_quality_reason(10, 5, min_edges=10, min_signed_fraction=0.5)
        )

    def test_filtro_totalmente_desligado_aprova_zero_arestas(self):
        """
        `min_edges=0` E `min_signed_fraction=0` juntos: via de zero arestas
        passa — necessário para `--min-edges 0 --min-signed-fraction 0`
        reproduzir o universo completo de verdade (inclusive vias sem
        nenhuma aresta).
        """
        self.assertIsNone(
            _topology_quality_reason(0, 0, min_edges=0, min_signed_fraction=0.0)
        )


class PfsTopologyQualityFilterAggregationTests(PfsScoringBaseTestCase):
    """
    `_resolve_default_pathway_ids` — agregação `Count`/`Count(filter=Q(...))`
    via ORM sobre o cenário base (3 vias, TODAS com zero `PathwayEdge` no
    `setUp` herdado de `PfsScoringBaseTestCase`).
    """

    def test_filtro_desligado_reproduz_universo_completo(self):
        """
        `min_edges=0, min_signed_fraction=0`: TODAS as `Pathway` (inclusive
        as de zero arestas do setUp) são mantidas — comportamento anterior
        a este filtro preservado byte a byte.
        """
        kept, report = _resolve_default_pathway_ids(
            min_edges=0, min_signed_fraction=0.0,
        )

        self.assertEqual(
            sorted(kept), sorted(['hsa04151', 'hsa04010', 'hsa04115']),
        )
        self.assertEqual(report['n_pathways_considered'], 3)
        self.assertEqual(report['n_pathways_kept'], 3)
        self.assertEqual(report['n_pathways_excluded'], 0)
        self.assertEqual(
            report['excluded_by_reason'],
            {'no_edges': 0, 'too_few_edges': 0, 'low_signed_fraction': 0},
        )

    def test_relatorio_de_exclusao_quebra_por_motivo(self):
        """
        4 perfis de grafo distintos → relatório de exclusão bate
        EXATAMENTE com a quebra por motivo esperada (uma via por motivo +
        uma aprovada).
        """
        # hsa04151 (pathway_pik3k, setUp): 0 arestas → no_edges.
        # hsa04010 (pathway_mapk, setUp): 3 arestas, 100% assinadas, mas
        #   3 < min_edges=10 → too_few_edges (prioridade sobre fração).
        _make_pathway_topology(self.pathway_mapk, n_edges=3, n_signed=3)
        # hsa04115 (pathway_p53, setUp): 10 arestas, 3 assinadas (30%) →
        #   low_signed_fraction (10 >= min_edges, mas 0.3 < 0.5).
        _make_pathway_topology(self.pathway_p53, n_edges=10, n_signed=3)
        # via extra aprovada: 20 arestas, 15 assinadas (75%).
        approved = _make_pathway('hsa04150', 'mTOR signaling pathway')
        _make_pathway_topology(approved, n_edges=20, n_signed=15)

        kept, report = _resolve_default_pathway_ids(
            min_edges=RECOMMENDED_MIN_EDGES,
            min_signed_fraction=RECOMMENDED_MIN_SIGNED_FRACTION,
        )

        self.assertEqual(kept, ['hsa04150'])
        self.assertEqual(report['min_edges'], RECOMMENDED_MIN_EDGES)
        self.assertEqual(report['min_signed_fraction'], RECOMMENDED_MIN_SIGNED_FRACTION)
        self.assertEqual(report['n_pathways_considered'], 4)
        self.assertEqual(report['n_pathways_kept'], 1)
        self.assertEqual(report['n_pathways_excluded'], 3)
        self.assertEqual(
            report['excluded_by_reason'],
            {
                TOPOLOGY_EXCLUSION_NO_EDGES: 1,
                TOPOLOGY_EXCLUSION_TOO_FEW_EDGES: 1,
                TOPOLOGY_EXCLUSION_LOW_SIGNED_FRACTION: 1,
            },
        )
        self.assertEqual(
            report['excluded_kegg_ids_by_reason'][TOPOLOGY_EXCLUSION_NO_EDGES],
            ['hsa04151'],
        )
        self.assertEqual(
            report['excluded_kegg_ids_by_reason'][TOPOLOGY_EXCLUSION_TOO_FEW_EDGES],
            ['hsa04010'],
        )
        self.assertEqual(
            report['excluded_kegg_ids_by_reason'][TOPOLOGY_EXCLUSION_LOW_SIGNED_FRACTION],
            ['hsa04115'],
        )


class PfsTopologyQualityFilterIntegrationTests(PfsScoringBaseTestCase):
    """
    Integração via `PathwayScoringService.run()`/`dispatch()` — via
    aprovada é pontuada de verdade; universo default some do relatório
    quando reprovado; `--pathways` explícito NUNCA é filtrado; gate de
    reprodutibilidade reage a `min_edges`/`min_signed_fraction`.
    """

    def _approve_all_base_pathways(self):
        """Dá às 3 vias do setUp topologia suficiente p/ passar no filtro
        recomendado (10 arestas, 75% assinada) — usado nos testes que
        precisam do universo default completo chegando ao Rust."""
        for pathway in (self.pathway_pik3k, self.pathway_mapk, self.pathway_p53):
            _make_pathway_topology(pathway, n_edges=20, n_signed=15)

    def test_run_universo_default_filtra_e_reporta(self):
        """
        Só `pathway_pik3k` tem topologia aprovada — `run()` deve pontuar
        SÓ essa via e o relatório trazer a quebra por motivo das outras 2.
        """
        _make_pathway_topology(self.pathway_pik3k, n_edges=20, n_signed=15)
        # pathway_mapk e pathway_p53 continuam com 0 arestas (setUp).

        fake_engine = MagicMock()
        fake_engine.run_pfs_scoring.return_value = _make_pfs_manifest()
        service = PathwayScoringService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            report = service.run(
                method_version='fase4-pfs-v1-topofilter-run',
                min_edges=RECOMMENDED_MIN_EDGES,
                min_signed_fraction=RECOMMENDED_MIN_SIGNED_FRACTION,
            )

        call_kwargs = fake_engine.run_pfs_scoring.call_args.kwargs
        self.assertEqual(call_kwargs['pathway_kegg_ids'], ['hsa04151'])
        self.assertEqual(report['pathway_kegg_ids'], ['hsa04151'])

        filter_report = report['topology_filter_report']
        self.assertIsNotNone(filter_report)
        self.assertEqual(filter_report['n_pathways_kept'], 1)
        self.assertEqual(filter_report['n_pathways_excluded'], 2)
        self.assertEqual(
            filter_report['excluded_by_reason'][TOPOLOGY_EXCLUSION_NO_EDGES], 2,
        )

    def test_pathways_explicito_nunca_e_filtrado(self):
        """
        Via pedida explicitamente com grafo pobre (0 arestas, setUp) NÃO é
        descartada mesmo com limiares extremamente exigentes — o operador
        que pede uma via não pode vê-la sumir em silêncio.
        """
        fake_engine = _make_persisting_engine(
            self.pathway_pik3k, self.tumor_sample, self.pairing_phospho,
        )
        service = PathwayScoringService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            report = service.run(
                pathway_kegg_ids=['hsa04151'],
                method_version='fase4-pfs-v1-explicit-nofilter',
                min_edges=999,
                min_signed_fraction=1.0,
            )

        self.assertEqual(report['pathway_kegg_ids'], ['hsa04151'])
        self.assertIsNone(report['topology_filter_report'])
        fake_engine.run_pfs_scoring.assert_called_once()

    @patch('apps.core.tasks.ingestion_tasks.run_pathway_scoring.delay', return_value=None)
    def test_dispatch_universo_default_grava_relatorio_no_job(self, mock_delay):
        _make_pathway_topology(self.pathway_pik3k, n_edges=20, n_signed=15)

        job = PathwayScoringService.dispatch(
            self.project,
            method_version='fase4-pfs-v1-topofilter-dispatch',
            min_edges=RECOMMENDED_MIN_EDGES,
            min_signed_fraction=RECOMMENDED_MIN_SIGNED_FRACTION,
        )

        self.assertEqual(job.parameters['pathway_kegg_ids'], ['hsa04151'])
        filter_report = job.parameters['topology_filter_report']
        self.assertIsNotNone(filter_report)
        self.assertEqual(filter_report['n_pathways_kept'], 1)
        self.assertEqual(filter_report['n_pathways_excluded'], 2)

    @patch('apps.core.tasks.ingestion_tasks.run_pathway_scoring.delay', return_value=None)
    def test_dispatch_pathways_explicito_relatorio_none_no_job(self, mock_delay):
        job = PathwayScoringService.dispatch(
            self.project,
            pathway_kegg_ids=['hsa04151'],
            method_version='fase4-pfs-v1-topofilter-dispatch-explicit',
            min_edges=999,
            min_signed_fraction=1.0,
        )

        self.assertIsNone(job.parameters['topology_filter_report'])
        self.assertEqual(job.parameters['pathway_kegg_ids'], ['hsa04151'])

    def test_filtro_diferente_sob_mesmo_method_version_dispara_gate(self):
        """
        `min_edges`/`min_signed_fraction` ENTRAM em GATE_FIELDS: rodar o
        MESMO `method_version` com `min_signed_fraction` diferente é
        reprovado pelo gate de reprodutibilidade (D3), mesmo o universo de
        vias resultante sendo idêntico neste cenário — o gate protege
        contra a POSSIBILIDADE de universos diferentes sob a mesma string,
        não reavalia o resultado real do run anterior.
        """
        self._approve_all_base_pathways()
        method_version = 'fase4-pfs-v1-topofilter-gate'

        fake_engine = _make_persisting_engine(
            self.pathway_pik3k, self.tumor_sample, self.pairing_phospho,
        )
        service = PathwayScoringService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            service.run(
                method_version=method_version,
                min_edges=10,
                min_signed_fraction=0.5,
            )

        with self.assertRaises(PfsReproducibilityGateError) as ctx:
            with patch.dict('sys.modules', {'rust_engine': fake_engine}):
                service.run(
                    method_version=method_version,
                    min_edges=10,
                    min_signed_fraction=0.7,
                )

        self.assertEqual(ctx.exception.reason, 'mismatch')
        self.assertIn('min_signed_fraction', str(ctx.exception))
        fake_engine.run_pfs_scoring.assert_called_once()  # só o 1º run chamou o Rust

    def test_filtro_igual_sob_mesmo_method_version_nao_dispara_gate(self):
        """Espelho do teste acima: MESMOS min_edges/min_signed_fraction não disparam o gate."""
        self._approve_all_base_pathways()
        method_version = 'fase4-pfs-v1-topofilter-gate-same'

        fake_engine = _make_persisting_engine(
            self.pathway_pik3k, self.tumor_sample, self.pairing_phospho,
        )
        service = PathwayScoringService(self.project)
        with patch.dict('sys.modules', {'rust_engine': fake_engine}):
            service.run(method_version=method_version, min_edges=10, min_signed_fraction=0.5)
            report2 = service.run(
                method_version=method_version, min_edges=10, min_signed_fraction=0.5,
            )

        self.assertEqual(report2['method_version'], method_version)


# =============================================================================
# 9. Modelo PathwayActivityScore
# =============================================================================

class PathwayActivityScoreModelTests(PfsScoringBaseTestCase):

    def test_nk_rejeita_duplicata(self):
        PathwayActivityScore.objects.create(
            pathway=self.pathway_pik3k,
            sample=self.tumor_sample,
            method_version='fase4-pfs-v1-nk',
            score=0.1, null_mean=0.0, null_sd=1.0, z_score=0.1, p_empirical=0.5,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PathwayActivityScore.objects.create(
                    pathway=self.pathway_pik3k,
                    sample=self.tumor_sample,
                    method_version='fase4-pfs-v1-nk',
                    score=0.9, null_mean=0.0, null_sd=1.0, z_score=0.9, p_empirical=0.5,
                )

    def test_pairing_aceita_null(self):
        score = PathwayActivityScore.objects.create(
            pathway=self.pathway_mapk,
            sample=self.tumor_sample,
            method_version='fase4-pfs-v1-nullpairing',
            score=0.1, null_mean=0.0, null_sd=1.0, z_score=0.1, p_empirical=0.5,
            pairing=None,
        )
        score.refresh_from_db()
        self.assertIsNone(score.pairing)

    def test_method_version_diferente_coexiste_mesmo_pathway_sample(self):
        """Mesmo (pathway, sample) sob DUAS method_version distintas coexiste (D10)."""
        PathwayActivityScore.objects.create(
            pathway=self.pathway_pik3k,
            sample=self.tumor_sample,
            method_version='fase4-pfs-v1-a',
            score=0.1, null_mean=0.0, null_sd=1.0, z_score=0.1, p_empirical=0.5,
        )
        PathwayActivityScore.objects.create(
            pathway=self.pathway_pik3k,
            sample=self.tumor_sample,
            method_version='fase4-pfs-v1-b',
            score=0.2, null_mean=0.0, null_sd=1.0, z_score=0.2, p_empirical=0.5,
        )
        self.assertEqual(
            PathwayActivityScore.objects.filter(
                pathway=self.pathway_pik3k, sample=self.tumor_sample,
            ).count(),
            2,
        )
