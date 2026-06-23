"""
DiseaseAxisReferenceService — carga idempotente de âncoras de eixo de doença.

Responsabilidades:
  - normalize_trait_name(): normalização de nome de trait para fuzzy matching.
  - load_gwas_catalog():    download/parse/UPSERT do gwas-efo-trait-mappings.tsv.
  - load_orphanet():        download/parse streaming/UPSERT do en_product6.xml
                            (DiseaseAxisReference monogenic + MendelianGene orphanet).
  - load_g2p():             download/parse/UPSERT do G2P CSV
                            (DiseaseAxisReference monogenic + MendelianGene g2p).

Fontes:
    GWAS Catalog — https://ftp.ebi.ac.uk/pub/databases/gwas/releases/latest/gwas-efo-trait-mappings.tsv
                   (~18 MB, TSV, UTF-8).
    Orphanet     — https://www.orphadata.com/data/xml/en_product6.xml
                   (~22.8 MB, XML, CC-BY-4.0; parse via iterparse streaming).
    G2P          — https://www.ebi.ac.uk/gene2phenotype/api/panel/all/download/
                   (~2.4 MB, CSV, EBI aberto).

Caching local:
    Cada arquivo é salvo em diagnostics/cache/<nome>.
    Re-rodar reutiliza o cache. Passar force=True rebaixa e sobrescreve.

UPSERT idempotente:
    DiseaseAxisReference: unique_fields=['source','name'].
    MendelianGene:        unique_fields=['source','gene_symbol','disease_name'].
    bulk_create com update_conflicts=True em ambos.

Normalização (reutilizável pelo fuzzy classifier da Fase 3):
    - Lowercase.
    - Remoção de pontuação leve (hífens mantidos).
    - Colapso de espaços múltiplos.
    - Strip.
    Exportada como `normalize_trait_name`.
"""

import csv
import logging
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from django.utils import timezone as dj_timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes — URLs e caminhos de cache
# ---------------------------------------------------------------------------

GWAS_FTP_URL = (
    'https://ftp.ebi.ac.uk/pub/databases/gwas/releases/latest/'
    'gwas-efo-trait-mappings.tsv'
)
ORPHANET_XML_URL = 'https://www.orphadata.com/data/xml/en_product6.xml'
G2P_CSV_URL = 'https://www.ebi.ac.uk/gene2phenotype/api/panel/all/download/'

_CACHE_SUBPATH_GWAS = os.path.join('diagnostics', 'cache', 'gwas-efo-trait-mappings.tsv')
_CACHE_SUBPATH_ORPHANET = os.path.join('diagnostics', 'cache', 'en_product6.xml')
_CACHE_SUBPATH_G2P = os.path.join('diagnostics', 'cache', 'g2p_panel_all.csv')

# Número de objetos por chamada a bulk_create (evita statements muito longos).
_BATCH_SIZE = 1000

# ---------------------------------------------------------------------------
# Normalização de pontuação
# ---------------------------------------------------------------------------

# Pontuação leve a remover na normalização (exceto hífen — "type-2 diabetes").
_PUNCT_RE = re.compile(r'[^\w\s\-]', re.UNICODE)
_SPACES_RE = re.compile(r'\s+')


# ---------------------------------------------------------------------------
# Normalização pública (reutilizável pelo fuzzy classifier da Fase 3)
# ---------------------------------------------------------------------------

def normalize_trait_name(name: str) -> str:
    """
    Normaliza um nome de trait/doença para uso no fuzzy name-matching.

    Passos:
      1. Lowercase.
      2. Remove pontuação leve (mantém hífens).
      3. Colapsa espaços múltiplos.
      4. Strip.

    Exemplos:
        'Type 2 Diabetes'         → 'type 2 diabetes'
        'Alzheimer\'s disease'    → 'alzheimers disease'
        'COVID-19'                → 'covid-19'
        '  multiple  sclerosis '  → 'multiple sclerosis'
    """
    if not name:
        return ''
    normalized = name.lower()
    normalized = _PUNCT_RE.sub(' ', normalized)
    normalized = _SPACES_RE.sub(' ', normalized)
    return normalized.strip()


# ---------------------------------------------------------------------------
# Mapeamento de confiança G2P → MendelianGene.Confidence
# ---------------------------------------------------------------------------

# Valores nativos do G2P mapeados diretamente para o enum.
_G2P_CONFIDENCE_MAP: dict[str, str] = {
    'definitive': 'definitive',
    'strong': 'strong',
    'moderate': 'moderate',
    'limited': 'limited',
}


def _map_g2p_confidence(raw: str) -> str:
    """Retorna o valor do enum Confidence dado o valor bruto do G2P."""
    return _G2P_CONFIDENCE_MAP.get(raw.strip().lower(), 'unknown')


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class DiseaseAxisReferenceService:
    """
    Serviço de carga de âncoras de eixo de doença.

    Métodos públicos:
        load_gwas_catalog(force=False) -> dict   # âncora multifatorial
        load_orphanet(force=False)     -> dict   # âncora monogênica (nomes + genes)
        load_g2p(force=False)          -> dict   # âncora monogênica (nomes + genes)
    """

    # ------------------------------------------------------------------
    # Utilitários internos compartilhados
    # ------------------------------------------------------------------

    @staticmethod
    def _project_root() -> str:
        """Raiz do projeto derivada da localização deste arquivo."""
        service_dir = os.path.dirname(os.path.abspath(__file__))
        # apps/core/services/ → subir 3 níveis → davinci/
        return os.path.dirname(os.path.dirname(os.path.dirname(service_dir)))

    @classmethod
    def _cache_path(cls, subpath: str) -> str:
        """Caminho absoluto de um arquivo de cache."""
        return os.path.join(cls._project_root(), subpath)

    @staticmethod
    def _download(url: str, dest_path: str) -> str:
        """
        Faz o download de `url` para `dest_path` via GET.

        Retorna o valor do header Last-Modified normalizado para ISO8601,
        ou a data atual se o header estiver ausente.
        """
        logger.info('DiseaseAxisReferenceService: baixando %s → %s', url, dest_path)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)

        req = urllib.request.Request(url, headers={'User-Agent': 'davinci-biohub/1.0'})
        with urllib.request.urlopen(req, timeout=180) as response:
            last_modified = response.headers.get('Last-Modified', '')
            raw_bytes = response.read()

        with open(dest_path, 'wb') as f:
            f.write(raw_bytes)

        logger.info(
            'DiseaseAxisReferenceService: %d bytes gravados (Last-Modified: %r)',
            len(raw_bytes),
            last_modified,
        )
        return DiseaseAxisReferenceService._parse_last_modified(last_modified)

    @staticmethod
    def _parse_last_modified(value: str) -> str:
        """
        Converte o header HTTP Last-Modified (RFC 2822) para ISO8601.

        Se o parse falhar ou o header estiver vazio, retorna a data atual em UTC.
        """
        if not value:
            return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(value)
            return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        except Exception:
            pass
        return value.strip()

    @staticmethod
    def _upsert_disease_refs(objs: list, source_label: str) -> dict[str, int]:
        """
        UPSERT em DiseaseAxisReference em batches de _BATCH_SIZE.

        Retorna {'inserted': int, 'updated': int} (aproximado: pré-existentes = updated).
        """
        from apps.core.models import DiseaseAxisReference

        if not objs:
            return {'inserted': 0, 'updated': 0}

        existing = set(
            DiseaseAxisReference.objects
            .filter(source=source_label)
            .values_list('name', flat=True)
        )

        for i in range(0, len(objs), _BATCH_SIZE):
            batch = objs[i:i + _BATCH_SIZE]
            DiseaseAxisReference.objects.bulk_create(
                batch,
                update_conflicts=True,
                unique_fields=['source', 'name'],
                update_fields=['name_normalized', 'axis', 'source_version', 'loaded_at'],
            )
            logger.debug(
                'DiseaseAxisReferenceService [%s]: disease refs batch %d–%d upsertado',
                source_label, i, i + len(batch),
            )

        new_names = {obj.name for obj in objs}
        return {
            'inserted': len(new_names - existing),
            'updated': len(new_names & existing),
        }

    @staticmethod
    def _upsert_mendelian_genes(objs: list, source_label: str) -> dict[str, int]:
        """
        UPSERT em MendelianGene em batches de _BATCH_SIZE.

        Retorna {'inserted': int, 'updated': int} (aproximado).
        """
        from apps.core.models import MendelianGene

        if not objs:
            return {'inserted': 0, 'updated': 0}

        existing = set(
            MendelianGene.objects
            .filter(source=source_label)
            .values_list('gene_symbol', 'disease_name')
        )

        for i in range(0, len(objs), _BATCH_SIZE):
            batch = objs[i:i + _BATCH_SIZE]
            MendelianGene.objects.bulk_create(
                batch,
                update_conflicts=True,
                unique_fields=['source', 'gene_symbol', 'disease_name'],
                update_fields=[
                    'hgnc_id', 'ensembl_id', 'entrez_id',
                    'confidence', 'confidence_raw', 'source_version', 'loaded_at',
                ],
            )
            logger.debug(
                'DiseaseAxisReferenceService [%s]: mendelian genes batch %d–%d upsertado',
                source_label, i, i + len(batch),
            )

        new_pairs = {(obj.gene_symbol, obj.disease_name) for obj in objs}
        return {
            'inserted': len(new_pairs - existing),
            'updated': len(new_pairs & existing),
        }

    # ------------------------------------------------------------------
    # load_gwas_catalog (âncora multifatorial — P4, mantido intacto)
    # ------------------------------------------------------------------

    @classmethod
    def load_gwas_catalog(cls, *, force: bool = False) -> dict:
        """
        Baixa (se necessário) e carrega os nomes de trait do GWAS Catalog
        em DiseaseAxisReference com source='gwas_catalog', axis='multifactorial'.

        Retorna dict com:
            cache_hit, total_names, inserted, updated, source_version, trait_column.
        """
        from apps.core.models import DiseaseAxisReference

        cache_path = cls._cache_path(_CACHE_SUBPATH_GWAS)
        cache_hit = os.path.exists(cache_path) and not force

        if cache_hit:
            logger.info('DiseaseAxisReferenceService: cache GWAS local (%s)', cache_path)
            mtime = os.path.getmtime(cache_path)
            source_version = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime(
                '%Y-%m-%dT%H:%M:%SZ'
            )
        else:
            raw_lm = cls._download(GWAS_FTP_URL, cache_path)
            source_version = raw_lm

        names, trait_col = cls._parse_gwas_tsv(cache_path)
        logger.info(
            'DiseaseAxisReferenceService: %d nomes GWAS distintos (coluna %r)',
            len(names), trait_col,
        )

        now = dj_timezone.now()
        objs = [
            DiseaseAxisReference(
                source=DiseaseAxisReference.Source.GWAS_CATALOG,
                axis='multifactorial',
                name=name,
                name_normalized=normalize_trait_name(name),
                source_version=source_version,
                loaded_at=now,
            )
            for name in names
        ]

        counts = cls._upsert_disease_refs(objs, DiseaseAxisReference.Source.GWAS_CATALOG)
        logger.info(
            'DiseaseAxisReferenceService [gwas_catalog]: inserted=%d updated=%d',
            counts['inserted'], counts['updated'],
        )
        return {
            'cache_hit': cache_hit,
            'total_names': len(names),
            'source_version': source_version,
            'trait_column': trait_col,
            **counts,
        }

    @staticmethod
    def _parse_gwas_tsv(path: str) -> tuple[list[str], str]:
        """Lê o TSV GWAS e retorna (nomes_distintos, coluna_usada)."""
        with open(path, encoding='utf-8', newline='') as f:
            reader = csv.DictReader(f, delimiter='\t')
            header = reader.fieldnames or []

            trait_col = None
            for col in header:
                col_lower = col.lower()
                if 'disease' in col_lower or 'trait' in col_lower:
                    trait_col = col
                    break

            if trait_col is None:
                raise ValueError(
                    f'Coluna de trait não encontrada no TSV GWAS. Header: {header}'
                )

            seen: set[str] = set()
            names: list[str] = []
            for row in reader:
                name = (row.get(trait_col) or '').strip()
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)

        return names, trait_col

    # ------------------------------------------------------------------
    # load_orphanet (âncora monogênica — nomes de doença + genes)
    # ------------------------------------------------------------------

    @classmethod
    def load_orphanet(cls, *, force: bool = False) -> dict:
        """
        Baixa (se necessário) e carrega o Orphadata product6 XML:
          - DiseaseAxisReference (source='orphanet', axis='monogenic') para cada <Disorder>
          - MendelianGene (source='orphanet') para cada <Gene> nas associações

        Parse via iterparse streaming (elemento por elemento) para controle de memória.

        Source version: atributo `date` do elemento raiz <JDBOR>.

        Confiança Orphanet:
          - <SourceOfValidation> presente e não-vazio → 'assessed'
                                                        (raw = conteúdo, ex.: '22587682[PMID]')
          - <SourceOfValidation> ausente ou vazio      → 'not_assessed' (raw = '')

        Retorna dict com:
            cache_hit, source_version,
            diseases_total, diseases_inserted, diseases_updated,
            genes_total, genes_inserted, genes_updated.
        """
        from apps.core.models import DiseaseAxisReference, MendelianGene

        cache_path = cls._cache_path(_CACHE_SUBPATH_ORPHANET)
        cache_hit = os.path.exists(cache_path) and not force

        if cache_hit:
            logger.info('DiseaseAxisReferenceService: cache Orphanet local (%s)', cache_path)
        else:
            cls._download(ORPHANET_XML_URL, cache_path)

        now = dj_timezone.now()
        disease_objs: list[DiseaseAxisReference] = []
        gene_objs: list[MendelianGene] = []

        source_version = ''

        # ---- Parse streaming do XML ----
        # Estratégia: capturar source_version do root, depois iterar <Disorder>.
        # Para cada Disorder: extrair Name e lista de (Gene, SourceOfValidation).
        # Limpar elem após processar para manter memória baixa.

        context = ET.iterparse(cache_path, events=('start', 'end'))
        root_seen = False
        current_disorder_name: str = ''
        inside_disorder = False
        # Acumular associações dentro do <Disorder> atual
        _current_gene_associations: list[dict] = []  # [{symbol, ensembl_id, hgnc_id, sov}]

        disease_names_seen: set[str] = set()  # para unicidade de DiseaseAxisReference
        # Deduplicação de (gene_symbol_upper, disease_name) — Orphanet pode ter
        # múltiplas DisorderGeneAssociation para o mesmo gene/doença (ex.: tipos
        # de mutação distintos). O unique_together colapsaria no UPSERT, mas o
        # PostgreSQL rejeita ON CONFLICT DO UPDATE se o mesmo par aparecer duas
        # vezes no mesmo batch.
        gene_pairs_seen: set[tuple[str, str]] = set()

        for event, elem in context:
            # ---- Capturar source_version do root ----
            if event == 'start' and elem.tag == 'JDBOR' and not root_seen:
                source_version = elem.get('date', '').strip() or elem.get('version', '').strip()
                root_seen = True
                continue

            # ---- Início do <Disorder> ----
            if event == 'start' and elem.tag == 'Disorder':
                inside_disorder = True
                current_disorder_name = ''
                _current_gene_associations = []
                continue

            # ---- Fim do <Disorder>: processa e limpa ----
            if event == 'end' and elem.tag == 'Disorder':
                inside_disorder = False

                # DiseaseAxisReference — cada Disorder vira um nome (deduplicado)
                if current_disorder_name and current_disorder_name not in disease_names_seen:
                    disease_names_seen.add(current_disorder_name)
                    disease_objs.append(
                        DiseaseAxisReference(
                            source=DiseaseAxisReference.Source.ORPHANET,
                            axis='monogenic',
                            name=current_disorder_name,
                            name_normalized=normalize_trait_name(current_disorder_name),
                            source_version=source_version,
                            loaded_at=now,
                        )
                    )

                # MendelianGene — uma linha por par (gene_symbol, disease_name).
                # Orphanet pode ter múltiplas DisorderGeneAssociation para o mesmo
                # gene/doença (ex.: tipos de mutação distintos). Deduplicamos antes
                # do bulk_create para evitar CardinalityViolation do ON CONFLICT.
                for assoc in _current_gene_associations:
                    if not assoc['symbol']:
                        continue
                    symbol_upper = assoc['symbol'].upper()
                    pair = (symbol_upper, current_disorder_name)
                    if pair in gene_pairs_seen:
                        continue
                    gene_pairs_seen.add(pair)
                    raw_sov = assoc['sov']
                    confidence = 'assessed' if raw_sov else 'not_assessed'
                    gene_objs.append(
                        MendelianGene(
                            gene_symbol=symbol_upper,
                            ensembl_id=assoc['ensembl_id'],
                            hgnc_id=assoc['hgnc_id'],
                            disease_name=current_disorder_name,
                            source=MendelianGene.Source.ORPHANET,
                            confidence=confidence,
                            confidence_raw=raw_sov,
                            source_version=source_version,
                            loaded_at=now,
                        )
                    )

                elem.clear()
                current_disorder_name = ''
                _current_gene_associations = []
                continue

            # ---- Dentro do Disorder: capturar Name (lang=en, filho direto) ----
            if event == 'end' and elem.tag == 'Name' and inside_disorder:
                # O <Name lang="en"> filho direto do <Disorder> é o nome da doença.
                # Há <Name> em subtags (DisorderType, Gene, etc.) — distinguir pelo
                # texto que não esteja dentro de uma associação em andamento e pelo
                # fato de que o Disorder Name vem antes das associações no XML.
                if not current_disorder_name and elem.get('lang') == 'en':
                    current_disorder_name = (elem.text or '').strip()
                continue

            # ---- Processar associações dentro de <DisorderGeneAssociation> ----
            if event == 'end' and elem.tag == 'DisorderGeneAssociation' and inside_disorder:
                # Extrair SourceOfValidation
                sov_elem = elem.find('SourceOfValidation')
                sov = (sov_elem.text or '').strip() if sov_elem is not None else ''

                # Extrair dados do Gene
                gene_elem = elem.find('Gene')
                if gene_elem is not None:
                    symbol_elem = gene_elem.find('Symbol')
                    symbol = (symbol_elem.text or '').strip() if symbol_elem is not None else ''

                    # Ensembl e HGNC via ExternalReferenceList
                    ensembl_id = ''
                    hgnc_id = ''
                    for ref in gene_elem.findall('./ExternalReferenceList/ExternalReference'):
                        src_elem = ref.find('Source')
                        ref_elem = ref.find('Reference')
                        if src_elem is None or ref_elem is None:
                            continue
                        src_text = (src_elem.text or '').strip()
                        ref_text = (ref_elem.text or '').strip()
                        if src_text == 'Ensembl':
                            ensembl_id = ref_text
                        elif src_text == 'HGNC':
                            # Orphanet fornece só o número; normalizar para "HGNC:XXXXX"
                            hgnc_id = f'HGNC:{ref_text}' if ref_text else ''

                    _current_gene_associations.append({
                        'symbol': symbol,
                        'ensembl_id': ensembl_id,
                        'hgnc_id': hgnc_id,
                        'sov': sov,
                    })
                continue

        logger.info(
            'DiseaseAxisReferenceService [orphanet]: '
            '%d doenças, %d associações gene×doença, source_version=%r',
            len(disease_objs), len(gene_objs), source_version,
        )

        disease_counts = cls._upsert_disease_refs(
            disease_objs, DiseaseAxisReference.Source.ORPHANET
        )
        gene_counts = cls._upsert_mendelian_genes(
            gene_objs, MendelianGene.Source.ORPHANET
        )

        return {
            'cache_hit': cache_hit,
            'source_version': source_version,
            'diseases_total': len(disease_objs),
            'diseases_inserted': disease_counts['inserted'],
            'diseases_updated': disease_counts['updated'],
            'genes_total': len(gene_objs),
            'genes_inserted': gene_counts['inserted'],
            'genes_updated': gene_counts['updated'],
        }

    # ------------------------------------------------------------------
    # load_g2p (âncora monogênica — nomes de doença + genes)
    # ------------------------------------------------------------------

    @classmethod
    def load_g2p(cls, *, force: bool = False) -> dict:
        """
        Baixa (se necessário) e carrega o G2P panel CSV:
          - DiseaseAxisReference (source='g2p', axis='monogenic') por 'disease name'
          - MendelianGene (source='g2p') por 'gene symbol' × 'disease name'

        Source version: header Last-Modified do download (ou mtime do cache).

        Confiança G2P:
          Coluna 'confidence' (definitive/strong/moderate/limited) mapeada diretamente
          para MendelianGene.Confidence. Valores fora do mapeamento → 'unknown'.

        Colunas usadas:
          'gene symbol', 'hgnc id', 'disease name', 'confidence'.
          'gene mim'/'disease mim' ignorados (restrição de licença OMIM).

        Retorna dict com:
            cache_hit, source_version,
            diseases_total, diseases_inserted, diseases_updated,
            genes_total, genes_inserted, genes_updated.
        """
        from apps.core.models import DiseaseAxisReference, MendelianGene

        cache_path = cls._cache_path(_CACHE_SUBPATH_G2P)
        cache_hit = os.path.exists(cache_path) and not force

        if cache_hit:
            logger.info('DiseaseAxisReferenceService: cache G2P local (%s)', cache_path)
            mtime = os.path.getmtime(cache_path)
            source_version = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime(
                '%Y-%m-%dT%H:%M:%SZ'
            )
        else:
            source_version = cls._download(G2P_CSV_URL, cache_path)

        now = dj_timezone.now()
        disease_objs: list[DiseaseAxisReference] = []
        gene_objs: list[MendelianGene] = []
        disease_names_seen: set[str] = set()
        gene_pairs_seen: set[tuple[str, str]] = set()  # (gene_symbol_upper, disease_name)

        with open(cache_path, encoding='utf-8', newline='') as f:
            reader = csv.DictReader(f)
            header = reader.fieldnames or []

            # Localizar colunas com tolerância a variações de espaçamento
            col_map = {h.strip().lower(): h for h in header}

            gene_col = col_map.get('gene symbol')
            disease_col = col_map.get('disease name')
            conf_col = col_map.get('confidence')
            hgnc_col = col_map.get('hgnc id')

            if not all([gene_col, disease_col, conf_col]):
                raise ValueError(
                    f'Colunas obrigatórias não encontradas no G2P CSV. '
                    f'Esperado: "gene symbol", "disease name", "confidence". '
                    f'Header: {header}'
                )

            for row in reader:
                gene_raw = (row.get(gene_col) or '').strip()
                disease_raw = (row.get(disease_col) or '').strip()
                conf_raw = (row.get(conf_col) or '').strip()
                hgnc_raw = (row.get(hgnc_col) or '').strip() if hgnc_col else ''

                if not gene_raw or not disease_raw:
                    continue

                gene_upper = gene_raw.upper()

                # DiseaseAxisReference — deduplicado por nome exato
                if disease_raw not in disease_names_seen:
                    disease_names_seen.add(disease_raw)
                    disease_objs.append(
                        DiseaseAxisReference(
                            source=DiseaseAxisReference.Source.G2P,
                            axis='monogenic',
                            name=disease_raw,
                            name_normalized=normalize_trait_name(disease_raw),
                            source_version=source_version,
                            loaded_at=now,
                        )
                    )

                # MendelianGene — deduplicado por (gene_upper, disease_raw)
                # O CSV pode ter múltiplas linhas para o mesmo par com alelic
                # requirements diferentes; a unique_together (source, gene_symbol,
                # disease_name) colapsaria em UPSERT, mas evitamos duplicar objs
                # na lista em memória para não desperdiçar bulk slots.
                pair = (gene_upper, disease_raw)
                if pair not in gene_pairs_seen:
                    gene_pairs_seen.add(pair)

                    # Normalizar hgnc_id: G2P entrega como número puro ou "HGNC:NNNN"
                    if hgnc_raw and not hgnc_raw.upper().startswith('HGNC:'):
                        hgnc_id_norm = f'HGNC:{hgnc_raw}'
                    else:
                        hgnc_id_norm = hgnc_raw

                    gene_objs.append(
                        MendelianGene(
                            gene_symbol=gene_upper,
                            hgnc_id=hgnc_id_norm,
                            disease_name=disease_raw,
                            source=MendelianGene.Source.G2P,
                            confidence=_map_g2p_confidence(conf_raw),
                            confidence_raw=conf_raw,
                            source_version=source_version,
                            loaded_at=now,
                        )
                    )

        logger.info(
            'DiseaseAxisReferenceService [g2p]: '
            '%d doenças distintas, %d pares gene×doença, source_version=%r',
            len(disease_objs), len(gene_objs), source_version,
        )

        disease_counts = cls._upsert_disease_refs(
            disease_objs, DiseaseAxisReference.Source.G2P
        )
        gene_counts = cls._upsert_mendelian_genes(
            gene_objs, MendelianGene.Source.G2P
        )

        return {
            'cache_hit': cache_hit,
            'source_version': source_version,
            'diseases_total': len(disease_objs),
            'diseases_inserted': disease_counts['inserted'],
            'diseases_updated': disease_counts['updated'],
            'genes_total': len(gene_objs),
            'genes_inserted': gene_counts['inserted'],
            'genes_updated': gene_counts['updated'],
        }
