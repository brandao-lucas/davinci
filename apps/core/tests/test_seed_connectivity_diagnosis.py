"""
test_seed_connectivity_diagnosis.py — Cobertura de
diagnose_seed_connectivity / SeedConnectivityService (OmnisPathway Obj 2,
Fase 4 — diagnóstico pré-execução).

Motivação do command (ver docstring do módulo/service): o motor PFS
produziu resultado fraco numa bancada real e a causa (VHL — nó ISOLADO no
KEGG, sem nenhuma aresta de saída) só apareceu no FIM da investigação, após
rodar score_pathways + apply_pathway_fdr. Este diagnóstico responde "vale a
pena rodar isto?" ANTES do run, classificando cada nó-gene semeado em
sem-saída / saída-cega / útil pelo GRAU DE SAÍDA (nunca o total).

Casos cobertos:
  1. Classificação por nó: sem saída (out_degree=0); saída cega (out>0,
     todas sign=0); útil (>=1 aresta assinada); nó com SÓ entradas (sem
     nenhuma saída) classificado como sem-saída — é o caso HIF1A/hsa04066
     que trava a decisão "grau de SAÍDA, não total".
  2. Semente neutra (direction='neutral') não semeia — nó correspondente
     fica de fora do universo semeado.
  3. Junção semente↔nó é case-insensitive (UPPER dos dois lados).
  4. Isolamento por projeto: seed cuja matriz pertence a um dataset NÃO
     vinculado ao projeto (ou vinculado a outro projeto) não conta.
  5. Escopo por --seed-method-versions: versão fora da lista pedida não
     conta.
  6. Escopo por --pathways: restringe o universo de vias.
  7. Agregação "por via" (piores primeiro) e "por gene" (isolados,
     ordenados por nº de sementes, com fração vias-isoladas/vias-totais).
  8. Veredito: fração útil abaixo do patamar sinaliza risco de execução
     degenerada; acima, não.
  9. Command: --project obrigatório/valido; universo vazio → CommandError
     acionável; --format json grava sob settings.REPO_ROOT/diagnostics/
     exports; --top limita as listas.

Padrões obrigatórios: SEM pytest (django.test.TestCase); SEM rede; helpers
de factory espelham test_omnispathway_fase4_pfs.py / test_cnv_allele.py.
"""

from __future__ import annotations

import json
import os
import uuid
from io import StringIO

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.core.models import (
    DaVinciProject,
    OmicDataset,
    OmicMatrix,
    OmicSample,
    Pathway,
    PathwayEdge,
    PathwayNode,
    ProjectDataset,
    VariantEffectSeed,
)
from apps.core.services.seed_connectivity_service import (
    SeedConnectivityEmptyError,
    build_report,
    render_text,
)

SEED_MV_CNV = 'fase2-cnv-v2'
SEED_MV_SNV = 'fase2-snv-v1'


# =============================================================================
# Helpers de factory
# =============================================================================

def _make_user(username: str) -> User:
    return User.objects.create_user(username=username, password='pw')


def _make_project(user: User, title: str = 'Seed Connectivity Bench') -> DaVinciProject:
    slug = f'{title.lower().replace(" ", "-")}-{user.username}-{uuid.uuid4().hex[:6]}'
    return DaVinciProject.objects.create(
        user=user, title=title, slug=slug, query_term='seed-connectivity-test',
    )


def _make_dataset(accession: str) -> OmicDataset:
    ds, _ = OmicDataset.objects.get_or_create(
        accession=accession,
        defaults={
            'source_db': OmicDataset.SourceDB.CPTAC,
            'title': f'Dataset {accession}',
            'access_type': OmicDataset.AccessType.PUBLIC,
        },
    )
    return ds


def _make_matrix(dataset: OmicDataset, omics_layer: str = 'copy_number') -> OmicMatrix:
    matrix, _ = OmicMatrix.objects.get_or_create(
        dataset=dataset,
        omics_layer=omics_layer,
        feature_axis=OmicMatrix.FeatureAxis.GENE,
        loader_version='v1',
        defaults={
            'data_format_level': OmicMatrix.DataFormatLevel.LOG_RATIO,
            'storage_key': f'omics/_shared/{dataset.accession}/matrix.parquet',
            'n_features': 10,
            'n_samples': 1,
            'checksum_md5': 'aa11bb22cc33dd44ee55ff6677889900',
        },
    )
    return matrix


def _link_dataset(
    project: DaVinciProject,
    dataset: OmicDataset,
    status: str = ProjectDataset.CurationStatus.INCLUDED,
) -> ProjectDataset:
    return ProjectDataset.objects.create(
        project=project, dataset=dataset, curation_status=status,
    )


def _make_sample(dataset: OmicDataset, accession: str) -> OmicSample:
    return OmicSample.objects.create(
        dataset=dataset, accession=accession, title=accession,
        organism='Homo sapiens', characteristics={'case_id': accession},
    )


def _make_seed(
    matrix: OmicMatrix,
    sample: OmicSample,
    gene_symbol: str,
    *,
    direction: str = VariantEffectSeed.Direction.INACTIVATOR,
    evidence_type: str = VariantEffectSeed.EvidenceType.CNV,
    method_version: str = SEED_MV_CNV,
    variant_key: str = '',
) -> VariantEffectSeed:
    return VariantEffectSeed.objects.create(
        matrix=matrix, sample=sample, gene_symbol=gene_symbol,
        variant_key=variant_key, evidence_type=evidence_type,
        direction=direction, magnitude=0.8, confidence=0.9,
        method_version=method_version,
    )


def _make_pathway(kegg_id: str, name: str = '') -> Pathway:
    pathway, _ = Pathway.objects.get_or_create(
        kegg_id=kegg_id,
        defaults={'name': name or f'Pathway {kegg_id}', 'source': Pathway.Source.KEGG},
    )
    return pathway


def _make_node(pathway: Pathway, gene_symbol: str, entry_id: str | None = None) -> PathwayNode:
    eid = entry_id or f'entry-{gene_symbol}-{pathway.kegg_id}'
    node, _ = PathwayNode.objects.get_or_create(
        pathway=pathway, kegg_entry_id=eid,
        defaults={
            'node_type': PathwayNode.NodeType.GENE,
            'gene_symbol': gene_symbol,
            'graphics_name': gene_symbol,
            'kegg_ids': [f'hsa:{gene_symbol}'],
        },
    )
    return node


def _make_edge(
    pathway: Pathway, source: PathwayNode, target: PathwayNode, sign: int,
    interaction: str = PathwayEdge.Interaction.ACTIVATION,
) -> PathwayEdge:
    return PathwayEdge.objects.create(
        pathway=pathway, source_node=source, target_node=target, sign=sign,
        relation_type=PathwayEdge.RelationType.PPREL, interaction=interaction,
        subtypes=['activation'] if sign else ['binding/association'],
    )


# =============================================================================
# 1-6. Classificação por nó + escopos (projeto / method-version / pathways /
#      neutro / case-insensitive)
# =============================================================================

class SeedConnectivityClassificationTests(TestCase):
    """
    Um projeto com uma via só (hsaTEST01), quatro genes:
      SEMFIM  — nó sem NENHUMA aresta        → sem_saida
      CEGO    — 1 aresta de saída sign=0     → cega
      UTIL    — 1 aresta de saída sign=+1    → util
      HIF1A   — só ENTRADA (2 arestas chegando, zero saindo) → sem_saida
                (o teste que trava "grau de SAÍDA, não total")
    Mais NEUTRO (só semente neutra — fora do universo) e um par
    projeto/dataset fora de escopo para o teste de isolamento.
    """

    def setUp(self):
        self.user = _make_user('seedconn-user')
        self.project = _make_project(self.user)
        self.dataset = _make_dataset('SEEDCONN-DS')
        self.matrix = _make_matrix(self.dataset)
        _link_dataset(self.project, self.dataset)
        self.sample = _make_sample(self.dataset, 'SEEDCONN-S1')

        self.pathway = _make_pathway('hsaTEST01')

        self.n_semfim = _make_node(self.pathway, 'SEMFIM')
        self.n_cego = _make_node(self.pathway, 'CEGO')
        self.n_cego_alvo = _make_node(self.pathway, 'CEGO_ALVO')
        self.n_util = _make_node(self.pathway, 'UTIL')
        self.n_util_alvo = _make_node(self.pathway, 'UTIL_ALVO')
        self.n_hif1a = _make_node(self.pathway, 'HIF1A')
        self.n_hif1a_fonte1 = _make_node(self.pathway, 'HIF1A_UP1')
        self.n_hif1a_fonte2 = _make_node(self.pathway, 'HIF1A_UP2')

        # CEGO → CEGO_ALVO, sign=0 (só arestas sem sinal saindo de CEGO)
        _make_edge(self.pathway, self.n_cego, self.n_cego_alvo, sign=0)
        # UTIL → UTIL_ALVO, sign=+1
        _make_edge(self.pathway, self.n_util, self.n_util_alvo, sign=1)
        # HIF1A_UP1 → HIF1A, HIF1A_UP2 → HIF1A (SÓ entradas para HIF1A)
        _make_edge(self.pathway, self.n_hif1a_fonte1, self.n_hif1a, sign=1)
        _make_edge(self.pathway, self.n_hif1a_fonte2, self.n_hif1a, sign=-1)
        # SEMFIM: nenhuma aresta (nem entrada nem saída)

        # Sementes direcionais para os 4 genes-alvo do teste de classificação.
        _make_seed(self.matrix, self.sample, 'SEMFIM')
        _make_seed(self.matrix, self.sample, 'CEGO')
        _make_seed(self.matrix, self.sample, 'UTIL')
        _make_seed(self.matrix, self.sample, 'HIF1A')

        # Semente NEUTRA — não deve semear (gene fora do universo).
        _make_node(self.pathway, 'NEUTRO')
        _make_seed(
            self.matrix, self.sample, 'NEUTRO',
            direction=VariantEffectSeed.Direction.NEUTRAL,
        )

    def _report(self, **kwargs):
        return build_report(
            self.project,
            seed_method_versions=[SEED_MV_CNV, SEED_MV_SNV],
            **kwargs,
        )

    def test_no_sem_nenhuma_aresta_e_sem_saida(self):
        report = self._report()
        genes = {row['gene_symbol']: row for row in _by_gene_all(report)}
        # SEMFIM não tem nenhuma via onde NÃO seja isolado (só 1 via, isolada).
        self.assertIn('SEMFIM', genes)
        self.assertEqual(genes['SEMFIM']['n_pathways_isolated'], 1)
        self.assertEqual(genes['SEMFIM']['n_pathways_total'], 1)

    def test_no_com_saida_so_sign_zero_e_cego(self):
        report = self._report()
        s = report['summary']
        self.assertEqual(s['n_cega'], 1)
        # CEGO não pode aparecer na lista de isolados (não é sem_saida).
        genes = {row['gene_symbol']: row for row in _by_gene_all(report)}
        self.assertNotIn('CEGO', genes)

    def test_no_com_saida_assinada_e_util(self):
        report = self._report()
        s = report['summary']
        self.assertEqual(s['n_util'], 1)
        genes = {row['gene_symbol']: row for row in _by_gene_all(report)}
        self.assertNotIn('UTIL', genes)

    def test_no_so_com_entradas_e_sem_saida_caso_hif1a(self):
        """
        O teste que trava a decisão de usar grau de SAÍDA, não total: HIF1A
        recebe DUAS arestas (entradas) e não emite NENHUMA — deve ser
        classificado sem_saida, não util/cego.
        """
        report = self._report()
        genes = {row['gene_symbol']: row for row in _by_gene_all(report)}
        self.assertIn('HIF1A', genes)
        self.assertEqual(genes['HIF1A']['n_pathways_isolated'], 1)

        # Confirma via SQL bruto: out_degree de HIF1A é 0 apesar de in_degree=2.
        from apps.core.models import PathwayEdge
        self.assertEqual(
            PathwayEdge.objects.filter(target_node=self.n_hif1a).count(), 2,
        )
        self.assertEqual(
            PathwayEdge.objects.filter(source_node=self.n_hif1a).count(), 0,
        )

    def test_semente_neutra_nao_semeia(self):
        report = self._report()
        s = report['summary']
        # Universo: SEMFIM, CEGO, UTIL, HIF1A — NEUTRO fica de fora.
        self.assertEqual(s['n_total'], 4)
        genes = {row['gene_symbol']: row for row in _by_gene_all(report)}
        self.assertNotIn('NEUTRO', genes)

    def test_juncao_case_insensitive(self):
        # Semente com gene_symbol minúsculo deve casar com nó UPPERCASE.
        pathway2 = _make_pathway('hsaTEST02')
        node = _make_node(pathway2, 'LOWERGENE')
        _make_seed(self.matrix, self.sample, 'lowergene')

        report = build_report(
            self.project,
            seed_method_versions=[SEED_MV_CNV, SEED_MV_SNV],
            pathway_ids=['hsaTEST02'],
        )
        s = report['summary']
        self.assertEqual(s['n_total'], 1)
        self.assertEqual(s['n_sem_saida'], 1)

    def test_isolamento_por_projeto_seed_de_outro_dataset_nao_conta(self):
        other_user = _make_user('seedconn-other')
        other_project = _make_project(other_user, 'Outro Projeto')
        other_dataset = _make_dataset('SEEDCONN-OTHER-DS')
        other_matrix = _make_matrix(other_dataset)
        _link_dataset(other_project, other_dataset)
        other_sample = _make_sample(other_dataset, 'SEEDCONN-OTHER-S1')

        # Gene só semeado no OUTRO projeto — não deve aparecer no relatório
        # do self.project.
        _make_node(self.pathway, 'FORADOPROJETO')
        _make_seed(other_matrix, other_sample, 'FORADOPROJETO')

        report = self._report()
        genes_all = {row['gene_symbol'] for row in _by_gene_all(report)}
        self.assertNotIn('FORADOPROJETO', genes_all)
        # E o relatório do OUTRO projeto deve incluir esse gene (positivo,
        # não apenas negativo — confirma que o filtro é por projeto, não
        # "sempre vazio").
        other_report = build_report(
            other_project, seed_method_versions=[SEED_MV_CNV, SEED_MV_SNV],
        )
        self.assertEqual(other_report['summary']['n_total'], 1)

    def test_escopo_seed_method_versions_exclui_versao_fora_da_lista(self):
        # Gene semeado só sob uma versão fora do escopo pedido.
        pathway3 = _make_pathway('hsaTEST03')
        _make_node(pathway3, 'FORADAVERSAO')
        _make_seed(
            self.matrix, self.sample, 'FORADAVERSAO',
            method_version='fase2-cnv-v1',
        )

        # Sem nenhuma seed sob fase2-cnv-v2/fase2-snv-v1 nesta via restrita,
        # o universo fica vazio — build_report levanta, não retorna zerado.
        with self.assertRaises(SeedConnectivityEmptyError):
            build_report(
                self.project,
                seed_method_versions=[SEED_MV_CNV, SEED_MV_SNV],
                pathway_ids=['hsaTEST03'],
            )

    def test_escopo_pathways_restringe_universo(self):
        pathway_out = _make_pathway('hsaTEST99')
        _make_node(pathway_out, 'FORADAVIA')
        _make_seed(self.matrix, self.sample, 'FORADAVIA')

        report = self._report(pathway_ids=['hsaTEST01'])
        genes = {row['gene_symbol'] for row in _by_gene_all(report)}
        self.assertNotIn('FORADAVIA', genes)


def _by_gene_all(report: dict) -> list[dict]:
    """`build_report` já corta em --top; para os testes de composição usamos
    `by_gene_isolated_total` como sinal e a lista cortada (top default cobre
    o cenário pequeno destes testes)."""
    return report['by_gene_isolated']


# =============================================================================
# 7. Agregação "por via" (piores primeiro) e veredito
# =============================================================================

class SeedConnectivityByPathwayAndVerdictTests(TestCase):

    def setUp(self):
        self.user = _make_user('seedconn-pw-user')
        self.project = _make_project(self.user, 'PW Bench')
        self.dataset = _make_dataset('SEEDCONN-PW-DS')
        self.matrix = _make_matrix(self.dataset)
        _link_dataset(self.project, self.dataset)
        self.sample = _make_sample(self.dataset, 'SEEDCONN-PW-S1')

        # Via A: 100% sem saída (2 nós, ambos isolados).
        self.pa = _make_pathway('hsaPWA')
        na1 = _make_node(self.pa, 'GENEA1')
        na2 = _make_node(self.pa, 'GENEA2')
        _make_seed(self.matrix, self.sample, 'GENEA1')
        _make_seed(self.matrix, self.sample, 'GENEA2')

        # Via B: 100% útil (1 nó com saída assinada).
        self.pb = _make_pathway('hsaPWB')
        nb1 = _make_node(self.pb, 'GENEB1')
        nb1_alvo = _make_node(self.pb, 'GENEB1_ALVO')
        _make_edge(self.pb, nb1, nb1_alvo, sign=1)
        _make_seed(self.matrix, self.sample, 'GENEB1')

    def test_por_via_ordena_piores_primeiro(self):
        report = build_report(
            self.project, seed_method_versions=[SEED_MV_CNV, SEED_MV_SNV],
        )
        rows = report['by_pathway_worst']
        by_kegg = {r['kegg_id']: r for r in rows}
        self.assertEqual(by_kegg['hsaPWA']['frac_sem_saida'], 1.0)
        self.assertEqual(by_kegg['hsaPWB']['frac_sem_saida'], 0.0)
        # Piores primeiro.
        self.assertEqual(rows[0]['kegg_id'], 'hsaPWA')

    def test_veredito_alerta_quando_fracao_util_baixa(self):
        report = build_report(
            self.project,
            seed_method_versions=[SEED_MV_CNV, SEED_MV_SNV],
            pathway_ids=['hsaPWA'],
        )
        self.assertTrue(report['verdict']['degenerate_risk'])
        self.assertIn('DEGENERADO', report['verdict']['message'])

    def test_veredito_nao_alerta_quando_fracao_util_alta(self):
        report = build_report(
            self.project,
            seed_method_versions=[SEED_MV_CNV, SEED_MV_SNV],
            pathway_ids=['hsaPWB'],
        )
        self.assertFalse(report['verdict']['degenerate_risk'])


# =============================================================================
# 8. Command: obrigatoriedade, erro vazio, texto, JSON, --top
# =============================================================================

class DiagnoseSeedConnectivityCommandTests(TestCase):

    def setUp(self):
        self.user = _make_user('seedconn-cmd-user')
        self.project = _make_project(self.user, 'CMD Bench')
        self.dataset = _make_dataset('SEEDCONN-CMD-DS')
        self.matrix = _make_matrix(self.dataset)
        _link_dataset(self.project, self.dataset)
        self.sample = _make_sample(self.dataset, 'SEEDCONN-CMD-S1')

        self.pathway = _make_pathway('hsaCMD01')
        _make_node(self.pathway, 'CMDGENE')
        _make_seed(self.matrix, self.sample, 'CMDGENE')

    def test_projeto_invalido_e_command_error(self):
        with self.assertRaises(CommandError):
            call_command(
                'diagnose_seed_connectivity', '--project', str(uuid.uuid4()),
                stdout=StringIO(),
            )

    def test_universo_vazio_e_command_error_acionavel(self):
        empty_project = _make_project(self.user, 'Vazio Bench')
        with self.assertRaises(CommandError):
            call_command(
                'diagnose_seed_connectivity', '--project', str(empty_project.id),
                stdout=StringIO(),
            )

    def test_command_text_stdout(self):
        out = StringIO()
        call_command(
            'diagnose_seed_connectivity', '--project', str(self.project.id),
            stdout=out,
        )
        text = out.getvalue()
        self.assertIn('Global', text)
        self.assertIn('Por via', text)
        self.assertIn('Por gene', text)
        self.assertIn('Veredito', text)
        self.assertIn('CMDGENE', text)

    def test_command_format_json_grava_sob_repo_root_diagnostics_exports(self):
        out = StringIO()
        call_command(
            'diagnose_seed_connectivity', '--project', str(self.project.id),
            '--format', 'json', stdout=out,
        )
        line = out.getvalue()
        expected_dir = os.path.join(str(settings.REPO_ROOT), 'diagnostics', 'exports')
        self.assertIn(expected_dir, line)

        path = line.split('Diagnóstico JSON gravado em:')[-1].strip()
        self.assertTrue(os.path.isfile(path))
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            self.assertEqual(data['project_id'], str(self.project.id))
            self.assertEqual(data['summary']['n_total'], 1)
        finally:
            os.remove(path)

    def test_command_top_limita_listas(self):
        for i in range(5):
            gene = f'TOPGENE{i}'
            _make_node(self.pathway, gene, entry_id=f'entry-top-{i}')
            _make_seed(self.matrix, self.sample, gene)

        report = build_report(
            self.project, seed_method_versions=[SEED_MV_CNV, SEED_MV_SNV], top=2,
        )
        self.assertLessEqual(len(report['by_gene_isolated']), 2)
        self.assertLessEqual(len(report['by_pathway_worst']), 2)

    def test_render_text_nao_quebra_sem_genes_isolados(self):
        # Todo mundo útil — by_gene_isolated vazio, render_text não deve quebrar.
        pathway = _make_pathway('hsaCMD02')
        n1 = _make_node(pathway, 'ALLUTIL1')
        n2 = _make_node(pathway, 'ALLUTIL2')
        _make_edge(pathway, n1, n2, sign=1)
        _make_seed(self.matrix, self.sample, 'ALLUTIL1')

        report = build_report(
            self.project,
            seed_method_versions=[SEED_MV_CNV, SEED_MV_SNV],
            pathway_ids=['hsaCMD02'],
        )
        text = render_text(report)
        self.assertIn('Global', text)
