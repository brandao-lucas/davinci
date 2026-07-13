"""
VariantEffectResolveService — combinação set-based ClinVar>AlphaMissense>dbNSFP.

OmnisPathway Objetivo 2, Fase 2, passo 2.5 (Slice 2C).

Responsabilidade:
  Para cada (variant_key, gene_symbol) presente em VariantEffectRaw, produzir
  UMA linha em VariantEffectResolved com:
    - magnitude [0,1] normalizada (§Tabela G do plano)
    - effect_class textual (damaging/benign/uncertain)
    - direction (activator/inactivator/neutral) por magnitude + papel do gene
    - confidence, effect_source, gene_role_used, resolution_version

  É uma derivação SET-BASED pura — ZERO Rust, ZERO parse de arquivo.
  Tudo vive em PG (VariantEffectRaw + GeneRole → VariantEffectResolved).

Fronteira de camadas (Regra #1 / django-rust-boundary):
  Django ORM + bulk_create com update_conflicts — ZERO Rust, ZERO abertura de
  arquivo. Cruza tabelas já em PG. Nenhum dado binário, nenhum fetch HTTP.

Isolamento (firebase-auth-guard / Regra #3):
  VariantEffectRaw, GeneRole e VariantEffectResolved são CATÁLOGOS GLOBAIS
  (sem FK de projeto). O isolamento de leitura das seeds derivadas (2D) vive
  em VariantEffectSeed, ancorada em OmicMatrix via ProjectDataset.

Idempotência:
  bulk_create(update_conflicts=True, update_fields=[...]) na natural key
  (variant_key, gene_symbol, resolution_version). Rodar 2x não duplica;
  re-rodagens com nova lógica atualizam os registros existentes.

Catálogo derivado:
  VariantEffectResolved não é curadoria de usuário — Regra #2 NÃO se aplica.
  Delete/regeneração são permitidos.

§Tabela G — âncora categórico→score (v1, travada no plano):

  | Fonte      | Entrada crua                           | magnitude | confidence |
  |------------|----------------------------------------|-----------|------------|
  | ClinVar    | Pathogenic                             | 1.0       | 0.9        |
  | ClinVar    | Likely pathogenic                      | 0.9       | 0.9        |
  | ClinVar    | Benign                                 | 0.0       | 0.9        |
  | ClinVar    | Likely benign                          | 0.1       | 0.9        |
  | ClinVar    | Uncertain significance / conflitante   | -> cai p/ AlphaMissense |
  | ClinVar    | Oncogenic (campo oncogenicity)         | 1.0       | 0.9        |
  | ClinVar    | Likely oncogenic                       | 0.9       | 0.9        |
  | AM         | am_pathogenicity ∈ [0,1]              | = valor   | 0.7        |

  Limiar "danoso" para resolução de direção:
    magnitude >= 0.5 → variante danosa (candidata a activator/inactivator)
    magnitude <  0.5 → benigna/tolerada → neutral (independente do papel)

Regras de direção (Decisão J — §2.5 do plano):
  - danoso + role=tsg           → inactivator
  - danoso + role=oncogene      → activator  [heurística v1: missense danoso em
                                               oncogene assume GoF; refino futuro
                                               via OncoKB API (Decisão K)]
  - danoso + role=oncogene_and_tsg → neutral + gene_role_used='oncogene_and_tsg'
                                      (Decisão J: não inventar direção em ambiguidade)
  - danoso + role=neither/unknown/ausente → neutral
  - benigno (magnitude < 0.5)  → neutral (independente do papel)

Versão do algoritmo:
  RESOLUTION_VERSION = 'fase2-snv-v1'
  Parte da natural key de VariantEffectResolved. Trocar para 'fase2-snv-v2' ao
  refinar o algoritmo coexiste sem apagar a versão anterior.

Processamento set-based em chunks:
  Para escalar a dezenas de milhares de (variant_key, gene_symbol), a resolução
  é feita em chunks configuráveis (padrão: CHUNK_SIZE=5000). Cada chunk:
    1. Busca N pares (variant_key, gene_symbol) distintos (com offset/limit).
    2. Para cada par, escolhe a linha vencedora de VariantEffectRaw por
       precedência (clinvar > alphamissense > dbnsfp), filtrando ClinVar VUS.
    3. LEFT JOIN GeneRole (source='oncokb') para resolver direção.
    4. Monta VariantEffectResolved em memória e faz bulk_create (upsert).
  Chunks evitam carga monolítica na memória; o processamento é stateless e
  idempotente (safe to re-run mid-stream).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.db import transaction

from apps.core.models import GeneRole, VariantEffectRaw, VariantEffectResolved

logger = logging.getLogger(__name__)

# ─── Constantes ──────────────────────────────────────────────────────────────

RESOLUTION_VERSION = 'fase2-snv-v1'

# Limiar de magnitude para classificar como "danoso" e resolver direção.
DAMAGING_THRESHOLD = 0.5

# Chunk size padrão para processamento set-based (n pares por iteração).
CHUNK_SIZE = 5_000

# Precedência de fonte (menor = maior prioridade).
_SOURCE_PRIORITY = {
    VariantEffectRaw.Source.CLINVAR: 1,
    VariantEffectRaw.Source.ALPHAMISSENSE: 2,
    VariantEffectRaw.Source.DBNSFP: 3,
}

# Significâncias ClinVar que NÃO resolvem magnitude (VUS / conflitantes).
# Se a única linha disponível for ClinVar com uma dessas significâncias,
# cai para AlphaMissense (ou dbNSFP) como se ClinVar não existisse.
# NOTA: '' NÃO está aqui — campo vazio é tratado ANTES desta lista pela
# verificação `if not sig: return None, 'uncertain', True`.
_CLINVAR_VUS_PATTERNS = (
    'uncertain',
    'vus',
    'conflicting',
    'not provided',
    'other',
)

# Âncoras magnitude (§Tabela G — travadas no plano).
# ORDEM IMPORTA: entradas mais específicas (prefixo 'likely') ANTES das genéricas,
# porque o match é por substring (key in sig). 'pathogenic' seria substring de
# 'likely pathogenic', então 'likely pathogenic' deve vir primeiro.
_CLINVAR_SIGNIFICANCE_TO_MAGNITUDE: dict[str, tuple[float, str] | None] = {
    # (magnitude, effect_class) — mais específicos primeiro
    'likely pathogenic': (0.9, 'damaging'),
    'likely benign': (0.1, 'benign'),
    'likely oncogenic': (0.9, 'damaging'),
    'pathogenic': (1.0, 'damaging'),
    'benign': (0.0, 'benign'),
    'oncogenic': (1.0, 'damaging'),
    'likely benign/likely pathogenic': None,  # conflitante → VUS
}

# Âncoras oncogenicity (campo separado do ClinVar somático).
_ONCOGENICITY_TO_MAGNITUDE: dict[str, tuple[float, str]] = {
    'oncogenic': (1.0, 'damaging'),
    'likely oncogenic': (0.9, 'damaging'),
    'likely benign': (0.1, 'benign'),
    'benign': (0.0, 'benign'),
}

# Confiança base por fonte vencedora.
_SOURCE_CONFIDENCE: dict[str, float] = {
    VariantEffectRaw.Source.CLINVAR: 0.9,
    VariantEffectRaw.Source.ALPHAMISSENSE: 0.7,
    VariantEffectRaw.Source.DBNSFP: 0.6,
}


# ─── Dataclass do relatório ───────────────────────────────────────────────────

@dataclass
class ResolutionReport:
    """Relatório produzido pelo VariantEffectResolveService."""
    resolution_version: str = RESOLUTION_VERSION
    n_pairs_processed: int = 0          # pares (variant_key, gene_symbol) visitados
    n_resolved: int = 0                  # linhas gravadas/atualizadas
    n_activator: int = 0
    n_inactivator: int = 0
    n_neutral: int = 0
    n_dual_flagged: int = 0             # oncogene_and_tsg → neutral + flag
    n_no_gene_role: int = 0             # sem GeneRole → neutral
    n_by_source: dict = field(default_factory=dict)  # {source: count}
    n_clinvar_vus_fell_to_am: int = 0   # ClinVar VUS → caiu p/ AM
    n_no_magnitude: int = 0             # pares sem magnitude resolvível → skipped
    dry_run: bool = False
    duration_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            'resolution_version': self.resolution_version,
            'n_pairs_processed': self.n_pairs_processed,
            'n_resolved': self.n_resolved,
            'n_activator': self.n_activator,
            'n_inactivator': self.n_inactivator,
            'n_neutral': self.n_neutral,
            'n_dual_flagged': self.n_dual_flagged,
            'n_no_gene_role': self.n_no_gene_role,
            'n_by_source': self.n_by_source,
            'n_clinvar_vus_fell_to_am': self.n_clinvar_vus_fell_to_am,
            'n_no_magnitude': self.n_no_magnitude,
            'dry_run': self.dry_run,
            'duration_seconds': self.duration_seconds,
        }


# ─── Service ─────────────────────────────────────────────────────────────────

class VariantEffectResolveService:
    """
    Serviço de resolução de VariantEffectResolved (combinação set-based 2.5).

    Uso típico:
        service = VariantEffectResolveService()
        report = service.run(dry_run=False)

    Uso com genes-alvo restritos:
        service = VariantEffectResolveService(gene_symbols=['TP53', 'PIK3CA'])
        report = service.run(dry_run=True)

    O serviço processa em chunks de CHUNK_SIZE pares (variant_key, gene_symbol)
    para escalar sem explodir a memória. Idempotente — re-rodar atualiza os
    registros existentes (update_conflicts=True).
    """

    def __init__(
        self,
        resolution_version: str = RESOLUTION_VERSION,
        gene_symbols: list[str] | None = None,
        chunk_size: int = CHUNK_SIZE,
    ):
        """
        Args:
            resolution_version: Versão da resolução (parte da natural key).
                Usar a constante padrão salvo em testes ou reprocessamentos.
            gene_symbols: Se fornecido, restringe a resolução a esses genes.
                None = processa todos os genes em VariantEffectRaw.
            chunk_size: Número de pares (variant_key, gene_symbol) por chunk.
        """
        self.resolution_version = resolution_version
        self.gene_symbols = gene_symbols
        self.chunk_size = chunk_size

    # ─── API pública ─────────────────────────────────────────────────────────

    def run(self, dry_run: bool = False) -> ResolutionReport:
        """
        Executa a resolução set-based (ou simula se dry_run=True).

        Fluxo por chunk:
          1. Seleciona N pares (variant_key, gene_symbol) distintos de
             VariantEffectRaw (com filtro opcional por gene_symbols).
          2. Para cada par, resolve a linha vencedora de VariantEffectRaw por
             precedência e normaliza a magnitude (§Tabela G).
          3. LEFT JOIN GeneRole (source='oncokb') para resolver direção.
          4. Monta VariantEffectResolved em Python e faz bulk_create (upsert)
             na natural key (variant_key, gene_symbol, resolution_version).
          5. Acumula o relatório e avança para o próximo chunk.

        Args:
            dry_run: Se True, executa toda a lógica mas NÃO persiste nada.

        Returns:
            ResolutionReport com contagens detalhadas.
        """
        import time
        start = time.monotonic()

        report = ResolutionReport(
            resolution_version=self.resolution_version,
            dry_run=dry_run,
        )

        # Pré-check: VariantEffectRaw populado?
        if not VariantEffectRaw.objects.exists():
            logger.warning(
                'VariantEffectResolveService: VariantEffectRaw está vazio — '
                'nada a resolver. Execute load_variant_effects primeiro.'
            )
            report.duration_seconds = time.monotonic() - start
            return report

        # Pré-check: GeneRole populado? (warning, não erro — pode resolver sem papel)
        n_roles = GeneRole.objects.filter(source='oncokb').count()
        if n_roles == 0:
            logger.warning(
                'VariantEffectResolveService: GeneRole (source=oncokb) vazio. '
                'Todos os pares serão resolvidos como direction=neutral. '
                'Execute load_gene_roles para obter direção correta.'
            )

        logger.info(
            'VariantEffectResolveService: iniciando resolução '
            '(version=%s, gene_filter=%s, chunk_size=%d, dry_run=%s, '
            'n_gene_roles=%d)',
            self.resolution_version,
            self.gene_symbols or 'todos',
            self.chunk_size,
            dry_run,
            n_roles,
        )

        # Carrega GeneRole em memória (é pequeno: ~700 genes OncoKB).
        # Dict gene_symbol → role (source='oncokb' tem precedência).
        gene_roles = self._load_gene_roles()

        # Processa em chunks de pares distintos (variant_key, gene_symbol).
        offset = 0
        while True:
            pairs = self._fetch_pairs(offset, self.chunk_size)
            if not pairs:
                break

            chunk_resolved = self._resolve_chunk(pairs, gene_roles, report)

            if not dry_run and chunk_resolved:
                self._persist_chunk(chunk_resolved)

            offset += len(pairs)
            logger.debug(
                'VariantEffectResolveService: chunk offset=%d, pairs=%d, '
                'resolved_so_far=%d',
                offset - len(pairs),
                len(pairs),
                report.n_resolved,
            )

        report.n_pairs_processed = offset
        report.duration_seconds = time.monotonic() - start

        logger.info(
            'VariantEffectResolveService: concluído — n_pairs=%d, '
            'n_resolved=%d, n_activator=%d, n_inactivator=%d, n_neutral=%d, '
            'n_dual_flagged=%d, n_no_gene_role=%d, dry_run=%s, '
            'duration=%.1fs',
            report.n_pairs_processed,
            report.n_resolved,
            report.n_activator,
            report.n_inactivator,
            report.n_neutral,
            report.n_dual_flagged,
            report.n_no_gene_role,
            dry_run,
            report.duration_seconds,
        )

        return report

    # ─── Implementação interna ────────────────────────────────────────────────

    def _load_gene_roles(self) -> dict[str, str]:
        """
        Carrega GeneRole (source='oncokb') em memória.

        Returns:
            Dict {gene_symbol → role}. Genes ausentes no dict → 'unknown'.
        """
        qs = GeneRole.objects.filter(source='oncokb').values('gene_symbol', 'role')
        if self.gene_symbols:
            qs = qs.filter(gene_symbol__in=self.gene_symbols)
        return {row['gene_symbol']: row['role'] for row in qs}

    def _fetch_pairs(self, offset: int, limit: int) -> list[tuple[str, str]]:
        """
        Busca N pares distintos (variant_key, gene_symbol) de VariantEffectRaw.

        Paginação por offset/limit (stateless — idempotente mesmo em re-runs).

        Args:
            offset: Número de pares a pular (para chunks subsequentes).
            limit: Número máximo de pares a retornar.

        Returns:
            Lista de tuplas (variant_key, gene_symbol) ordenadas para estabilidade.
        """
        qs = (
            VariantEffectRaw.objects
            .values('variant_key', 'gene_symbol')
            .distinct()
            .order_by('variant_key', 'gene_symbol')
        )
        if self.gene_symbols:
            qs = qs.filter(gene_symbol__in=self.gene_symbols)

        rows = qs[offset: offset + limit]
        return [(r['variant_key'], r['gene_symbol']) for r in rows]

    def _resolve_chunk(
        self,
        pairs: list[tuple[str, str]],
        gene_roles: dict[str, str],
        report: ResolutionReport,
    ) -> list[VariantEffectResolved]:
        """
        Resolve um chunk de pares (variant_key, gene_symbol).

        Para cada par:
          1. Busca todas as linhas de VariantEffectRaw para o par.
          2. Escolhe a fonte vencedora por precedência (clinvar > am > dbnsfp),
             ignorando ClinVar VUS (que cai para AM/dbNSFP).
          3. Normaliza magnitude (§Tabela G).
          4. Resolve direction via magnitude + papel do gene.

        Args:
            pairs: Lista de (variant_key, gene_symbol) deste chunk.
            gene_roles: Dict pré-carregado de gene_symbol → role.
            report: Relatório acumulado (modificado in-place).

        Returns:
            Lista de VariantEffectResolved para bulk_create (pode ser vazia).
        """
        if not pairs:
            return []

        # Busca todas as anotações para os pares deste chunk em UMA query.
        vkeys = [p[0] for p in pairs]
        gene_syms = [p[1] for p in pairs]

        # Usamos Q(variant_key__in=...) & Q(gene_symbol__in=...) pois os pares
        # são correlacionados — a interseção exata é feita em Python abaixo.
        # Para chunks pequenos (5000 pares) isso é aceitável; a alternativa seria
        # um VALUES() tuple lookup que varia por banco.
        raw_rows = list(
            VariantEffectRaw.objects
            .filter(
                variant_key__in=vkeys,
                gene_symbol__in=gene_syms,
            )
            .values(
                'variant_key',
                'gene_symbol',
                'source',
                'raw_magnitude',
                'raw_class',
                'clinvar_significance',
                'oncogenicity',
                'am_pathogenicity',
                'confidence',
            )
        )

        # Indexa por (variant_key, gene_symbol) → list[row].
        pair_to_rows: dict[tuple[str, str], list[dict]] = {}
        for row in raw_rows:
            key = (row['variant_key'], row['gene_symbol'])
            pair_to_rows.setdefault(key, []).append(row)

        resolved_objects: list[VariantEffectResolved] = []

        pair_set = set(pairs)
        for vkey, gene_sym in pairs:
            key = (vkey, gene_sym)
            if key not in pair_set:
                continue  # segurança — evita pares espúrios do IN

            rows = pair_to_rows.get(key, [])
            if not rows:
                continue

            result = self._resolve_pair(vkey, gene_sym, rows, gene_roles, report)
            if result is not None:
                resolved_objects.append(result)

        return resolved_objects

    def _resolve_pair(
        self,
        variant_key: str,
        gene_symbol: str,
        rows: list[dict],
        gene_roles: dict[str, str],
        report: ResolutionReport,
    ) -> VariantEffectResolved | None:
        """
        Resolve UM par (variant_key, gene_symbol) aplicando precedência + §Tabela G.

        Fluxo:
          1. Ordena as linhas por precedência (clinvar=1 < am=2 < dbnsfp=3).
          2. Tenta ClinVar primeiro: se existe e NÃO é VUS/conflitante, usa.
             Se ClinVar VUS, marca n_clinvar_vus_fell_to_am e cai para AM.
          3. Tenta AlphaMissense: usa am_pathogenicity diretamente.
          4. Tenta dbNSFP: usa raw_magnitude (v1: pronto mas vazio).
          5. Se nenhuma fonte resolveu magnitude → skip (n_no_magnitude++).
          6. Resolve direction via magnitude + gene_role.

        Returns:
            VariantEffectResolved pronto para bulk_create, ou None se não
            foi possível resolver a magnitude.
        """
        # Organiza por fonte
        by_source: dict[str, dict] = {}
        for row in rows:
            src = row['source']
            # Guarda a primeira linha por fonte (pode haver duplicatas de raw_class)
            if src not in by_source:
                by_source[src] = row

        # ── Passo 1: ClinVar (precedência 1) ─────────────────────────────────
        magnitude: float | None = None
        confidence: float = 0.0
        effect_class: str = 'uncertain'
        winning_source: str | None = None
        clinvar_vus = False

        if VariantEffectRaw.Source.CLINVAR in by_source:
            cv = by_source[VariantEffectRaw.Source.CLINVAR]
            mag, ec, is_vus = _resolve_clinvar_magnitude(cv)
            if is_vus:
                clinvar_vus = True
                report.n_clinvar_vus_fell_to_am += 1
            elif mag is not None:
                magnitude = mag
                effect_class = ec
                confidence = _SOURCE_CONFIDENCE[VariantEffectRaw.Source.CLINVAR]
                winning_source = VariantEffectRaw.Source.CLINVAR

        # ── Passo 2: AlphaMissense (precedência 2) ────────────────────────────
        if magnitude is None and VariantEffectRaw.Source.ALPHAMISSENSE in by_source:
            am = by_source[VariantEffectRaw.Source.ALPHAMISSENSE]
            am_path = am.get('am_pathogenicity')
            if am_path is None:
                am_path = am.get('raw_magnitude')
            if am_path is not None:
                magnitude = float(am_path)
                # Clamp para [0,1] por segurança
                magnitude = max(0.0, min(1.0, magnitude))
                effect_class = _am_magnitude_to_class(magnitude)
                confidence = _SOURCE_CONFIDENCE[VariantEffectRaw.Source.ALPHAMISSENSE]
                winning_source = VariantEffectRaw.Source.ALPHAMISSENSE

        # ── Passo 3: dbNSFP (precedência 3, v1 vazio mas pronto) ─────────────
        if magnitude is None and VariantEffectRaw.Source.DBNSFP in by_source:
            db = by_source[VariantEffectRaw.Source.DBNSFP]
            raw_mag = db.get('raw_magnitude')
            if raw_mag is not None:
                magnitude = float(raw_mag)
                magnitude = max(0.0, min(1.0, magnitude))
                effect_class = _am_magnitude_to_class(magnitude)
                confidence = _SOURCE_CONFIDENCE[VariantEffectRaw.Source.DBNSFP]
                winning_source = VariantEffectRaw.Source.DBNSFP

        # ── Sem magnitude resolvível → skip ───────────────────────────────────
        if magnitude is None or winning_source is None:
            report.n_no_magnitude += 1
            logger.debug(
                'VariantEffectResolveService: sem magnitude para '
                '(%s, %s) — clinvar_vus=%s, fontes=%s',
                variant_key,
                gene_symbol,
                clinvar_vus,
                list(by_source.keys()),
            )
            return None

        # ── Passo 4: resolução de direção (magnitude + papel do gene) ─────────
        gene_role = gene_roles.get(gene_symbol, GeneRole.Role.UNKNOWN)
        direction, dual_flagged = _resolve_direction(magnitude, gene_role)

        # Atualiza contadores do relatório
        if direction == VariantEffectResolved.Direction.ACTIVATOR:
            report.n_activator += 1
        elif direction == VariantEffectResolved.Direction.INACTIVATOR:
            report.n_inactivator += 1
        else:
            report.n_neutral += 1

        if dual_flagged:
            report.n_dual_flagged += 1

        if gene_role in (GeneRole.Role.NEITHER, GeneRole.Role.UNKNOWN) or gene_symbol not in gene_roles:
            report.n_no_gene_role += 1

        # Atualiza distribuição por fonte
        report.n_by_source[winning_source] = report.n_by_source.get(winning_source, 0) + 1
        report.n_resolved += 1

        return VariantEffectResolved(
            variant_key=variant_key,
            gene_symbol=gene_symbol,
            magnitude=magnitude,
            effect_class=effect_class,
            direction=direction,
            confidence=confidence,
            effect_source=winning_source,
            gene_role_used=gene_role,
            resolution_version=self.resolution_version,
        )

    def _persist_chunk(self, objects: list[VariantEffectResolved]) -> None:
        """
        Persiste um chunk de VariantEffectResolved via bulk_create com upsert.

        Usa update_conflicts=True na natural key (variant_key, gene_symbol,
        resolution_version) — re-rodar atualiza os campos vencedores sem
        duplicar. Transação atômica por chunk.

        Args:
            objects: Lista de VariantEffectResolved prontos para gravação.
        """
        if not objects:
            return

        with transaction.atomic():
            VariantEffectResolved.objects.bulk_create(
                objects,
                update_conflicts=True,
                unique_fields=['variant_key', 'gene_symbol', 'resolution_version'],
                update_fields=[
                    'magnitude',
                    'effect_class',
                    'direction',
                    'confidence',
                    'effect_source',
                    'gene_role_used',
                    'resolved_at',
                ],
            )

        logger.debug(
            'VariantEffectResolveService: chunk persistido (%d objetos)',
            len(objects),
        )


# ─── Helpers de módulo ────────────────────────────────────────────────────────

def _resolve_clinvar_magnitude(
    row: dict,
) -> tuple[float | None, str, bool]:
    """
    Resolve magnitude de uma linha ClinVar aplicando §Tabela G.

    Precedência de campos ClinVar:
      1. `oncogenicity` (ClinVar somático — campo dedicado; substitui significance)
      2. `clinvar_significance` (ClinVar germinativo — classificação clínica)

    Campos raw_class / raw_magnitude do ClinVar são NULL (ClinVar é categórico).

    Returns:
        (magnitude, effect_class, is_vus)
        - magnitude: float [0,1] ou None se VUS/conflitante.
        - effect_class: 'damaging'/'benign'/'uncertain'
        - is_vus: True se a significância é VUS/conflitante (cai p/ próxima fonte).
    """
    # 1. Tentar oncogenicity (ClinVar somático)
    oncogenicity = (row.get('oncogenicity') or '').strip().lower()
    if oncogenicity:
        entry = _ONCOGENICITY_TO_MAGNITUDE.get(oncogenicity)
        if entry is not None:
            return entry[0], entry[1], False
        # Oncogenicity presente mas não mapeado → trata como VUS
        logger.debug(
            'ClinVar oncogenicity não mapeada: %r — tratando como VUS',
            oncogenicity,
        )
        return None, 'uncertain', True

    # 2. Tentar clinvar_significance (ClinVar germinativo)
    sig = (row.get('clinvar_significance') or '').strip().lower()
    if not sig:
        # Sem significância → VUS
        return None, 'uncertain', True

    # Verificar se é VUS / conflitante
    for vus_pattern in _CLINVAR_VUS_PATTERNS:
        if vus_pattern in sig:
            return None, 'uncertain', True

    # Busca correspondência nas âncoras (match parcial para tolerar texto extra)
    for key, value in _CLINVAR_SIGNIFICANCE_TO_MAGNITUDE.items():
        if key in sig:
            if value is None:
                # Mapeamento explícito → conflitante/VUS
                return None, 'uncertain', True
            return value[0], value[1], False

    # Significância presente mas não mapeada
    logger.debug(
        'ClinVar significance não mapeada: %r — tratando como VUS',
        sig,
    )
    return None, 'uncertain', True


def _am_magnitude_to_class(magnitude: float) -> str:
    """
    Converte magnitude AlphaMissense/dbNSFP para effect_class textual.

    Limiar §Tabela G (v1):
      magnitude >= 0.8  → 'damaging'   (likely pathogenic/pathogenic por AM)
      0.3 < magnitude < 0.8 → 'uncertain'
      magnitude <= 0.3  → 'benign'
    """
    if magnitude >= 0.8:
        return 'damaging'
    if magnitude <= 0.3:
        return 'benign'
    return 'uncertain'


def _resolve_direction(
    magnitude: float,
    gene_role: str,
) -> tuple[str, bool]:
    """
    Resolve a direção do efeito por magnitude + papel do gene.

    Regras (§2.5 do plano, Decisão J — travadas):
      - benigno (magnitude < DAMAGING_THRESHOLD) → neutral (sem importa o papel)
      - danoso + tsg                → inactivator
      - danoso + oncogene           → activator [heurística v1: missense danoso
                                                  em oncogene assume GoF; refinar
                                                  via OncoKB mutation effect (K)]
      - danoso + oncogene_and_tsg   → neutral + dual_flag=True  (Decisão J)
      - danoso + neither/unknown    → neutral

    Args:
        magnitude: Magnitude resolvida [0,1].
        gene_role: Papel do gene (GeneRole.Role ou string equivalente).

    Returns:
        (direction, dual_flagged)
        - direction: 'activator'/'inactivator'/'neutral'
        - dual_flagged: True se role=oncogene_and_tsg (ambiguidade flagrada)
    """
    # Variante benigna/tolerada → sempre neutral (independente do papel)
    if magnitude < DAMAGING_THRESHOLD:
        return VariantEffectResolved.Direction.NEUTRAL, False

    # Variante danosa — resolve pelo papel do gene
    if gene_role == GeneRole.Role.TSG:
        return VariantEffectResolved.Direction.INACTIVATOR, False

    if gene_role == GeneRole.Role.ONCOGENE:
        return VariantEffectResolved.Direction.ACTIVATOR, False

    if gene_role == GeneRole.Role.ONCOGENE_AND_TSG:
        # Decisão J: papel ambíguo → neutral + flag de revisão
        return VariantEffectResolved.Direction.NEUTRAL, True

    # neither / unknown / ausente → neutral
    return VariantEffectResolved.Direction.NEUTRAL, False
