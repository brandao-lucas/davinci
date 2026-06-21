"""
ContractClassifierService — Classificadores de categorização do contrato OmnisPathway.

Responsabilidades (Fase 2):
  - classify_has_control_group(dataset): keyword/regex → yes/no/unknown + confiança.
  - classify_is_single_cell(dataset): keyword → single_cell/bulk/unknown + confiança.
  - classify_sample_join_key(dataset): normalização PMID/DOI/BioProject → ArrayField + confiança.
  - backfill_legacy_data_format(dataset): data_format para GEO/SRA onde derivável.
  - backfill_legacy_access_type(dataset): access_type para GEO/SRA; SRA sem sinal → 'unknown'.

Decisões de implementação:
  D1 — pyahocorasick NÃO está no venv. Fallback: regex compilados com alternation
       (mesmo padrão de drug_service/_drug_sentence_re). Adequado ao volume de keywords
       do MVP; Aho-Corasick pode ser adicionado na Fase 3 sem alterar interface pública.

  D4 — PRIDE grava sampleProcessingProtocol em OmicDataset.summary (pode ser
       project_description OR sample_processing_protocol — ver pride_parser.rs linha 215).
       O campo extra_metadata['contract'] traz tissue_raw, disease_raw, ref_pmids, ref_dois,
       mas NÃO contém sampleProcessingProtocol como chave direta. Logo, para PRIDE, o texto
       classificável está em dataset.summary + dataset.extra_metadata.get('keywords', []).

Tri-estado semântico:
  - Chave AUSENTE em contract_confidence  → classificador ainda não rodou (não-classificado).
  - Chave PRESENTE com score 0..1         → classificador rodou (classificado ou indeterminado).
  - score >= 0.5                          → valor auto-aceito gravado no campo.
  - score < 0.5                           → campo = 'unknown'; registro entra na fila de curadoria.

Anti-clobber:
  - has_control_group, is_single_cell, sample_join_key estão FORA do SET do COPY
    (copy_writer.rs linhas 156-193) → re-ingestão nunca sobrescreve classificações Django.
  - data_format e access_type estão NO SET com COALESCE/NULLIF: incoming 'unknown' não
    sobrescreve valor já populado pelo backfill.

Escrita sempre via update_fields para não tocar campos não relacionados.
"""

import re
import logging
from typing import Literal

from django.utils import timezone

logger = logging.getLogger(__name__)

# =============================================================================
# Vocabulários (sem migration de dados — constantes no service, padrão drug_service)
# =============================================================================

# --- has_control_group ---
# Padrões positivos: indicam presença de grupo controle
_CONTROL_GROUP_POSITIVE = [
    r'\bhealthy\s+control',
    r'\bnormal\s+tissue',
    r'\bvs\.?\s+control',
    r'\bcase[\s\-]control',
    r'\bcontrol\s+group',
    r'\bhealthy\s+donor',
    r'\bundiseased\s+control',
    r'\bmatched\s+control',
    r'\bundiseased\s+tissue',
    r'\bnormal\s+adjacent',
    r'\bcontrol\s+sample',
    r'\bbaseline\s+sample',
    r'\buntreated\s+control',
    r'\bwild[- ]type\s+control',
    r'\bhealthy\s+volunteer',
    r'\bnon[\s\-]disease',
    r'\bno[\s\-]disease',
    r'\bdisease[\s\-]free',
    r'\bnormal\s+control',
    r'\bcontrol\s+cohort',
    r'\bcase\s+vs\b',
    r'\bpatient\s+vs\.',
    r'\bcompared\s+to\s+(healthy|normal|control)',
]

# Padrões negativos: "control" em contextos que NÃO indicam grupo controle biológico
# (ex.: "quality control", "positive control", "control region", "process control")
_CONTROL_GROUP_NEGATIVE = [
    r'\bquality\s+control',
    r'\bpositive\s+control',
    r'\bnegative\s+control',
    r'\binternal\s+control',
    r'\bprocess\s+control',
    r'\bcontrol\s+region',
    r'\bcontrol\s+element',
    r'\bvehicle\s+control',
    r'\bisotype\s+control',
]

# --- is_single_cell ---
# Co-ocorrência: "10x" só conta se acompanhado de termo single-cell
_SINGLE_CELL_POSITIVE = [
    r'\bscRNA[\s\-]?seq\b',
    r'\bsingle[\s\-]cell\b',
    r'\b10x\s+chromium\b',
    r'\bChromium\s+10x\b',
    r'\bSmart[\s\-]?seq\b',
    r'\bDrop[\s\-]?seq\b',
    r'\bSEQ[\s\-]?Well\b',
    r'\bCEL[\s\-]?Seq\b',
    r'\bSingle[\s\-]?Cell\s+Proteomics\b',
    r'\bSCP\b(?=.*(?:single[\s\-]?cell|proteom))',  # SCP + contexto
    r'\bscProteomics\b',
    r'\bsingle[\s\-]cell\s+sequencing\b',
    r'\bsingle[\s\-]cell\s+RNA\b',
    r'\bsingle[\s\-]cell\s+ATAC\b',
    r'\bsingle[\s\-]cell\s+multi',
    r'\bspatial\s+transcriptomics?\b',
    r'\bspatial\s+single[\s\-]cell\b',
]

_BULK_POSITIVE = [
    r'\bbulk\s+RNA[\s\-]?seq\b',
    r'\bbulk\s+sequencing\b',
    r'\bbulk\s+transcriptom',
    r'\bmicroarray\b',
    r'\bRNA\s+microarray\b',
]

# --- data_format backfill ---
_RAW_FILE_TYPES = {'fastq', 'sra'}  # DatasetFile.FileType values que indicam raw
_PROCESSED_FILE_TYPES = {'series_matrix', 'supplementary', 'cel'}

# =============================================================================
# Compilação de padrões (feita UMA vez no import, não por chamada)
# =============================================================================

_RE_FLAGS = re.IGNORECASE | re.MULTILINE

_re_control_positive = re.compile(
    '|'.join(_CONTROL_GROUP_POSITIVE), _RE_FLAGS
)
_re_control_negative = re.compile(
    '|'.join(_CONTROL_GROUP_NEGATIVE), _RE_FLAGS
)
_re_single_cell = re.compile(
    '|'.join(_SINGLE_CELL_POSITIVE), _RE_FLAGS
)
_re_bulk = re.compile(
    '|'.join(_BULK_POSITIVE), _RE_FLAGS
)

# PMID: número puro de 7-8 dígitos
_re_pmid_plain = re.compile(r'\b(\d{7,8})\b')
# DOI canônico
_re_doi = re.compile(r'\b(10\.\d{4,9}/[^\s"<>]+)', re.IGNORECASE)


# =============================================================================
# Helpers internos
# =============================================================================

def _aggregate_text_for_dataset(dataset) -> str:
    """
    Agrega campos de texto relevantes de um OmicDataset para classificação.

    Para PRIDE: summary (que pode ser project_description ou sample_processing_protocol),
    mais keywords do extra_metadata.
    Para GEO/SRA: summary + extra_metadata.get('overall_design', '').

    NÃO faz prefetch de OmicSample.characteristics aqui (custo O(N)).
    O classificador trabalha no nível de dataset, não de amostra.
    """
    parts = []

    if dataset.title:
        parts.append(dataset.title)

    if dataset.summary:
        parts.append(dataset.summary)

    em = dataset.extra_metadata or {}

    # GEO/SRA: campos de design no extra_metadata
    overall_design = em.get('overall_design', '') or em.get('design', '')
    if overall_design:
        parts.append(overall_design)

    # PRIDE: keywords list
    keywords = em.get('keywords', [])
    if isinstance(keywords, list):
        parts.append(' '.join(str(k) for k in keywords))

    # omic_subcategory pode trazer "RNA-Seq", "single-cell RNA-Seq" etc.
    if dataset.omic_subcategory:
        parts.append(dataset.omic_subcategory)

    # platform (ex: "Illumina HiSeq 2500")
    if dataset.platform:
        parts.append(dataset.platform)

    return ' '.join(parts)


def _save_classification(dataset, fields: dict, confidence_updates: dict) -> None:
    """
    Persiste campos do dataset e atualiza contract_confidence atomicamente.

    Usa update_fields para não tocar campos não relacionados.
    contract_confidence é mesclado (não substituído) para preservar escores
    de eixos classificados anteriormente.
    """
    # Mesclar confiança: preservar chaves de outros eixos
    current_confidence = dataset.contract_confidence or {}
    updated_confidence = {**current_confidence, **confidence_updates}

    # Aplicar campos + confiança no objeto
    for field, value in fields.items():
        setattr(dataset, field, value)
    dataset.contract_confidence = updated_confidence
    dataset.updated_at = timezone.now()

    update_fields = list(fields.keys()) + ['contract_confidence', 'updated_at']
    dataset.save(update_fields=update_fields)


# =============================================================================
# Passo 1 — has_control_group
# =============================================================================

def classify_has_control_group(dataset) -> dict:
    """
    Classifica has_control_group via keyword/regex sobre texto agregado do dataset.

    Saída:
      {'value': 'yes'|'no'|'unknown', 'score': float, 'classified': bool}

    Lógica:
      - Conta matches positivos e negativos no texto agregado.
      - Penaliza negativos (reduz confiança bruta).
      - Score final = matches_positivos_líquidos / (total_positivos + total_negativos + 1)
      - score >= 0.5 → valor aceito automaticamente como 'yes'; no sem matches = 'no'.
      - score < 0.5 → campo permanece 'unknown'; chave gravada em contract_confidence
        (classificado-indeterminado → fila de curadoria).
      - Se nenhuma keyword foi encontrada: valor = 'unknown', score gravado como 0.0.

    Grava via update_fields=['has_control_group', 'contract_confidence', 'updated_at'].
    """
    text = _aggregate_text_for_dataset(dataset)

    pos_matches = _re_control_positive.findall(text)
    neg_matches = _re_control_negative.findall(text)

    n_pos = len(pos_matches)
    n_neg = len(neg_matches)
    n_total = n_pos + n_neg

    if n_total == 0:
        # Nenhum sinal encontrado — classificado-indeterminado com score 0
        score = 0.0
        value = 'unknown'
    else:
        # Score: proporção de positivos líquidos (penaliza negativos)
        net_positive = max(0, n_pos - n_neg)
        score = net_positive / (n_total + 1)  # +1 suaviza denominador

        if score >= 0.5:
            value = 'yes'
        elif n_pos == 0 and n_neg > 0:
            # Só negativos → provavelmente não tem grupo controle
            # Mas com score baixo → unknown (não é evidência forte de ausência)
            value = 'unknown'
        else:
            value = 'unknown'

    result = {'value': value, 'score': round(score, 4), 'classified': score >= 0.5}

    _save_classification(
        dataset,
        fields={'has_control_group': value},
        confidence_updates={'has_control_group': round(score, 4)},
    )

    logger.info(
        'classify_has_control_group: dataset=%s value=%s score=%.4f pos=%d neg=%d',
        dataset.accession, value, score, n_pos, n_neg,
    )
    return result


# =============================================================================
# Passo 2 — is_single_cell
# =============================================================================

def classify_is_single_cell(dataset) -> dict:
    """
    Classifica is_single_cell via keyword sobre texto agregado do dataset.

    Saída:
      {'value': 'single_cell'|'bulk'|'unknown', 'score': float}

    Lógica:
      - Match de keyword single-cell → score alto (0.9).
      - Match de keyword bulk explícito (sem single-cell) → 'bulk', score 0.75.
      - Nenhum sinal → 'unknown', score gravado como 0.0 (ausência NÃO prova bulk).

    Grava via update_fields=['is_single_cell', 'contract_confidence', 'updated_at'].
    """
    text = _aggregate_text_for_dataset(dataset)

    sc_matches = _re_single_cell.findall(text)
    bulk_matches = _re_bulk.findall(text)

    n_sc = len(sc_matches)
    n_bulk = len(bulk_matches)

    if n_sc > 0:
        # Single-cell tem prioridade se presente com ou sem bulk
        value = 'single_cell'
        score = min(0.9 + 0.02 * (n_sc - 1), 1.0)  # escala suave com número de matches
    elif n_bulk > 0:
        # Bulk explícito, sem sinal single-cell
        value = 'bulk'
        score = min(0.75 + 0.05 * (n_bulk - 1), 0.95)
    else:
        # Sem sinal — default unknown (ausência não é evidência de bulk)
        value = 'unknown'
        score = 0.0

    _save_classification(
        dataset,
        fields={'is_single_cell': value},
        confidence_updates={'is_single_cell': round(score, 4)},
    )

    logger.info(
        'classify_is_single_cell: dataset=%s value=%s score=%.4f sc_matches=%d bulk_matches=%d',
        dataset.accession, value, round(score, 4), n_sc, n_bulk,
    )
    return {'value': value, 'score': round(score, 4)}


# =============================================================================
# Passo 3 — sample_join_key
# =============================================================================

def _normalize_pmid(raw: str | int) -> str | None:
    """Normaliza PMID para 'pmid:NNN'. Retorna None se inválido."""
    try:
        pmid = int(str(raw).strip())
        if pmid > 0:
            return f'pmid:{pmid}'
    except (ValueError, TypeError):
        pass
    return None


def _normalize_doi(raw: str) -> str | None:
    """Normaliza DOI para 'doi:10.XXXX/...'. Retorna None se inválido."""
    raw = str(raw).strip()
    m = _re_doi.search(raw)
    if m:
        return f'doi:{m.group(1).rstrip(".,;)")}'
    return None


def _normalize_bioproject(raw: str) -> str | None:
    """Normaliza BioProject ID para 'bioproject:PRJXXX'. Retorna None se inválido."""
    raw = str(raw).strip()
    if re.match(r'^PRJ[A-Z]{2}\d+$', raw, re.IGNORECASE):
        return f'bioproject:{raw.upper()}'
    return None


def classify_sample_join_key(dataset) -> dict:
    """
    Constrói sample_join_key (nível-estudo, não amostra-a-amostra) a partir de:
      - bioproject_id (coluna direta) → sinal mais forte (≈0.9)
      - extra_metadata['contract']['ref_pmids'] (PRIDE) → PMID (≈0.7)
      - extra_metadata['contract']['ref_dois'] (PRIDE) → DOI (≈0.5)
      - DatasetPaperLink (GEO/SRA via elink) → PMID (≈0.7)
      - DatasetPaperLinkPending (staging) → PMID (≈0.65)

    Saída: {'keys': [str, ...], 'score': float}
    Confiança reflete o sinal mais forte encontrado (BioProject > PMID > DOI).

    Grava via update_fields=['sample_join_key', 'contract_confidence', 'updated_at'].
    """
    from apps.core.models import DatasetPaperLink, DatasetPaperLinkPending

    keys = set()
    max_score = 0.0

    # 1. BioProject — sinal mais forte
    if dataset.bioproject_id:
        norm = _normalize_bioproject(dataset.bioproject_id)
        if norm:
            keys.add(norm)
            max_score = max(max_score, 0.9)

    # 2. PRIDE: extra_metadata['contract']['ref_pmids'] e ['ref_dois']
    em = dataset.extra_metadata or {}
    contract = em.get('contract', {}) if isinstance(em, dict) else {}

    ref_pmids = contract.get('ref_pmids', []) or []
    for pmid_raw in ref_pmids:
        norm = _normalize_pmid(pmid_raw)
        if norm:
            keys.add(norm)
            max_score = max(max_score, 0.7)

    ref_dois = contract.get('ref_dois', []) or []
    for doi_raw in ref_dois:
        norm = _normalize_doi(doi_raw)
        if norm:
            keys.add(norm)
            max_score = max(max_score, 0.5)

    # 3. DatasetPaperLink (GEO/SRA via elink) — PMIDs já resolvidos
    dpl_pmids = (
        DatasetPaperLink.objects
        .filter(dataset=dataset)
        .values_list('paper__pmid', flat=True)
    )
    for pmid_val in dpl_pmids:
        norm = _normalize_pmid(pmid_val)
        if norm:
            keys.add(norm)
            max_score = max(max_score, 0.7)

    # 4. DatasetPaperLinkPending (staging — ainda não resolvidos)
    pending_pmids = (
        DatasetPaperLinkPending.objects
        .filter(dataset_accession=dataset.accession)
        .values_list('paper_pmid', flat=True)
    )
    for pmid_val in pending_pmids:
        norm = _normalize_pmid(pmid_val)
        if norm:
            keys.add(norm)
            max_score = max(max_score, 0.65)

    # Ordenar para saída estável (bioproject < doi < pmid — lexicográfico)
    sorted_keys = sorted(keys)

    _save_classification(
        dataset,
        fields={'sample_join_key': sorted_keys},
        confidence_updates={'sample_join_key': round(max_score, 4)},
    )

    logger.info(
        'classify_sample_join_key: dataset=%s keys=%d score=%.4f',
        dataset.accession, len(sorted_keys), max_score,
    )
    return {'keys': sorted_keys, 'score': round(max_score, 4)}


# =============================================================================
# Passo 4 — Backfill legado data_format / access_type
# =============================================================================

def backfill_legacy_data_format(dataset) -> dict:
    """
    Deriva data_format para datasets não-PRIDE onde o valor atual é 'unknown'.

    Lógica:
      - Só opera se data_format == 'unknown' (idempotente: não sobrescreve valor já populado).
      - Verifica DatasetFile do dataset:
          - Tem arquivo tipo series_matrix/supplementary/cel → 'processed'.
          - Tem arquivo tipo fastq/sra (sem processed) → 'raw'.
          - Sem arquivos → mantém 'unknown'.

    PRIDE não é elegível: o conector já seta data_format corretamente.
    """
    from apps.core.models import DatasetFile

    if dataset.data_format != 'unknown':
        # Já populado — não sobrescreve (idempotente)
        return {'value': dataset.data_format, 'action': 'skipped'}

    if dataset.source_db == 'pride_archive':
        # PRIDE já tem data_format populado pelo conector
        return {'value': dataset.data_format, 'action': 'skipped_pride'}

    # Busca arquivos do dataset
    file_types = set(
        DatasetFile.objects
        .filter(dataset=dataset)
        .values_list('file_type', flat=True)
    )

    if file_types & _PROCESSED_FILE_TYPES:
        new_format = 'processed'
    elif file_types & _RAW_FILE_TYPES:
        new_format = 'raw'
    else:
        return {'value': 'unknown', 'action': 'no_files'}

    _save_classification(
        dataset,
        fields={'data_format': new_format},
        confidence_updates={},  # data_format não usa contract_confidence
    )

    logger.info(
        'backfill_legacy_data_format: dataset=%s → %s (file_types=%s)',
        dataset.accession, new_format, file_types,
    )
    return {'value': new_format, 'action': 'updated'}


def backfill_legacy_access_type(dataset) -> dict:
    """
    Deriva access_type para datasets não-PRIDE onde o valor atual é 'unknown'.

    Lógica:
      - Só opera se access_type == 'unknown' (idempotente).
      - GEO → sempre 'public' (GEO é repositório público por política).
      - SRA → verifica indicadores de controle de acesso:
          - dbGaP IDs no accession ou extra_metadata → 'controlled'.
          - Sem sinal determinável → 'unknown' (NUNCA hardcode 'public' para SRA).
      - ArrayExpress → 'public'.
      - Outras fontes → mantém 'unknown'.
    """
    if dataset.access_type != 'unknown':
        return {'value': dataset.access_type, 'action': 'skipped'}

    source = dataset.source_db

    if source == 'geo':
        new_access = 'public'
    elif source == 'arrayexpress':
        new_access = 'public'
    elif source == 'sra':
        # Detectar dbGaP: accession começa com phs ou está no extra_metadata
        accession_lower = (dataset.accession or '').lower()
        em = dataset.extra_metadata or {}
        em_str = str(em).lower()

        is_dbgap = (
            accession_lower.startswith('phs')
            or 'dbgap' in em_str
            or 'phs0' in em_str
            or 'controlled access' in em_str
        )
        new_access = 'controlled' if is_dbgap else 'unknown'
    else:
        # TCGA, BioProject, GWAS, PRIDE — sem backfill (manter unknown)
        return {'value': 'unknown', 'action': 'skipped_source'}

    if new_access == 'unknown':
        # Nenhuma mudança necessária
        return {'value': 'unknown', 'action': 'no_signal'}

    _save_classification(
        dataset,
        fields={'access_type': new_access},
        confidence_updates={},  # access_type não usa contract_confidence
    )

    logger.info(
        'backfill_legacy_access_type: dataset=%s → %s (source=%s)',
        dataset.accession, new_access, source,
    )
    return {'value': new_access, 'action': 'updated'}


# =============================================================================
# Função orquestradora (usada pela task e pelo management command)
# =============================================================================

def classify_all_axes(
    dataset,
    axes: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """
    Roda todos (ou subset) os classificadores em um dataset.

    axes: lista de strings em ['has_control_group', 'is_single_cell',
          'sample_join_key', 'data_format', 'access_type'].
          None = todos.

    dry_run: se True, coleta o texto e retorna diagnóstico sem gravar.

    Retorna dict com resultados por eixo.
    """
    ALL_AXES = [
        'has_control_group',
        'is_single_cell',
        'sample_join_key',
        'data_format',
        'access_type',
    ]
    active_axes = set(axes) if axes else set(ALL_AXES)

    results = {}

    if dry_run:
        text = _aggregate_text_for_dataset(dataset)
        return {
            'dry_run': True,
            'dataset': dataset.accession,
            'text_length': len(text),
            'text_preview': text[:500],
            'source_db': dataset.source_db,
            'current_has_control_group': dataset.has_control_group,
            'current_is_single_cell': dataset.is_single_cell,
            'current_sample_join_key': dataset.sample_join_key,
            'current_data_format': dataset.data_format,
            'current_access_type': dataset.access_type,
            'current_contract_confidence': dataset.contract_confidence,
        }

    if 'has_control_group' in active_axes:
        results['has_control_group'] = classify_has_control_group(dataset)

    if 'is_single_cell' in active_axes:
        results['is_single_cell'] = classify_is_single_cell(dataset)

    if 'sample_join_key' in active_axes:
        results['sample_join_key'] = classify_sample_join_key(dataset)

    if 'data_format' in active_axes:
        results['data_format'] = backfill_legacy_data_format(dataset)

    if 'access_type' in active_axes:
        results['access_type'] = backfill_legacy_access_type(dataset)

    return results
