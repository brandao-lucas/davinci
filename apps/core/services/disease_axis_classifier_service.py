"""
DiseaseAxisClassifierService — Classificador de `disease_axis` + sinal `monogenic_gene_hit`.

OmnisPathway Fase 3 — núcleo do classificador Django-only, sem Rust, sem endpoint,
sem nova dependência pip.

=================================================================================
PARTE 1 — Classificador disease_axis
=================================================================================

Texto de doença do dataset (por prioridade decrescente):

  A) PRIDE: extra_metadata['contract']['disease_raw']  (lista de strings)
  B) MeSH terms dos papers ligados via DatasetPaperLink → Paper → PaperMeSHTerm
     (only MeSH major topics e disease-relevant descriptors)
  C) Fallback: title + summary  (menor confiança — reflete no score)

Estratégia de matching:

  1. Normaliza cada candidato com normalize_trait_name() (mesma função das referências).
  2. Exact match primeiro (busca indexada em (axis, name_normalized)).
  3. Fuzzy leve só quando sem exact hit — difflib.SequenceMatcher com blocking:
       - Seleciona candidatos que compartilham ≥1 token raro (len>=5) via
         name_normalized__icontains do token menos frequente.
       - Limiar ALTO para promover 'monogenic'  (FUZZY_MONO_THRESHOLD=0.82).
       - Limiar médio para 'multifactorial' (FUZZY_MULTI_THRESHOLD=0.70).
       - Sem exact e sem fuzzy forte → indeterminate.

Decisão final:

  - Só âncora monogênica → 'monogenic'
  - Só multifatorial → 'multifactorial'
  - Ambas → 'mixed'
  - Nenhuma → 'indeterminate'

Gravação:

  - OmicDataset.disease_axis via update_fields.
  - contract_confidence['disease_axis'] via read-modify-write (não apaga outras chaves).

=================================================================================
PARTE 2 — Sinal monogenic_gene_hit
=================================================================================

  - Cruza DatasetGene.gene_symbol × MendelianGene.gene_symbol (igualdade exata,
    uppercase — índice B-tree, sem fuzzy).
  - Resultado em extra_metadata['contract']['monogenic_gene_hit'] via
    read-modify-write (nunca substitui o dict inteiro).

Fórmula de confiança do gene_hit:

  Para cada gene que bate:

    score_fonte = 1.0 se gene visto nas DUAS fontes (paper_link E dataset_metadata)
                  0.6 se visto em uma só fonte

    score_confidence = {
        'definitive':   1.0,   # G2P — maior evidência experimental
        'strong':       0.85,
        'assessed':     0.80,  # Orphanet c/ SourceOfValidation (PMID)
        'moderate':     0.65,
        'limited':      0.40,
        'not_assessed': 0.30,  # Orphanet s/ validação
        'unknown':      0.20,
    }

    gene_score = score_fonte * score_confidence

  Confiança final do hit = max(gene_score) sobre todos os genes que batem.
  Racional: tomamos o pico, não a média — um único gene definitivo-definitive
  e visto nas duas fontes vale o hit; uma coleção de genes 'limited' não deve
  inflar artificialmente.

=================================================================================
AVISO ANTI-CLOBBER (R4)
=================================================================================

  `monogenic_gene_hit` vive em extra_metadata['contract']['monogenic_gene_hit'].
  O COPY writer (copy_writer.rs) usa `||` (jsonb merge, RHS vence) para atualizar
  extra_metadata. Se um conector re-ingerir um dataset e reenviar a chave 'contract'
  com todos os campos do parser (disease_raw, tissue_raw, etc.), os campos
  adicionados por este service (monogenic_gene_hit) SERÃO APAGADOS porque o RHS
  (incoming) venceria.

  Mitigação atual: este service usa read-modify-write via ORM (não COPY), então
  a gravação inicial é segura. O risco é na RE-INGESTÃO futura do conector. A
  sentinela deve testar o cenário: ingerir → classificar → re-ingerir → checar
  que monogenic_gene_hit sobrevive (ou não — registrar a perda como comportamento
  documentado se o COPY writer não for alterado).

  Para que o campo sobreviva, o copy_writer.rs precisaria fazer
  jsonb_set(extra_metadata, '{contract}', existing_contract || incoming_contract)
  em vez de extra_metadata || incoming (Rust intocado nesta tarefa).

=================================================================================
Idempotência
=================================================================================

  Re-rodar reclassifica e sobrescreve. Não há efeito colateral espúrio além de
  update_fields nas colunas/JSON. Um dataset sem DatasetGene populado terá
  monogenic_gene_hit ausente/vazio — rode populate_dataset_genes antes se necessário.
"""

import difflib
import logging
from collections import defaultdict
from typing import NamedTuple

from django.db import transaction

from apps.core.services.disease_axis_service import normalize_trait_name

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Limiares fuzzy — justificativa precisão-first
# ---------------------------------------------------------------------------
#
# Monogênico (FUZZY_MONO_THRESHOLD = 0.82):
#   Precisão é o objetivo central. Um falso positivo 'monogenic' é mais danoso
#   do que perder alguns verdadeiros positivos — o pesquisador vai confiar nesse
#   label para priorização clínica. SequenceMatcher >= 0.82 elimina matches
#   parciais como "Basal ganglia calcification, idiopathic" vs.
#   "Basal ganglia calcification" (score ~0.78) mas captura grafias ligeiramente
#   diferentes do mesmo nome ("Bardet-Biedl syndrome" vs "Bardet Biedl syndrome").
#
# Multifatorial (FUZZY_MULTI_THRESHOLD = 0.70):
#   Mais permissivo — um erro de label multifatorial é menos grave (vai para
#   'mixed' ou 'multifactorial'; o pesquisador curará se quiser). Captura
#   sinônimos como "Type 2 diabetes mellitus" vs "Diabetes mellitus, type 2".
#
# Token mínimo para blocking (TOKEN_MIN_LEN = 5):
#   Tokens curtos ('the', 'and', 'type', 'gene') são tão frequentes no corpus
#   que o blocking não seria eficaz. 5 chars filtra esses ruídos e ainda captura
#   termos diagnósticos como 'ataxia', 'fever', etc.
#
# Max candidates por fuzzy pass (FUZZY_MAX_CANDIDATES = 200):
#   Limita o produto da busca icontains antes de rodar SequenceMatcher.
#   Garante O(candidatos) << O(104k) para cada termo candidato.

FUZZY_MONO_THRESHOLD = 0.82
FUZZY_MULTI_THRESHOLD = 0.70
TOKEN_MIN_LEN = 5
FUZZY_MAX_CANDIDATES = 200

# ---------------------------------------------------------------------------
# Pesos de confiança do MendelianGene
# ---------------------------------------------------------------------------

_CONFIDENCE_WEIGHT: dict[str, float] = {
    'definitive':   1.00,
    'strong':       0.85,
    'assessed':     0.80,
    'moderate':     0.65,
    'limited':      0.40,
    'not_assessed': 0.30,
    'unknown':      0.20,
}

# Bônus de fonte: gene visto nas DUAS fontes vale mais
_SCORE_BOTH_SOURCES = 1.0
_SCORE_ONE_SOURCE = 0.6

# ---------------------------------------------------------------------------
# Tipos auxiliares
# ---------------------------------------------------------------------------


class AxisHits(NamedTuple):
    """Resultado do matching de um único termo candidato."""
    monogenic_score: float        # 0 = nenhum hit; >0 = melhor score monogênico
    multifactorial_score: float   # idem para multifatorial
    mono_method: str              # 'exact' | 'fuzzy' | ''
    multi_method: str             # 'exact' | 'fuzzy' | ''
    mono_matched: str             # nome da âncora que bateu (para auditoria)
    multi_matched: str            # idem
    mono_source: str              # fonte da âncora (orphanet / g2p)
    multi_source: str             # fonte da âncora (gwas_catalog / ...)


# ---------------------------------------------------------------------------
# Helpers internos de matching
# ---------------------------------------------------------------------------

def _exact_hit(axis: str, norm_term: str) -> tuple[str, str] | None:
    """
    Busca exact match em DiseaseAxisReference indexado por (axis, name_normalized).
    Retorna (name, source) do primeiro hit ou None.
    """
    from apps.core.models import DiseaseAxisReference
    ref = (
        DiseaseAxisReference.objects
        .filter(axis=axis, name_normalized=norm_term)
        .values('name', 'source')
        .first()
    )
    if ref:
        return ref['name'], ref['source']
    return None


def _fuzzy_hit(axis: str, norm_term: str, threshold: float) -> tuple[str, str, float] | None:
    """
    Fuzzy match com blocking por token.

    Estratégia:
      1. Extrai tokens do norm_term com len >= TOKEN_MIN_LEN.
      2. Para o token menos frequente (menor count no eixo), busca candidatos
         via name_normalized__icontains — no máximo FUZZY_MAX_CANDIDATES.
      3. Roda SequenceMatcher sobre cada candidato.
      4. Retorna (name, source, score) do melhor >= threshold, ou None.

    Não faz produto cartesiano sobre 104k registros.
    """
    from apps.core.models import DiseaseAxisReference

    tokens = [t for t in norm_term.split() if len(t) >= TOKEN_MIN_LEN]
    if not tokens:
        return None

    # Selecionar o token com menor frequência no eixo (blocking mais seletivo)
    # Para não fazer N queries, buscamos com o menor dos primeiros 3 tokens por heurística
    # (tokens mais raros tendem a ser específicos de doença, não prefixos genéricos)
    best_token = None
    best_count = None
    for token in tokens[:3]:
        count = DiseaseAxisReference.objects.filter(
            axis=axis, name_normalized__icontains=token
        ).count()
        if best_count is None or count < best_count:
            best_count = count
            best_token = token

    if not best_token or best_count == 0:
        return None

    candidates = list(
        DiseaseAxisReference.objects
        .filter(axis=axis, name_normalized__icontains=best_token)
        .values('name_normalized', 'name', 'source')
        [:FUZZY_MAX_CANDIDATES]
    )

    best_score = 0.0
    best_name = ''
    best_source = ''

    for cand in candidates:
        ratio = difflib.SequenceMatcher(
            None, norm_term, cand['name_normalized'], autojunk=False
        ).ratio()
        if ratio > best_score:
            best_score = ratio
            best_name = cand['name']
            best_source = cand['source']

    if best_score >= threshold:
        return best_name, best_source, best_score

    return None


def _match_term(norm_term: str) -> AxisHits:
    """
    Tenta exact + fuzzy para ambos os eixos dado um termo normalizado.
    Retorna AxisHits com scores e metadados para auditoria.
    """
    mono_score = 0.0
    multi_score = 0.0
    mono_method = ''
    multi_method = ''
    mono_matched = ''
    multi_matched = ''
    mono_source = ''
    multi_source = ''

    # --- Monogênico ---
    hit = _exact_hit('monogenic', norm_term)
    if hit:
        mono_score = 1.0
        mono_method = 'exact'
        mono_matched, mono_source = hit
    else:
        fhit = _fuzzy_hit('monogenic', norm_term, FUZZY_MONO_THRESHOLD)
        if fhit:
            mono_matched, mono_source, mono_score = fhit
            mono_method = 'fuzzy'

    # --- Multifatorial ---
    hit = _exact_hit('multifactorial', norm_term)
    if hit:
        multi_score = 1.0
        multi_method = 'exact'
        multi_matched, multi_source = hit
    else:
        fhit = _fuzzy_hit('multifactorial', norm_term, FUZZY_MULTI_THRESHOLD)
        if fhit:
            multi_matched, multi_source, multi_score = fhit
            multi_method = 'fuzzy'

    return AxisHits(
        monogenic_score=mono_score,
        multifactorial_score=multi_score,
        mono_method=mono_method,
        multi_method=multi_method,
        mono_matched=mono_matched,
        multi_matched=multi_matched,
        mono_source=mono_source,
        multi_source=multi_source,
    )


# ---------------------------------------------------------------------------
# Coleta de termos candidatos do dataset
# ---------------------------------------------------------------------------

def _collect_disease_candidates(dataset) -> list[tuple[str, str]]:
    """
    Monta a lista de (texto_candidato, fonte) para matching.

    Ordem e pesos de confiança da fonte:
      A) PRIDE disease_raw  → fonte='pride_disease_raw'
      B) MeSH terms (major topics first, depois minor)
         → fonte='mesh_major' ou 'mesh_minor'
      C) title + summary (fallback fraco) → fonte='title_summary'

    Retorna lista de (candidato_raw, fonte_str). Deduplicado por texto normalizado.
    """
    from apps.core.models import PaperMeSHTerm

    seen_norm: set[str] = set()
    result: list[tuple[str, str]] = []

    def add(text: str, source: str) -> None:
        norm = normalize_trait_name(text)
        if norm and norm not in seen_norm:
            seen_norm.add(norm)
            result.append((text, source))

    extra = dataset.extra_metadata or {}
    contract = extra.get('contract') or {}

    # A) PRIDE disease_raw
    disease_raw = contract.get('disease_raw')
    if disease_raw:
        if isinstance(disease_raw, list):
            for d in disease_raw:
                if d:
                    add(str(d).strip(), 'pride_disease_raw')
        else:
            add(str(disease_raw).strip(), 'pride_disease_raw')

    # B) MeSH terms dos papers ligados — major topics primeiro
    mesh_qs = (
        PaperMeSHTerm.objects
        .filter(paper__datasetpaperlink__dataset=dataset)
        .values('descriptor', 'is_major_topic')
        .distinct()
        .order_by('-is_major_topic', 'descriptor')
    )
    for row in mesh_qs:
        src = 'mesh_major' if row['is_major_topic'] else 'mesh_minor'
        add(row['descriptor'], src)

    # C) Fallback: title e summary (palavras-chave, não o texto inteiro)
    # Para evitar ruído, usamos title como um único candidato e summary APENAS
    # se não houver candidatos melhores já — verificamos ao final.
    if not result:
        if dataset.title:
            add(dataset.title.strip(), 'title_summary')
        if dataset.summary:
            # Tomar só os primeiros 200 chars do summary para evitar ruído
            add(dataset.summary.strip()[:200], 'title_summary')

    return result


# ---------------------------------------------------------------------------
# Classificador principal — disease_axis
# ---------------------------------------------------------------------------

def classify_disease_axis_for_dataset(dataset, *, dry_run: bool = False) -> dict:
    """
    Classifica disease_axis de um OmicDataset.

    Algoritmo:
      1. Coleta candidatos de doença do dataset.
      2. Para cada candidato: exact match → fuzzy (se necessário).
      3. Agrega hits: best mono score, best multi score.
      4. Decide eixo pelos 4 estados.
      5. Computa score geral e método dominante.
      6. Grava disease_axis e contract_confidence['disease_axis'] (se não dry_run).

    Retorna dict com resultado da classificação para logging/relatório.
    """
    candidates = _collect_disease_candidates(dataset)

    best_mono_score = 0.0
    best_multi_score = 0.0
    best_mono_method = ''
    best_multi_method = ''
    best_mono_matched = ''
    best_multi_matched = ''
    best_mono_source = ''
    best_multi_source = ''
    best_mono_candidate = ''
    best_multi_candidate = ''

    for raw_text, cand_source in candidates:
        norm = normalize_trait_name(raw_text)
        if not norm:
            continue

        hits = _match_term(norm)

        if hits.monogenic_score > best_mono_score:
            best_mono_score = hits.monogenic_score
            best_mono_method = hits.mono_method
            best_mono_matched = hits.mono_matched
            best_mono_source = hits.mono_source
            best_mono_candidate = f'{raw_text!r} [{cand_source}]'

        if hits.multifactorial_score > best_multi_score:
            best_multi_score = hits.multifactorial_score
            best_multi_method = hits.multi_method
            best_multi_matched = hits.multi_matched
            best_multi_source = hits.multi_source
            best_multi_candidate = f'{raw_text!r} [{cand_source}]'

    # --- Decisão dos 4 estados ---
    has_mono = best_mono_score > 0.0
    has_multi = best_multi_score > 0.0

    if has_mono and has_multi:
        axis = 'mixed'
    elif has_mono:
        axis = 'monogenic'
    elif has_multi:
        axis = 'multifactorial'
    else:
        axis = 'indeterminate'

    # Score geral: max das melhores pontuações resolvidas
    overall_score = max(best_mono_score, best_multi_score)

    # Método dominante
    if has_mono and best_mono_score >= best_multi_score:
        dominant_method = best_mono_method
    elif has_multi:
        dominant_method = best_multi_method
    else:
        dominant_method = ''

    confidence_entry = {
        'score': round(overall_score, 4),
        'method': dominant_method or 'none',
        'n_candidates': len(candidates),
        'axis_hits': {
            'monogenic': {
                'score': round(best_mono_score, 4),
                'method': best_mono_method,
                'matched_name': best_mono_matched,
                'matched_source': best_mono_source,
                'from_candidate': best_mono_candidate,
            },
            'multifactorial': {
                'score': round(best_multi_score, 4),
                'method': best_multi_method,
                'matched_name': best_multi_matched,
                'matched_source': best_multi_source,
                'from_candidate': best_multi_candidate,
            },
        },
    }

    result = {
        'accession': dataset.accession,
        'axis': axis,
        'score': overall_score,
        'method': dominant_method,
        'n_candidates': len(candidates),
        'mono_matched': best_mono_matched,
        'multi_matched': best_multi_matched,
        'changed': dataset.disease_axis != axis,
        'dry_run': dry_run,
    }

    if dry_run:
        return result

    # --- Gravar via read-modify-write ---
    with transaction.atomic():
        # Re-fetch para o update (evita race condition no JSON)
        from apps.core.models import OmicDataset
        obj = OmicDataset.objects.select_for_update().get(pk=dataset.pk)

        # contract_confidence: read-modify-write para não apagar outras chaves
        cc = obj.contract_confidence or {}
        cc['disease_axis'] = confidence_entry
        obj.contract_confidence = cc
        obj.disease_axis = axis

        obj.save(update_fields=['disease_axis', 'contract_confidence', 'updated_at'])

    # Espelhar no objeto original (para evitar re-fetch no caller)
    dataset.disease_axis = axis
    dataset.contract_confidence = {**(dataset.contract_confidence or {}), 'disease_axis': confidence_entry}

    logger.info(
        'classify_disease_axis: %s → %s (score=%.3f method=%s candidates=%d)',
        dataset.accession, axis, overall_score, dominant_method, len(candidates),
    )

    return result


# ---------------------------------------------------------------------------
# Parte 2 — Sinal monogenic_gene_hit
# ---------------------------------------------------------------------------

def compute_monogenic_gene_hit_for_dataset(dataset, *, dry_run: bool = False) -> dict:
    """
    Computa e grava monogenic_gene_hit em extra_metadata['contract']['monogenic_gene_hit'].

    Cross: DatasetGene.gene_symbol × MendelianGene.gene_symbol (igualdade exata, uppercase).

    Fórmula de confiança (documentada no módulo):
      gene_score = score_fonte * score_confidence
      confiança_final = max(gene_score) sobre todos os genes que batem

      score_fonte:
        BOTH_SOURCES (paper_link E dataset_metadata visto)  → 1.0
        ONE_SOURCE                                           → 0.6

      score_confidence (MendelianGene.confidence):
        definitive   → 1.00
        strong       → 0.85
        assessed     → 0.80  (Orphanet c/ SourceOfValidation)
        moderate     → 0.65
        limited      → 0.40
        not_assessed → 0.30  (Orphanet s/ validação)
        unknown      → 0.20

    Grava SOMENTE se houver genes que batem. Se vazio → remove a chave (idempotente).

    AVISO ANTI-CLOBBER:
        Este campo vive em extra_metadata['contract']['monogenic_gene_hit'] e é
        gravado via read-modify-write pelo ORM. Re-ingestão de um conector que
        reescreva extra_metadata['contract'] integralmente via COPY (jsonb merge
        RHS vence) irá apagar este campo. Ver docstring do módulo.

    Retorna dict com resultado para logging/relatório.
    """
    from apps.core.models import DatasetGene, MendelianGene

    # --- 1. Coletar genes do dataset agrupados por símbolo e fontes ---
    dg_rows = list(
        DatasetGene.objects
        .filter(dataset=dataset)
        .values('gene_symbol', 'source', 'mention_count')
    )

    if not dg_rows:
        return {
            'accession': dataset.accession,
            'genes_found': 0,
            'confidence': 0.0,
            'dry_run': dry_run,
        }

    # Agrupa por símbolo: conjunto de fontes
    genes_by_symbol: dict[str, set[str]] = defaultdict(set)
    for row in dg_rows:
        genes_by_symbol[row['gene_symbol']].add(row['source'])

    dataset_symbols = set(genes_by_symbol.keys())

    # --- 2. Cross com MendelianGene (igualdade exata, index scan) ---
    mendelian_rows = list(
        MendelianGene.objects
        .filter(gene_symbol__in=dataset_symbols)
        .values('gene_symbol', 'confidence', 'source', 'disease_name')
    )

    if not mendelian_rows:
        # Nenhum hit — remover chave se existir
        result = {
            'accession': dataset.accession,
            'genes_found': 0,
            'confidence': 0.0,
            'dry_run': dry_run,
        }
        if not dry_run:
            _write_gene_hit(dataset, None)
        return result

    # --- 3. Calcular score por símbolo ---
    # Para cada símbolo que bate: pegar a MAIOR confiança do MendelianGene
    # (um gene pode estar em orphanet E g2p com confianças diferentes).
    best_conf_by_symbol: dict[str, float] = {}
    best_conf_label_by_symbol: dict[str, str] = {}

    for row in mendelian_rows:
        sym = row['gene_symbol']
        conf_score = _CONFIDENCE_WEIGHT.get(row['confidence'], 0.20)
        if sym not in best_conf_by_symbol or conf_score > best_conf_by_symbol[sym]:
            best_conf_by_symbol[sym] = conf_score
            best_conf_label_by_symbol[sym] = row['confidence']  # label textual, ex: 'definitive'

    # Score composto por símbolo
    gene_scores: list[dict] = []
    max_score = 0.0

    for sym, conf_score in best_conf_by_symbol.items():
        sources = genes_by_symbol[sym]
        fonte_score = _SCORE_BOTH_SOURCES if len(sources) >= 2 else _SCORE_ONE_SOURCE
        gene_score = round(fonte_score * conf_score, 4)
        gene_scores.append({
            'gene_symbol': sym,
            'score': gene_score,
            'sources': sorted(sources),
            'mendelian_confidence': best_conf_label_by_symbol.get(sym, ''),
        })
        if gene_score > max_score:
            max_score = gene_score

    # Ordenar por score desc para facilitar inspeção
    gene_scores.sort(key=lambda x: x['score'], reverse=True)
    hit_symbols = [g['gene_symbol'] for g in gene_scores]

    hit_payload = {
        'genes': hit_symbols,
        'confidence': round(max_score, 4),
        'gene_details': gene_scores,
    }

    result = {
        'accession': dataset.accession,
        'genes_found': len(hit_symbols),
        'confidence': max_score,
        'genes': hit_symbols,
        'dry_run': dry_run,
    }

    if not dry_run:
        _write_gene_hit(dataset, hit_payload)

    logger.info(
        'compute_monogenic_gene_hit: %s → %d genes (confidence=%.3f)',
        dataset.accession, len(hit_symbols), max_score,
    )

    return result


def _write_gene_hit(dataset, payload: dict | None) -> None:
    """
    Grava (ou remove) monogenic_gene_hit em extra_metadata['contract']
    via read-modify-write. Nunca substitui o dict inteiro de extra_metadata.
    """
    from apps.core.models import OmicDataset

    with transaction.atomic():
        obj = OmicDataset.objects.select_for_update().get(pk=dataset.pk)

        extra = obj.extra_metadata or {}
        # read-modify-write em profundidade: cria sub-dicts se necessário
        contract = dict(extra.get('contract') or {})

        if payload is None:
            contract.pop('monogenic_gene_hit', None)
        else:
            contract['monogenic_gene_hit'] = payload

        extra = {**extra, 'contract': contract}
        obj.extra_metadata = extra
        obj.save(update_fields=['extra_metadata', 'updated_at'])

    # Espelhar no objeto original
    dataset.extra_metadata = {**(dataset.extra_metadata or {})}
    if 'contract' not in dataset.extra_metadata:
        dataset.extra_metadata['contract'] = {}
    if payload is None:
        dataset.extra_metadata['contract'].pop('monogenic_gene_hit', None)
    else:
        dataset.extra_metadata['contract']['monogenic_gene_hit'] = payload


# ---------------------------------------------------------------------------
# Parte 3 — Política de fila de alto valor
# ---------------------------------------------------------------------------

def _has_monogenic_gene_hit(extra_metadata: dict | None) -> bool:
    """
    Predicado: retorna True se extra_metadata['contract']['monogenic_gene_hit']
    existe e contém pelo menos um gene.

    Usado tanto pela contagem global quanto pela fila por projeto — ponto único
    de verdade para a política de discordância.
    """
    contract = (extra_metadata or {}).get('contract') or {}
    gene_hit = contract.get('monogenic_gene_hit')
    return bool(gene_hit and gene_hit.get('genes'))


def dataset_high_value_reason(disease_axis: str, extra_metadata: dict | None) -> str | None:
    """
    Retorna o motivo pelo qual um OmicDataset entra na fila de alto valor,
    ou None se não entrar.

    Política (decisão travada, OmnisPathway Fase 3):
      - 'mixed'       → disease_axis == 'mixed'
      - 'discordance' → monogenic_gene_hit presente MAS disease_axis ∉ {monogenic, mixed}
      - None          → não entra (inclui 'indeterminate' puro sem gene hit)

    Mutualmente exclusivo: 'mixed' tem prioridade sobre 'discordance' porque
    disease_axis='mixed' já implica tanto âncora monogênica quanto multifatorial,
    e o predicado de discordância exclui 'mixed' explicitamente.
    """
    if disease_axis == 'mixed':
        return 'mixed'
    if (
        disease_axis not in ('monogenic', 'mixed')
        and _has_monogenic_gene_hit(extra_metadata)
    ):
        return 'discordance'
    return None


def high_value_queue_queryset(project):
    """
    Retorna um queryset de ProjectDataset do projeto que entram na fila de alto
    valor, anotado com o campo sintético `queue_reason` ('mixed' ou 'discordance').

    Usado tanto pelo endpoint DiseaseAxisCurationQueueView quanto por
    compute_high_value_queue_counts() para garantir que a contagem e a listagem
    nunca divirjam — a política vive aqui, em um único lugar.

    Estratégia:
      - Grupo 'mixed': filtro ORM puro (disease_axis='mixed') — eficiente.
      - Grupo 'discordance': scan Python sobre candidatos (não-monogenic/não-mixed),
        coletando IDs. O volume é manejável (são datasets que já passaram pelo
        classificador mas divergem).

    Retorna lista de ProjectDataset (não queryset lazy) porque o grupo discordante
    exige scan Python para identificar IDs antes do fetch final. A lista inclui
    `_queue_reason` como atributo sintético em cada instância para o serializer.

    Nota de performance: select_related('dataset') é aplicado antes da iteração
    para evitar N+1 no acesso a dataset.disease_axis / dataset.extra_metadata.
    """
    from apps.core.models import ProjectDataset

    # Grupo 1 — mixed: puramente ORM
    mixed_pds = list(
        ProjectDataset.objects
        .filter(project=project, dataset__disease_axis='mixed')
        .select_related('dataset')
        .order_by('added_at')
    )
    for pd in mixed_pds:
        pd._queue_reason = 'mixed'

    # Grupo 2 — discordância: candidatos non-monogenic/non-mixed, scan Python
    candidate_pds = list(
        ProjectDataset.objects
        .filter(project=project)
        .exclude(dataset__disease_axis__in=['monogenic', 'mixed'])
        .select_related('dataset')
        .order_by('added_at')
    )
    discordant_pds = []
    for pd in candidate_pds:
        if _has_monogenic_gene_hit(pd.dataset.extra_metadata):
            pd._queue_reason = 'discordance'
            discordant_pds.append(pd)

    # União: mixed primeiro, depois discordantes (ambos ordenados por added_at)
    return mixed_pds + discordant_pds


def compute_high_value_queue_counts() -> dict:
    """
    Computa a contagem dos dois grupos da fila de alto valor (sem endpoint):

      Grupo 1 — disease_axis == 'mixed'
      Grupo 2 — discordância: monogenic_gene_hit presente MAS disease_axis
                NÃO é 'monogenic' NEM 'mixed'

    'indeterminate' puro (sem gene hit) é TERMINAL — não entra.

    Retorna dict com:
        mixed_count          — datasets com disease_axis = 'mixed'
        discordant_count     — datasets com gene_hit MAS disease_axis fora de {monogenic, mixed}
        total_high_value     — soma dos dois (sem dupla contagem)
        details              — lista de exemplos (accession, axis, has_gene_hit)

    Implementação: usa dataset_high_value_reason() para o predicado de discordância,
    garantindo consistência com o endpoint de listagem (high_value_queue_queryset).
    """
    from apps.core.models import OmicDataset

    # Grupo 1: mixed
    mixed_qs = OmicDataset.objects.filter(is_active=True, disease_axis='mixed')
    mixed_count = mixed_qs.count()

    # Grupo 2: discordância — gene_hit presente e axis fora de {monogenic, mixed}
    # Usa o predicado compartilhado dataset_high_value_reason() via
    # _has_monogenic_gene_hit() para garantir consistência com a fila de listagem.
    discordant_ids: list[int] = []
    discordant_examples: list[dict] = []

    candidates_qs = OmicDataset.objects.filter(
        is_active=True,
    ).exclude(
        disease_axis__in=['monogenic', 'mixed']
    ).only('id', 'accession', 'disease_axis', 'extra_metadata')

    for ds in candidates_qs.iterator(chunk_size=500):
        if _has_monogenic_gene_hit(ds.extra_metadata):
            discordant_ids.append(ds.id)
            if len(discordant_examples) < 5:
                contract = (ds.extra_metadata or {}).get('contract') or {}
                gene_hit = contract.get('monogenic_gene_hit', {})
                discordant_examples.append({
                    'accession': ds.accession,
                    'disease_axis': ds.disease_axis,
                    'genes': gene_hit.get('genes', [])[:3],
                    'confidence': gene_hit.get('confidence', 0),
                })

    discordant_count = len(discordant_ids)

    # Sem dupla contagem: mixed e discordante são mutualmente exclusivos
    # (mixed exclui por definição; discordante é disease_axis NOT IN [monogenic, mixed])
    total_high_value = mixed_count + discordant_count

    # Exemplos de mixed
    mixed_examples = []
    for ds in mixed_qs.only('accession', 'disease_axis', 'contract_confidence')[:5]:
        cc = (ds.contract_confidence or {}).get('disease_axis', {})
        mixed_examples.append({
            'accession': ds.accession,
            'disease_axis': ds.disease_axis,
            'mono_matched': (cc.get('axis_hits') or {}).get('monogenic', {}).get('matched_name', ''),
            'multi_matched': (cc.get('axis_hits') or {}).get('multifactorial', {}).get('matched_name', ''),
        })

    return {
        'mixed_count': mixed_count,
        'discordant_count': discordant_count,
        'total_high_value': total_high_value,
        'mixed_examples': mixed_examples,
        'discordant_examples': discordant_examples,
    }


# ---------------------------------------------------------------------------
# Orquestrador em massa
# ---------------------------------------------------------------------------

def classify_all(
    *,
    accessions: list[str] | None = None,
    source_dbs: list[str] | None = None,
    limit: int | None = None,
    batch_size: int = 200,
    dry_run: bool = False,
    skip_gene_hit: bool = False,
) -> dict:
    """
    Classifica disease_axis e computa monogenic_gene_hit para um lote de datasets.

    Parâmetros:
        accessions   — lista de accessions específicos (None = todos).
        source_dbs   — filtro por source_db (None = todos).
        limit        — máximo de datasets (None = sem limite).
        batch_size   — chunk size para iteração.
        dry_run      — não grava nada.
        skip_gene_hit — pula a parte 2 (útil para benchmarking).

    Retorna dict com estatísticas agregadas.
    """
    from apps.core.models import OmicDataset

    qs = OmicDataset.objects.filter(is_active=True).order_by('id')
    if accessions:
        qs = qs.filter(accession__in=accessions)
    if source_dbs:
        qs = qs.filter(source_db__in=source_dbs)
    if limit:
        qs = qs[:limit]

    stats = {
        'processed': 0,
        'errors': 0,
        'axis_distribution': {
            'monogenic': 0,
            'multifactorial': 0,
            'mixed': 0,
            'indeterminate': 0,
        },
        'method_breakdown': {
            'exact_mono': 0,
            'exact_multi': 0,
            'fuzzy_mono': 0,
            'fuzzy_multi': 0,
            'no_match': 0,
        },
        'gene_hit_count': 0,
        'changed_count': 0,
    }

    for dataset in qs.iterator(chunk_size=batch_size):
        try:
            # Parte 1: disease_axis
            axis_result = classify_disease_axis_for_dataset(dataset, dry_run=dry_run)
            axis = axis_result['axis']
            stats['axis_distribution'][axis] = stats['axis_distribution'].get(axis, 0) + 1
            if axis_result.get('changed'):
                stats['changed_count'] += 1

            # Contabilizar método
            method = axis_result.get('method', '')
            mono_matched = bool(axis_result.get('mono_matched'))
            multi_matched = bool(axis_result.get('multi_matched'))

            if mono_matched and method == 'exact':
                stats['method_breakdown']['exact_mono'] += 1
            elif mono_matched and method == 'fuzzy':
                stats['method_breakdown']['fuzzy_mono'] += 1
            if multi_matched and method == 'exact':
                stats['method_breakdown']['exact_multi'] += 1
            elif multi_matched and method == 'fuzzy':
                stats['method_breakdown']['fuzzy_multi'] += 1
            if not mono_matched and not multi_matched:
                stats['method_breakdown']['no_match'] += 1

            # Parte 2: gene_hit
            if not skip_gene_hit:
                gene_result = compute_monogenic_gene_hit_for_dataset(dataset, dry_run=dry_run)
                if gene_result.get('genes_found', 0) > 0:
                    stats['gene_hit_count'] += 1

            stats['processed'] += 1

        except Exception:
            stats['errors'] += 1
            logger.exception(
                'classify_all: erro em dataset=%s', dataset.accession
            )

    return stats
