"""
test_variant_effect_resolve.py — Cobertura do VariantEffectResolveService
e management command resolve_variant_effects.

OmnisPathway Objetivo 2, Fase 2, Slice 2C (passo 2.5).

NÚCLEO CIENTÍFICO — coberto extensivamente:

  §Tabela G: mapeamento categoria ClinVar → magnitude/confiança:
    - Pathogenic → 1.0, conf=0.9
    - Likely_pathogenic → 0.9, conf=0.9
    - Benign → 0.0, conf=0.9
    - Likely_benign → 0.1, conf=0.9
    - VUS/Uncertain/Conflicting → cai para AlphaMissense
    - Oncogenic (campo oncogenicity) → 1.0, conf=0.9
    - Likely oncogenic → 0.9, conf=0.9

  Precedência de fonte (ClinVar > AlphaMissense > dbNSFP):
    - ClinVar Pathogenic vence AM para o mesmo variant_key.
    - ClinVar VUS cai para AM.
    - Só AM (sem ClinVar): usa am_pathogenicity.
    - Só dbNSFP (sem ClinVar/AM): usa raw_magnitude.
    - Tudo vazio: n_no_magnitude++ e skip.

  Regras de direção (Decisão J):
    - danoso (mag >= 0.5) + tsg → inactivator.
    - danoso + oncogene → activator.
    - danoso + oncogene_and_tsg → neutral + n_dual_flagged.
    - danoso + neither → neutral.
    - danoso + unknown → neutral.
    - danoso sem papel (gene ausente em GeneRole) → neutral + n_no_gene_role.
    - benigno (mag < 0.5) → neutral (sem importar papel).

  Idempotência:
    - Re-run mesma resolution_version não duplica.
    - --dry-run não grava.

  Set-based sem N+1:
    - Carga de GeneRole em memória (1 query).
    - Chunk de pares via _fetch_pairs (paginação offset/limit).

  Management command:
    - Outputs contagens corretas.
    - --dry-run → sem gravação.
    - --genes restringe genes.
    - --chunk-size inválido → CommandError.
    - VariantEffectRaw vazio → n_pairs_processed=0.

Padrões obrigatórios:
  - SEM internet, SEM Rust — tudo Django ORM.
  - SEM pytest — usa django.test.TestCase.
  - Dados sintéticos via ORM (nenhum CSV/VCF commitado).
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from django.test import TestCase

from apps.core.models import GeneRole, VariantEffectRaw, VariantEffectResolved
from apps.core.services.variant_effect_resolve_service import (
    DAMAGING_THRESHOLD,
    RESOLUTION_VERSION,
    ResolutionReport,
    VariantEffectResolveService,
    _am_magnitude_to_class,
    _resolve_clinvar_magnitude,
    _resolve_direction,
)


# =============================================================================
# Helpers de fixture
# =============================================================================

def _make_clinvar_row(
    variant_key: str = 'chr1:100:A:T',
    gene_symbol: str = 'TP53',
    clinvar_significance: str = 'Pathogenic',
    oncogenicity: str = '',
    am_pathogenicity=None,
) -> dict:
    """Cria dict representando uma linha ClinVar de VariantEffectRaw.values()."""
    return {
        'variant_key': variant_key,
        'gene_symbol': gene_symbol,
        'source': VariantEffectRaw.Source.CLINVAR,
        'raw_magnitude': None,
        'raw_class': '',
        'clinvar_significance': clinvar_significance,
        'oncogenicity': oncogenicity,
        'am_pathogenicity': am_pathogenicity,
        'confidence': None,
    }


def _make_am_row(
    variant_key: str = 'chr1:100:A:T',
    gene_symbol: str = 'TP53',
    am_pathogenicity: float = 0.9,
) -> dict:
    """Cria dict representando uma linha AlphaMissense de VariantEffectRaw.values()."""
    return {
        'variant_key': variant_key,
        'gene_symbol': gene_symbol,
        'source': VariantEffectRaw.Source.ALPHAMISSENSE,
        'raw_magnitude': am_pathogenicity,
        'raw_class': 'likely_pathogenic' if am_pathogenicity >= 0.8 else 'ambiguous',
        'clinvar_significance': '',
        'oncogenicity': '',
        'am_pathogenicity': am_pathogenicity,
        'confidence': None,
    }


def _make_dbnsfp_row(
    variant_key: str = 'chr1:100:A:T',
    gene_symbol: str = 'TP53',
    raw_magnitude: float = 0.8,
) -> dict:
    return {
        'variant_key': variant_key,
        'gene_symbol': gene_symbol,
        'source': VariantEffectRaw.Source.DBNSFP,
        'raw_magnitude': raw_magnitude,
        'raw_class': '',
        'clinvar_significance': '',
        'oncogenicity': '',
        'am_pathogenicity': None,
        'confidence': None,
    }


def _create_vareff_raw(
    variant_key: str,
    gene_symbol: str,
    source: str,
    clinvar_significance: str = '',
    oncogenicity: str = '',
    am_pathogenicity: float | None = None,
    raw_magnitude: float | None = None,
    raw_class: str = '',
) -> VariantEffectRaw:
    return VariantEffectRaw.objects.create(
        variant_key=variant_key,
        gene_symbol=gene_symbol,
        source=source,
        clinvar_significance=clinvar_significance,
        oncogenicity=oncogenicity,
        am_pathogenicity=am_pathogenicity,
        raw_magnitude=raw_magnitude,
        raw_class=raw_class,
    )


def _create_gene_role(
    symbol: str, role: str, source: str = 'oncokb'
) -> GeneRole:
    gr, _ = GeneRole.objects.get_or_create(
        gene_symbol=symbol,
        source=source,
        defaults={'role': role},
    )
    return gr


# =============================================================================
# 1. Helpers de módulo — _resolve_clinvar_magnitude (§Tabela G)
# =============================================================================

class ClinvarMagnitudeResolutionTests(TestCase):
    """Testa _resolve_clinvar_magnitude contra §Tabela G."""

    def _row(self, sig='', onco='') -> dict:
        return {
            'clinvar_significance': sig,
            'oncogenicity': onco,
            'raw_magnitude': None,
            'am_pathogenicity': None,
        }

    # ── §Tabela G: significâncias germinativas ────────────────────────────────

    def test_pathogenic_maps_to_1_0(self):
        mag, ec, vus = _resolve_clinvar_magnitude(self._row(sig='Pathogenic'))
        self.assertAlmostEqual(mag, 1.0)
        self.assertEqual(ec, 'damaging')
        self.assertFalse(vus)

    def test_likely_pathogenic_maps_to_0_9(self):
        mag, ec, vus = _resolve_clinvar_magnitude(self._row(sig='Likely pathogenic'))
        self.assertAlmostEqual(mag, 0.9)
        self.assertEqual(ec, 'damaging')
        self.assertFalse(vus)

    def test_benign_maps_to_0_0(self):
        mag, ec, vus = _resolve_clinvar_magnitude(self._row(sig='Benign'))
        self.assertAlmostEqual(mag, 0.0)
        self.assertEqual(ec, 'benign')
        self.assertFalse(vus)

    def test_likely_benign_maps_to_0_1(self):
        mag, ec, vus = _resolve_clinvar_magnitude(self._row(sig='Likely benign'))
        self.assertAlmostEqual(mag, 0.1)
        self.assertEqual(ec, 'benign')
        self.assertFalse(vus)

    def test_uncertain_significance_is_vus(self):
        mag, ec, vus = _resolve_clinvar_magnitude(
            self._row(sig='Uncertain significance')
        )
        self.assertIsNone(mag)
        self.assertTrue(vus)

    def test_conflicting_is_vus(self):
        mag, ec, vus = _resolve_clinvar_magnitude(
            self._row(sig='Conflicting interpretations of pathogenicity')
        )
        self.assertIsNone(mag)
        self.assertTrue(vus)

    def test_not_provided_is_vus(self):
        mag, ec, vus = _resolve_clinvar_magnitude(
            self._row(sig='not provided')
        )
        self.assertIsNone(mag)
        self.assertTrue(vus)

    def test_empty_significance_is_vus(self):
        """Sem significância → VUS (não skip completo — cai para AM)."""
        mag, ec, vus = _resolve_clinvar_magnitude(self._row(sig=''))
        self.assertIsNone(mag)
        self.assertTrue(vus)

    # ── §Tabela G: oncogenicity (campo somático) ──────────────────────────────

    def test_oncogenic_maps_to_1_0(self):
        mag, ec, vus = _resolve_clinvar_magnitude(self._row(onco='Oncogenic'))
        self.assertAlmostEqual(mag, 1.0)
        self.assertEqual(ec, 'damaging')
        self.assertFalse(vus)

    def test_likely_oncogenic_maps_to_0_9(self):
        mag, ec, vus = _resolve_clinvar_magnitude(self._row(onco='Likely oncogenic'))
        self.assertAlmostEqual(mag, 0.9)
        self.assertEqual(ec, 'damaging')
        self.assertFalse(vus)

    def test_oncogenic_field_takes_precedence_over_significance(self):
        """
        oncogenicity preenchido → usa campo oncogenicity, ignora
        clinvar_significance.
        """
        row = self._row(sig='Benign', onco='Oncogenic')
        mag, ec, vus = _resolve_clinvar_magnitude(row)
        # Oncogenicity vence: 1.0, não 0.0 do Benign
        self.assertAlmostEqual(mag, 1.0)
        self.assertFalse(vus)

    def test_benign_oncogenicity_maps_to_0_0(self):
        mag, ec, vus = _resolve_clinvar_magnitude(self._row(onco='Benign'))
        self.assertAlmostEqual(mag, 0.0)
        self.assertFalse(vus)


# =============================================================================
# 2. Helper _am_magnitude_to_class (§Tabela G thresholds)
# =============================================================================

class AmMagnitudeToClassTests(TestCase):
    """Testa thresholds AM: >= 0.8 → damaging, < 0.3 → benign."""

    def test_high_am_is_damaging(self):
        self.assertEqual(_am_magnitude_to_class(0.9), 'damaging')
        self.assertEqual(_am_magnitude_to_class(0.8), 'damaging')

    def test_mid_am_is_uncertain(self):
        self.assertEqual(_am_magnitude_to_class(0.5), 'uncertain')
        self.assertEqual(_am_magnitude_to_class(0.4), 'uncertain')

    def test_low_am_is_benign(self):
        self.assertEqual(_am_magnitude_to_class(0.1), 'benign')
        self.assertEqual(_am_magnitude_to_class(0.3), 'benign')
        self.assertEqual(_am_magnitude_to_class(0.0), 'benign')


# =============================================================================
# 3. Helper _resolve_direction (Decisão J)
# =============================================================================

class ResolveDirectionTests(TestCase):
    """
    Testa _resolve_direction — NÚCLEO da regra de sinal.

    Cobre todos os ramos da Decisão J:
      - benigno (mag < DAMAGING_THRESHOLD) → neutral, independente do papel.
      - danoso + TSG → inactivator.
      - danoso + oncogene → activator.
      - danoso + oncogene_and_tsg → neutral + dual_flag=True.
      - danoso + neither → neutral, sem flag.
      - danoso + unknown → neutral, sem flag.
    """

    def test_benign_tsg_is_neutral(self):
        """Variante benigna em TSG → neutral (mag < limiar, papel ignorado)."""
        direction, dual = _resolve_direction(0.1, GeneRole.Role.TSG)
        self.assertEqual(direction, VariantEffectResolved.Direction.NEUTRAL)
        self.assertFalse(dual)

    def test_benign_oncogene_is_neutral(self):
        """Variante benigna em oncogene → neutral."""
        direction, dual = _resolve_direction(0.2, GeneRole.Role.ONCOGENE)
        self.assertEqual(direction, VariantEffectResolved.Direction.NEUTRAL)
        self.assertFalse(dual)

    def test_at_threshold_benign_is_neutral(self):
        """Magnitude exatamente abaixo do limiar → neutral."""
        direction, dual = _resolve_direction(DAMAGING_THRESHOLD - 0.001, GeneRole.Role.TSG)
        self.assertEqual(direction, VariantEffectResolved.Direction.NEUTRAL)

    def test_damaging_tsg_is_inactivator(self):
        """Variante danosa + TSG → inactivator."""
        direction, dual = _resolve_direction(0.9, GeneRole.Role.TSG)
        self.assertEqual(direction, VariantEffectResolved.Direction.INACTIVATOR)
        self.assertFalse(dual)

    def test_at_threshold_tsg_is_inactivator(self):
        """Magnitude exatamente igual ao limiar → danosa → inactivator em TSG."""
        direction, dual = _resolve_direction(DAMAGING_THRESHOLD, GeneRole.Role.TSG)
        self.assertEqual(direction, VariantEffectResolved.Direction.INACTIVATOR)

    def test_damaging_oncogene_is_activator(self):
        """Variante danosa + oncogene → activator (heurística v1 GoF)."""
        direction, dual = _resolve_direction(1.0, GeneRole.Role.ONCOGENE)
        self.assertEqual(direction, VariantEffectResolved.Direction.ACTIVATOR)
        self.assertFalse(dual)

    def test_damaging_oncogene_and_tsg_is_neutral_with_flag(self):
        """
        Variante danosa + oncogene_and_tsg → neutral + dual_flag=True.
        Decisão J: ambiguidade não é resolvida em v1.
        """
        direction, dual = _resolve_direction(0.9, GeneRole.Role.ONCOGENE_AND_TSG)
        self.assertEqual(direction, VariantEffectResolved.Direction.NEUTRAL)
        self.assertTrue(dual)

    def test_damaging_neither_is_neutral(self):
        """Variante danosa + neither → neutral."""
        direction, dual = _resolve_direction(0.8, GeneRole.Role.NEITHER)
        self.assertEqual(direction, VariantEffectResolved.Direction.NEUTRAL)
        self.assertFalse(dual)

    def test_damaging_unknown_is_neutral(self):
        """Variante danosa + unknown → neutral."""
        direction, dual = _resolve_direction(0.7, GeneRole.Role.UNKNOWN)
        self.assertEqual(direction, VariantEffectResolved.Direction.NEUTRAL)
        self.assertFalse(dual)

    def test_benign_oncogene_and_tsg_no_flag(self):
        """Variante benigna em oncogene_and_tsg → neutral sem flag (benigno não flaga)."""
        direction, dual = _resolve_direction(0.1, GeneRole.Role.ONCOGENE_AND_TSG)
        self.assertEqual(direction, VariantEffectResolved.Direction.NEUTRAL)
        # Benigno → não chega à regra de dual_flag
        self.assertFalse(dual)


# =============================================================================
# 4. Precedência de fonte (integração via _resolve_pair)
# =============================================================================

class VariantEffectResolvePrecedenceTests(TestCase):
    """
    Testa precedência ClinVar > AlphaMissense > dbNSFP na resolução
    de _resolve_pair via run() integrado com dados reais no DB.
    """

    def setUp(self):
        GeneRole.objects.all().delete()
        VariantEffectRaw.objects.all().delete()
        VariantEffectResolved.objects.all().delete()

        _create_gene_role('TP53', GeneRole.Role.TSG)
        _create_gene_role('EGFR', GeneRole.Role.ONCOGENE)
        _create_gene_role('DUAL', GeneRole.Role.ONCOGENE_AND_TSG)

    def _run(self, gene_symbols=None) -> ResolutionReport:
        svc = VariantEffectResolveService(
            resolution_version=RESOLUTION_VERSION,
            gene_symbols=gene_symbols,
            chunk_size=100,
        )
        return svc.run(dry_run=False)

    def test_clinvar_wins_over_am_same_variant(self):
        """
        ClinVar Pathogenic vence AM para o mesmo (variant_key, gene_symbol).
        effect_source deve ser 'clinvar'; magnitude=1.0.
        """
        vkey = 'chr17:7673776:C:T'
        _create_vareff_raw(vkey, 'TP53', VariantEffectRaw.Source.CLINVAR,
                           clinvar_significance='Pathogenic')
        _create_vareff_raw(vkey, 'TP53', VariantEffectRaw.Source.ALPHAMISSENSE,
                           am_pathogenicity=0.5, raw_magnitude=0.5)

        self._run()

        resolved = VariantEffectResolved.objects.get(
            variant_key=vkey, gene_symbol='TP53',
            resolution_version=RESOLUTION_VERSION,
        )
        self.assertEqual(resolved.effect_source, VariantEffectRaw.Source.CLINVAR)
        self.assertAlmostEqual(resolved.magnitude, 1.0)
        self.assertEqual(resolved.direction, VariantEffectResolved.Direction.INACTIVATOR)

    def test_clinvar_vus_falls_to_am(self):
        """
        ClinVar VUS → cai para AM.
        effect_source deve ser 'alphamissense'; magnitude = am_pathogenicity.
        """
        vkey = 'chr17:7673777:G:A'
        _create_vareff_raw(vkey, 'TP53', VariantEffectRaw.Source.CLINVAR,
                           clinvar_significance='Uncertain significance')
        _create_vareff_raw(vkey, 'TP53', VariantEffectRaw.Source.ALPHAMISSENSE,
                           am_pathogenicity=0.85, raw_magnitude=0.85)

        report = self._run()

        self.assertEqual(report.n_clinvar_vus_fell_to_am, 1)
        resolved = VariantEffectResolved.objects.get(
            variant_key=vkey, gene_symbol='TP53',
            resolution_version=RESOLUTION_VERSION,
        )
        self.assertEqual(resolved.effect_source, VariantEffectRaw.Source.ALPHAMISSENSE)
        self.assertAlmostEqual(resolved.magnitude, 0.85)

    def test_only_am_no_clinvar(self):
        """
        Só AM (sem ClinVar): magnitude = am_pathogenicity, effect_source = alphamissense.
        """
        vkey = 'chr17:7673778:A:G'
        _create_vareff_raw(vkey, 'EGFR', VariantEffectRaw.Source.ALPHAMISSENSE,
                           am_pathogenicity=0.9, raw_magnitude=0.9)

        self._run()

        resolved = VariantEffectResolved.objects.get(
            variant_key=vkey, gene_symbol='EGFR',
            resolution_version=RESOLUTION_VERSION,
        )
        self.assertEqual(resolved.effect_source, VariantEffectRaw.Source.ALPHAMISSENSE)
        self.assertAlmostEqual(resolved.magnitude, 0.9)
        self.assertEqual(resolved.direction, VariantEffectResolved.Direction.ACTIVATOR)

    def test_only_dbnsfp_used_when_clinvar_and_am_absent(self):
        """
        Só dbNSFP (sem ClinVar, sem AM): magnitude = raw_magnitude.
        """
        vkey = 'chr17:7673779:C:G'
        _create_vareff_raw(vkey, 'TP53', VariantEffectRaw.Source.DBNSFP,
                           raw_magnitude=0.75)

        self._run()

        resolved = VariantEffectResolved.objects.get(
            variant_key=vkey, gene_symbol='TP53',
            resolution_version=RESOLUTION_VERSION,
        )
        self.assertEqual(resolved.effect_source, VariantEffectRaw.Source.DBNSFP)
        self.assertAlmostEqual(resolved.magnitude, 0.75)

    def test_all_sources_vus_no_magnitude_skip(self):
        """
        Todas as fontes sem magnitude resolvível → n_no_magnitude++, sem registro.
        """
        vkey = 'chr17:7673780:T:C'
        _create_vareff_raw(vkey, 'TP53', VariantEffectRaw.Source.CLINVAR,
                           clinvar_significance='Uncertain significance')
        # Sem AM nem dbNSFP

        report = self._run()

        self.assertEqual(report.n_no_magnitude, 1)
        self.assertFalse(
            VariantEffectResolved.objects.filter(
                variant_key=vkey, gene_symbol='TP53'
            ).exists()
        )


# =============================================================================
# 5. Regras de direção — integração completa com DB
# =============================================================================

class VariantEffectResolveDirectionIntegrationTests(TestCase):
    """
    Testa regras de direção (Decisão J) via run() completo.
    Usa VariantEffectRaw Pathogenic (mag=1.0) para garantir variante "danosa".
    """

    def setUp(self):
        GeneRole.objects.all().delete()
        VariantEffectRaw.objects.all().delete()
        VariantEffectResolved.objects.all().delete()

    def _pathogenic_raw(self, vkey: str, gene: str):
        _create_vareff_raw(
            vkey, gene, VariantEffectRaw.Source.CLINVAR,
            clinvar_significance='Pathogenic'
        )

    def _run(self) -> ResolutionReport:
        return VariantEffectResolveService(chunk_size=100).run(dry_run=False)

    def test_damaging_tsg_becomes_inactivator(self):
        """Pathogenic + TSG → direction=inactivator."""
        _create_gene_role('TP53', GeneRole.Role.TSG)
        self._pathogenic_raw('chr1:1:A:T', 'TP53')

        report = self._run()

        self.assertEqual(report.n_inactivator, 1)
        r = VariantEffectResolved.objects.get(
            variant_key='chr1:1:A:T', gene_symbol='TP53'
        )
        self.assertEqual(r.direction, VariantEffectResolved.Direction.INACTIVATOR)
        self.assertAlmostEqual(r.confidence, 0.9)

    def test_damaging_oncogene_becomes_activator(self):
        """Pathogenic + oncogene → direction=activator."""
        _create_gene_role('EGFR', GeneRole.Role.ONCOGENE)
        self._pathogenic_raw('chr1:2:A:T', 'EGFR')

        report = self._run()

        self.assertEqual(report.n_activator, 1)
        r = VariantEffectResolved.objects.get(
            variant_key='chr1:2:A:T', gene_symbol='EGFR'
        )
        self.assertEqual(r.direction, VariantEffectResolved.Direction.ACTIVATOR)

    def test_damaging_oncogene_and_tsg_is_neutral_and_flagged(self):
        """
        Pathogenic + oncogene_and_tsg → direction=neutral + n_dual_flagged++.
        Decisão J: não inventa direção em ambiguidade de papel.
        """
        _create_gene_role('DUAL', GeneRole.Role.ONCOGENE_AND_TSG)
        self._pathogenic_raw('chr1:3:A:T', 'DUAL')

        report = self._run()

        self.assertEqual(report.n_dual_flagged, 1)
        self.assertEqual(report.n_neutral, 1)
        r = VariantEffectResolved.objects.get(
            variant_key='chr1:3:A:T', gene_symbol='DUAL'
        )
        self.assertEqual(r.direction, VariantEffectResolved.Direction.NEUTRAL)
        self.assertEqual(r.gene_role_used, GeneRole.Role.ONCOGENE_AND_TSG)

    def test_damaging_neither_is_neutral_no_flag(self):
        """Pathogenic + neither → neutral sem dual_flag."""
        _create_gene_role('NODIR', GeneRole.Role.NEITHER)
        self._pathogenic_raw('chr1:4:A:T', 'NODIR')

        report = self._run()

        self.assertEqual(report.n_neutral, 1)
        self.assertEqual(report.n_dual_flagged, 0)

    def test_damaging_unknown_role_is_neutral(self):
        """Pathogenic + unknown → neutral."""
        _create_gene_role('UNKN', GeneRole.Role.UNKNOWN)
        self._pathogenic_raw('chr1:5:A:T', 'UNKN')

        report = self._run()

        r = VariantEffectResolved.objects.get(
            variant_key='chr1:5:A:T', gene_symbol='UNKN'
        )
        self.assertEqual(r.direction, VariantEffectResolved.Direction.NEUTRAL)

    def test_damaging_no_gene_role_is_neutral_and_counted(self):
        """
        Pathogenic + gene SEM GeneRole (não está na tabela) → neutral + n_no_gene_role++.
        """
        # Gene sem GeneRole criado
        self._pathogenic_raw('chr1:6:A:T', 'NOVOGENE')

        report = self._run()

        self.assertGreaterEqual(report.n_no_gene_role, 1)
        r = VariantEffectResolved.objects.get(
            variant_key='chr1:6:A:T', gene_symbol='NOVOGENE'
        )
        self.assertEqual(r.direction, VariantEffectResolved.Direction.NEUTRAL)

    def test_benign_tsg_is_neutral(self):
        """
        Benign + TSG → neutral (variante benigna não tem LoF, independente do papel).
        """
        _create_gene_role('TP53', GeneRole.Role.TSG)
        _create_vareff_raw(
            'chr1:7:A:T', 'TP53', VariantEffectRaw.Source.CLINVAR,
            clinvar_significance='Benign'
        )

        report = self._run()

        r = VariantEffectResolved.objects.get(
            variant_key='chr1:7:A:T', gene_symbol='TP53'
        )
        self.assertEqual(r.direction, VariantEffectResolved.Direction.NEUTRAL)
        self.assertAlmostEqual(r.magnitude, 0.0)

    def test_am_damaging_tsg_is_inactivator(self):
        """
        AM am_pathogenicity=0.9 + TSG → magnitude=0.9 → inactivator.
        Confirma Tabela G para AM.
        """
        _create_gene_role('TP53', GeneRole.Role.TSG)
        _create_vareff_raw(
            'chr1:8:A:T', 'TP53', VariantEffectRaw.Source.ALPHAMISSENSE,
            am_pathogenicity=0.9, raw_magnitude=0.9
        )

        self._run()

        r = VariantEffectResolved.objects.get(
            variant_key='chr1:8:A:T', gene_symbol='TP53'
        )
        self.assertEqual(r.effect_source, VariantEffectRaw.Source.ALPHAMISSENSE)
        self.assertAlmostEqual(r.magnitude, 0.9)
        self.assertAlmostEqual(r.confidence, 0.7)
        self.assertEqual(r.direction, VariantEffectResolved.Direction.INACTIVATOR)

    def test_likely_pathogenic_tsg_is_inactivator(self):
        """Likely pathogenic (mag=0.9 >= 0.5) + TSG → inactivator."""
        _create_gene_role('TP53', GeneRole.Role.TSG)
        _create_vareff_raw(
            'chr1:9:A:T', 'TP53', VariantEffectRaw.Source.CLINVAR,
            clinvar_significance='Likely pathogenic'
        )

        self._run()

        r = VariantEffectResolved.objects.get(
            variant_key='chr1:9:A:T', gene_symbol='TP53'
        )
        self.assertAlmostEqual(r.magnitude, 0.9)
        self.assertEqual(r.direction, VariantEffectResolved.Direction.INACTIVATOR)


# =============================================================================
# 6. Idempotência — re-run mesma resolution_version não duplica
# =============================================================================

class VariantEffectResolveIdempotencyTests(TestCase):
    """Testa idempotência do VariantEffectResolveService."""

    def setUp(self):
        GeneRole.objects.all().delete()
        VariantEffectRaw.objects.all().delete()
        VariantEffectResolved.objects.all().delete()

        _create_gene_role('TP53', GeneRole.Role.TSG)
        _create_vareff_raw(
            'chr1:1001:A:T', 'TP53', VariantEffectRaw.Source.CLINVAR,
            clinvar_significance='Pathogenic'
        )

    def test_second_run_same_version_does_not_duplicate(self):
        """Re-run com mesma resolution_version não cria registro duplicado."""
        svc = VariantEffectResolveService(
            resolution_version=RESOLUTION_VERSION, chunk_size=100
        )
        svc.run(dry_run=False)
        svc.run(dry_run=False)

        count = VariantEffectResolved.objects.filter(
            variant_key='chr1:1001:A:T',
            gene_symbol='TP53',
            resolution_version=RESOLUTION_VERSION,
        ).count()
        self.assertEqual(count, 1, 'Re-run não deve duplicar VariantEffectResolved')

    def test_dry_run_does_not_persist(self):
        """dry_run=True → nenhum VariantEffectResolved criado."""
        svc = VariantEffectResolveService(chunk_size=100)
        report = svc.run(dry_run=True)

        self.assertTrue(report.dry_run)
        self.assertEqual(VariantEffectResolved.objects.count(), 0)

    def test_dry_run_still_counts_pairs(self):
        """dry_run=True ainda conta pares processados e direções."""
        _create_vareff_raw(
            'chr1:1002:A:T', 'TP53', VariantEffectRaw.Source.CLINVAR,
            clinvar_significance='Benign'
        )

        svc = VariantEffectResolveService(chunk_size=100)
        report = svc.run(dry_run=True)

        self.assertGreater(report.n_pairs_processed, 0)
        self.assertGreater(report.n_resolved, 0)
        self.assertEqual(VariantEffectResolved.objects.count(), 0)

    def test_different_resolution_version_coexists(self):
        """
        Re-run com resolution_version diferente cria segundo registro
        (coexistência de versões — auditoria).
        """
        svc_v1 = VariantEffectResolveService(
            resolution_version='fase2-snv-v1', chunk_size=100
        )
        svc_v2 = VariantEffectResolveService(
            resolution_version='fase2-snv-v2', chunk_size=100
        )
        svc_v1.run(dry_run=False)
        svc_v2.run(dry_run=False)

        count = VariantEffectResolved.objects.filter(
            variant_key='chr1:1001:A:T', gene_symbol='TP53'
        ).count()
        self.assertEqual(count, 2, 'Duas versões devem coexistir (v1 e v2)')


# =============================================================================
# 7. Set-based sem N+1 — _fetch_pairs e _load_gene_roles
# =============================================================================

class VariantEffectResolveFetchTests(TestCase):
    """Testa _fetch_pairs (paginação) e _load_gene_roles (1 query em memória)."""

    def setUp(self):
        GeneRole.objects.all().delete()
        VariantEffectRaw.objects.all().delete()

    def test_fetch_pairs_returns_distinct_pairs(self):
        """_fetch_pairs retorna pares distintos (variant_key, gene_symbol)."""
        _create_vareff_raw('vk1', 'G1', VariantEffectRaw.Source.CLINVAR,
                           clinvar_significance='Pathogenic')
        _create_vareff_raw('vk1', 'G1', VariantEffectRaw.Source.ALPHAMISSENSE,
                           am_pathogenicity=0.9, raw_magnitude=0.9)
        _create_vareff_raw('vk2', 'G2', VariantEffectRaw.Source.CLINVAR,
                           clinvar_significance='Benign')

        svc = VariantEffectResolveService(chunk_size=100)
        pairs = svc._fetch_pairs(0, 100)

        # Deve retornar 2 pares distintos, não 3 linhas
        self.assertEqual(len(pairs), 2)
        self.assertIn(('vk1', 'G1'), pairs)
        self.assertIn(('vk2', 'G2'), pairs)

    def test_fetch_pairs_respects_chunk_size(self):
        """_fetch_pairs limita ao chunk_size."""
        for i in range(10):
            _create_vareff_raw(
                f'vkchunk{i}', f'G{i}', VariantEffectRaw.Source.CLINVAR,
                clinvar_significance='Pathogenic'
            )

        svc = VariantEffectResolveService(chunk_size=5)
        first_chunk = svc._fetch_pairs(0, 5)
        second_chunk = svc._fetch_pairs(5, 5)

        self.assertEqual(len(first_chunk), 5)
        self.assertEqual(len(second_chunk), 5)
        # Nenhuma sobreposição
        self.assertEqual(
            len(set(first_chunk) & set(second_chunk)), 0,
            'Chunks não devem sobrepor'
        )

    def test_load_gene_roles_returns_dict(self):
        """_load_gene_roles retorna {gene_symbol → role} de source=oncokb."""
        _create_gene_role('TP53', GeneRole.Role.TSG)
        _create_gene_role('EGFR', GeneRole.Role.ONCOGENE)

        svc = VariantEffectResolveService()
        roles = svc._load_gene_roles()

        self.assertEqual(roles.get('TP53'), GeneRole.Role.TSG)
        self.assertEqual(roles.get('EGFR'), GeneRole.Role.ONCOGENE)

    def test_load_gene_roles_with_gene_filter(self):
        """_load_gene_roles com gene_symbols filtra apenas os genes solicitados."""
        _create_gene_role('TP53', GeneRole.Role.TSG)
        _create_gene_role('EGFR', GeneRole.Role.ONCOGENE)

        svc = VariantEffectResolveService(gene_symbols=['TP53'])
        roles = svc._load_gene_roles()

        self.assertIn('TP53', roles)
        self.assertNotIn('EGFR', roles)

    def test_report_n_by_source_populated(self):
        """ResolutionReport.n_by_source contém a fonte vencedora com contagem."""
        GeneRole.objects.all().delete()
        VariantEffectRaw.objects.all().delete()
        VariantEffectResolved.objects.all().delete()

        _create_gene_role('TP53', GeneRole.Role.TSG)
        _create_vareff_raw(
            'chr1:999:A:T', 'TP53', VariantEffectRaw.Source.CLINVAR,
            clinvar_significance='Pathogenic'
        )

        svc = VariantEffectResolveService(chunk_size=100)
        report = svc.run(dry_run=False)

        self.assertIn('clinvar', report.n_by_source)
        self.assertEqual(report.n_by_source['clinvar'], 1)


# =============================================================================
# 8. Management command resolve_variant_effects
# =============================================================================

class ResolveVariantEffectsCommandTests(TestCase):
    """Testa o management command resolve_variant_effects."""

    def setUp(self):
        GeneRole.objects.all().delete()
        VariantEffectRaw.objects.all().delete()
        VariantEffectResolved.objects.all().delete()

        _create_gene_role('TP53', GeneRole.Role.TSG)
        _create_gene_role('EGFR', GeneRole.Role.ONCOGENE)

        # Fixture com 2 pares
        _create_vareff_raw(
            'chr1:2000:A:T', 'TP53', VariantEffectRaw.Source.CLINVAR,
            clinvar_significance='Pathogenic'
        )
        _create_vareff_raw(
            'chr1:2001:G:C', 'EGFR', VariantEffectRaw.Source.CLINVAR,
            clinvar_significance='Benign'
        )

    def _call_command(self, **kwargs) -> tuple[str, str]:
        from django.core.management import call_command
        stdout = StringIO()
        stderr = StringIO()
        call_command('resolve_variant_effects',
                     stdout=stdout, stderr=stderr, **kwargs)
        return stdout.getvalue(), stderr.getvalue()

    def test_command_reports_n_resolved(self):
        """Comando relata n_resolved corretamente."""
        stdout_val, _ = self._call_command()
        self.assertIn('n_resolved', stdout_val.lower())

    def test_command_reports_direction_counts(self):
        """Comando relata n_activator, n_inactivator, n_neutral."""
        stdout_val, _ = self._call_command()
        for field in ('n_activator', 'n_inactivator', 'n_neutral'):
            self.assertIn(field, stdout_val.lower(),
                          f'stdout deve conter "{field}": {stdout_val[:500]!r}')

    def test_command_dry_run_reports_without_persisting(self):
        """--dry-run reporta contagens mas não persiste VariantEffectResolved."""
        self._call_command(dry_run=True)
        self.assertEqual(VariantEffectResolved.objects.count(), 0)

    def test_command_persists_when_not_dry_run(self):
        """Sem --dry-run, VariantEffectResolved é persistido."""
        self._call_command(dry_run=False)
        self.assertGreater(VariantEffectResolved.objects.count(), 0)

    def test_command_gene_filter_restricts_processing(self):
        """--genes TP53 processa apenas TP53, ignora EGFR."""
        self._call_command(gene_symbols=['TP53'])

        # Apenas TP53 resolvido
        resolved_genes = set(
            VariantEffectResolved.objects
            .values_list('gene_symbol', flat=True)
        )
        self.assertIn('TP53', resolved_genes)
        self.assertNotIn('EGFR', resolved_genes)

    def test_command_invalid_chunk_size_raises_command_error(self):
        """--chunk-size <= 0 → CommandError."""
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command('resolve_variant_effects', chunk_size=0)

    def test_command_empty_variant_effect_raw_reports_zero_pairs(self):
        """VariantEffectRaw vazio → n_pairs_processed=0, stdout avisa."""
        VariantEffectRaw.objects.all().delete()

        stdout_val, _ = self._call_command()

        # Sem pares, não deve haver VariantEffectResolved
        self.assertEqual(VariantEffectResolved.objects.count(), 0)
        # stdout menciona pré-requisitos (VariantEffectRaw vazio)
        lower = stdout_val.lower()
        self.assertTrue(
            any(kw in lower for kw in ['vazio', 'nenhum', 'empty', 'prerequisit', 'pré-requisito']),
            f'stdout deve mencionar pré-requisitos: {lower[:400]!r}',
        )

    def test_command_resolution_version_custom(self):
        """--resolution-version personalizado é usado na natural key."""
        self._call_command(resolution_version='fase2-snv-vTESTE')

        count = VariantEffectResolved.objects.filter(
            resolution_version='fase2-snv-vTESTE'
        ).count()
        self.assertGreater(count, 0)

    def test_command_idempotent_second_run_no_duplicate(self):
        """Dois calls consecutivos não duplicam VariantEffectResolved."""
        self._call_command()
        n_first = VariantEffectResolved.objects.count()
        self._call_command()
        n_second = VariantEffectResolved.objects.count()
        self.assertEqual(n_first, n_second,
                         'Re-run não deve duplicar VariantEffectResolved')
