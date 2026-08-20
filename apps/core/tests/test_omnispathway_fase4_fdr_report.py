"""
test_omnispathway_fase4_fdr_report.py — Cobertura de
apply_pathway_fdr/report_pathway_activity (OmnisPathway Obj 2, Fase 4).

Antes deste par de commands, o Benjamini-Hochberg era aplicado por SQL
avulso digitado no shell, e os números do relatório vinham de consultas
ad-hoc — nada reprodutível. Este arquivo cobre:

  1. BH correto num cenário pequeno e HAND-COMPUTÁVEL (dois escopos:
     population = partição por via; individual = partição por amostra),
     incluindo empate (mesmo p → mesmo q) e degenerados EXCLUÍDOS do
     denominador mas ROTULADOS (fdr_method preenchido, q NULL,
     fdr_n_tests=0).
  2. --dry-run não persiste nada (só conta avaliáveis/degenerados).
  3. --scope inválido rejeitado tanto no service (guarda contra qualquer
     string fora do mapa fechado) quanto no command (argparse choices).
  4. Taxonomia do relatório soma o total de vias do catálogo, cobrindo as
     quatro categorias (atribuível / não atribuível / bloqueada-sem-semente
     / bloqueada-sem-readout).
  5. report_pathway_activity: método vazio levanta erro acionável; --format
     json grava sob settings.REPO_ROOT/diagnostics/exports.

Padrões obrigatórios: SEM pytest (django.test.TestCase); SEM rede.

── Derivação do cenário hand-computável (population, partição = pathway_id) ─
  P1: S1 p=0.01, S2 p=0.04, S3 DEGENERADO           → m=2
      ordenado: (S1,0.01) rk1 raw=0.01*2/1=0.02
                (S2,0.04) rk2 raw=0.04*2/2=0.04
      cummin do fim: q(S2)=0.04; q(S1)=min(0.02,0.04)=0.02
  P2: S1 p=0.02, S2 p=0.02 (EMPATE), S3 p=0.06       → m=3
      ordenado (empate desempatado por id): (S1,0.02) rk1, (S2,0.02) rk2,
                (S3,0.06) rk3
      raw: rk1=0.02*3/1=0.06; rk2=0.02*3/2=0.03; rk3=0.06*3/3=0.06
      cummin do fim: q(rk3)=0.06; q(rk2)=min(0.03,0.06)=0.03;
                     q(rk1)=min(0.06,0.03)=0.03
      → q(S1)=q(S2)=0.03 (MESMO q apesar de rk diferente — é o ponto do
        empate: o cummin "step-up" absorve a ordem exata do desempate).
  P3: S1 p=0.10, S2 p=0.30, S3 DEGENERADO            → m=2
      ordenado: (S1,0.10) rk1 raw=0.10*2/1=0.20; (S2,0.30) rk2 raw=0.30
      cummin: q(S2)=0.30; q(S1)=min(0.20,0.30)=0.20
  P4: NUNCA pontuada (0 linhas)                       → bloqueada-sem-readout
  P5: S1/S2/S3 todos DEGENERADOS                      → bloqueada-sem-semente

── Individual, partição = sample_id (mesmos p, P5 sempre degenerado) ────────
  S1: P1 p=0.01, P2 p=0.02, P3 p=0.10                 → m=3
      ordenado: (P1,0.01) rk1, (P2,0.02) rk2, (P3,0.10) rk3
      raw: rk1=0.03; rk2=0.03; rk3=0.10
      cummin: q(P3)=0.10; q(P2)=min(0.03,0.10)=0.03; q(P1)=min(0.03,0.03)=0.03
  S2: P1 p=0.04, P2 p=0.02, P3 p=0.30                 → m=3
      ordenado: (P2,0.02) rk1, (P1,0.04) rk2, (P3,0.30) rk3
      raw: rk1=0.06; rk2=0.06; rk3=0.30
      cummin: q(P3)=0.30; q(P1)=min(0.06,0.30)=0.06; q(P2)=min(0.06,0.06)=0.06
  S3: só P2 avaliável (P1/P3/P5 degenerados nesta amostra)  → m=1
      raw=q=0.06*1/1=0.06

Com threshold=0.05: P1 (0.02,0.04 < 0.05) e P2 (0.03,0.03 < 0.05) são
ATRIBUÍVEIS; P3 (0.20,0.30) NÃO ATRIBUÍVEL; P4 bloqueada-sem-readout; P5
bloqueada-sem-semente. Taxonomia: 2/1/1/1 = 5 vias no catálogo do teste.
"""

from __future__ import annotations

import json
import os

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase
from io import StringIO

from apps.core.models import OmicDataset, OmicSample, Pathway, PathwayActivityScore
from apps.core.services.pathway_fdr_service import (
    PathwayFdrScopeError,
    apply_fdr,
)
from apps.core.services.pathway_report_service import (
    PathwayReportEmptyError,
    build_report,
)

MV = 'fdr-test-v1'


def _make_pathway(kegg_id: str) -> Pathway:
    return Pathway.objects.create(
        kegg_id=kegg_id, name=f'Pathway {kegg_id}', source=Pathway.Source.KEGG,
    )


def _make_sample(accession: str) -> OmicSample:
    ds, _ = OmicDataset.objects.get_or_create(
        accession='FDR-TEST-DATASET',
        defaults={
            'source_db': OmicDataset.SourceDB.CPTAC,
            'title': 'FDR test dataset',
            'access_type': OmicDataset.AccessType.PUBLIC,
        },
    )
    return OmicSample.objects.create(
        dataset=ds, accession=accession, title=accession,
        organism='Homo sapiens', characteristics={'case_id': accession},
    )


def _score(pathway, sample, *, p=0.5, null_sd=1.0, method_version=MV):
    return PathwayActivityScore.objects.create(
        pathway=pathway, sample=sample, method_version=method_version,
        score=0.0, null_mean=0.0, null_sd=null_sd,
        z_score=0.0 if null_sd == 0 else 1.0,
        p_empirical=p, n_permutations=1000 if null_sd != 0 else 0,
    )


class FdrReportFixtureMixin:
    """Monta o cenário hand-computável descrito no docstring do módulo."""

    def setUp(self):
        super().setUp()
        self.p1 = _make_pathway('hsaFDR01')
        self.p2 = _make_pathway('hsaFDR02')
        self.p3 = _make_pathway('hsaFDR03')
        self.p4 = _make_pathway('hsaFDR04')  # nunca pontuada
        self.p5 = _make_pathway('hsaFDR05')  # 100% degenerada

        self.s1 = _make_sample('FDR-S1')
        self.s2 = _make_sample('FDR-S2')
        self.s3 = _make_sample('FDR-S3')

        # P1
        _score(self.p1, self.s1, p=0.01)
        _score(self.p1, self.s2, p=0.04)
        _score(self.p1, self.s3, null_sd=0.0)
        # P2
        _score(self.p2, self.s1, p=0.02)
        _score(self.p2, self.s2, p=0.02)
        _score(self.p2, self.s3, p=0.06)
        # P3
        _score(self.p3, self.s1, p=0.10)
        _score(self.p3, self.s2, p=0.30)
        _score(self.p3, self.s3, null_sd=0.0)
        # P5 — 100% degenerada (P4 nunca é pontuada, nenhuma linha)
        _score(self.p5, self.s1, null_sd=0.0)
        _score(self.p5, self.s2, null_sd=0.0)
        _score(self.p5, self.s3, null_sd=0.0)


# =============================================================================
# 1. BH correto — dois escopos, empate, degenerados excluídos e rotulados
# =============================================================================

class ApplyPathwayFdrCorrectnessTests(FdrReportFixtureMixin, TestCase):

    def test_bh_populacional_com_empate_e_degenerado_excluido(self):
        report = apply_fdr(method_version=MV, scope='population')

        self.assertEqual(report['scopes']['population']['n_evaluable'], 7)  # 2+3+2
        self.assertEqual(report['scopes']['population']['n_degenerate'], 5)  # 1+1+3
        self.assertEqual(report['scopes']['population']['rows_updated_bh'], 7)
        self.assertEqual(report['scopes']['population']['rows_labeled_degenerate'], 5)

        def q_of(pathway, sample):
            row = PathwayActivityScore.objects.get(
                pathway=pathway, sample=sample, method_version=MV,
            )
            return row.q_value_across_samples, row.fdr_method_across_samples, row.fdr_n_tests_across_samples

        q, method, m = q_of(self.p1, self.s1)
        self.assertAlmostEqual(q, 0.02, places=9)
        self.assertEqual(method, 'benjamini_hochberg')
        self.assertEqual(m, 2)

        q, _, m = q_of(self.p1, self.s2)
        self.assertAlmostEqual(q, 0.04, places=9)
        self.assertEqual(m, 2)

        # Degenerado: rotulado (fdr_method preenchido), q NULL, m=0.
        q, method, m = q_of(self.p1, self.s3)
        self.assertIsNone(q)
        self.assertEqual(method, 'benjamini_hochberg')
        self.assertEqual(m, 0)

        # P2 — empate: S1 e S2 têm o MESMO p (0.02) e terminam com o MESMO q,
        # mesmo que o desempate por id lhes dê ranks diferentes.
        q_s1, _, m_s1 = q_of(self.p2, self.s1)
        q_s2, _, m_s2 = q_of(self.p2, self.s2)
        self.assertAlmostEqual(q_s1, 0.03, places=9)
        self.assertAlmostEqual(q_s2, 0.03, places=9)
        self.assertEqual(q_s1, q_s2)
        self.assertEqual(m_s1, 3)
        self.assertEqual(m_s2, 3)

        q, _, m = q_of(self.p2, self.s3)
        self.assertAlmostEqual(q, 0.06, places=9)
        self.assertEqual(m, 3)

        # P3 — sem sinal, ambos os q ficam altos.
        q, _, m = q_of(self.p3, self.s1)
        self.assertAlmostEqual(q, 0.20, places=9)
        q, _, m = q_of(self.p3, self.s2)
        self.assertAlmostEqual(q, 0.30, places=9)

        # P5 — 100% degenerada: todas as linhas rotuladas, q NULL, m=0.
        for sample in (self.s1, self.s2, self.s3):
            q, method, m = q_of(self.p5, sample)
            self.assertIsNone(q)
            self.assertEqual(method, 'benjamini_hochberg')
            self.assertEqual(m, 0)

    def test_bh_individual_partitions_por_amostra(self):
        apply_fdr(method_version=MV, scope='individual')

        def q_of(pathway, sample):
            row = PathwayActivityScore.objects.get(
                pathway=pathway, sample=sample, method_version=MV,
            )
            return row.q_value_across_pathways, row.fdr_n_tests_across_pathways

        q, m = q_of(self.p1, self.s1)
        self.assertAlmostEqual(q, 0.03, places=9)
        self.assertEqual(m, 3)
        q, m = q_of(self.p2, self.s1)
        self.assertAlmostEqual(q, 0.03, places=9)
        self.assertEqual(m, 3)
        q, m = q_of(self.p3, self.s1)
        self.assertAlmostEqual(q, 0.10, places=9)
        self.assertEqual(m, 3)

        q, m = q_of(self.p1, self.s2)
        self.assertAlmostEqual(q, 0.06, places=9)
        q, m = q_of(self.p2, self.s2)
        self.assertAlmostEqual(q, 0.06, places=9)
        q, m = q_of(self.p3, self.s2)
        self.assertAlmostEqual(q, 0.30, places=9)

        # S3: só P2 é avaliável nesta amostra (m=1).
        q, m = q_of(self.p2, self.s3)
        self.assertAlmostEqual(q, 0.06, places=9)
        self.assertEqual(m, 1)

        # Degenerados desta amostra continuam rotulados/NULL, mesmo com m=1
        # em outra linha da MESMA amostra — a exclusão é por LINHA.
        row = PathwayActivityScore.objects.get(
            pathway=self.p1, sample=self.s3, method_version=MV,
        )
        self.assertIsNone(row.q_value_across_pathways)
        self.assertEqual(row.fdr_method_across_pathways, 'benjamini_hochberg')
        self.assertEqual(row.fdr_n_tests_across_pathways, 0)

    def test_both_aplica_os_dois_escopos_na_mesma_linha(self):
        report = apply_fdr(method_version=MV, scope='both')
        self.assertIn('population', report['scopes'])
        self.assertIn('individual', report['scopes'])

        row = PathwayActivityScore.objects.get(
            pathway=self.p1, sample=self.s1, method_version=MV,
        )
        self.assertAlmostEqual(row.q_value_across_samples, 0.02, places=9)
        self.assertAlmostEqual(row.q_value_across_pathways, 0.03, places=9)

    def test_idempotente_rodar_duas_vezes_produz_o_mesmo_resultado(self):
        apply_fdr(method_version=MV, scope='both')
        row_before = PathwayActivityScore.objects.get(
            pathway=self.p2, sample=self.s1, method_version=MV,
        )
        q1_before = row_before.q_value_across_samples
        q2_before = row_before.q_value_across_pathways

        apply_fdr(method_version=MV, scope='both')
        row_after = PathwayActivityScore.objects.get(
            pathway=self.p2, sample=self.s1, method_version=MV,
        )
        self.assertAlmostEqual(q1_before, row_after.q_value_across_samples, places=9)
        self.assertAlmostEqual(q2_before, row_after.q_value_across_pathways, places=9)


# =============================================================================
# 2. --dry-run não persiste
# =============================================================================

class ApplyPathwayFdrDryRunTests(FdrReportFixtureMixin, TestCase):

    def test_dry_run_conta_mas_nao_grava(self):
        report = apply_fdr(method_version=MV, scope='both', dry_run=True)

        self.assertEqual(report['scopes']['population']['n_evaluable'], 7)
        self.assertEqual(report['scopes']['population']['n_degenerate'], 5)
        self.assertEqual(report['scopes']['population']['rows_updated_bh'], 0)
        self.assertEqual(report['scopes']['individual']['rows_labeled_degenerate'], 0)

        # Nada mudou no banco — todas as colunas de FDR seguem "nunca rodou".
        for row in PathwayActivityScore.objects.filter(method_version=MV):
            self.assertEqual(row.fdr_method_across_samples, '')
            self.assertEqual(row.fdr_method_across_pathways, '')
            self.assertIsNone(row.q_value_across_samples)
            self.assertIsNone(row.q_value_across_pathways)

    def test_command_dry_run_nao_persiste(self):
        out = StringIO()
        call_command(
            'apply_pathway_fdr', '--method-version', MV, '--dry-run', stdout=out,
        )
        self.assertIn('DRY RUN', out.getvalue())
        self.assertFalse(
            PathwayActivityScore.objects.filter(
                method_version=MV,
            ).exclude(fdr_method_across_samples='').exists()
        )


# =============================================================================
# 3. --scope inválido — mapa fechado, nunca string livre
# =============================================================================

class PathwayFdrScopeGuardTests(FdrReportFixtureMixin, TestCase):

    def test_scope_invalido_no_service_levanta_erro_sem_montar_sql(self):
        with self.assertRaises(PathwayFdrScopeError):
            apply_fdr(method_version=MV, scope='pathway_id; DROP TABLE core_pathwayactivityscore;--')

        # Nada foi tocado.
        self.assertTrue(
            PathwayActivityScore.objects.filter(method_version=MV).exists()
        )
        self.assertFalse(
            PathwayActivityScore.objects.filter(
                method_version=MV,
            ).exclude(fdr_method_across_samples='').exists()
        )

    def test_scope_invalido_no_command_e_rejeitado_pelo_argparse(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command(
                'apply_pathway_fdr', '--method-version', MV, '--scope', 'nao_existe',
                stderr=StringIO(), stdout=StringIO(),
            )

    def test_method_version_vazio_rejeitado(self):
        with self.assertRaises(ValueError):
            apply_fdr(method_version='', scope='both')


# =============================================================================
# 4. Taxonomia soma o total do catálogo
# =============================================================================

class PathwayReportTaxonomyTests(FdrReportFixtureMixin, TestCase):

    def setUp(self):
        super().setUp()
        apply_fdr(method_version=MV, scope='both')

    def test_taxonomia_soma_o_total_de_vias_e_classifica_corretamente(self):
        report = build_report(method_version=MV, threshold=0.05)
        tax = report['taxonomy']

        self.assertEqual(tax['atribuivel'], 2)          # P1, P2
        self.assertEqual(tax['nao_atribuivel'], 1)       # P3
        self.assertEqual(tax['bloqueada_sem_semente'], 1)  # P5
        self.assertEqual(tax['bloqueada_sem_readout'], 1)  # P4
        self.assertEqual(tax['total'], 5)
        self.assertEqual(tax['total'], Pathway.objects.count())

    def test_resumo_avaliaveis_degenerados_e_total(self):
        report = build_report(method_version=MV, threshold=0.05)
        s = report['summary']
        self.assertEqual(s['total_pairs'], 12)  # 3+3+3+0+3
        self.assertEqual(s['evaluable'], 7)
        self.assertEqual(s['degenerate'], 5)

    def test_populacional_lista_apenas_vias_atribuiveis(self):
        report = build_report(method_version=MV, threshold=0.05)
        kegg_ids = {row['kegg_id'] for row in report['population']['by_pathway']}
        self.assertEqual(kegg_ids, {'hsaFDR01', 'hsaFDR02'})
        self.assertEqual(report['population']['n_pathways'], 2)

    def test_individual_lista_amostras_com_via_abaixo_do_limiar(self):
        report = build_report(method_version=MV, threshold=0.05)
        accessions = {row['accession'] for row in report['individual']['by_sample']}
        # S1: P1(0.03)/P2(0.03) < 0.05; S2: P1(0.06)/P2(0.06) >= 0.05 (fora);
        # S3: P2(0.06) >= 0.05 (fora). Só S1 aparece.
        self.assertEqual(accessions, {'FDR-S1'})

    def test_sensibilidade_populacional_em_tres_limiares(self):
        report = build_report(method_version=MV, threshold=0.05)
        sens = report['sensitivity']
        # q_value_across_samples avaliáveis: 0.02,0.04,0.03,0.03,0.06,0.20,0.30
        self.assertEqual(sens['q<0.05'], 4)   # 0.02,0.04,0.03,0.03
        self.assertEqual(sens['q<0.01'], 0)
        self.assertEqual(sens['q<0.001'], 0)


# =============================================================================
# 5. report_pathway_activity — erro acionável e --format json
# =============================================================================

class ReportPathwayActivityCommandTests(FdrReportFixtureMixin, TestCase):

    def test_method_version_sem_linhas_levanta_erro_acionavel(self):
        with self.assertRaises(PathwayReportEmptyError):
            build_report(method_version='method-version-inexistente')

    def test_command_method_version_vazio_e_command_error(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command(
                'report_pathway_activity',
                '--method-version', 'method-version-inexistente',
                stdout=StringIO(),
            )

    def test_command_text_stdout(self):
        apply_fdr(method_version=MV, scope='both')
        out = StringIO()
        call_command('report_pathway_activity', '--method-version', MV, stdout=out)
        text = out.getvalue()
        self.assertIn('Resumo', text)
        self.assertIn('Taxonomia', text)
        self.assertIn('atribuível', text)

    def test_command_format_json_grava_sob_repo_root_diagnostics_exports(self):
        apply_fdr(method_version=MV, scope='both')
        out = StringIO()
        call_command(
            'report_pathway_activity', '--method-version', MV,
            '--format', 'json', stdout=out,
        )
        line = out.getvalue()
        expected_dir = os.path.join(str(settings.REPO_ROOT), 'diagnostics', 'exports')
        self.assertIn(expected_dir, line)

        # Extrai o caminho gravado e confirma que o JSON é válido e tem o
        # method_version correto.
        path = line.split('Relatório JSON gravado em:')[-1].strip()
        self.assertTrue(os.path.isfile(path))
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            self.assertEqual(data['method_version'], MV)
            self.assertEqual(data['taxonomy']['total'], 5)
        finally:
            os.remove(path)
