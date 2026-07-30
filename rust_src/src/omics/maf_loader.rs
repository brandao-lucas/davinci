/// Loader do GDC masked MAF (somatic mutation) para CPTAC CCRCC — passo 2.6.
///
/// # Decisões de design (travadas no plano 2026-07-13 e correção crítica de design)
///
/// ## Contrato de entrada
///
/// O Django resolve `file_id → OmicSample.accession` via GDC API (uma chamada que traz
/// `cases.submitter_id`). O loader recebe o mapa pronto e NUNCA tenta derivar o case_id
/// a partir do Tumor_Sample_Barcode (que é um aliquot CPTAC como `CPT0077490009`,
/// não contém C3L-/C3N-).
///
/// ## variant_key
///
/// Reutiliza `normalize_variant_key` / `strip_chr_prefix` de `variant_effect_loader.rs`
/// para garantir consistência de chave entre MAF e ClinVar/AlphaMissense.
/// MAF usa `Chromosome` com prefixo `chr` e `-` para a base de ancoragem de indels VCF
/// (tratado como base de ancoragem standard — strip idêntico ao ClinVar).
///
/// ## Dois artefatos
///
/// (a) **Parquet gene×amostra (burden)** — contagem de variantes não-sinônimas por
///     (gene, sample_accession); coluna 0 = `feature`, colunas seguintes = amostras
///     com prefixo `T_`. Shape = OmicMatrix (genomic/counts/gene).
/// (b) **Arquivo de ocorrências TSV** — linhas `(sample_accession, variant_key,
///     gene_symbol, variant_classification)`; consumido pelo seed Django (vitruvio).
///
/// ## Variantes não-sinônimas (lista exata usada no filtro)
///
/// - Missense_Mutation
/// - Nonsense_Mutation
/// - Frame_Shift_Del
/// - Frame_Shift_Ins
/// - In_Frame_Del
/// - In_Frame_Ins
/// - Splice_Site
/// - Translation_Start_Site
/// - Nonstop_Mutation
///
/// Silent, Intron, 3'UTR, 5'UTR, 3'Flank, 5'Flank, RNA, IGR, Splice_Region,
/// Targeted_Region, De_novo_Start_InFrame, De_novo_Start_OutOfFrame → descartados.
///
/// ## Dedupe
///
/// A mesma `(sample_accession, variant_key)` pode aparecer em 2 arquivos MAF do mesmo
/// caso (CPTAC tem casos com múltiplos MAFs). Deduplicação por
/// `(sample_accession, variant_key)` antes de contar o burden e antes de gravar a
/// ocorrência.
///
/// ## Segurança
///
/// - Host allowlist: `api.gdc.cancer.gov` via HTTPS apenas.
/// - Teto de bytes por arquivo (M2): `MAX_MAF_BYTES = 512 MB`.
/// - `db_url` (se fornecido) nunca logado.
/// - Sem SQL cru; sem shell injection (sem exec externo).
/// - Descompressão streaming gzip — sem bufferizar o arquivo inteiro em memória.

use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use arrow::array::{ArrayRef, Int32Array, StringArray};
use arrow::datatypes::{DataType, Field, Schema};
use arrow::record_batch::RecordBatch;
use flate2::read::GzDecoder;
use md5::{Digest, Md5};
use parquet::arrow::ArrowWriter;
use parquet::file::properties::WriterProperties;

// Reutiliza normalize_variant_key e strip_chr_prefix do variant_effect_loader.
use crate::omics::variant_effect_loader::normalize_variant_key;

// ─── Constantes ───────────────────────────────────────────────────────────────

/// Teto de bytes por arquivo MAF (512 MB). Um MAF individual raramente supera 50MB;
/// 512MB é guarda razoável contra arquivo inesperadamente grande.
const MAX_MAF_BYTES: u64 = 512 * 1024 * 1024;

/// URL base do GDC Data API.
const GDC_DATA_BASE: &str = "https://api.gdc.cancer.gov/data/";

/// Variantes não-sinônimas aceitas no burden e nas ocorrências.
const NONSYNONYMOUS_CLASSIFICATIONS: &[&str] = &[
    "Missense_Mutation",
    "Nonsense_Mutation",
    "Frame_Shift_Del",
    "Frame_Shift_Ins",
    "In_Frame_Del",
    "In_Frame_Ins",
    "Splice_Site",
    "Translation_Start_Site",
    "Nonstop_Mutation",
];

// ─── Structs públicos ─────────────────────────────────────────────────────────

/// Entrada do mapa `file_id → sample_accession` passado pelo Django.
///
/// O Django resolve o mapa via GDC API (`/files?fields=cases.submitter_id`)
/// e passa uma lista de `{file_id: str, sample_accession: str}` — ex.:
/// `file_id = "9a4c1948-..."`, `sample_accession = "C3L-00004_tumor"`.
#[derive(Debug, Clone)]
pub struct MafFileEntry {
    /// UUID do arquivo no GDC (ex.: `"9a4c1948-abcd-1234-efgh-000000000000"`).
    pub file_id: String,
    /// Accession da amostra em `core_omicsample` (ex.: `"C3L-00004_tumor"`).
    pub sample_accession: String,
}

/// Metadados de uma coluna de amostra no Parquet MAF.
#[derive(Debug, Clone)]
pub struct MafSampleColumn {
    /// Accession da amostra (ex.: `"C3L-00004_tumor"`).
    pub case_id: String,
    /// Sempre `"tumor"` para MAFs somáticos CPTAC.
    pub sample_role: String,
    /// Índice 0-based da coluna no Parquet (col 0 = `feature`; amostras ≥ 1).
    pub column_index: usize,
}

/// Manifesto retornado por `load_somatic_maf`.
///
/// Handoff para o vitruvio (passo 2.6):
///
/// | Campo | Tipo | Descrição |
/// |---|---|---|
/// | `parquet_path` | `str` | Caminho absoluto do Parquet burden gravado. |
/// | `checksum_md5` | `str` | MD5 lowercase-hex do Parquet. |
/// | `n_features` | `usize` | Número de genes (features = linhas do Parquet). |
/// | `n_samples` | `usize` | Número de amostras únicas no Parquet. |
/// | `sample_columns` | `Vec<MafSampleColumn>` | Todas com `sample_role = "tumor"`. |
/// | `occurrences_path` | `str` | Caminho absoluto do TSV de ocorrências. |
/// | `n_files_processed` | `usize` | Arquivos MAF baixados com sucesso. |
/// | `n_occurrences` | `usize` | Ocorrências únicas (sample, variant_key) depois de dedupe. |
/// | `n_variants_skipped_synonymous` | `usize` | Variantes descartadas por serem sinônimas. |
/// | `n_variants_skipped_offlist` | `usize` | Variantes descartadas (gene não na allowlist). |
/// | `errors` | `Vec<String>` | Erros não-fatais por arquivo. |
#[derive(Debug)]
pub struct SomaticMafManifest {
    pub parquet_path: String,
    pub checksum_md5: String,
    pub n_features: usize,
    pub n_samples: usize,
    pub sample_columns: Vec<MafSampleColumn>,
    pub occurrences_path: String,
    pub n_files_processed: usize,
    pub n_occurrences: usize,
    pub n_variants_skipped_synonymous: usize,
    pub n_variants_skipped_offlist: usize,
    pub errors: Vec<String>,
}

// ─── Índices de coluna MAF (detectados por nome) ─────────────────────────────

/// Índices de coluna do cabeçalho MAF detectados por nome.
///
/// O MAF GDC tem ~140 colunas; usando índice por nome — não por posição fixa.
#[derive(Debug)]
struct MafColIdx {
    hugo_symbol: usize,
    chromosome: usize,
    start_position: usize,
    reference_allele: usize,
    tumor_seq_allele2: usize,
    variant_classification: usize,
    variant_type: usize,
}

/// Detecta os índices de coluna do cabeçalho MAF (linha sem `##`).
///
/// Retorna `Err` se alguma coluna obrigatória não for encontrada.
fn parse_maf_header(header_line: &str) -> Result<MafColIdx, String> {
    let fields: Vec<&str> = header_line.split('\t').collect();
    let mut idx: HashMap<String, usize> = HashMap::new();
    for (i, f) in fields.iter().enumerate() {
        idx.insert(f.trim().to_string(), i);
    }

    let get = |name: &str| -> Result<usize, String> {
        idx.get(name)
            .copied()
            .ok_or_else(|| format!("Coluna '{}' não encontrada no cabeçalho MAF", name))
    };

    Ok(MafColIdx {
        hugo_symbol: get("Hugo_Symbol")?,
        chromosome: get("Chromosome")?,
        start_position: get("Start_Position")?,
        reference_allele: get("Reference_Allele")?,
        tumor_seq_allele2: get("Tumor_Seq_Allele2")?,
        variant_classification: get("Variant_Classification")?,
        variant_type: get("Variant_Type")?,
    })
}

// ─── Verificação de variant_classification ────────────────────────────────────

/// Retorna `true` se a classificação é não-sinônima (deve entrar no burden/ocorrências).
#[inline]
fn is_nonsynonymous(classification: &str) -> bool {
    NONSYNONYMOUS_CLASSIFICATIONS.contains(&classification)
}

// ─── Validação de URL GDC ─────────────────────────────────────────────────────

/// Valida que a URL pertence exclusivamente ao host `api.gdc.cancer.gov` via HTTPS.
///
/// Rejeita qualquer URL com host diferente ou esquema diferente de `https`,
/// prevenindo SSRF caso o caller passe uma URL não confiável.
pub fn validate_gdc_url(url: &str) -> Result<(), String> {
    if !url.starts_with("https://api.gdc.cancer.gov/") {
        return Err(format!(
            "URL rejeitada por política de host: '{}'. \
             Apenas https://api.gdc.cancer.gov/ é permitido para o loader GDC MAF.",
            url
        ));
    }
    let path_part = &url["https://api.gdc.cancer.gov/".len()..];
    if path_part.contains("..") {
        return Err(format!(
            "URL rejeitada: contém path traversal '..' em '{}'",
            url
        ));
    }
    Ok(())
}

// ─── Uma ocorrência (sample + variante + gene) ───────────────────────────────

/// Ocorrência variant-level usada internamente e gravada no TSV de saída.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct Occurrence {
    sample_accession: String,
    variant_key: String,
    gene_symbol: String,
    variant_classification: String,
}

// ─── Parse streaming de um MAF (lido do disco como .maf.gz) ──────────────────

/// Parseia um arquivo MAF gzip a partir de um `Path` no disco.
///
/// Retorna as ocorrências não-sinônimas para os genes na allowlist,
/// além dos contadores de variantes descartadas.
///
/// Uma passada: extrai todos os campos em único loop `quick-xml`-like (tab split).
fn parse_maf_gz(
    path: &Path,
    sample_accession: &str,
    gene_allowlist: &HashSet<String>,
) -> Result<(Vec<Occurrence>, usize, usize), String> {
    let file = std::fs::File::open(path)
        .map_err(|e| format!("Falha ao abrir MAF {:?}: {}", path, e))?;
    let gz = GzDecoder::new(file);
    let reader = BufReader::new(gz);

    let mut col_idx: Option<MafColIdx> = None;
    let mut occurrences: Vec<Occurrence> = Vec::new();
    let mut n_skipped_synonymous = 0usize;
    let mut n_skipped_offlist = 0usize;

    for (line_num, line_result) in reader.lines().enumerate() {
        let line = line_result
            .map_err(|e| format!("Erro ao ler linha {} de {:?}: {}", line_num + 1, path, e))?;

        let line = line.trim_end_matches('\r');

        // Pular linhas de comentário ##
        if line.starts_with("##") {
            continue;
        }

        if line.is_empty() {
            continue;
        }

        // Cabeçalho (primeira linha não-##, começa com Hugo_Symbol ou #Hugo_Symbol)
        if col_idx.is_none() {
            // O MAF GDC pode ter o cabeçalho como `Hugo_Symbol\t...` (sem #)
            // ou `#Hugo_Symbol\t...` em alguns arquivos. Strip do # se presente.
            let header_line = line.trim_start_matches('#');
            match parse_maf_header(header_line) {
                Ok(idx) => {
                    col_idx = Some(idx);
                    continue;
                }
                Err(e) => {
                    return Err(format!(
                        "Falha ao parsear cabeçalho MAF em {:?}: {}",
                        path, e
                    ));
                }
            }
        }

        let idx = col_idx.as_ref().unwrap();
        let fields: Vec<&str> = line.split('\t').collect();

        // Verificar que temos colunas suficientes
        let min_col = [
            idx.hugo_symbol,
            idx.chromosome,
            idx.start_position,
            idx.reference_allele,
            idx.tumor_seq_allele2,
            idx.variant_classification,
            idx.variant_type,
        ]
        .iter()
        .copied()
        .max()
        .unwrap_or(0)
            + 1;

        if fields.len() < min_col {
            // Linha com colunas insuficientes — pular silenciosamente
            continue;
        }

        let hugo_symbol = fields[idx.hugo_symbol].trim();
        let chromosome = fields[idx.chromosome].trim();
        let start_position = fields[idx.start_position].trim();
        let reference_allele = fields[idx.reference_allele].trim();
        let tumor_seq_allele2 = fields[idx.tumor_seq_allele2].trim();
        let variant_classification = fields[idx.variant_classification].trim();

        if hugo_symbol.is_empty() {
            continue;
        }

        // Filtro 1: gene na allowlist
        if !gene_allowlist.contains(hugo_symbol) {
            n_skipped_offlist += 1;
            continue;
        }

        // Filtro 2: não-sinônimo
        if !is_nonsynonymous(variant_classification) {
            n_skipped_synonymous += 1;
            continue;
        }

        // Normalizar variant_key usando a mesma função do variant_effect_loader.
        // MAF GDC usa '-' como REF ou ALT para indels — tratar '-' como string vazia
        // para alinhar com a representação VCF canônica.
        let ref_norm = if reference_allele == "-" { "" } else { reference_allele };
        let alt_norm = if tumor_seq_allele2 == "-" { "" } else { tumor_seq_allele2 };

        // Casos com ALT = '-' (deleção pura, sem base de ancoragem VCF): criar chave
        // sintética CHROM:POS:REF: (REF sem base de ancoragem é impossível aqui sem
        // o contexto VCF — gravámos como-está com REF original).
        // Para consistência máxima com ClinVar, usamos ref_norm/alt_norm depois do strip.
        let variant_key = match normalize_variant_key(
            chromosome,
            start_position,
            ref_norm,
            alt_norm,
        ) {
            Some(k) => k,
            None => {
                // normalize_variant_key retorna None para: multi-alélico, spanning, alt vazio.
                // Para o MAF: '-' já foi convertido para ""; se alt ficou vazio e é deleção
                // legítima do MAF, construímos chave manual.
                if alt_norm.is_empty() && !ref_norm.is_empty() {
                    // Deleção pura MAF: CHROM:POS:REF:
                    let chrom_norm = crate::omics::variant_effect_loader::strip_chr_prefix(chromosome);
                    format!("{}:{}:{}:", chrom_norm, start_position, ref_norm)
                } else if ref_norm.is_empty() && !alt_norm.is_empty() {
                    // Inserção pura MAF: CHROM:POS::ALT
                    let chrom_norm = crate::omics::variant_effect_loader::strip_chr_prefix(chromosome);
                    format!("{}:{}::{}", chrom_norm, start_position, alt_norm)
                } else {
                    // Outro caso não mapeável (ex.: ambos vazios) — pular
                    n_skipped_synonymous += 1;
                    continue;
                }
            }
        };

        occurrences.push(Occurrence {
            sample_accession: sample_accession.to_string(),
            variant_key,
            gene_symbol: hugo_symbol.to_string(),
            variant_classification: variant_classification.to_string(),
        });
    }

    Ok((occurrences, n_skipped_synonymous, n_skipped_offlist))
}

// ─── Download streaming de um MAF do GDC ─────────────────────────────────────

/// Baixa um arquivo MAF do GDC `/data/<file_id>` com teto de bytes.
///
/// Usa GET (não HEAD — HEAD retorna 400 no GDC).
/// Streaming chunk-a-chunk; aborta se exceder `MAX_MAF_BYTES`.
async fn download_gdc_maf(
    http_client: &reqwest::Client,
    file_id: &str,
    dest_path: &Path,
) -> Result<(), String> {
    use futures::StreamExt;
    use tokio::io::AsyncWriteExt;

    let url = format!("{}{}", GDC_DATA_BASE, file_id);
    validate_gdc_url(&url)?;

    if let Some(parent) = dest_path.parent() {
        tokio::fs::create_dir_all(parent)
            .await
            .map_err(|e| format!("create_dir_all {:?}: {}", parent, e))?;
    }

    let response = http_client
        .get(&url)
        .send()
        .await
        .map_err(|e| format!("HTTP GET falhou para file_id={}: {}", file_id, e))?;

    let status = response.status();
    if !status.is_success() {
        return Err(format!(
            "HTTP {} ao baixar file_id={} (url={})",
            status, file_id, url
        ));
    }

    let mut file = tokio::fs::File::create(dest_path)
        .await
        .map_err(|e| format!("Falha ao criar {:?}: {}", dest_path, e))?;

    let mut bytes_written: u64 = 0;
    let mut stream = response.bytes_stream();

    while let Some(chunk_result) = stream.next().await {
        let chunk =
            chunk_result.map_err(|e| format!("Erro ao ler chunk de file_id={}: {}", file_id, e))?;

        bytes_written += chunk.len() as u64;
        if bytes_written > MAX_MAF_BYTES {
            return Err(format!(
                "file_id={} excede o teto de {} bytes (M2)",
                file_id, MAX_MAF_BYTES
            ));
        }

        file.write_all(&chunk)
            .await
            .map_err(|e| format!("Erro ao escrever em {:?}: {}", dest_path, e))?;
    }

    file.flush()
        .await
        .map_err(|e| format!("Erro ao fechar {:?}: {}", dest_path, e))?;

    Ok(())
}

// ─── Construção do Parquet burden ─────────────────────────────────────────────

/// Grava o Parquet gene×amostra (burden não-sinônimo).
///
/// Schema:
/// - `feature` (Utf8): gene symbol (linha) — ordenado alfabeticamente.
/// - `T_<sample_accession>` (Int32, nullable): contagem de variantes não-sinônimas.
///
/// Retorna (checksum_md5, sample_columns).
fn write_burden_parquet(
    dest_path: &Path,
    genes_sorted: &[String],
    samples_sorted: &[String],
    burden: &HashMap<(String, String), u32>, // (gene, sample) → count
) -> Result<(String, Vec<MafSampleColumn>), String> {
    // Construir Schema Arrow
    let mut fields: Vec<Field> = Vec::new();
    fields.push(Field::new("feature", DataType::Utf8, false));
    for sample in samples_sorted {
        fields.push(Field::new(
            format!("T_{}", sample),
            DataType::Int32,
            true, // nullable — sample pode não ter variante em um gene
        ));
    }
    let schema = Arc::new(Schema::new(fields));

    // Coluna feature
    let feature_array: ArrayRef = Arc::new(StringArray::from(
        genes_sorted.iter().map(|s| s.as_str()).collect::<Vec<_>>(),
    ));

    let mut arrays: Vec<ArrayRef> = vec![feature_array];

    // Uma coluna por amostra
    for sample in samples_sorted {
        let col: Vec<Option<i32>> = genes_sorted
            .iter()
            .map(|gene| {
                burden
                    .get(&(gene.clone(), sample.clone()))
                    .map(|&c| c as i32)
            })
            .collect();
        arrays.push(Arc::new(Int32Array::from(col)));
    }

    let batch = RecordBatch::try_new(schema.clone(), arrays)
        .map_err(|e| format!("Falha ao criar RecordBatch burden: {}", e))?;

    if let Some(parent) = dest_path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("Falha ao criar diretório {:?}: {}", parent, e))?;
    }

    let props = WriterProperties::builder()
        .set_created_by("ferris/maf_loader v0.1".to_string())
        .build();

    let file = std::fs::File::create(dest_path)
        .map_err(|e| format!("Falha ao criar {:?}: {}", dest_path, e))?;

    let mut writer = ArrowWriter::try_new(file, schema, Some(props))
        .map_err(|e| format!("Falha ao inicializar ArrowWriter: {}", e))?;

    writer
        .write(&batch)
        .map_err(|e| format!("Falha ao escrever RecordBatch burden: {}", e))?;

    writer
        .close()
        .map_err(|e| format!("Falha ao fechar ArrowWriter: {}", e))?;

    // MD5 do Parquet
    let parquet_bytes = std::fs::read(dest_path)
        .map_err(|e| format!("Falha ao ler Parquet para MD5: {}", e))?;
    let mut hasher = Md5::new();
    hasher.update(&parquet_bytes);
    let checksum = format!("{:x}", hasher.finalize());

    // sample_columns: todas tumor, column_index = 1 + i (col 0 = feature)
    let sample_columns: Vec<MafSampleColumn> = samples_sorted
        .iter()
        .enumerate()
        .map(|(i, s)| MafSampleColumn {
            case_id: s.clone(),
            sample_role: "tumor".to_string(),
            column_index: 1 + i,
        })
        .collect();

    Ok((checksum, sample_columns))
}

// ─── Escrita do TSV de ocorrências ────────────────────────────────────────────

/// Grava o arquivo TSV de ocorrências variant-level.
///
/// Formato: `sample_accession\tvariant_key\tgene_symbol\tvariant_classification\n`
/// (sem cabeçalho — o vitruvio sabe o schema).
fn write_occurrences_tsv(
    dest_path: &Path,
    occurrences: &[Occurrence],
) -> Result<(), String> {
    if let Some(parent) = dest_path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("Falha ao criar diretório {:?}: {}", parent, e))?;
    }

    let mut file = std::fs::File::create(dest_path)
        .map_err(|e| format!("Falha ao criar {:?}: {}", dest_path, e))?;

    // Cabeçalho explícito para o vitruvio
    writeln!(file, "sample_accession\tvariant_key\tgene_symbol\tvariant_classification")
        .map_err(|e| format!("Falha ao escrever cabeçalho em {:?}: {}", dest_path, e))?;

    for occ in occurrences {
        writeln!(
            file,
            "{}\t{}\t{}\t{}",
            occ.sample_accession, occ.variant_key, occ.gene_symbol, occ.variant_classification
        )
        .map_err(|e| format!("Falha ao escrever linha em {:?}: {}", dest_path, e))?;
    }

    Ok(())
}

// ─── Função principal (async) ──────────────────────────────────────────────────

/// Baixa e parseia um lote de arquivos MAF do GDC, produz Parquet burden +
/// arquivo de ocorrências.
///
/// # Fluxo
///
/// 1. Para cada `(file_id, sample_accession)` no `file_map`:
///    a. Baixa `https://api.gdc.cancer.gov/data/{file_id}` (GET, streaming gzip).
///    b. Parseia o MAF streaming (uma passada); extrai variantes não-sinônimas na allowlist.
///    c. Normaliza `variant_key` reusando `normalize_variant_key`.
///    d. Associa cada variante à `sample_accession` passada (não ao barcode in-file).
/// 2. Dedupe por `(sample_accession, variant_key)` — mesma variante em 2 arquivos do
///    mesmo caso conta 1x.
/// 3. Escreve Parquet gene×amostra (burden inteiro) e TSV de ocorrências.
/// 4. Retorna `SomaticMafManifest`.
pub async fn load_somatic_maf_async(
    file_map: &[MafFileEntry],
    dest_dir: &Path,
    gene_allowlist: &HashSet<String>,
    parquet_name: &str,
) -> Result<SomaticMafManifest, String> {
    tokio::fs::create_dir_all(dest_dir)
        .await
        .map_err(|e| format!("create_dir_all {:?}: {}", dest_dir, e))?;

    // Cliente HTTP com timeout longo (arquivos MAF grandes)
    //
    // `redirect::Policy::none()` — hardening A4 (laudo 007): checagem empírica
    // (download real de um arquivo `access=open` via `api.gdc.cancer.gov/data/`)
    // confirmou 200 direto, sem 3xx, então proibir redirect não quebra a
    // ingestão. Isso impede que a allowlist de host (`validate_gdc_url`) seja
    // contornada por um redirect para host fora da allowlist.
    let http_client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(1800)) // 30 min
        .redirect(reqwest::redirect::Policy::none())
        .user_agent(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) \
             AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        .build()
        .map_err(|e| format!("Falha ao criar HTTP client: {}", e))?;

    // Acumuladores
    // Conjunto de ocorrências únicas (sample_accession, variant_key) para dedupe
    let mut seen: HashSet<(String, String)> = HashSet::new();
    // Ocorrências únicas para o TSV (inclui gene + classification para a 1ª ocorrência)
    let mut unique_occurrences: Vec<Occurrence> = Vec::new();
    // burden: (gene, sample) → contagem
    let mut burden: HashMap<(String, String), u32> = HashMap::new();
    // Conjuntos de genes e amostras vistos
    let mut genes_seen: HashSet<String> = HashSet::new();
    let mut samples_seen: HashSet<String> = HashSet::new();

    let mut n_files_processed = 0usize;
    let mut n_variants_skipped_synonymous = 0usize;
    let mut n_variants_skipped_offlist = 0usize;
    let mut errors: Vec<String> = Vec::new();

    for entry in file_map {
        let file_id = &entry.file_id;
        let sample_accession = &entry.sample_accession;

        // Construir caminho local para o MAF baixado
        // Usamos file_id como nome para evitar colisões entre arquivos do mesmo sample
        let maf_path: PathBuf = dest_dir.join(format!("{}.maf.gz", file_id));

        // Download
        eprintln!("[maf_loader] baixando file_id={} → {:?}", file_id, maf_path);
        if let Err(e) = download_gdc_maf(&http_client, file_id, &maf_path).await {
            errors.push(format!("file_id={}: download falhou: {}", file_id, e));
            continue; // erro não-fatal: pula este arquivo
        }

        // Parse streaming
        match parse_maf_gz(&maf_path, sample_accession, gene_allowlist) {
            Ok((occ_list, n_syn, n_off)) => {
                n_variants_skipped_synonymous += n_syn;
                n_variants_skipped_offlist += n_off;
                n_files_processed += 1;

                for occ in occ_list {
                    let dedup_key = (occ.sample_accession.clone(), occ.variant_key.clone());

                    if seen.contains(&dedup_key) {
                        // Mesma (sample, variant_key) já vista — pula (dedupe)
                        continue;
                    }
                    seen.insert(dedup_key);

                    // Atualizar burden: (gene, sample) → +1
                    *burden
                        .entry((occ.gene_symbol.clone(), occ.sample_accession.clone()))
                        .or_insert(0) += 1;

                    genes_seen.insert(occ.gene_symbol.clone());
                    samples_seen.insert(occ.sample_accession.clone());
                    unique_occurrences.push(occ);
                }
            }
            Err(e) => {
                errors.push(format!("file_id={}: parse falhou: {}", file_id, e));
                // Não incrementa n_files_processed — arquivo não contribuiu
            }
        }

        // Remover arquivo temporário (MAF pode ser grande; liberar espaço)
        let _ = std::fs::remove_file(&maf_path);
    }

    // Ordenar genes e amostras alfabeticamente para idempotência do Parquet
    let mut genes_sorted: Vec<String> = genes_seen.into_iter().collect();
    genes_sorted.sort_unstable();

    let mut samples_sorted: Vec<String> = samples_seen.into_iter().collect();
    samples_sorted.sort_unstable();

    let n_features = genes_sorted.len();
    let n_samples = samples_sorted.len();
    let n_occurrences = unique_occurrences.len();

    eprintln!(
        "[maf_loader] {} arquivos processados; {} genes; {} amostras; {} ocorrências únicas",
        n_files_processed, n_features, n_samples, n_occurrences
    );

    // Gravar Parquet burden
    let parquet_path: PathBuf = dest_dir.join(parquet_name);
    let (checksum_md5, sample_columns) = if n_features > 0 && n_samples > 0 {
        write_burden_parquet(&parquet_path, &genes_sorted, &samples_sorted, &burden)?
    } else {
        // Sem dados: Parquet vazio mas válido
        write_burden_parquet(&parquet_path, &[], &[], &HashMap::new())?
    };

    eprintln!(
        "[maf_loader] Parquet burden gravado em {:?} (MD5={})",
        parquet_path, checksum_md5
    );

    // Gravar TSV de ocorrências
    let occurrences_path: PathBuf = dest_dir.join("somatic_occurrences.tsv");
    write_occurrences_tsv(&occurrences_path, &unique_occurrences)?;

    eprintln!(
        "[maf_loader] TSV de ocorrências gravado em {:?} ({} linhas)",
        occurrences_path,
        unique_occurrences.len()
    );

    Ok(SomaticMafManifest {
        parquet_path: parquet_path.to_string_lossy().into_owned(),
        checksum_md5,
        n_features,
        n_samples,
        sample_columns,
        occurrences_path: occurrences_path.to_string_lossy().into_owned(),
        n_files_processed,
        n_occurrences,
        n_variants_skipped_synonymous,
        n_variants_skipped_offlist,
        errors,
    })
}

/// Entry point síncrono para PyO3.
///
/// # Argumentos
///
/// - `file_map`: lista de `{file_id: str, sample_accession: str}` — o Django montou
///   este mapa resolvendo `file_id → C3L/C3N` via GDC API.
/// - `dest_dir`: diretório local onde os artefatos (Parquet + TSV) serão gravados.
/// - `db_url`: PostgreSQL connection string (não usada no loader; reservado para
///   leituras futuras de `OmicSample`).
/// - `gene_allowlist`: lista de `gene_symbol` (UPPERCASE) do GeneRole.
/// - `parquet_name`: nome do arquivo Parquet de saída (default: `"cptac_ccrcc_somatic.parquet"`).
pub fn load_somatic_maf(
    file_map: Vec<MafFileEntry>,
    dest_dir: &str,
    _db_url: &str, // reservado — não usado no loader (db_url nunca logado)
    gene_allowlist: Vec<String>,
    parquet_name: &str,
) -> Result<SomaticMafManifest, String> {
    let rt = tokio::runtime::Runtime::new()
        .map_err(|e| format!("Falha ao criar runtime Tokio: {}", e))?;

    let dest_path = Path::new(dest_dir);
    let allowlist: HashSet<String> = gene_allowlist.into_iter().collect();

    rt.block_on(load_somatic_maf_async(&file_map, dest_path, &allowlist, parquet_name))
}

// ─── Testes unitários (#[cfg(test)], SEM rede / SEM DB) ─────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write as IoWrite;
    use tempfile::TempDir;

    // ── Helpers para criar fixtures MAF gzip ──────────────────────────────────

    /// Escreve um arquivo MAF gzip no diretório `dir` com o conteúdo `content`.
    fn write_maf_gz_fixture(dir: &Path, filename: &str, content: &str) -> PathBuf {
        use flate2::write::GzEncoder;
        use flate2::Compression;

        let path = dir.join(filename);
        let file = std::fs::File::create(&path).unwrap();
        let mut gz = GzEncoder::new(file, Compression::default());
        gz.write_all(content.as_bytes()).unwrap();
        gz.finish().unwrap();
        path
    }

    /// Fixture MAF mínimo com:
    /// - Linhas de comentário `##`
    /// - Cabeçalho por nome (não por índice fixo)
    /// - 1 missense (TP53 — na allowlist)
    /// - 1 nonsense (VHL — na allowlist)
    /// - 1 silent/sinônima (TP53 — deve ser descartada)
    /// - 1 missense fora da allowlist (BRCA1 — deve ser descartada)
    /// - 1 indel Frame_Shift_Del (VHL — na allowlist)
    const MAF_FIXTURE_A: &str = "\
##fileformat=MAFv2.4\n\
##GDC version\n\
Hugo_Symbol\tEntrez_Gene_Id\tCenter\tNCBI_Build\tChromosome\tStart_Position\tEnd_Position\tStrand\tVariant_Classification\tVariant_Type\tReference_Allele\tTumor_Seq_Allele1\tTumor_Seq_Allele2\tdbSNP_RS\tdbSNP_Val_Status\tTumor_Sample_Barcode\tMatched_Norm_Sample_Barcode\n\
TP53\t7157\tWUSM\tGRCh38\tchr17\t7676154\t7676154\t+\tMissense_Mutation\tSNP\tC\tC\tT\t.\t.\tCPT0077490009\tCPT0077490003\n\
VHL\t7428\tWUSM\tGRCh38\tchr3\t10183774\t10183774\t+\tNonsense_Mutation\tSNP\tG\tG\tA\t.\t.\tCPT0077490009\tCPT0077490003\n\
TP53\t7157\tWUSM\tGRCh38\tchr17\t7674221\t7674221\t+\tSilent\tSNP\tG\tG\tA\t.\t.\tCPT0077490009\tCPT0077490003\n\
BRCA1\t672\tWUSM\tGRCh38\tchr17\t43094692\t43094692\t+\tMissense_Mutation\tSNP\tA\tA\tG\t.\t.\tCPT0077490009\tCPT0077490003\n\
VHL\t7428\tWUSM\tGRCh38\tchr3\t10183800\t10183807\t+\tFrame_Shift_Del\tDEL\tAGCTTGCA\tAGCTTGCA\t-\t.\t.\tCPT0077490009\tCPT0077490003\n\
";

    /// Fixture MAF para uma segunda amostra — compartilha a variante TP53:17:7676154:C:T
    /// com MAF_FIXTURE_A (será deduplicada se mesma sample, ou contada separada se sample diferente).
    const MAF_FIXTURE_B: &str = "\
##fileformat=MAFv2.4\n\
Hugo_Symbol\tEntrez_Gene_Id\tCenter\tNCBI_Build\tChromosome\tStart_Position\tEnd_Position\tStrand\tVariant_Classification\tVariant_Type\tReference_Allele\tTumor_Seq_Allele1\tTumor_Seq_Allele2\tdbSNP_RS\tdbSNP_Val_Status\tTumor_Sample_Barcode\tMatched_Norm_Sample_Barcode\n\
TP53\t7157\tWUSM\tGRCh38\tchr17\t7676154\t7676154\t+\tMissense_Mutation\tSNP\tC\tC\tT\t.\t.\tCPT0088880001\tCPT0088880002\n\
VHL\t7428\tWUSM\tGRCh38\tchr3\t10183900\t10183900\t+\tMissense_Mutation\tSNP\tA\tA\tG\t.\t.\tCPT0088880001\tCPT0088880002\n\
";

    /// Fixture para testar dedupe: mesmo file_id carregado 2× para a mesma sample
    const MAF_FIXTURE_DUP: &str = "\
##fileformat=MAFv2.4\n\
Hugo_Symbol\tEntrez_Gene_Id\tCenter\tNCBI_Build\tChromosome\tStart_Position\tEnd_Position\tStrand\tVariant_Classification\tVariant_Type\tReference_Allele\tTumor_Seq_Allele1\tTumor_Seq_Allele2\tdbSNP_RS\tdbSNP_Val_Status\tTumor_Sample_Barcode\tMatched_Norm_Sample_Barcode\n\
TP53\t7157\tWUSM\tGRCh38\tchr17\t7676154\t7676154\t+\tMissense_Mutation\tSNP\tC\tC\tT\t.\t.\tCPT0077490009\tCPT0077490003\n\
";

    // ─── Teste: skip de comentário ## e cabeçalho por nome ─────────────────────

    #[test]
    fn test_parse_maf_skip_comments_header_by_name() {
        let dir = TempDir::new().unwrap();
        let gz_path = write_maf_gz_fixture(dir.path(), "a.maf.gz", MAF_FIXTURE_A);

        let allowlist: HashSet<String> = ["TP53".to_string(), "VHL".to_string()]
            .into_iter()
            .collect();

        let (occ, _n_syn, _n_off) =
            parse_maf_gz(&gz_path, "C3L-00004_tumor", &allowlist).unwrap();

        // Deve ter 3 ocorrências: TP53 missense, VHL nonsense, VHL Frame_Shift_Del
        // Silent e BRCA1 (offlist) são descartados
        assert_eq!(
            occ.len(),
            3,
            "Esperava 3 ocorrências não-sinônimas na allowlist; got={}",
            occ.len()
        );
    }

    // ─── Teste: filtro não-sinônimo ─────────────────────────────────────────────

    #[test]
    fn test_filtro_sinonimo() {
        let dir = TempDir::new().unwrap();
        let gz_path = write_maf_gz_fixture(dir.path(), "a.maf.gz", MAF_FIXTURE_A);

        let allowlist: HashSet<String> = ["TP53".to_string(), "VHL".to_string()]
            .into_iter()
            .collect();

        let (_occ, n_syn, _n_off) =
            parse_maf_gz(&gz_path, "C3L-00004_tumor", &allowlist).unwrap();

        // 1 linha Silent em TP53
        assert!(n_syn >= 1, "Deveria ter pelo menos 1 variante sinônima descartada");
    }

    // ─── Teste: filtro offlist ──────────────────────────────────────────────────

    #[test]
    fn test_filtro_offlist() {
        let dir = TempDir::new().unwrap();
        let gz_path = write_maf_gz_fixture(dir.path(), "a.maf.gz", MAF_FIXTURE_A);

        let allowlist: HashSet<String> = ["TP53".to_string(), "VHL".to_string()]
            .into_iter()
            .collect();

        let (_occ, _n_syn, n_off) =
            parse_maf_gz(&gz_path, "C3L-00004_tumor", &allowlist).unwrap();

        // 1 linha BRCA1 fora da allowlist
        assert!(n_off >= 1, "Deveria ter pelo menos 1 variante offlist descartada");
    }

    // ─── Teste: variant_key consistente com normalize_variant_key ───────────────

    #[test]
    fn test_variant_key_consistencia_snv() {
        let dir = TempDir::new().unwrap();
        let gz_path = write_maf_gz_fixture(dir.path(), "a.maf.gz", MAF_FIXTURE_A);

        let allowlist: HashSet<String> = ["TP53".to_string(), "VHL".to_string()]
            .into_iter()
            .collect();

        let (occ, _, _) = parse_maf_gz(&gz_path, "C3L-00004_tumor", &allowlist).unwrap();

        // TP53 missense: chr17:7676154:C:T → normalize deve remover 'chr'
        let tp53_occ = occ
            .iter()
            .find(|o| o.gene_symbol == "TP53" && o.variant_classification == "Missense_Mutation")
            .expect("TP53 missense deve estar presente");

        // A chave esperada: strip de 'chr17' → '17'
        let expected = normalize_variant_key("chr17", "7676154", "C", "T").unwrap();
        assert_eq!(
            tp53_occ.variant_key, expected,
            "variant_key do MAF deve ser idêntico ao de normalize_variant_key"
        );
        // Confirmar que NÃO começa com 'chr'
        assert!(
            !tp53_occ.variant_key.starts_with("chr"),
            "variant_key não deve ter prefixo 'chr': {}",
            tp53_occ.variant_key
        );
    }

    // ─── Teste: consistência com ClinVar (mesma variante) ───────────────────────

    #[test]
    fn test_variant_key_consistente_com_clinvar() {
        // Simula a mesma variante TP53 como viria do ClinVar (sem chr) e do MAF (com chr)
        let maf_key = normalize_variant_key("chr17", "7676154", "C", "T");
        let clinvar_key = normalize_variant_key("17", "7676154", "C", "T");
        assert_eq!(
            maf_key, clinvar_key,
            "MAF e ClinVar devem gerar a mesma chave para a mesma variante"
        );
    }

    // ─── Teste: dedupe entre 2 arquivos da mesma amostra ──────────────────────

    #[test]
    fn test_dedupe_dois_arquivos_mesma_amostra() {
        let dir = TempDir::new().unwrap();
        let gz_a = write_maf_gz_fixture(dir.path(), "dup_a.maf.gz", MAF_FIXTURE_DUP);
        let gz_b = write_maf_gz_fixture(dir.path(), "dup_b.maf.gz", MAF_FIXTURE_DUP);

        let allowlist: HashSet<String> = ["TP53".to_string()].into_iter().collect();
        let sample = "C3L-00004_tumor";

        // Parse os dois arquivos e simula o dedupe global
        let (occ_a, _, _) = parse_maf_gz(&gz_a, sample, &allowlist).unwrap();
        let (occ_b, _, _) = parse_maf_gz(&gz_b, sample, &allowlist).unwrap();

        let mut seen: HashSet<(String, String)> = HashSet::new();
        let mut unique: Vec<Occurrence> = Vec::new();

        for occ in occ_a.into_iter().chain(occ_b.into_iter()) {
            let key = (occ.sample_accession.clone(), occ.variant_key.clone());
            if seen.insert(key) {
                unique.push(occ);
            }
        }

        // A mesma variante TP53:17:7676154:C:T não deve ser contada 2×
        assert_eq!(
            unique.len(),
            1,
            "Mesma (sample, variant_key) em 2 arquivos deve contar 1× após dedupe; got={}",
            unique.len()
        );
    }

    // ─── Teste: 2 amostras — mesma variante conta separada ──────────────────────

    #[test]
    fn test_duas_amostras_mesma_variante_conta_separada() {
        let dir = TempDir::new().unwrap();
        let gz_a = write_maf_gz_fixture(dir.path(), "s1.maf.gz", MAF_FIXTURE_DUP);
        let gz_b = write_maf_gz_fixture(dir.path(), "s2.maf.gz", MAF_FIXTURE_DUP);

        let allowlist: HashSet<String> = ["TP53".to_string()].into_iter().collect();

        let (occ_a, _, _) = parse_maf_gz(&gz_a, "C3L-00004_tumor", &allowlist).unwrap();
        let (occ_b, _, _) = parse_maf_gz(&gz_b, "C3L-00099_tumor", &allowlist).unwrap();

        let mut seen: HashSet<(String, String)> = HashSet::new();
        let mut unique: Vec<Occurrence> = Vec::new();

        for occ in occ_a.into_iter().chain(occ_b.into_iter()) {
            let key = (occ.sample_accession.clone(), occ.variant_key.clone());
            if seen.insert(key) {
                unique.push(occ);
            }
        }

        // Mesma variante em amostras DIFERENTES → 2 entradas
        assert_eq!(
            unique.len(),
            2,
            "Mesma variante em 2 amostras diferentes deve gerar 2 ocorrências; got={}",
            unique.len()
        );
    }

    // ─── Teste: burden correto ─────────────────────────────────────────────────

    #[test]
    fn test_burden_correto_duas_amostras() {
        let dir = TempDir::new().unwrap();
        let gz_a = write_maf_gz_fixture(dir.path(), "a.maf.gz", MAF_FIXTURE_A);
        let gz_b = write_maf_gz_fixture(dir.path(), "b.maf.gz", MAF_FIXTURE_B);

        let allowlist: HashSet<String> = ["TP53".to_string(), "VHL".to_string()]
            .into_iter()
            .collect();

        let sample_a = "C3L-00004_tumor";
        let sample_b = "C3L-00099_tumor";

        let (occ_a, _, _) = parse_maf_gz(&gz_a, sample_a, &allowlist).unwrap();
        let (occ_b, _, _) = parse_maf_gz(&gz_b, sample_b, &allowlist).unwrap();

        let mut seen: HashSet<(String, String)> = HashSet::new();
        let mut burden: HashMap<(String, String), u32> = HashMap::new();

        for occ in occ_a.into_iter().chain(occ_b.into_iter()) {
            let dedup_key = (occ.sample_accession.clone(), occ.variant_key.clone());
            if seen.insert(dedup_key) {
                *burden
                    .entry((occ.gene_symbol.clone(), occ.sample_accession.clone()))
                    .or_insert(0) += 1;
            }
        }

        // sample_a: TP53=1 (missense), VHL=2 (nonsense + frame_shift_del)
        let tp53_a = burden.get(&("TP53".to_string(), sample_a.to_string())).copied().unwrap_or(0);
        let vhl_a = burden.get(&("VHL".to_string(), sample_a.to_string())).copied().unwrap_or(0);

        // sample_b: TP53=1 (missense), VHL=1 (missense)
        let tp53_b = burden.get(&("TP53".to_string(), sample_b.to_string())).copied().unwrap_or(0);
        let vhl_b = burden.get(&("VHL".to_string(), sample_b.to_string())).copied().unwrap_or(0);

        assert_eq!(tp53_a, 1, "TP53 burden para sample_a deve ser 1");
        assert_eq!(vhl_a, 2, "VHL burden para sample_a deve ser 2 (nonsense + frame_shift)");
        assert_eq!(tp53_b, 1, "TP53 burden para sample_b deve ser 1");
        assert_eq!(vhl_b, 1, "VHL burden para sample_b deve ser 1");
    }

    // ─── Teste: manifesto n_occurrences e sample_columns role=tumor ───────────

    #[test]
    fn test_manifesto_sample_columns_role_tumor() {
        let dir = TempDir::new().unwrap();
        let gz_a = write_maf_gz_fixture(dir.path(), "a.maf.gz", MAF_FIXTURE_A);
        let gz_b = write_maf_gz_fixture(dir.path(), "b.maf.gz", MAF_FIXTURE_B);

        let allowlist: HashSet<String> = ["TP53".to_string(), "VHL".to_string()]
            .into_iter()
            .collect();

        // Simular o mesmo fluxo do loader (sem rede): parsear e montar burden/sample_columns
        let sample_a = "C3L-00004_tumor";
        let sample_b = "C3L-00099_tumor";

        let (occ_a, _, _) = parse_maf_gz(&gz_a, sample_a, &allowlist).unwrap();
        let (occ_b, _, _) = parse_maf_gz(&gz_b, sample_b, &allowlist).unwrap();

        let mut seen: HashSet<(String, String)> = HashSet::new();
        let mut unique_occurrences: Vec<Occurrence> = Vec::new();
        let mut burden: HashMap<(String, String), u32> = HashMap::new();
        let mut genes_seen: HashSet<String> = HashSet::new();
        let mut samples_seen: HashSet<String> = HashSet::new();

        for occ in occ_a.into_iter().chain(occ_b.into_iter()) {
            let dedup_key = (occ.sample_accession.clone(), occ.variant_key.clone());
            if seen.insert(dedup_key) {
                *burden
                    .entry((occ.gene_symbol.clone(), occ.sample_accession.clone()))
                    .or_insert(0) += 1;
                genes_seen.insert(occ.gene_symbol.clone());
                samples_seen.insert(occ.sample_accession.clone());
                unique_occurrences.push(occ);
            }
        }

        let mut genes_sorted: Vec<String> = genes_seen.into_iter().collect();
        genes_sorted.sort_unstable();
        let mut samples_sorted: Vec<String> = samples_seen.into_iter().collect();
        samples_sorted.sort_unstable();

        let parquet_path = dir.path().join("test_burden.parquet");
        let (checksum, sample_columns) =
            write_burden_parquet(&parquet_path, &genes_sorted, &samples_sorted, &burden).unwrap();

        // Verificar sample_columns: todas com sample_role = "tumor"
        for sc in &sample_columns {
            assert_eq!(
                sc.sample_role, "tumor",
                "sample_role deve ser 'tumor'; got={}",
                sc.sample_role
            );
        }

        // n_occurrences correto
        let n_occurrences = unique_occurrences.len();
        assert!(
            n_occurrences >= 3,
            "Deve ter pelo menos 3 ocorrências únicas; got={}",
            n_occurrences
        );

        // Checksum não vazio
        assert!(!checksum.is_empty(), "MD5 não pode ser vazio");

        // Parquet foi criado
        assert!(parquet_path.exists(), "Parquet deve existir em disco");
    }

    // ─── Teste: Parquet tem coluna feature + colunas T_<sample> Int32 ──────────

    #[test]
    fn test_parquet_schema_correto() {
        let dir = TempDir::new().unwrap();
        let gz_a = write_maf_gz_fixture(dir.path(), "a.maf.gz", MAF_FIXTURE_A);

        let allowlist: HashSet<String> = ["TP53".to_string(), "VHL".to_string()]
            .into_iter()
            .collect();

        let (occ, _, _) = parse_maf_gz(&gz_a, "C3L-00004_tumor", &allowlist).unwrap();

        let mut burden: HashMap<(String, String), u32> = HashMap::new();
        let mut genes_seen: HashSet<String> = HashSet::new();
        let mut samples_seen: HashSet<String> = HashSet::new();
        let mut seen_keys: HashSet<(String, String)> = HashSet::new();

        for o in occ {
            let dk = (o.sample_accession.clone(), o.variant_key.clone());
            if seen_keys.insert(dk) {
                *burden
                    .entry((o.gene_symbol.clone(), o.sample_accession.clone()))
                    .or_insert(0) += 1;
                genes_seen.insert(o.gene_symbol.clone());
                samples_seen.insert(o.sample_accession.clone());
            }
        }

        let mut genes_sorted: Vec<String> = genes_seen.into_iter().collect();
        genes_sorted.sort_unstable();
        let mut samples_sorted: Vec<String> = samples_seen.into_iter().collect();
        samples_sorted.sort_unstable();

        let parquet_path = dir.path().join("schema_test.parquet");
        let (_, sample_columns) =
            write_burden_parquet(&parquet_path, &genes_sorted, &samples_sorted, &burden).unwrap();

        // Verificar que sample_columns tem column_index correto (col 0 = feature → amostras ≥ 1)
        for sc in &sample_columns {
            assert!(
                sc.column_index >= 1,
                "column_index deve ser >= 1 (col 0 = feature); got={}",
                sc.column_index
            );
        }

        // n_features = genes na allowlist com variante
        assert_eq!(genes_sorted.len(), 2, "TP53 e VHL devem aparecer");
        // n_samples = 1 amostra
        assert_eq!(samples_sorted.len(), 1);
    }

    // ─── Teste: validate_gdc_url ───────────────────────────────────────────────

    #[test]
    fn test_validate_gdc_url_aceito() {
        assert!(validate_gdc_url(
            "https://api.gdc.cancer.gov/data/9a4c1948-abcd-1234-efgh-000000000000"
        )
        .is_ok());
    }

    #[test]
    fn test_validate_gdc_url_host_errado() {
        assert!(validate_gdc_url("https://evil.com/data/xxx").is_err());
    }

    #[test]
    fn test_validate_gdc_url_http_rejeitado() {
        assert!(validate_gdc_url(
            "http://api.gdc.cancer.gov/data/9a4c1948-abcd-1234-efgh-000000000000"
        )
        .is_err());
    }

    #[test]
    fn test_validate_gdc_url_path_traversal() {
        assert!(validate_gdc_url("https://api.gdc.cancer.gov/../etc/passwd").is_err());
    }

    // ─── Teste: is_nonsynonymous cobre todos os tipos listados ────────────────

    #[test]
    fn test_is_nonsynonymous_tipos() {
        let nonsynonymous = [
            "Missense_Mutation",
            "Nonsense_Mutation",
            "Frame_Shift_Del",
            "Frame_Shift_Ins",
            "In_Frame_Del",
            "In_Frame_Ins",
            "Splice_Site",
            "Translation_Start_Site",
            "Nonstop_Mutation",
        ];
        for t in &nonsynonymous {
            assert!(is_nonsynonymous(t), "Deve ser não-sinônimo: {}", t);
        }

        let synonymous = ["Silent", "Intron", "3'UTR", "5'UTR", "RNA", "IGR", "3'Flank"];
        for t in &synonymous {
            assert!(!is_nonsynonymous(t), "Deve ser sinônimo (descartado): {}", t);
        }
    }

    // ─── Teste: TSV de ocorrências tem cabeçalho e formato correto ─────────────

    #[test]
    fn test_tsv_ocorrencias_formato() {
        let dir = TempDir::new().unwrap();
        let tsv_path = dir.path().join("occ.tsv");

        let occ = vec![
            Occurrence {
                sample_accession: "C3L-00004_tumor".to_string(),
                variant_key: "17:7676154:C:T".to_string(),
                gene_symbol: "TP53".to_string(),
                variant_classification: "Missense_Mutation".to_string(),
            },
            Occurrence {
                sample_accession: "C3L-00004_tumor".to_string(),
                variant_key: "3:10183774:G:A".to_string(),
                gene_symbol: "VHL".to_string(),
                variant_classification: "Nonsense_Mutation".to_string(),
            },
        ];

        write_occurrences_tsv(&tsv_path, &occ).unwrap();

        let content = std::fs::read_to_string(&tsv_path).unwrap();
        let lines: Vec<&str> = content.lines().collect();

        // Cabeçalho na linha 0
        assert!(
            lines[0].contains("sample_accession"),
            "Linha 0 deve ser cabeçalho: {}",
            lines[0]
        );
        assert!(
            lines[0].contains("variant_key"),
            "Cabeçalho deve ter variant_key: {}",
            lines[0]
        );
        assert!(
            lines[0].contains("gene_symbol"),
            "Cabeçalho deve ter gene_symbol: {}",
            lines[0]
        );
        assert!(
            lines[0].contains("variant_classification"),
            "Cabeçalho deve ter variant_classification: {}",
            lines[0]
        );

        // 2 linhas de dados
        assert_eq!(lines.len(), 3, "1 cabeçalho + 2 ocorrências; got={}", lines.len());

        // Verificar linha 1
        assert!(lines[1].contains("C3L-00004_tumor"));
        assert!(lines[1].contains("17:7676154:C:T"));
        assert!(lines[1].contains("TP53"));
        assert!(lines[1].contains("Missense_Mutation"));
    }
}
