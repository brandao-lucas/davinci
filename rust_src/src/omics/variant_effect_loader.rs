/// Loaders de magnitude de dano para `core_varianteffectraw` — ClinVar VCF GRCh38 e
/// AlphaMissense hg38.
///
/// # Decisões de design (travadas no plano 2026-07-13)
///
/// ## Normalização de variant_key (Decisão F)
///
/// `variant_key` CANÔNICO = `"{CHROM}:{POS}:{REF}:{ALT}"` em GRCh38.
/// - `CHROM` sem prefixo `chr` (ClinVar VCF já vem sem `chr`; AlphaMissense vem com
///   `chr` → REMOVIDO aqui; MAF GDC também virá com `chr` → removerá lá).
/// - `POS` 1-based (formato VCF padrão; ClinVar e AlphaMissense já são 1-based).
/// - `REF`/`ALT` como estão na fonte (bases únicas para SNV; indels: normalização
///   simples de strip de padding — ver `normalize_variant_key`).
/// - **Consistência é CRÍTICA**: o seed SNV cruza `VariantEffectRaw` por `variant_key`.
///   Qualquer fonte que gerar chaves diferentes para a mesma variante quebra o cruzamento.
///
/// ## Foco SNV/missense
///
/// ClinVar tem indels. Para indels, aplicamos normalização simples (strip de padding
/// VCF — remove base de ancoragem comum quando REF/ALT têm comprimento > 1 e
/// compartilham a primeira base). Indels que exijam normalização complexa (left-align
/// strand-specific) são passados como-está; a limitação é documentada. O sinal de
/// indel virá do `Variant_Classification` no seed MAF (passo 2.6), não da magnitude.
///
/// ## Filtro de escala v1 (Decisão de escala travada)
///
/// Carregamos SOMENTE variantes de genes presentes na `gene_allowlist` (conjunto derivado
/// de `GeneRole` — genes com papel oncokb). Descarta em streaming antes do COPY.
/// Evita explodir `VariantEffectRaw` com as dezenas de milhões de linhas do ClinVar.
///
/// ## AlphaMissense — HANDOFF (abordagem c do plano)
///
/// AlphaMissense não tem `gene_symbol` (só `uniprot_id`/`transcript_id`). Filtrar por
/// coordenada de gene exigiria um mapa de faixas GRCh38 por gene. Carregar tudo (71M
/// linhas) sem filtro viola a Regra #-1 (explodem `VariantEffectRaw`). Decisão: a função
/// `load_alphamissense_effects` é implementada com suporte a mapa `uniprot_id→gene_symbol`
/// injetado pelo caller, mas para v1 o vitruvio pode NÃO passar o mapa (resultado: AM
/// não carregado) e sinalizar como handoff. O caller (service Django) é responsável por
/// fornecer o mapa quando disponível (ex.: derivado de UniProt ou Ensembl lookup).
/// Função pura de parse/normalização de AM está implementada e testada.
///
/// ## COPY UPSERT
///
/// Staging temp table + COPY FROM STDIN + `INSERT … ON CONFLICT (variant_key, source,
/// gene_symbol) DO UPDATE` — idempotente. Reusar o padrão de `gene_role_loader.rs`.
///
/// ## Segurança
///
/// - `db_url` nunca logado.
/// - Hosts validados via allowlist: `ftp.ncbi.nlm.nih.gov` (ClinVar) e
///   `storage.googleapis.com` / `zenodo.org` (AlphaMissense).
/// - Todos os valores de string passam por `escape_csv_field` antes do COPY.
/// - Sem SQL cru com interpolação de strings.
/// - `redirect::Policy::none()` no client HTTP (hardening A4, laudo 007):
///   checagem empírica confirmou 200 direto para `ftp.ncbi.nlm.nih.gov`,
///   `storage.googleapis.com` e a URL **canônica** do mirror Zenodo
///   (`zenodo.org/records/<id>/…`, plural). A forma legada `zenodo.org/record/<id>/…`
///   (singular) faz 301 para a forma canônica — **callers devem usar a forma
///   plural** para evitar quebra. Ver `build_http_client`.

use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};

use bytes::Bytes;
use chrono::Utc;
use flate2::read::GzDecoder;
use futures::SinkExt;
use std::pin::pin;
use tokio_postgres::Client;

// ─── Manifesto ClinVar ────────────────────────────────────────────────────────

/// Manifesto retornado pelo loader ClinVar após COPY em `core_varianteffectraw`.
///
/// Handoff para o vitruvio (passo 2.5):
///
/// | Campo | Tipo | Descrição |
/// |---|---|---|
/// | `n_variants_processed` | `usize` | Linhas VCF parseadas (excluindo comentários/cabeçalho). |
/// | `n_kept` | `usize` | Variantes cujo gene está na allowlist e foram gravadas. |
/// | `n_skipped_offlist` | `usize` | Variantes descartadas (gene não está na allowlist). |
/// | `n_skipped_no_gene` | `usize` | Linhas sem campo `GENEINFO` no INFO. |
/// | `n_upserted` | `u64` | Linhas gravadas via COPY UPSERT em `core_varianteffectraw`. |
/// | `source_version` | `String` | Versão da fonte extraída do cabeçalho VCF (fileDate + build). |
/// | `errors` | `Vec<String>` | Erros não-fatais durante o parse (linha corrompida, etc.). |
#[derive(Debug)]
pub struct ClinVarLoadManifest {
    pub n_variants_processed: usize,
    pub n_kept: usize,
    pub n_skipped_offlist: usize,
    pub n_skipped_no_gene: usize,
    pub n_upserted: u64,
    pub source_version: String,
    pub errors: Vec<String>,
}

/// Manifesto retornado pelo loader AlphaMissense após COPY em `core_varianteffectraw`.
///
/// Handoff para o vitruvio (passo 2.5):
///
/// | Campo | Tipo | Descrição |
/// |---|---|---|
/// | `n_variants_processed` | `usize` | Linhas TSV parseadas (excluindo cabeçalho). |
/// | `n_kept` | `usize` | Variantes cujo gene está no mapa e foram gravadas. |
/// | `n_skipped_no_map` | `usize` | Variantes descartadas (uniprot_id não mapeado para gene da allowlist). |
/// | `n_upserted` | `u64` | Linhas gravadas via COPY UPSERT em `core_varianteffectraw`. |
/// | `source_version` | `String` | Versão da fonte (data do download). |
/// | `errors` | `Vec<String>` | Erros não-fatais. |
/// | `handoff_required` | `bool` | `true` quando mapa uniprot→gene não foi fornecido (AM não carregado). |
#[derive(Debug)]
pub struct AlphaMissenseLoadManifest {
    pub n_variants_processed: usize,
    pub n_kept: usize,
    pub n_skipped_no_map: usize,
    pub n_upserted: u64,
    pub source_version: String,
    pub errors: Vec<String>,
    /// `true` quando o mapa uniprot→gene não foi fornecido (AM não pode filtrar por gene).
    /// O caller deve fornecer o mapa em v2 (ver decisão de filtro AlphaMissense no módulo).
    pub handoff_required: bool,
}

// ─── Normalização de variant_key ──────────────────────────────────────────────

/// Normaliza `(chrom, pos, ref_allele, alt_allele)` para o formato canônico
/// `CHROM:POS:REF:ALT` (GRCh38, sem prefixo `chr`, 1-based).
///
/// # Regras de normalização
///
/// 1. `chrom`: remove prefixo `chr` (case-insensitive). Ex.: `chr7` → `7`, `chrX` → `X`.
/// 2. `pos`: retorna como string (já 1-based no VCF e no TSV AM).
/// 3. SNV (`len(REF) == 1 && len(ALT) == 1`): sem alteração. Ex.: `7:117548628:G:A`.
/// 4. Indel simples: strip de padding (base de ancoragem à esquerda comum):
///    - REF=`AGCT`, ALT=`A` → REF=`GCT`, ALT=`` (deletério) — representação VCF canônica.
///    - Para v1, mantemos a representação como-está se a strip não for ambígua.
///    - Indels complexos (multi-alélicos, len > 1 em ambos) são passados como-está.
/// 5. Retorna `None` se `alt` contém `,` (multi-alélico) ou `*` (spanning deletion) —
///    filtramos para manter apenas variantes bi-alelicas simples.
///
/// # Exemplos
///
/// ```text
/// normalize_variant_key("chr7", "117548628", "G", "A") → Some("7:117548628:G:A")
/// normalize_variant_key("7", "117548628", "G", "A")    → Some("7:117548628:G:A")
/// normalize_variant_key("chrX", "1000", "A", "T")      → Some("X:1000:A:T")
/// normalize_variant_key("1", "100", "AGCT", "A")       → Some("1:101:GCT:")  (indel strip)
/// normalize_variant_key("1", "100", "A", "AGT")        → Some("1:100:A:GT")   (ins strip v1)
/// normalize_variant_key("1", "100", "A", "A,T")        → None (multi-alélico)
/// normalize_variant_key("1", "100", "A", "*")          → None (spanning deletion)
/// ```
pub fn normalize_variant_key(
    chrom: &str,
    pos: &str,
    ref_allele: &str,
    alt_allele: &str,
) -> Option<String> {
    // Rejeitar multi-alélico ou spanning deletion
    if alt_allele.contains(',') || alt_allele == "*" || alt_allele.is_empty() {
        return None;
    }

    // Normalizar CHROM: strip prefixo 'chr' (case-insensitive)
    let chrom_norm = strip_chr_prefix(chrom);
    if chrom_norm.is_empty() {
        return None;
    }

    // SNV: ref e alt são bases únicas → sem normalização de indel
    if ref_allele.len() == 1 && alt_allele.len() == 1 {
        return Some(format!("{}:{}:{}:{}", chrom_norm, pos, ref_allele, alt_allele));
    }

    // Indel: strip de padding VCF (base de ancoragem à esquerda comum)
    let pos_int: u64 = match pos.parse() {
        Ok(v) => v,
        Err(_) => return None,
    };

    let (new_ref, new_alt, new_pos) = strip_vcf_padding(ref_allele, alt_allele, pos_int);

    Some(format!("{}:{}:{}:{}", chrom_norm, new_pos, new_ref, new_alt))
}

/// Remove o prefixo `chr` (case-insensitive) de um cromossomo.
/// Ex.: `"chr7"` → `"7"`, `"CHR7"` → `"7"`, `"7"` → `"7"`.
pub fn strip_chr_prefix(chrom: &str) -> &str {
    if chrom.len() > 3 && chrom[..3].eq_ignore_ascii_case("chr") {
        &chrom[3..]
    } else {
        chrom
    }
}

/// Strip de padding VCF: remove a base de ancoragem à esquerda comum entre REF e ALT.
///
/// Retorna `(new_ref, new_alt, new_pos)` onde `new_pos` é ajustado se a base
/// de ancoragem foi removida da esquerda (pos avança 1).
///
/// Para indels com múltiplas bases à esquerda comuns, remove apenas UMA (VCF minimal
/// left-anchor convention para v1 — normalização mais completa é trabalho futuro).
fn strip_vcf_padding(ref_allele: &str, alt_allele: &str, pos: u64) -> (String, String, u64) {
    let ref_bytes = ref_allele.as_bytes();
    let alt_bytes = alt_allele.as_bytes();

    // Se o primeiro byte é idêntico E ambos têm mais de 1 base → strip
    if ref_bytes.len() > 1 && alt_bytes.len() > 1 && ref_bytes[0] == alt_bytes[0] {
        // Remove 1 base de ancoragem à esquerda (v1: strip único)
        return (
            ref_allele[1..].to_string(),
            alt_allele[1..].to_string(),
            pos + 1,
        );
    }

    // Deleção pura: REF multi, ALT single, 1ª base comum
    if ref_bytes.len() > 1 && alt_bytes.len() == 1 && ref_bytes[0] == alt_bytes[0] {
        return (ref_allele[1..].to_string(), String::new(), pos + 1);
    }

    // Inserção pura: REF single, ALT multi, 1ª base comum
    if ref_bytes.len() == 1 && alt_bytes.len() > 1 && ref_bytes[0] == alt_bytes[0] {
        return (String::new(), alt_allele[1..].to_string(), pos + 1);
    }

    // Sem base comum à esquerda — retorna como-está
    (ref_allele.to_string(), alt_allele.to_string(), pos)
}

// ─── Parse de linha VCF ClinVar ──────────────────────────────────────────────

/// Campos extraídos de uma linha VCF ClinVar.
#[derive(Debug, PartialEq)]
pub struct ClinVarRecord {
    /// Chave canônica `CHROM:POS:REF:ALT` (GRCh38, sem `chr`).
    pub variant_key: String,
    /// Gene symbol extraído de `GENEINFO` (primeira entrada: `SYMBOL:id`).
    pub gene_symbol: String,
    /// Valor cru de `CLNSIG` (ex.: `"Pathogenic"`, `"Likely_pathogenic/Pathogenic"`).
    pub clinvar_significance: String,
    /// `raw_class` = mesmo que `clinvar_significance` (ClinVar não tem score numérico).
    pub raw_class: String,
    /// Valor de oncogenicidade (`ONCOGENICITY` / `ONC` / `ONCDN` se presente).
    pub oncogenicity: String,
    /// `CLNHGVS` ou vazio.
    pub clnhgvs: String,
}

/// Extrai o primeiro gene symbol de `GENEINFO`.
///
/// Formato: `SYMBOL:entrez_id|SYMBOL2:entrez_id2|...`
/// Retorna o SYMBOL da primeira entrada (UPPERCASE).
/// Retorna `""` se o campo estiver ausente ou malformado.
pub fn extract_geneinfo_symbol(geneinfo: &str) -> String {
    // Toma o primeiro segmento separado por '|'
    let first = geneinfo.split('|').next().unwrap_or("");
    // O símbolo está antes do ':'
    let symbol = first.split(':').next().unwrap_or("").trim();
    symbol.to_uppercase()
}

/// Extrai o valor de um campo INFO do VCF pelo nome da chave.
///
/// Aceita tanto `KEY=VALUE` quanto flag `KEY` (sem valor, retorna `"true"`).
/// Retorna `""` se a chave não for encontrada.
pub fn extract_info_field<'a>(info: &'a str, key: &str) -> &'a str {
    // Procura `key=` ou `key;` ou `key` no final
    let prefix_eq = format!("{}=", key);
    let prefix_semi = format!("{};", key);

    for field in info.split(';') {
        if field.starts_with(prefix_eq.as_str()) {
            return &field[prefix_eq.len()..];
        }
        if field == key || field.starts_with(prefix_semi.as_str()) {
            return "true";
        }
    }
    ""
}

/// Parseia uma linha de dados VCF ClinVar (não comentário, não cabeçalho).
///
/// Retorna `Ok(Some(record))` se a linha é válida e contém GENEINFO.
/// Retorna `Ok(None)` se a linha deve ser descartada (multi-alélico, sem GENEINFO, etc.).
/// Retorna `Err(msg)` se o formato é inesperado.
pub fn parse_clinvar_vcf_line(line: &str) -> Result<Option<ClinVarRecord>, String> {
    // VCF tem pelo menos 8 colunas tab-delimitadas
    let fields: Vec<&str> = line.split('\t').collect();
    if fields.len() < 8 {
        return Err(format!("VCF linha com {} colunas (esperava >= 8)", fields.len()));
    }

    let chrom = fields[0];
    let pos = fields[1];
    // fields[2] = ID (CLINVAR ID — não usamos)
    let ref_allele = fields[3];
    let alt_allele = fields[4];
    // fields[5] = QUAL, fields[6] = FILTER, fields[7] = INFO
    let info = fields[7];

    // Normalizar variant_key; descarta multi-alélico/spanning
    let variant_key = match normalize_variant_key(chrom, pos, ref_allele, alt_allele) {
        Some(k) => k,
        None => return Ok(None),
    };

    // Extrair GENEINFO
    let geneinfo = extract_info_field(info, "GENEINFO");
    if geneinfo.is_empty() {
        return Ok(None); // sem gene → descarta
    }
    let gene_symbol = extract_geneinfo_symbol(geneinfo);
    if gene_symbol.is_empty() {
        return Ok(None);
    }

    // Extrair CLNSIG
    let clinvar_significance = extract_info_field(info, "CLNSIG").to_string();
    let raw_class = clinvar_significance.clone();

    // Extrair oncogenicidade: ONCOGENICITY (novo campo), ONC, ONCDN
    let oncogenicity = {
        let v = extract_info_field(info, "ONCOGENICITY");
        if !v.is_empty() {
            v.to_string()
        } else {
            let v2 = extract_info_field(info, "ONC");
            if !v2.is_empty() {
                v2.to_string()
            } else {
                extract_info_field(info, "ONCDN").to_string()
            }
        }
    };

    // Extrair CLNHGVS (opcional)
    let clnhgvs = extract_info_field(info, "CLNHGVS").to_string();

    Ok(Some(ClinVarRecord {
        variant_key,
        gene_symbol,
        clinvar_significance,
        raw_class,
        oncogenicity,
        clnhgvs,
    }))
}

// ─── Parse de linha AlphaMissense ────────────────────────────────────────────

/// Campos extraídos de uma linha TSV AlphaMissense.
///
/// Colunas AM: `#CHROM`, `POS`, `REF`, `ALT`, `genome`, `uniprot_id`,
/// `transcript_id`, `protein_variant`, `am_pathogenicity`, `am_class`.
#[derive(Debug, PartialEq)]
pub struct AlphaMissenseRecord {
    /// Chave canônica `CHROM:POS:REF:ALT` (GRCh38, sem `chr`).
    pub variant_key: String,
    /// `uniprot_id` da linha AM (ex.: `"Q9Y6I3"`).
    pub uniprot_id: String,
    /// `am_pathogenicity` ∈ [0,1] — score contínuo de patogenicidade missense.
    pub am_pathogenicity: f64,
    /// `am_class` — classe categórica: `"likely_benign"`, `"ambiguous"`, `"likely_pathogenic"`.
    pub am_class: String,
}

/// Parseia uma linha TSV AlphaMissense.
///
/// Retorna `Ok(Some(record))` se a linha é válida e contém `am_pathogenicity`.
/// Retorna `Ok(None)` se a linha deve ser descartada (multi-alélico, campo ausente).
/// Retorna `Err(msg)` se o formato é inesperado.
///
/// Aceita tanto o cabeçalho com `#CHROM` quanto com `CHROM`.
pub fn parse_alphamissense_line(line: &str, col_idx: &AlphaMissenseColIdx) -> Result<Option<AlphaMissenseRecord>, String> {
    let fields: Vec<&str> = line.split('\t').collect();
    let n_required = [
        col_idx.chrom, col_idx.pos, col_idx.ref_allele, col_idx.alt_allele,
        col_idx.uniprot_id, col_idx.am_pathogenicity, col_idx.am_class,
    ].iter().copied().max().unwrap_or(0) + 1;

    if fields.len() < n_required {
        return Err(format!(
            "AM linha com {} colunas (esperava >= {})", fields.len(), n_required
        ));
    }

    let chrom = fields[col_idx.chrom];
    let pos = fields[col_idx.pos];
    let ref_allele = fields[col_idx.ref_allele];
    let alt_allele = fields[col_idx.alt_allele];
    let uniprot_id = fields[col_idx.uniprot_id].trim().to_string();
    let am_path_str = fields[col_idx.am_pathogenicity].trim();
    let am_class = fields[col_idx.am_class].trim().to_string();

    // Normalizar variant_key (remove 'chr' do CHROM)
    let variant_key = match normalize_variant_key(chrom, pos, ref_allele, alt_allele) {
        Some(k) => k,
        None => return Ok(None),
    };

    // Parse am_pathogenicity
    let am_pathogenicity: f64 = match am_path_str.parse() {
        Ok(v) => v,
        Err(_) => return Ok(None), // linha sem score → descarta
    };

    if uniprot_id.is_empty() {
        return Ok(None);
    }

    Ok(Some(AlphaMissenseRecord {
        variant_key,
        uniprot_id,
        am_pathogenicity,
        am_class,
    }))
}

/// Índices de coluna do cabeçalho AlphaMissense (detectados por nome).
#[derive(Debug, Clone)]
pub struct AlphaMissenseColIdx {
    pub chrom: usize,
    pub pos: usize,
    pub ref_allele: usize,
    pub alt_allele: usize,
    pub uniprot_id: usize,
    pub am_pathogenicity: usize,
    pub am_class: usize,
}

/// Detecta os índices de coluna do cabeçalho AlphaMissense.
///
/// Aceita linhas com `#CHROM` ou `CHROM` (o arquivo real usa `#CHROM`).
pub fn parse_am_header(header_line: &str) -> Result<AlphaMissenseColIdx, String> {
    let fields: Vec<&str> = header_line.split('\t').collect();
    let mut idx: std::collections::HashMap<String, usize> = std::collections::HashMap::new();
    for (i, f) in fields.iter().enumerate() {
        // Strip '#' do primeiro campo
        let name = f.trim_start_matches('#').trim().to_string();
        idx.insert(name.to_uppercase(), i);
    }

    let get = |name: &str| -> Result<usize, String> {
        idx.get(name)
            .copied()
            .ok_or_else(|| format!("Coluna '{}' não encontrada no cabeçalho AM", name))
    };

    Ok(AlphaMissenseColIdx {
        chrom: get("CHROM")?,
        pos: get("POS")?,
        ref_allele: get("REF")?,
        alt_allele: get("ALT")?,
        uniprot_id: get("UNIPROT_ID")?,
        am_pathogenicity: get("AM_PATHOGENICITY")?,
        am_class: get("AM_CLASS")?,
    })
}

// ─── Extração de source_version do header VCF ClinVar ────────────────────────

/// Extrai a versão da fonte do cabeçalho VCF ClinVar.
///
/// Procura `##fileDate=YYYYMMDD` e `##dbSNP_BUILD_ID=xxx` no cabeçalho.
/// Retorna string no formato `"clinvar/GRCh38/YYYYMMDD"` ou fallback da data de download.
pub fn extract_clinvar_source_version(header_lines: &[String]) -> String {
    let mut file_date = String::new();
    let mut reference = String::new();

    for line in header_lines {
        if line.starts_with("##fileDate=") {
            file_date = line["##fileDate=".len()..].to_string();
        } else if line.starts_with("##reference=") {
            reference = line["##reference=".len()..].to_string();
        }
    }

    if file_date.is_empty() {
        file_date = Utc::now().format("%Y%m%d").to_string();
    }

    let build = if reference.is_empty() { "GRCh38" } else { &reference };
    format!("clinvar/{}/{}", build, file_date)
}

// ─── Validação de URL ─────────────────────────────────────────────────────────

/// Valida que a URL pertence à allowlist de hosts permitidos para loaders de variantes.
///
/// Hosts permitidos:
/// - `ftp.ncbi.nlm.nih.gov` (ClinVar VCF GRCh38)
/// - `storage.googleapis.com` (AlphaMissense GCS bucket)
/// - `zenodo.org` (AlphaMissense Zenodo mirror)
///
/// Rejeita qualquer URL com host diferente ou esquema diferente de `https`,
/// prevenindo SSRF caso o caller passe uma URL não confiável.
pub fn validate_variant_effect_url(url: &str) -> Result<(), String> {
    let allowed_prefixes = [
        "https://ftp.ncbi.nlm.nih.gov/",
        "https://storage.googleapis.com/",
        "https://zenodo.org/",
    ];

    for prefix in &allowed_prefixes {
        if url.starts_with(prefix) {
            // Verificar ausência de path traversal
            let path_part = &url[prefix.len()..];
            if path_part.contains("..") {
                return Err(format!(
                    "URL rejeitada: contém path traversal '..' em '{}'", url
                ));
            }
            return Ok(());
        }
    }

    Err(format!(
        "URL rejeitada por política de host: '{}'. \
         Apenas ftp.ncbi.nlm.nih.gov, storage.googleapis.com e zenodo.org são permitidos \
         para loaders de variantes.",
        url
    ))
}

// ─── CSV/COPY helpers ─────────────────────────────────────────────────────────

fn escape_csv_field(s: &str) -> String {
    format!("\"{}\"", s.replace('"', "\"\""))
}

fn sanitize_str(s: &str, max_len: usize) -> String {
    let sanitized = s.replace('\0', "");
    if sanitized.len() <= max_len {
        sanitized
    } else {
        let mut end = max_len;
        while !sanitized.is_char_boundary(end) && end > 0 {
            end -= 1;
        }
        sanitized[..end].to_string()
    }
}

// ─── COPY UPSERT em core_varianteffectraw ────────────────────────────────────

/// Uma linha para COPY UPSERT em `core_varianteffectraw`.
struct VariantEffectRawRow {
    variant_key: String,
    gene_symbol: String,
    source: String,         // 'clinvar' | 'alphamissense' | 'dbnsfp'
    raw_magnitude: Option<f64>,
    raw_class: String,
    clinvar_significance: String,
    oncogenicity: String,
    am_pathogenicity: Option<f64>,
    confidence: Option<f64>,
    source_version: String,
}

/// COPY UPSERT de `VariantEffectRawRow` em `core_varianteffectraw`.
///
/// ON CONFLICT (variant_key, source, gene_symbol) DO UPDATE — idempotente.
async fn copy_variant_effect_raw(
    client: &Client,
    rows: &[VariantEffectRawRow],
) -> Result<u64, tokio_postgres::Error> {
    if rows.is_empty() {
        return Ok(0);
    }

    // Staging table
    client
        .execute("DROP TABLE IF EXISTS _staging_varianteffectraw", &[])
        .await?;
    client
        .execute(
            "CREATE TEMP TABLE _staging_varianteffectraw (
                variant_key          VARCHAR(128),
                gene_symbol          VARCHAR(50),
                source               VARCHAR(20),
                raw_magnitude        DOUBLE PRECISION,
                raw_class            VARCHAR(128),
                clinvar_significance VARCHAR(255),
                oncogenicity         VARCHAR(64),
                am_pathogenicity     DOUBLE PRECISION,
                confidence           DOUBLE PRECISION,
                source_version       VARCHAR(255),
                loaded_at            TIMESTAMPTZ
            )",
            &[],
        )
        .await?;

    let csv = build_variant_effect_raw_csv(rows);

    // COPY FROM STDIN
    {
        let sink = client
            .copy_in(
                "COPY _staging_varianteffectraw (
                    variant_key, gene_symbol, source, raw_magnitude, raw_class,
                    clinvar_significance, oncogenicity, am_pathogenicity,
                    confidence, source_version, loaded_at
                ) FROM STDIN WITH (FORMAT csv, NULL 'NULL')",
            )
            .await?;
        let mut sink = pin!(sink);
        sink.send(Bytes::from(csv.as_bytes().to_vec())).await?;
        sink.finish().await?;
    }

    // UPSERT ON CONFLICT (variant_key, source, gene_symbol)
    let affected = client
        .execute(
            "INSERT INTO core_varianteffectraw (
                variant_key, gene_symbol, source, raw_magnitude, raw_class,
                clinvar_significance, oncogenicity, am_pathogenicity,
                confidence, source_version, loaded_at
            )
            SELECT variant_key, gene_symbol, source, raw_magnitude, raw_class,
                   clinvar_significance, oncogenicity, am_pathogenicity,
                   confidence, source_version, loaded_at
            FROM _staging_varianteffectraw
            ON CONFLICT (variant_key, source, gene_symbol) DO UPDATE SET
                raw_magnitude        = COALESCE(EXCLUDED.raw_magnitude, core_varianteffectraw.raw_magnitude),
                raw_class            = EXCLUDED.raw_class,
                clinvar_significance = EXCLUDED.clinvar_significance,
                oncogenicity         = COALESCE(NULLIF(EXCLUDED.oncogenicity, ''), core_varianteffectraw.oncogenicity),
                am_pathogenicity     = COALESCE(EXCLUDED.am_pathogenicity, core_varianteffectraw.am_pathogenicity),
                confidence           = COALESCE(EXCLUDED.confidence, core_varianteffectraw.confidence),
                source_version       = EXCLUDED.source_version,
                loaded_at            = EXCLUDED.loaded_at",
            &[],
        )
        .await?;

    client
        .execute("DROP TABLE IF EXISTS _staging_varianteffectraw", &[])
        .await?;

    Ok(affected)
}

fn build_variant_effect_raw_csv(rows: &[VariantEffectRawRow]) -> String {
    let now = Utc::now().format("%Y-%m-%d %H:%M:%S%.6f+00").to_string();
    let mut csv = String::new();
    for r in rows {
        csv.push_str(&format!(
            "{},{},{},{},{},{},{},{},{},{},{}\n",
            escape_csv_field(&sanitize_str(&r.variant_key, 128)),
            escape_csv_field(&sanitize_str(&r.gene_symbol, 50)),
            escape_csv_field(&sanitize_str(&r.source, 20)),
            r.raw_magnitude.map_or("NULL".to_string(), |v| v.to_string()),
            escape_csv_field(&sanitize_str(&r.raw_class, 128)),
            escape_csv_field(&sanitize_str(&r.clinvar_significance, 255)),
            escape_csv_field(&sanitize_str(&r.oncogenicity, 64)),
            r.am_pathogenicity.map_or("NULL".to_string(), |v| v.to_string()),
            r.confidence.map_or("NULL".to_string(), |v| v.to_string()),
            escape_csv_field(&sanitize_str(&r.source_version, 255)),
            &now,
        ));
    }
    csv
}

// ─── Download streaming com User-Agent ───────────────────────────────────────

/// Download streaming de arquivo gzip para disco, retorna caminho do arquivo gravado.
async fn download_gzip_file(
    client: &reqwest::Client,
    url: &str,
    dest_path: &Path,
) -> Result<(), String> {
    use futures::StreamExt;
    use tokio::io::AsyncWriteExt;

    if let Some(parent) = dest_path.parent() {
        tokio::fs::create_dir_all(parent)
            .await
            .map_err(|e| format!("create_dir_all {:?}: {}", parent, e))?;
    }

    let response = client
        .get(url)
        .send()
        .await
        .map_err(|e| format!("HTTP GET falhou para {}: {}", url, e))?;

    let status = response.status();
    if !status.is_success() {
        return Err(format!("HTTP {} ao baixar {}", status, url));
    }

    let mut file = tokio::fs::File::create(dest_path)
        .await
        .map_err(|e| format!("Falha ao criar {:?}: {}", dest_path, e))?;

    let mut stream = response.bytes_stream();

    while let Some(chunk_result) = stream.next().await {
        let chunk = chunk_result
            .map_err(|e| format!("Erro ao ler chunk de {}: {}", url, e))?;
        file.write_all(&chunk)
            .await
            .map_err(|e| format!("Erro ao escrever em {:?}: {}", dest_path, e))?;
    }

    file.flush()
        .await
        .map_err(|e| format!("Erro ao fechar {:?}: {}", dest_path, e))?;

    Ok(())
}

/// Cria cliente HTTP com User-Agent de browser e timeout longo (arquivos grandes).
///
/// # Hardening A4 (laudo 007) — redirect proibido, fonte legada corrigida
///
/// Checagem empírica (`curl -IL`) mostrou que a URL legada do mirror Zenodo
/// para AlphaMissense (`https://zenodo.org/record/<id>/files/...`, singular)
/// responde **301** para a forma canônica `https://zenodo.org/records/<id>/files/...`
/// (plural, mesmo host) — resquício de uma migração de path do Zenodo, não CDN
/// nem balanceamento. A forma canônica responde **200 direto**, assim como
/// `ftp.ncbi.nlm.nih.gov` e `storage.googleapis.com`. Logo, nenhuma das três
/// fontes desta allowlist depende de redirect — `Policy::none()` fecha o A4
/// por completo, sem política customizada de revalidação por salto.
///
/// **Callers devem usar a forma plural** (`zenodo.org/records/...`) para o
/// mirror Zenodo; a forma singular passa em `validate_variant_effect_url`
/// (que só checa host) mas falha no client (redirect proibido).
fn build_http_client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(1800)) // 30 min — ClinVar ~192MB, AM ~643MB
        .redirect(reqwest::redirect::Policy::none())
        .user_agent(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) \
             AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        .build()
        .map_err(|e| format!("Falha ao criar HTTP client: {}", e))
}

// ─── Loader ClinVar ───────────────────────────────────────────────────────────

/// Parse streaming do VCF ClinVar GRCh38 (gzip) e COPY UPSERT em `core_varianteffectraw`.
///
/// Lee o arquivo gzip linha-a-linha (sem descomprimir na memória), extrai os campos
/// relevantes, aplica o filtro `gene_allowlist` e faz COPY UPSERT em lotes.
///
/// # Fluxo
///
/// 1. Abre o `.vcf.gz` do disco (já baixado por `load_clinvar_effects_async`).
/// 2. Parse streaming: linhas `##` → cabeçalho (extrai `source_version`); linhas `#CHROM`
///    → cabeçalho de colunas (ignorado — VCF fixo); linhas de dados → `parse_clinvar_vcf_line`.
/// 3. Para cada registro válido: filtra `gene_symbol ∈ gene_allowlist`.
/// 4. Acumula em buffer de `BATCH_SIZE` linhas; flush via `copy_variant_effect_raw`.
/// 5. Retorna `ClinVarLoadManifest`.
pub async fn parse_and_copy_clinvar(
    gz_path: &Path,
    gene_allowlist: &HashSet<String>,
    source_version: &str,
    client: &Client,
) -> Result<ClinVarLoadManifest, String> {
    const BATCH_SIZE: usize = 10_000;

    let file = std::fs::File::open(gz_path)
        .map_err(|e| format!("Falha ao abrir {:?}: {}", gz_path, e))?;
    let gz_reader = GzDecoder::new(file);
    let buf_reader = BufReader::new(gz_reader);

    let mut header_lines: Vec<String> = Vec::new();
    let mut n_variants_processed = 0usize;
    let mut n_kept = 0usize;
    let mut n_skipped_offlist = 0usize;
    let mut n_skipped_no_gene = 0usize;
    let mut n_upserted = 0u64;
    let mut errors: Vec<String> = Vec::new();
    let mut batch: Vec<VariantEffectRawRow> = Vec::with_capacity(BATCH_SIZE);
    let mut resolved_version = source_version.to_string();
    let mut version_extracted = false;

    for (line_num, line_result) in buf_reader.lines().enumerate() {
        let line = match line_result {
            Ok(l) => l,
            Err(e) => {
                errors.push(format!("Erro ao ler linha {}: {}", line_num + 1, e));
                continue;
            }
        };

        let line = line.trim_end_matches('\r');

        // Linhas de comentário/meta do VCF (##)
        if line.starts_with("##") {
            header_lines.push(line.to_string());
            continue;
        }

        // Linha de cabeçalho de colunas (#CHROM)
        if line.starts_with('#') {
            // Extrair source_version do cabeçalho acumulado
            if !version_extracted {
                resolved_version = extract_clinvar_source_version(&header_lines);
                version_extracted = true;
            }
            continue;
        }

        if line.is_empty() {
            continue;
        }

        // Extrair source_version se ainda não foi feito (caso VCF sem linha #CHROM)
        if !version_extracted {
            resolved_version = extract_clinvar_source_version(&header_lines);
            version_extracted = true;
        }

        n_variants_processed += 1;

        // Parse da linha de dados
        match parse_clinvar_vcf_line(line) {
            Ok(None) => {
                // Descartado por multi-alélico, sem GENEINFO, etc.
                n_skipped_no_gene += 1;
            }
            Ok(Some(record)) => {
                // Filtro de allowlist
                if !gene_allowlist.contains(&record.gene_symbol) {
                    n_skipped_offlist += 1;
                    continue;
                }

                n_kept += 1;
                batch.push(VariantEffectRawRow {
                    variant_key: record.variant_key,
                    gene_symbol: record.gene_symbol,
                    source: "clinvar".to_string(),
                    raw_magnitude: None, // ClinVar é categórico
                    raw_class: sanitize_str(&record.raw_class, 128),
                    clinvar_significance: sanitize_str(&record.clinvar_significance, 255),
                    oncogenicity: sanitize_str(&record.oncogenicity, 64),
                    am_pathogenicity: None,
                    confidence: Some(0.9), // ClinVar é curado
                    source_version: resolved_version.clone(),
                });

                // Flush do lote
                if batch.len() >= BATCH_SIZE {
                    match copy_variant_effect_raw(client, &batch).await {
                        Ok(n) => n_upserted += n,
                        Err(e) => {
                            errors.push(format!("COPY batch error (linha ~{}): {:?}", line_num + 1, e));
                        }
                    }
                    batch.clear();
                }
            }
            Err(e) => {
                errors.push(format!("Linha {} parse error: {}", line_num + 1, e));
            }
        }
    }

    // Flush do lote final
    if !batch.is_empty() {
        match copy_variant_effect_raw(client, &batch).await {
            Ok(n) => n_upserted += n,
            Err(e) => errors.push(format!("COPY final batch error: {:?}", e)),
        }
    }

    eprintln!(
        "[variant_effect_loader] ClinVar: processadas={} kept={} offlist={} no_gene={} upserted={}",
        n_variants_processed, n_kept, n_skipped_offlist, n_skipped_no_gene, n_upserted
    );

    Ok(ClinVarLoadManifest {
        n_variants_processed,
        n_kept,
        n_skipped_offlist,
        n_skipped_no_gene,
        n_upserted,
        source_version: resolved_version,
        errors,
    })
}

/// Loader assíncrono de ClinVar: download + parse + COPY.
pub async fn load_clinvar_effects_async(
    url: &str,
    dest_dir: &Path,
    db_url: &str,
    gene_allowlist: &HashSet<String>,
) -> Result<ClinVarLoadManifest, String> {
    // 1. Validar URL (proteção SSRF)
    validate_variant_effect_url(url)?;

    // 2. Cliente HTTP
    let http_client = build_http_client()?;

    // 3. Download do VCF GRCh38 gzip
    tokio::fs::create_dir_all(dest_dir)
        .await
        .map_err(|e| format!("create_dir_all {:?}: {}", dest_dir, e))?;

    let gz_path: PathBuf = dest_dir.join("clinvar_GRCh38.vcf.gz");
    eprintln!("[variant_effect_loader] baixando ClinVar: {}", url);
    download_gzip_file(&http_client, url, &gz_path).await?;
    eprintln!("[variant_effect_loader] ClinVar gravado em {:?}", gz_path);

    // 4. Conectar ao banco
    let client = crate::db::connection::connect_db(db_url)
        .await
        .map_err(|e| format!("DB connection failed: {:?}", e))?;

    // 5. Parse streaming + COPY
    let source_version = Utc::now().format("%Y-%m-%d").to_string(); // fallback
    parse_and_copy_clinvar(&gz_path, gene_allowlist, &source_version, &client).await
}

/// Entry point síncrono para PyO3.
pub fn load_clinvar_effects(
    url: &str,
    dest_dir: &str,
    db_url: &str,
    gene_allowlist: Vec<String>,
) -> Result<ClinVarLoadManifest, String> {
    let rt = tokio::runtime::Runtime::new()
        .map_err(|e| format!("Falha ao criar runtime Tokio: {}", e))?;
    let dest_path = Path::new(dest_dir);
    let allowlist: HashSet<String> = gene_allowlist.into_iter().collect();
    rt.block_on(load_clinvar_effects_async(url, dest_path, db_url, &allowlist))
}

// ─── Loader AlphaMissense ─────────────────────────────────────────────────────

/// Parse streaming do TSV AlphaMissense hg38 (gzip) e COPY UPSERT.
///
/// Requer `uniprot_to_gene`: mapa `uniprot_id → gene_symbol` para os genes do GeneRole.
/// Se o mapa estiver vazio, a função retorna imediatamente com `handoff_required=true`
/// (AM não pode ser filtrado por gene sem o mapa).
///
/// # Decisão de filtro AlphaMissense (HANDOFF v1)
///
/// AlphaMissense não tem `gene_symbol` na coluna — só `uniprot_id` e `transcript_id`.
/// Para filtrar por gene, o caller deve fornecer um mapa `uniprot_id → gene_symbol`
/// derivado de uma fonte externa (UniProt, Ensembl). Sem esse mapa, AM não é carregado
/// (seria necessário carregar 71M linhas sem filtro → viola Regra #-1).
///
/// O mapa deve conter apenas os genes do `GeneRole` (subset do OncoKB), o que mantém
/// o volume controlado mesmo que AM tenha múltiplos transcritos por gene.
pub async fn parse_and_copy_alphamissense(
    gz_path: &Path,
    uniprot_to_gene: &HashMap<String, String>, // uniprot_id → gene_symbol UPPERCASE
    gene_allowlist: &HashSet<String>,
    source_version: &str,
    client: &Client,
) -> Result<AlphaMissenseLoadManifest, String> {
    // Se mapa vazio → handoff requerido
    if uniprot_to_gene.is_empty() {
        eprintln!(
            "[variant_effect_loader] AlphaMissense: mapa uniprot→gene vazio. \
             Handoff requerido — forneça o mapa derivado de UniProt/Ensembl para os genes do GeneRole."
        );
        return Ok(AlphaMissenseLoadManifest {
            n_variants_processed: 0,
            n_kept: 0,
            n_skipped_no_map: 0,
            n_upserted: 0,
            source_version: source_version.to_string(),
            errors: vec![
                "AlphaMissense: mapa uniprot_id→gene_symbol não fornecido. \
                 AM não carregado no v1. Handoff vitruvio: fornecer mapa para v2."
                    .to_string()
            ],
            handoff_required: true,
        });
    }

    const BATCH_SIZE: usize = 10_000;

    let file = std::fs::File::open(gz_path)
        .map_err(|e| format!("Falha ao abrir {:?}: {}", gz_path, e))?;
    let gz_reader = GzDecoder::new(file);
    let buf_reader = BufReader::new(gz_reader);

    let mut n_variants_processed = 0usize;
    let mut n_kept = 0usize;
    let mut n_skipped_no_map = 0usize;
    let mut n_upserted = 0u64;
    let mut errors: Vec<String> = Vec::new();
    let mut batch: Vec<VariantEffectRawRow> = Vec::with_capacity(BATCH_SIZE);
    let mut col_idx: Option<AlphaMissenseColIdx> = None;

    for (line_num, line_result) in buf_reader.lines().enumerate() {
        let line = match line_result {
            Ok(l) => l,
            Err(e) => {
                errors.push(format!("Erro ao ler linha {}: {}", line_num + 1, e));
                continue;
            }
        };

        let line = line.trim_end_matches('\r');

        // Comentários (linhas com ##)
        if line.starts_with("##") {
            continue;
        }

        // Cabeçalho (primeira linha não-## que começa com # ou é a primeira linha de dados)
        if col_idx.is_none() {
            // Tentar detectar o cabeçalho
            if line.starts_with('#') || line.to_uppercase().contains("CHROM") {
                match parse_am_header(line) {
                    Ok(idx) => {
                        col_idx = Some(idx);
                        continue;
                    }
                    Err(e) => {
                        errors.push(format!("Erro ao parsear cabeçalho AM: {}", e));
                        return Ok(AlphaMissenseLoadManifest {
                            n_variants_processed: 0,
                            n_kept: 0,
                            n_skipped_no_map: 0,
                            n_upserted: 0,
                            source_version: source_version.to_string(),
                            errors,
                            handoff_required: false,
                        });
                    }
                }
            }
        }

        let idx = match &col_idx {
            Some(i) => i,
            None => {
                errors.push(format!("Linha {} antes do cabeçalho AM", line_num + 1));
                continue;
            }
        };

        if line.is_empty() {
            continue;
        }

        n_variants_processed += 1;

        match parse_alphamissense_line(line, idx) {
            Ok(None) => {
                n_skipped_no_map += 1;
            }
            Ok(Some(record)) => {
                // Mapear uniprot_id → gene_symbol
                let gene_symbol = match uniprot_to_gene.get(&record.uniprot_id) {
                    Some(g) => g.clone(),
                    None => {
                        n_skipped_no_map += 1;
                        continue;
                    }
                };

                // Filtro de allowlist
                if !gene_allowlist.contains(&gene_symbol) {
                    n_skipped_no_map += 1;
                    continue;
                }

                n_kept += 1;
                batch.push(VariantEffectRawRow {
                    variant_key: record.variant_key,
                    gene_symbol,
                    source: "alphamissense".to_string(),
                    raw_magnitude: Some(record.am_pathogenicity),
                    raw_class: sanitize_str(&record.am_class, 128),
                    clinvar_significance: String::new(),
                    oncogenicity: String::new(),
                    am_pathogenicity: Some(record.am_pathogenicity),
                    confidence: Some(0.7), // preditor (AlphaMissense)
                    source_version: source_version.to_string(),
                });

                if batch.len() >= BATCH_SIZE {
                    match copy_variant_effect_raw(client, &batch).await {
                        Ok(n) => n_upserted += n,
                        Err(e) => {
                            errors.push(format!("COPY batch error AM (linha ~{}): {:?}", line_num + 1, e));
                        }
                    }
                    batch.clear();
                }
            }
            Err(e) => {
                errors.push(format!("AM linha {} parse error: {}", line_num + 1, e));
            }
        }
    }

    // Flush final
    if !batch.is_empty() {
        match copy_variant_effect_raw(client, &batch).await {
            Ok(n) => n_upserted += n,
            Err(e) => errors.push(format!("COPY final batch error AM: {:?}", e)),
        }
    }

    eprintln!(
        "[variant_effect_loader] AlphaMissense: processadas={} kept={} skipped={} upserted={}",
        n_variants_processed, n_kept, n_skipped_no_map, n_upserted
    );

    Ok(AlphaMissenseLoadManifest {
        n_variants_processed,
        n_kept,
        n_skipped_no_map,
        n_upserted,
        source_version: source_version.to_string(),
        errors,
        handoff_required: false,
    })
}

/// Loader assíncrono de AlphaMissense: download + parse + COPY.
///
/// Se `uniprot_to_gene` estiver vazio, retorna `handoff_required=true` sem baixar.
pub async fn load_alphamissense_effects_async(
    url: &str,
    dest_dir: &Path,
    db_url: &str,
    uniprot_to_gene: &HashMap<String, String>,
    gene_allowlist: &HashSet<String>,
) -> Result<AlphaMissenseLoadManifest, String> {
    // Se mapa vazio → não baixa (handoff)
    if uniprot_to_gene.is_empty() {
        return Ok(AlphaMissenseLoadManifest {
            n_variants_processed: 0,
            n_kept: 0,
            n_skipped_no_map: 0,
            n_upserted: 0,
            source_version: Utc::now().format("%Y-%m-%d").to_string(),
            errors: vec![
                "AlphaMissense: mapa uniprot_id→gene_symbol não fornecido. \
                 AM não carregado no v1. Handoff vitruvio: fornecer mapa para v2."
                    .to_string()
            ],
            handoff_required: true,
        });
    }

    // 1. Validar URL
    validate_variant_effect_url(url)?;

    // 2. Cliente HTTP
    let http_client = build_http_client()?;

    // 3. Download
    tokio::fs::create_dir_all(dest_dir)
        .await
        .map_err(|e| format!("create_dir_all {:?}: {}", dest_dir, e))?;

    let gz_path: PathBuf = dest_dir.join("AlphaMissense_hg38.tsv.gz");
    eprintln!("[variant_effect_loader] baixando AlphaMissense: {}", url);
    download_gzip_file(&http_client, url, &gz_path).await?;
    eprintln!("[variant_effect_loader] AlphaMissense gravado em {:?}", gz_path);

    // 4. Conectar ao banco
    let client = crate::db::connection::connect_db(db_url)
        .await
        .map_err(|e| format!("DB connection failed: {:?}", e))?;

    // 5. Parse + COPY
    let source_version = Utc::now().format("%Y-%m-%d").to_string();
    parse_and_copy_alphamissense(
        &gz_path, uniprot_to_gene, gene_allowlist, &source_version, &client,
    ).await
}

/// Entry point síncrono para PyO3.
pub fn load_alphamissense_effects(
    url: &str,
    dest_dir: &str,
    db_url: &str,
    uniprot_to_gene: HashMap<String, String>,
    gene_allowlist: Vec<String>,
) -> Result<AlphaMissenseLoadManifest, String> {
    let rt = tokio::runtime::Runtime::new()
        .map_err(|e| format!("Falha ao criar runtime Tokio: {}", e))?;
    let dest_path = Path::new(dest_dir);
    let allowlist: HashSet<String> = gene_allowlist.into_iter().collect();
    rt.block_on(load_alphamissense_effects_async(url, dest_path, db_url, &uniprot_to_gene, &allowlist))
}

// ─── Testes unitários (#[cfg(test)]) — SEM rede / SEM DB ────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use tempfile::TempDir;

    // ── normalize_variant_key ──────────────────────────────────────────────────

    #[test]
    fn test_normalize_snv_sem_chr() {
        // CHROM já sem 'chr' — deve passar como-está
        assert_eq!(
            normalize_variant_key("7", "117548628", "G", "A"),
            Some("7:117548628:G:A".to_string())
        );
    }

    #[test]
    fn test_normalize_snv_com_chr() {
        // ClinVar vem sem 'chr'; AlphaMissense vem com 'chr' → remove
        assert_eq!(
            normalize_variant_key("chr7", "117548628", "G", "A"),
            Some("7:117548628:G:A".to_string())
        );
    }

    #[test]
    fn test_normalize_snv_chr_uppercase() {
        assert_eq!(
            normalize_variant_key("CHR7", "117548628", "G", "A"),
            Some("7:117548628:G:A".to_string())
        );
    }

    #[test]
    fn test_normalize_x_chromosome() {
        assert_eq!(
            normalize_variant_key("chrX", "1000", "A", "T"),
            Some("X:1000:A:T".to_string())
        );
        assert_eq!(
            normalize_variant_key("X", "1000", "A", "T"),
            Some("X:1000:A:T".to_string())
        );
    }

    #[test]
    fn test_normalize_mt_chromosome() {
        assert_eq!(
            normalize_variant_key("chrMT", "1000", "A", "T"),
            Some("MT:1000:A:T".to_string())
        );
    }

    #[test]
    fn test_normalize_multi_allelico_descartado() {
        // Multi-alélico → None
        assert!(normalize_variant_key("1", "100", "A", "A,T").is_none());
    }

    #[test]
    fn test_normalize_spanning_deletion_descartado() {
        assert!(normalize_variant_key("1", "100", "A", "*").is_none());
    }

    #[test]
    fn test_normalize_alt_vazio_descartado() {
        assert!(normalize_variant_key("1", "100", "A", "").is_none());
    }

    #[test]
    fn test_normalize_delecao_simples_strip_padding() {
        // VCF: REF=AGCT, ALT=A (deleção com base de ancoragem A)
        // Após strip: REF=GCT, ALT='', POS avança 1
        let result = normalize_variant_key("1", "100", "AGCT", "A");
        assert!(result.is_some());
        let key = result.unwrap();
        // POS deve ser 101 (avançou 1); CHROM=1
        assert!(key.starts_with("1:101:"), "key={}", key);
    }

    #[test]
    fn test_normalize_insercao_simples_strip_padding() {
        // VCF: REF=A, ALT=AGT (inserção com base de ancoragem A)
        // Após strip: REF='', ALT=GT, POS avança 1
        let result = normalize_variant_key("1", "100", "A", "AGT");
        assert!(result.is_some());
        let key = result.unwrap();
        assert!(key.starts_with("1:101:"), "key={}", key);
    }

    #[test]
    fn test_normalize_indel_sem_base_comum() {
        // REF e ALT não compartilham a 1ª base → sem strip, POS mantido
        let result = normalize_variant_key("1", "100", "ACG", "TGT");
        assert!(result.is_some());
        let key = result.unwrap();
        assert!(key.starts_with("1:100:"), "key={}", key);
    }

    #[test]
    fn test_normalize_consistencia_entre_fontes() {
        // ClinVar (sem chr) e AlphaMissense (com chr) devem gerar a mesma chave para SNV
        let clinvar_key = normalize_variant_key("7", "117548628", "G", "A");
        let am_key = normalize_variant_key("chr7", "117548628", "G", "A");
        assert_eq!(clinvar_key, am_key, "Chaves devem ser idênticas entre ClinVar e AM");
    }

    // ── strip_chr_prefix ───────────────────────────────────────────────────────

    #[test]
    fn test_strip_chr_prefix_variantes() {
        assert_eq!(strip_chr_prefix("chr7"), "7");
        assert_eq!(strip_chr_prefix("CHR7"), "7");
        assert_eq!(strip_chr_prefix("Chr7"), "7");
        assert_eq!(strip_chr_prefix("7"), "7");
        assert_eq!(strip_chr_prefix("chrX"), "X");
        assert_eq!(strip_chr_prefix("chrMT"), "MT");
        // "chr" sozinho deve retornar "" (len=3, condição len>3 falsa → retorna "chr")
        // N.B.: len("chr") == 3, não > 3, então retorna "chr" sem strip
        assert_eq!(strip_chr_prefix("chr"), "chr");
    }

    // ── extract_geneinfo_symbol ────────────────────────────────────────────────

    #[test]
    fn test_extract_geneinfo_symbol_simples() {
        assert_eq!(extract_geneinfo_symbol("TP53:7157"), "TP53");
    }

    #[test]
    fn test_extract_geneinfo_symbol_multiplos() {
        // Múltiplos genes separados por '|' → retorna o primeiro
        assert_eq!(extract_geneinfo_symbol("VHL:7428|EGFR:1956"), "VHL");
    }

    #[test]
    fn test_extract_geneinfo_symbol_uppercase() {
        // Lowercase no arquivo → converte para UPPERCASE
        assert_eq!(extract_geneinfo_symbol("tp53:7157"), "TP53");
    }

    #[test]
    fn test_extract_geneinfo_symbol_vazio() {
        assert_eq!(extract_geneinfo_symbol(""), "");
    }

    #[test]
    fn test_extract_geneinfo_symbol_sem_id() {
        // Formato sem ':' → símbolo é o campo inteiro
        assert_eq!(extract_geneinfo_symbol("BRCA1"), "BRCA1");
    }

    // ── extract_info_field ────────────────────────────────────────────────────

    #[test]
    fn test_extract_info_field_encontrado() {
        let info = "GENEINFO=TP53:7157;CLNSIG=Pathogenic;CLNVC=single_nucleotide_variant";
        assert_eq!(extract_info_field(info, "GENEINFO"), "TP53:7157");
        assert_eq!(extract_info_field(info, "CLNSIG"), "Pathogenic");
    }

    #[test]
    fn test_extract_info_field_ausente() {
        let info = "GENEINFO=TP53:7157;CLNSIG=Pathogenic";
        assert_eq!(extract_info_field(info, "ONCOGENICITY"), "");
    }

    #[test]
    fn test_extract_info_field_flag_booleana() {
        let info = "CLNSIG=Pathogenic;RS=123456;DBSNP_FLAG";
        assert_eq!(extract_info_field(info, "DBSNP_FLAG"), "true");
    }

    #[test]
    fn test_extract_info_field_ultimo_campo() {
        let info = "A=1;B=2;CLNSIG=Pathogenic";
        assert_eq!(extract_info_field(info, "CLNSIG"), "Pathogenic");
    }

    // ── parse_clinvar_vcf_line ────────────────────────────────────────────────

    /// Fixture de linha VCF ClinVar (SNV Pathogenic em TP53)
    /// Colunas: CHROM POS ID REF ALT QUAL FILTER INFO
    const CLINVAR_SNV_LINE: &str = "7\t117548628\t12375\tG\tA\t.\t.\tAF_EXAC=0.00001;ALLELEID=28397;CLNDISDB=MedGen:CN169374;CLNDN=not_provided;CLNHGVS=NC_000007.14:g.117548628G>A;CLNREVSTAT=criteria_provided,_single_submitter;CLNSIG=Pathogenic;CLNVC=single_nucleotide_variant;CLNVCSO=SO:0001483;GENEINFO=CFTR:1080;MC=SO:0001587|nonsense;ONCOGENICITY=Oncogenic";

    #[test]
    fn test_parse_clinvar_vcf_line_snv_pathogenic() {
        let result = parse_clinvar_vcf_line(CLINVAR_SNV_LINE).unwrap();
        assert!(result.is_some(), "Linha válida deve retornar Some");
        let record = result.unwrap();

        assert_eq!(record.variant_key, "7:117548628:G:A");
        assert_eq!(record.gene_symbol, "CFTR");
        assert_eq!(record.clinvar_significance, "Pathogenic");
        assert_eq!(record.raw_class, "Pathogenic");
        assert_eq!(record.oncogenicity, "Oncogenic");
    }

    #[test]
    fn test_parse_clinvar_vcf_line_sem_geneinfo() {
        // Linha sem GENEINFO → None
        let line = "7\t117548628\t12375\tG\tA\t.\t.\tCLNSIG=Pathogenic;CLNVC=single_nucleotide_variant";
        let result = parse_clinvar_vcf_line(line).unwrap();
        assert!(result.is_none(), "Sem GENEINFO deve retornar None");
    }

    #[test]
    fn test_parse_clinvar_vcf_line_multi_allelico() {
        // ALT com vírgula → multi-alélico → None
        let line = "7\t117548628\t12375\tG\tA,T\t.\t.\tGENEINFO=TP53:7157;CLNSIG=Pathogenic";
        let result = parse_clinvar_vcf_line(line).unwrap();
        assert!(result.is_none(), "Multi-alélico deve retornar None");
    }

    #[test]
    fn test_parse_clinvar_vcf_line_colunas_insuficientes() {
        // Menos de 8 colunas → Err
        let line = "7\t117548628\tG\tA\t.";
        let result = parse_clinvar_vcf_line(line);
        assert!(result.is_err(), "Menos de 8 colunas deve retornar Err");
    }

    #[test]
    fn test_parse_clinvar_vcf_line_indel() {
        // Deleção VCF (REF longo, ALT curto)
        let line = "13\t32316461\t12375\tAGCT\tA\t.\t.\tGENEINFO=BRCA2:675;CLNSIG=Pathogenic";
        let result = parse_clinvar_vcf_line(line).unwrap();
        assert!(result.is_some());
        let record = result.unwrap();
        assert_eq!(record.gene_symbol, "BRCA2");
        // Indel normalizado: POS avança 1, REF sem a base de ancoragem
        assert!(record.variant_key.starts_with("13:32316462:"), "key={}", record.variant_key);
    }

    #[test]
    fn test_parse_clinvar_vcf_line_multiplos_genes_geneinfo() {
        // GENEINFO com múltiplos genes → extrai o primeiro
        let line = "17\t7676154\t12375\tG\tA\t.\t.\tGENEINFO=TP53:7157|MDM2:4193;CLNSIG=Likely_pathogenic";
        let result = parse_clinvar_vcf_line(line).unwrap();
        assert!(result.is_some());
        let record = result.unwrap();
        assert_eq!(record.gene_symbol, "TP53");
        assert_eq!(record.clinvar_significance, "Likely_pathogenic");
    }

    // ── parse_alphamissense_line ──────────────────────────────────────────────

    /// Fixture de cabeçalho e linha TSV AlphaMissense
    const AM_HEADER: &str = "#CHROM\tPOS\tREF\tALT\tgenome\tuniprot_id\ttranscript_id\tprotein_variant\tam_pathogenicity\tam_class";
    const AM_LINE_PATHOGENIC: &str = "chr17\t7676154\tC\tT\thg38\tP04637\tENST00000269305.9\tP278S\t0.9501\tlikely_pathogenic";
    const AM_LINE_BENIGN: &str = "chr7\t117548628\tG\tA\thg38\tQ9Y6I3\tENST00000003084.11\tG542X\t0.0812\tlikely_benign";

    #[test]
    fn test_parse_am_header_detecta_colunas() {
        let idx = parse_am_header(AM_HEADER).unwrap();
        assert_eq!(idx.chrom, 0);
        assert_eq!(idx.pos, 1);
        assert_eq!(idx.ref_allele, 2);
        assert_eq!(idx.alt_allele, 3);
        assert_eq!(idx.uniprot_id, 5);
        assert_eq!(idx.am_pathogenicity, 8);
        assert_eq!(idx.am_class, 9);
    }

    #[test]
    fn test_parse_alphamissense_pathogenic() {
        let idx = parse_am_header(AM_HEADER).unwrap();
        let result = parse_alphamissense_line(AM_LINE_PATHOGENIC, &idx).unwrap();
        assert!(result.is_some());
        let record = result.unwrap();

        // CHROM: 'chr17' → '17'
        assert_eq!(record.variant_key, "17:7676154:C:T");
        assert_eq!(record.uniprot_id, "P04637");
        assert!((record.am_pathogenicity - 0.9501).abs() < 1e-4);
        assert_eq!(record.am_class, "likely_pathogenic");
    }

    #[test]
    fn test_parse_alphamissense_benign() {
        let idx = parse_am_header(AM_HEADER).unwrap();
        let result = parse_alphamissense_line(AM_LINE_BENIGN, &idx).unwrap();
        assert!(result.is_some());
        let record = result.unwrap();

        // CHROM: 'chr7' → '7'
        assert_eq!(record.variant_key, "7:117548628:G:A");
        assert_eq!(record.uniprot_id, "Q9Y6I3");
        assert!((record.am_pathogenicity - 0.0812).abs() < 1e-4);
        assert_eq!(record.am_class, "likely_benign");
    }

    #[test]
    fn test_parse_alphamissense_normaliza_chr() {
        // Confirma que o prefixo 'chr' é removido
        let idx = parse_am_header(AM_HEADER).unwrap();
        let result = parse_alphamissense_line(AM_LINE_PATHOGENIC, &idx).unwrap().unwrap();
        assert!(
            !result.variant_key.starts_with("chr"),
            "variant_key não deve conter prefixo 'chr': {}",
            result.variant_key
        );
    }

    #[test]
    fn test_parse_alphamissense_consistencia_com_clinvar() {
        // SNV em TP53: ClinVar (sem chr) e AM (com chr) devem gerar a mesma chave
        let clinvar_key = normalize_variant_key("17", "7676154", "C", "T");
        let am_key = normalize_variant_key("chr17", "7676154", "C", "T");
        assert_eq!(clinvar_key, am_key);
    }

    // ── filtro por gene_allowlist ────────────────────────────────────────────

    #[test]
    fn test_filtro_gene_allowlist() {
        // Apenas genes na allowlist devem passar
        let allowlist: HashSet<String> = ["TP53".to_string(), "VHL".to_string()]
            .iter()
            .cloned()
            .collect();

        assert!(allowlist.contains("TP53"));
        assert!(!allowlist.contains("EGFR")); // EGFR não está na allowlist
    }

    // ── validate_variant_effect_url ────────────────────────────────────────────

    #[test]
    fn test_validate_url_ncbi_aceito() {
        assert!(validate_variant_effect_url(
            "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/clinvar.vcf.gz"
        ).is_ok());
    }

    #[test]
    fn test_validate_url_gcs_aceito() {
        assert!(validate_variant_effect_url(
            "https://storage.googleapis.com/dm_alphamissense/AlphaMissense_hg38.tsv.gz"
        ).is_ok());
    }

    #[test]
    fn test_validate_url_zenodo_aceito() {
        // Forma canônica (plural) — a que deve ser usada pelo caller (hardening A4).
        assert!(validate_variant_effect_url(
            "https://zenodo.org/records/8208688/files/AlphaMissense_hg38.tsv.gz"
        ).is_ok());
    }

    #[test]
    fn test_validate_url_zenodo_forma_legada_ainda_passa_no_validator() {
        // `validate_variant_effect_url` só checa host — não distingue singular/plural.
        // A forma legada (singular) responde 301 no Zenodo real e é rejeitada pelo
        // client HTTP (redirect proibido, hardening A4), não pelo validator.
        assert!(validate_variant_effect_url(
            "https://zenodo.org/record/8208688/files/AlphaMissense_hg38.tsv.gz"
        ).is_ok());
    }

    #[test]
    fn test_validate_url_host_rejeitado() {
        assert!(validate_variant_effect_url(
            "https://evil.com/clinvar.vcf.gz"
        ).is_err());
    }

    #[test]
    fn test_validate_url_http_rejeitado() {
        assert!(validate_variant_effect_url(
            "http://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/clinvar.vcf.gz"
        ).is_err());
    }

    #[test]
    fn test_validate_url_path_traversal_rejeitado() {
        assert!(validate_variant_effect_url(
            "https://ftp.ncbi.nlm.nih.gov/../etc/passwd"
        ).is_err());
    }

    // ── extract_clinvar_source_version ────────────────────────────────────────

    #[test]
    fn test_extract_clinvar_source_version() {
        let header = vec![
            "##fileDate=20260701".to_string(),
            "##reference=GRCh38".to_string(),
            "##source=ClinVar".to_string(),
        ];
        let version = extract_clinvar_source_version(&header);
        assert!(version.contains("20260701"), "versão deve conter a data: {}", version);
        assert!(version.contains("GRCh38"), "versão deve conter o build: {}", version);
    }

    #[test]
    fn test_extract_clinvar_source_version_sem_data() {
        // Sem ##fileDate → usa data atual (não testar valor exato, só formato)
        let header: Vec<String> = vec![];
        let version = extract_clinvar_source_version(&header);
        assert!(!version.is_empty());
        assert!(version.starts_with("clinvar/"));
    }

    // ── parse_and_copy_clinvar com fixture em memória (sem DB) ───────────────

    /// Cria um arquivo .vcf.gz sintético para testes.
    fn write_vcf_gz_fixture(dir: &Path, filename: &str, content: &str) -> PathBuf {
        use flate2::write::GzEncoder;
        use flate2::Compression;

        let path = dir.join(filename);
        let file = std::fs::File::create(&path).unwrap();
        let mut gz = GzEncoder::new(file, Compression::default());
        gz.write_all(content.as_bytes()).unwrap();
        gz.finish().unwrap();
        path
    }

    const VCF_FIXTURE: &str = "\
##fileDate=20260701\n\
##reference=GRCh38\n\
##source=ClinVar\n\
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n\
7\t117548628\t12375\tG\tA\t.\t.\tGENEINFO=CFTR:1080;CLNSIG=Pathogenic\n\
17\t7676154\t99999\tC\tT\t.\t.\tGENEINFO=TP53:7157;CLNSIG=Likely_pathogenic\n\
1\t1000\t11111\tA\tT\t.\t.\tGENEINFO=EGFR:1956;CLNSIG=Benign\n\
";

    #[test]
    fn test_parse_vcf_gz_fixture_conta_linhas() {
        // Testa parse do gzip sem DB: conta linhas processadas e filtradas
        let dir = TempDir::new().unwrap();
        let gz_path = write_vcf_gz_fixture(dir.path(), "test.vcf.gz", VCF_FIXTURE);

        // Allowlist apenas TP53 e CFTR
        let allowlist: HashSet<String> = ["TP53".to_string(), "CFTR".to_string()]
            .iter()
            .cloned()
            .collect();

        // Abrir gzip e parsear linhas manualmente (sem DB)
        let file = std::fs::File::open(&gz_path).unwrap();
        let gz = GzDecoder::new(file);
        let reader = BufReader::new(gz);
        let mut data_lines = 0usize;
        let mut kept = 0usize;
        let mut offlist = 0usize;

        let mut header_lines: Vec<String> = Vec::new();
        let mut header_done = false;

        for line_result in reader.lines() {
            let line = line_result.unwrap();
            let line = line.trim_end_matches('\r').to_string();

            if line.starts_with("##") {
                header_lines.push(line);
                continue;
            }
            if line.starts_with('#') {
                header_done = true;
                continue;
            }
            if line.is_empty() {
                continue;
            }
            let _ = header_done;

            data_lines += 1;
            if let Ok(Some(record)) = parse_clinvar_vcf_line(&line) {
                if allowlist.contains(&record.gene_symbol) {
                    kept += 1;
                } else {
                    offlist += 1;
                }
            }
        }

        let version = extract_clinvar_source_version(&header_lines);

        assert_eq!(data_lines, 3, "3 linhas de dados no fixture");
        assert_eq!(kept, 2, "TP53 e CFTR devem ser mantidos");
        assert_eq!(offlist, 1, "EGFR fora da allowlist");
        assert!(version.contains("20260701"), "versão={}", version);
    }

    // ── parse AM com fixture em memória (sem DB) ──────────────────────────────

    const AM_FIXTURE: &str = "\
##AlphaMissense_hg38\n\
#CHROM\tPOS\tREF\tALT\tgenome\tuniprot_id\ttranscript_id\tprotein_variant\tam_pathogenicity\tam_class\n\
chr17\t7676154\tC\tT\thg38\tP04637\tENST00000269305.9\tP278S\t0.9501\tlikely_pathogenic\n\
chr7\t117548628\tG\tA\thg38\tQ9Y6I3\tENST00000003084.11\tG542X\t0.0812\tlikely_benign\n\
chr1\t1000\tA\tT\thg38\tUNKNOWN_UNI\tENST00000000001.1\tA100T\t0.5\tambiguous\n\
";

    fn write_am_gz_fixture(dir: &Path, filename: &str, content: &str) -> PathBuf {
        use flate2::write::GzEncoder;
        use flate2::Compression;

        let path = dir.join(filename);
        let file = std::fs::File::create(&path).unwrap();
        let mut gz = GzEncoder::new(file, Compression::default());
        gz.write_all(content.as_bytes()).unwrap();
        gz.finish().unwrap();
        path
    }

    #[test]
    fn test_parse_am_gz_fixture_com_mapa() {
        let dir = TempDir::new().unwrap();
        let gz_path = write_am_gz_fixture(dir.path(), "am.tsv.gz", AM_FIXTURE);

        // Mapa uniprot→gene para TP53 e CFTR (Q9Y6I3 mapeado para CFTR)
        let mut uniprot_map: HashMap<String, String> = HashMap::new();
        uniprot_map.insert("P04637".to_string(), "TP53".to_string());
        uniprot_map.insert("Q9Y6I3".to_string(), "CFTR".to_string());
        // UNKNOWN_UNI não mapeado → deve ser descartado

        let allowlist: HashSet<String> = ["TP53".to_string(), "CFTR".to_string()]
            .iter()
            .cloned()
            .collect();

        // Parse sem DB — verifica contagens manualmente
        let file = std::fs::File::open(&gz_path).unwrap();
        let gz = GzDecoder::new(file);
        let reader = BufReader::new(gz);
        let mut col_idx: Option<AlphaMissenseColIdx> = None;
        let mut kept = 0usize;
        let mut skipped = 0usize;
        let mut processed = 0usize;

        for line_result in reader.lines() {
            let line = line_result.unwrap();
            let line = line.trim_end_matches('\r').to_string();

            if line.starts_with("##") {
                continue;
            }

            if col_idx.is_none() && (line.starts_with('#') || line.to_uppercase().contains("CHROM")) {
                col_idx = Some(parse_am_header(&line).unwrap());
                continue;
            }

            if line.is_empty() {
                continue;
            }

            let idx = col_idx.as_ref().unwrap();
            processed += 1;

            if let Ok(Some(record)) = parse_alphamissense_line(&line, idx) {
                if let Some(gene) = uniprot_map.get(&record.uniprot_id) {
                    if allowlist.contains(gene) {
                        kept += 1;
                    } else {
                        skipped += 1;
                    }
                } else {
                    skipped += 1;
                }
            }
        }

        assert_eq!(processed, 3, "3 linhas de dados no fixture AM");
        assert_eq!(kept, 2, "TP53 e CFTR mapeados e na allowlist");
        assert_eq!(skipped, 1, "UNKNOWN_UNI não mapeado");
    }

    #[test]
    fn test_parse_am_gz_fixture_sem_mapa_handoff() {
        // Sem mapa → nenhuma variante é mantida (handoff)
        let dir = TempDir::new().unwrap();
        let gz_path = write_am_gz_fixture(dir.path(), "am_empty.tsv.gz", AM_FIXTURE);

        let uniprot_map: HashMap<String, String> = HashMap::new(); // vazio
        let allowlist: HashSet<String> = HashSet::new();

        let file = std::fs::File::open(&gz_path).unwrap();
        let gz = GzDecoder::new(file);
        let reader = BufReader::new(gz);
        let mut col_idx: Option<AlphaMissenseColIdx> = None;
        let mut kept = 0usize;

        for line_result in reader.lines() {
            let line = line_result.unwrap();
            let line = line.trim_end_matches('\r').to_string();

            if line.starts_with("##") { continue; }

            if col_idx.is_none() && (line.starts_with('#') || line.to_uppercase().contains("CHROM")) {
                col_idx = Some(parse_am_header(&line).unwrap());
                continue;
            }
            if line.is_empty() { continue; }

            let idx = col_idx.as_ref().unwrap();
            if let Ok(Some(record)) = parse_alphamissense_line(&line, idx) {
                if let Some(gene) = uniprot_map.get(&record.uniprot_id) {
                    if allowlist.contains(gene) {
                        kept += 1;
                    }
                }
            }
        }

        assert_eq!(kept, 0, "Sem mapa → nenhuma variante mantida");
    }

    // ── escape_csv_field ──────────────────────────────────────────────────────

    #[test]
    fn test_escape_csv_field_aspas_internas() {
        let result = escape_csv_field("value with \"quotes\"");
        assert!(result.starts_with('"'));
        assert!(result.ends_with('"'));
        assert!(result.contains("\"\"quotes\"\""));
    }

    #[test]
    fn test_escape_csv_field_campo_vazio() {
        assert_eq!(escape_csv_field(""), "\"\"");
    }
}
