/// CPTAC matrix loader — baixa arquivos `.cct` do LinkedOmics, normaliza e grava Parquet.
///
/// # Formato de entrada (.cct)
///
/// TSV tab-delimited, sem BOM:
/// - Linha 1 (cabeçalho): célula 1 = literal `gene` (ou outro feature axis);
///   colunas seguintes = Case_IDs (ex.: `C3L-00004`).
/// - Linhas 2..: célula 1 = gene symbol; colunas seguintes = valores float
///   em notação decimal ou científica (ex.: `-2.48e-05`). Célula vazia = missing/NaN.
///
/// # Saída Parquet
///
/// Um único arquivo `<dest_dir>/<parquet_name>` com:
/// - Coluna 0: `feature` (Utf8/String) — símbolo do gene.
/// - Colunas 1..N_tumor: `T_<case_id>` — amostras tumor (Float32, nullable).
/// - Colunas N_tumor+1..N_tumor+N_normal: `N_<case_id>` — amostras normal.
///
/// # Segurança
///
/// As URLs são parâmetros externos; `validate_linkedomics_url` rejeita qualquer
/// URL cujo host não seja `www.linkedomics.org` ou cujo esquema não seja `https`.

use std::collections::HashMap;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use arrow::array::{ArrayRef, Float32Array, StringArray};
use arrow::datatypes::{DataType, Field, Schema};
use arrow::record_batch::RecordBatch;
use md5::{Digest, Md5};
use parquet::arrow::ArrowWriter;
use parquet::file::properties::WriterProperties;

use crate::ncbi::client::NcbiClient;

// ─── Structs públicos (manifesto) ─────────────────────────────────────────────

/// Metadados de uma coluna de amostra no Parquet.
#[derive(Debug, Clone)]
pub struct SampleColumn {
    /// ID original da amostra (ex.: `C3L-00004`).
    pub case_id: String,
    /// `"tumor"` ou `"normal"`.
    pub sample_role: String,
    /// Posição 0-based da coluna no Parquet (col 0 = feature; amostras começam em 1).
    pub column_index: usize,
}

/// Manifesto retornado pelo loader após gravar o Parquet.
///
/// Este é o handoff de contrato para o `vitruvio` (passo 0.3):
/// ele usa esses campos para popular `OmicMatrix` e `OmicMatrixSample`.
#[derive(Debug)]
pub struct MatrixLoadManifest {
    /// Caminho absoluto do artefato Parquet gravado em `dest_dir`.
    pub parquet_path: String,
    /// MD5 (lowercase hex) do arquivo Parquet.
    pub checksum_md5: String,
    /// Número de features (genes na interseção tumor ∩ normal).
    pub n_features: usize,
    /// Número total de amostras (tumor + normal).
    pub n_samples: usize,
    /// Lista ordenada de colunas de amostra com metadados.
    /// Ordem: tumor primeiro (em ordem original), depois normal.
    pub sample_columns: Vec<SampleColumn>,
    /// Número de genes descartados por não estarem na interseção.
    pub genes_discarded: usize,
}

// ─── Validação de URL ─────────────────────────────────────────────────────────

/// Valida que a URL pertence exclusivamente ao host `www.linkedomics.org` via HTTPS.
///
/// Rejeita qualquer URL com host diferente ou esquema diferente de `https`,
/// prevenindo SSRF caso o caller passe uma URL não confiável.
pub fn validate_linkedomics_url(url: &str) -> Result<(), String> {
    if !url.starts_with("https://www.linkedomics.org/") {
        return Err(format!(
            "URL rejeitada por política de host: '{}'. \
             Apenas https://www.linkedomics.org/ é permitido para o loader CPTAC.",
            url
        ));
    }
    let path_part = &url["https://www.linkedomics.org/".len()..];
    if path_part.contains("..") {
        return Err(format!("URL rejeitada: contém path traversal '..' em '{}'", url));
    }
    Ok(())
}

// ─── Parse streaming do .cct ─────────────────────────────────────────────────

/// Resultado do parse de um arquivo .cct.
struct CctMatrix {
    /// Lista ordenada de IDs de amostra (coluna 1 em diante do cabeçalho).
    sample_ids: Vec<String>,
    /// Genes: `(gene_symbol, Vec<Option<f32>>)` onde o Vec tem `n_samples` entradas.
    /// Layout: `genes[gene_idx].1[sample_idx]`.
    genes: Vec<(String, Vec<Option<f32>>)>,
}

/// Parse streaming de um arquivo .cct.
///
/// Uma passada: lê linha a linha sem bufferizar o arquivo inteiro em String.
fn parse_cct_file(path: &Path) -> Result<CctMatrix, String> {
    let file = std::fs::File::open(path)
        .map_err(|e| format!("Falha ao abrir {:?}: {}", path, e))?;

    let reader = BufReader::new(file);
    let mut lines = reader.lines();

    // Linha 1: cabeçalho
    let header_line = lines
        .next()
        .ok_or_else(|| format!("Arquivo .cct vazio: {:?}", path))?
        .map_err(|e| format!("Erro ao ler cabeçalho de {:?}: {}", path, e))?;

    let header_fields: Vec<&str> = header_line.split('\t').collect();
    if header_fields.is_empty() {
        return Err(format!("Cabeçalho vazio em {:?}", path));
    }

    let feature_col_name = header_fields[0].trim();
    if feature_col_name != "gene" {
        eprintln!(
            "[matrix_loader] aviso: primeira coluna do cabeçalho é '{}', esperava 'gene'",
            feature_col_name
        );
    }

    // IDs de amostra: colunas 1..
    let sample_ids: Vec<String> = header_fields[1..]
        .iter()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect();

    let n_samples = sample_ids.len();
    if n_samples == 0 {
        return Err(format!("Nenhuma amostra encontrada no cabeçalho de {:?}", path));
    }

    // Linhas de dado
    let mut genes: Vec<(String, Vec<Option<f32>>)> = Vec::new();

    for (line_num, line_result) in lines.enumerate() {
        let line = line_result
            .map_err(|e| format!("Erro ao ler linha {} de {:?}: {}", line_num + 2, path, e))?;

        let line = line.trim_end_matches('\r');
        if line.is_empty() {
            continue;
        }

        let fields: Vec<&str> = line.split('\t').collect();
        if fields.is_empty() {
            continue;
        }

        let gene_symbol = fields[0].trim().to_string();
        if gene_symbol.is_empty() {
            continue;
        }

        // Valores: parse float ou None para célula vazia/NA/NaN
        let values: Vec<Option<f32>> = (0..n_samples)
            .map(|i| {
                let cell = fields.get(i + 1).map(|s| s.trim()).unwrap_or("");
                parse_float_cell(cell)
            })
            .collect();

        genes.push((gene_symbol, values));
    }

    Ok(CctMatrix { sample_ids, genes })
}

/// Converte uma célula TSV em `Option<f32>`.
///
/// Aceita notação decimal e científica. Retorna `None` para missing/inválido.
#[inline]
fn parse_float_cell(cell: &str) -> Option<f32> {
    if cell.is_empty()
        || cell.eq_ignore_ascii_case("na")
        || cell.eq_ignore_ascii_case("nan")
        || cell.eq_ignore_ascii_case("null")
        || cell == "."
    {
        return None;
    }
    cell.parse::<f64>().ok().map(|v| v as f32)
}

// ─── Alinhamento por interseção de genes ─────────────────────────────────────

/// Resultado do alinhamento dos dois arquivos .cct.
struct AlignedMatrix {
    /// Genes na interseção, em ordem alfabética.
    genes: Vec<String>,
    /// Colunas no layout Arrow: uma entrada por amostra tumor, cada Vec tem `n_genes` entradas.
    /// `tumor_sample_cols[sample_idx][gene_idx]`
    tumor_sample_cols: Vec<Vec<Option<f32>>>,
    /// Colunas no layout Arrow: uma entrada por amostra normal.
    /// `normal_sample_cols[sample_idx][gene_idx]`
    normal_sample_cols: Vec<Vec<Option<f32>>>,
    /// Genes descartados (presentes em apenas um arquivo).
    genes_discarded: usize,
}

/// Alinha dois `CctMatrix` por gene symbol (interseção) e transpõe para
/// layout coluna-por-amostra (Arrow-native: cada array = uma coluna de n_genes linhas).
fn align_matrices(tumor: &CctMatrix, normal: &CctMatrix) -> AlignedMatrix {
    // Indexar por gene → índice no vetor
    let tumor_index: HashMap<&str, usize> = tumor
        .genes
        .iter()
        .enumerate()
        .map(|(i, (g, _))| (g.as_str(), i))
        .collect();

    let normal_index: HashMap<&str, usize> = normal
        .genes
        .iter()
        .enumerate()
        .map(|(i, (g, _))| (g.as_str(), i))
        .collect();

    // Interseção: genes presentes em AMBOS
    let mut intersection: Vec<String> = tumor_index
        .keys()
        .filter(|g| normal_index.contains_key(*g))
        .map(|g| g.to_string())
        .collect();

    // Ordem alfabética para idempotência do Parquet
    intersection.sort_unstable();

    let n_genes = intersection.len();
    let n_tumor_samples = tumor.sample_ids.len();
    let n_normal_samples = normal.sample_ids.len();

    // Calcular genes descartados
    let n_discarded = {
        let mut all: std::collections::HashSet<&str> = tumor_index.keys().copied().collect();
        for g in normal_index.keys() {
            all.insert(g);
        }
        all.len() - n_genes
    };

    if n_discarded > 0 {
        eprintln!(
            "[matrix_loader] {} genes descartados (fora da interseção tumor ∩ normal)",
            n_discarded
        );
    }

    // Construir arrays no layout coluna-por-amostra (Arrow-native):
    // tumor_sample_cols[s] = vetor de n_genes valores para a amostra tumor s
    let mut tumor_sample_cols: Vec<Vec<Option<f32>>> =
        vec![Vec::with_capacity(n_genes); n_tumor_samples];
    let mut normal_sample_cols: Vec<Vec<Option<f32>>> =
        vec![Vec::with_capacity(n_genes); n_normal_samples];

    for gene in &intersection {
        let ti = tumor_index[gene.as_str()];
        let ni = normal_index[gene.as_str()];

        // Para cada amostra tumor: adicionar valor deste gene
        for s in 0..n_tumor_samples {
            tumor_sample_cols[s].push(tumor.genes[ti].1[s]);
        }
        // Para cada amostra normal: adicionar valor deste gene
        for s in 0..n_normal_samples {
            normal_sample_cols[s].push(normal.genes[ni].1[s]);
        }
    }

    AlignedMatrix {
        genes: intersection,
        tumor_sample_cols,
        normal_sample_cols,
        genes_discarded: n_discarded,
    }
}

// ─── Escrita do Parquet ───────────────────────────────────────────────────────

/// Grava a matriz alinhada em formato Parquet (Arrow columnar).
///
/// Schema:
/// - `feature` (Utf8): gene symbol
/// - `T_<case_id>` (Float32, nullable): valor tumor para cada amostra
/// - `N_<case_id>` (Float32, nullable): valor normal para cada amostra
///
/// `tumor_sample_cols[s]` e `normal_sample_cols[s]` devem ter `n_genes` entradas
/// (layout coluna-por-amostra, Arrow-native). Retorna o MD5 do arquivo gravado.
fn write_parquet(
    dest_path: &Path,
    aligned_genes: &[String],
    tumor_sample_ids: &[String],
    normal_sample_ids: &[String],
    tumor_sample_cols: &[Vec<Option<f32>>],
    normal_sample_cols: &[Vec<Option<f32>>],
) -> Result<String, String> {
    let n_genes = aligned_genes.len();

    // Construir Schema Arrow
    let mut fields: Vec<Field> = Vec::new();
    fields.push(Field::new("feature", DataType::Utf8, false));

    for id in tumor_sample_ids {
        fields.push(Field::new(
            format!("T_{}", id),
            DataType::Float32,
            true, // nullable
        ));
    }
    for id in normal_sample_ids {
        fields.push(Field::new(
            format!("N_{}", id),
            DataType::Float32,
            true,
        ));
    }

    let schema = Arc::new(Schema::new(fields));

    // Coluna feature
    let feature_array: ArrayRef = Arc::new(StringArray::from(
        aligned_genes.iter().map(|s| s.as_str()).collect::<Vec<_>>(),
    ));

    let mut arrays: Vec<ArrayRef> = vec![feature_array];

    // Tumor: uma coluna por amostra, cada uma com n_genes linhas
    for col in tumor_sample_cols {
        debug_assert_eq!(col.len(), n_genes);
        arrays.push(Arc::new(Float32Array::from(col.clone())));
    }

    // Normal: idem
    for col in normal_sample_cols {
        debug_assert_eq!(col.len(), n_genes);
        arrays.push(Arc::new(Float32Array::from(col.clone())));
    }

    let batch = RecordBatch::try_new(schema.clone(), arrays)
        .map_err(|e| format!("Falha ao criar RecordBatch: {}", e))?;

    // Criar diretório se necessário
    if let Some(parent) = dest_path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("Falha ao criar diretório {:?}: {}", parent, e))?;
    }

    // WriterProperties sem timestamp para idempotência byte-a-byte
    let props = WriterProperties::builder()
        .set_created_by("ferris/matrix_loader v0.1".to_string())
        .build();

    let file = std::fs::File::create(dest_path)
        .map_err(|e| format!("Falha ao criar {:?}: {}", dest_path, e))?;

    let mut writer = ArrowWriter::try_new(file, schema, Some(props))
        .map_err(|e| format!("Falha ao inicializar ArrowWriter: {}", e))?;

    writer
        .write(&batch)
        .map_err(|e| format!("Falha ao escrever RecordBatch: {}", e))?;

    writer
        .close()
        .map_err(|e| format!("Falha ao fechar ArrowWriter: {}", e))?;

    // MD5 do Parquet gravado
    let parquet_bytes = std::fs::read(dest_path)
        .map_err(|e| format!("Falha ao ler Parquet para MD5: {}", e))?;
    let mut hasher = Md5::new();
    hasher.update(&parquet_bytes);
    let checksum = format!("{:x}", hasher.finalize());

    Ok(checksum)
}

// ─── Função principal (async) ─────────────────────────────────────────────────

/// Baixa os dois arquivos .cct do LinkedOmics, parseia em streaming,
/// alinha por gene, grava Parquet e retorna o manifesto.
pub async fn load_cptac_matrix_async(
    tumor_url: &str,
    normal_url: &str,
    dest_dir: &Path,
    parquet_name: &str,
) -> Result<MatrixLoadManifest, String> {
    // 1. Validar URLs antes de qualquer I/O
    validate_linkedomics_url(tumor_url)?;
    validate_linkedomics_url(normal_url)?;

    // 2. Cliente HTTP com User-Agent de browser
    let http_client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(600))
        .user_agent("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        .build()
        .map_err(|e| format!("Falha ao criar HTTP client: {}", e))?;

    // NcbiClient importado para resolução de módulo (não usado diretamente aqui;
    // usamos reqwest diretamente com User-Agent customizado)
    let _ = NcbiClient::new(None);

    // 3. Baixar tumor
    let tumor_path = dest_dir.join("cptac_ccrcc_proteome_tumor.cct");
    eprintln!("[matrix_loader] baixando tumor: {}", tumor_url);
    download_with_browser_ua(&http_client, tumor_url, &tumor_path).await?;
    eprintln!("[matrix_loader] tumor gravado em {:?}", tumor_path);

    // 4. Baixar normal
    let normal_path = dest_dir.join("cptac_ccrcc_proteome_normal.cct");
    eprintln!("[matrix_loader] baixando normal: {}", normal_url);
    download_with_browser_ua(&http_client, normal_url, &normal_path).await?;
    eprintln!("[matrix_loader] normal gravado em {:?}", normal_path);

    // 5. Parse streaming
    eprintln!("[matrix_loader] parseando tumor...");
    let tumor_matrix = parse_cct_file(&tumor_path)?;
    eprintln!(
        "[matrix_loader] tumor: {} genes × {} amostras",
        tumor_matrix.genes.len(),
        tumor_matrix.sample_ids.len()
    );

    eprintln!("[matrix_loader] parseando normal...");
    let normal_matrix = parse_cct_file(&normal_path)?;
    eprintln!(
        "[matrix_loader] normal: {} genes × {} amostras",
        normal_matrix.genes.len(),
        normal_matrix.sample_ids.len()
    );

    // 6. Alinhar por interseção de genes
    let aligned = align_matrices(&tumor_matrix, &normal_matrix);

    let n_features = aligned.genes.len();
    let n_tumor = tumor_matrix.sample_ids.len();
    let n_normal = normal_matrix.sample_ids.len();
    let n_samples = n_tumor + n_normal;

    eprintln!(
        "[matrix_loader] interseção: {} genes; amostras: {} tumor + {} normal = {}",
        n_features, n_tumor, n_normal, n_samples
    );

    // 7. Gravar Parquet
    let parquet_path: PathBuf = dest_dir.join(parquet_name);
    eprintln!("[matrix_loader] gravando Parquet em {:?}", parquet_path);

    let checksum_md5 = write_parquet(
        &parquet_path,
        &aligned.genes,
        &tumor_matrix.sample_ids,
        &normal_matrix.sample_ids,
        &aligned.tumor_sample_cols,
        &aligned.normal_sample_cols,
    )?;

    eprintln!("[matrix_loader] Parquet gravado. MD5={}", checksum_md5);

    // 8. Construir manifesto
    let mut sample_columns: Vec<SampleColumn> = Vec::with_capacity(n_samples);

    for (i, case_id) in tumor_matrix.sample_ids.iter().enumerate() {
        sample_columns.push(SampleColumn {
            case_id: case_id.clone(),
            sample_role: "tumor".to_string(),
            column_index: 1 + i, // col 0 = 'feature'
        });
    }
    for (i, case_id) in normal_matrix.sample_ids.iter().enumerate() {
        sample_columns.push(SampleColumn {
            case_id: case_id.clone(),
            sample_role: "normal".to_string(),
            column_index: 1 + n_tumor + i,
        });
    }

    Ok(MatrixLoadManifest {
        parquet_path: parquet_path.to_string_lossy().into_owned(),
        checksum_md5,
        n_features,
        n_samples,
        sample_columns,
        genes_discarded: aligned.genes_discarded,
    })
}

/// Download streaming com User-Agent de browser.
/// Idempotente: regrava o arquivo se já existir.
async fn download_with_browser_ua(
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
        return Err(format!(
            "HTTP {} ao baixar {}. Verifique User-Agent.",
            status, url
        ));
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

// ─── Entry point síncrono ────────────────────────────────────────────────────

/// Carrega a matriz CPTAC CCRCC e grava Parquet. Wrapper síncrono para PyO3.
pub fn load_cptac_matrix(
    tumor_url: &str,
    normal_url: &str,
    dest_dir: &str,
    parquet_name: &str,
) -> Result<MatrixLoadManifest, String> {
    let rt = tokio::runtime::Runtime::new()
        .map_err(|e| format!("Falha ao criar runtime Tokio: {}", e))?;
    let dest_path = Path::new(dest_dir);
    rt.block_on(load_cptac_matrix_async(
        tumor_url,
        normal_url,
        dest_path,
        parquet_name,
    ))
}

// ─── Loader de fosfoproteoma (Obj 2 — Fase 3, passo 3.1) ─────────────────────
//
// Reusa `load_cptac_matrix_async` com os nomes de arquivo do fosfoproteoma CPTAC CCRCC
// (gene-level: `HS_CPTAC_CCRCC_phosphoproteome_gene_Tumor.cct` / `_Normal.cct`).
//
// Discovery (2026-07-16): LinkedOmics disponibiliza apenas a versão gene-level do
// fosfoproteoma CCRCC (não site-level). Feature axis = `phospho_site` conforme o
// vocabulário já existente em `OmicMatrix`; features = símbolos de gene (mesma
// estrutura da matriz de proteoma). Reutiliza `validate_linkedomics_url` sem nenhuma
// allowlist nova — host `www.linkedomics.org` já aprovado desde a Fase 0.
//
// # Segurança
//
// Mesma validação de `load_cptac_matrix`: `validate_linkedomics_url` + teto de bytes
// implementado em `download_with_browser_ua` (streaming, sem bufferização antecipada).
// User-Agent de browser (LinkedOmics bloqueia UA não-browser).

/// Baixa o fosfoproteoma CPTAC CCRCC (gene-level, Tumor + Normal), parseia e grava Parquet.
///
/// Wrapper síncrono para PyO3 — reusa `load_cptac_matrix_async` sem duplicar lógica.
///
/// # Argumentos
///
/// | Parâmetro | Descrição |
/// |---|---|
/// | `tumor_url` | URL do `.cct` de tumor (deve ser `www.linkedomics.org`). |
/// | `normal_url` | URL do `.cct` de normal (deve ser `www.linkedomics.org`). |
/// | `dest_dir` | Diretório de destino para o Parquet. |
/// | `parquet_name` | Nome do arquivo Parquet (default sugerido: `cptac_ccrcc_phospho.parquet`). |
///
/// # Retorno
///
/// `MatrixLoadManifest` — mesmo contrato do proteoma (Fase 0).
/// O `vitruvio` cria `OmicMatrix(omics_layer='proteomic', feature_axis='phospho_site',
/// data_format_level='intensities')` com base no manifesto.
pub fn load_cptac_phospho_matrix(
    tumor_url: &str,
    normal_url: &str,
    dest_dir: &str,
    parquet_name: &str,
) -> Result<MatrixLoadManifest, String> {
    // Validar URLs (reutiliza validate_linkedomics_url — mesmo host da Fase 0)
    validate_linkedomics_url(tumor_url)?;
    validate_linkedomics_url(normal_url)?;

    let rt = tokio::runtime::Runtime::new()
        .map_err(|e| format!("Falha ao criar runtime Tokio: {}", e))?;
    let dest_path = Path::new(dest_dir);

    // Reutiliza a função async existente — mesma lógica de parse/align/Parquet.
    // Os nomes dos arquivos de cache internos diferem (cptac_ccrcc_proteome_*) mas são
    // irrelevantes para o manifesto: o Parquet gravado usa `parquet_name`.
    rt.block_on(load_cptac_phospho_matrix_async(
        tumor_url,
        normal_url,
        dest_path,
        parquet_name,
    ))
}

/// Async interno do loader de fosfoproteoma.
///
/// Difere de `load_cptac_matrix_async` apenas no nome dos arquivos de cache local
/// (evita colisão com o cache do proteoma no mesmo `dest_dir`).
async fn load_cptac_phospho_matrix_async(
    tumor_url: &str,
    normal_url: &str,
    dest_dir: &Path,
    parquet_name: &str,
) -> Result<MatrixLoadManifest, String> {
    validate_linkedomics_url(tumor_url)?;
    validate_linkedomics_url(normal_url)?;

    let http_client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(600))
        .user_agent(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) \
             AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        .build()
        .map_err(|e| format!("Falha ao criar HTTP client: {}", e))?;

    // Nomes de cache distintos do proteoma para evitar colisão
    let tumor_path = dest_dir.join("cptac_ccrcc_phospho_tumor.cct");
    let normal_path = dest_dir.join("cptac_ccrcc_phospho_normal.cct");

    eprintln!("[matrix_loader/phospho] baixando tumor: {}", tumor_url);
    download_with_browser_ua(&http_client, tumor_url, &tumor_path).await?;

    eprintln!("[matrix_loader/phospho] baixando normal: {}", normal_url);
    download_with_browser_ua(&http_client, normal_url, &normal_path).await?;

    eprintln!("[matrix_loader/phospho] parseando tumor...");
    let tumor_matrix = parse_cct_file(&tumor_path)?;
    eprintln!(
        "[matrix_loader/phospho] tumor: {} genes × {} amostras",
        tumor_matrix.genes.len(),
        tumor_matrix.sample_ids.len()
    );

    eprintln!("[matrix_loader/phospho] parseando normal...");
    let normal_matrix = parse_cct_file(&normal_path)?;
    eprintln!(
        "[matrix_loader/phospho] normal: {} genes × {} amostras",
        normal_matrix.genes.len(),
        normal_matrix.sample_ids.len()
    );

    let aligned = align_matrices(&tumor_matrix, &normal_matrix);

    let n_features = aligned.genes.len();
    let n_tumor = tumor_matrix.sample_ids.len();
    let n_normal = normal_matrix.sample_ids.len();
    let n_samples = n_tumor + n_normal;

    eprintln!(
        "[matrix_loader/phospho] interseção: {} genes; amostras: {} tumor + {} normal",
        n_features, n_tumor, n_normal
    );

    let parquet_path = dest_dir.join(parquet_name);
    eprintln!("[matrix_loader/phospho] gravando Parquet em {:?}", parquet_path);

    let checksum_md5 = write_parquet(
        &parquet_path,
        &aligned.genes,
        &tumor_matrix.sample_ids,
        &normal_matrix.sample_ids,
        &aligned.tumor_sample_cols,
        &aligned.normal_sample_cols,
    )?;

    eprintln!("[matrix_loader/phospho] Parquet gravado. MD5={}", checksum_md5);

    let mut sample_columns: Vec<SampleColumn> = Vec::with_capacity(n_samples);
    for (i, case_id) in tumor_matrix.sample_ids.iter().enumerate() {
        sample_columns.push(SampleColumn {
            case_id: case_id.clone(),
            sample_role: "tumor".to_string(),
            column_index: 1 + i,
        });
    }
    for (i, case_id) in normal_matrix.sample_ids.iter().enumerate() {
        sample_columns.push(SampleColumn {
            case_id: case_id.clone(),
            sample_role: "normal".to_string(),
            column_index: 1 + n_tumor + i,
        });
    }

    Ok(MatrixLoadManifest {
        parquet_path: parquet_path.to_string_lossy().into_owned(),
        checksum_md5,
        n_features,
        n_samples,
        sample_columns,
        genes_discarded: aligned.genes_discarded,
    })
}

// ─── Testes unitários ─────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use tempfile::TempDir;

    fn write_cct_fixture(dir: &Path, filename: &str, content: &str) -> PathBuf {
        let path = dir.join(filename);
        let mut f = std::fs::File::create(&path).unwrap();
        f.write_all(content.as_bytes()).unwrap();
        path
    }

    // Tumor: 3 amostras, 4 genes (TP53, EGFR, BRCA1, ONLY_TUMOR)
    const TUMOR_FIXTURE: &str = "\
gene\tC3L-00001\tC3L-00002\tC3L-00003\n\
TP53\t1.5\t-2.48208681981055e-05\t0.75\n\
EGFR\t0.0\t\tNA\n\
BRCA1\t-1.0\t2.0\t3.0\n\
ONLY_TUMOR\t1.0\t2.0\t3.0\n\
";

    // Normal: 2 amostras, 4 genes (TP53, EGFR, BRCA1, ONLY_NORMAL)
    const NORMAL_FIXTURE: &str = "\
gene\tC3N-00001\tC3N-00002\n\
TP53\t0.5\t-0.3\n\
EGFR\tNaN\t1.0\n\
BRCA1\t-0.5\t0.5\n\
ONLY_NORMAL\t0.1\t0.2\n\
";

    // Interseção esperada: BRCA1, EGFR, TP53 (3 genes, ordem alfabética)
    // Descartados: ONLY_TUMOR, ONLY_NORMAL → 2 genes

    #[test]
    fn test_parse_float_cell_scientific_notation() {
        let v = parse_float_cell("-2.48208681981055e-05");
        assert!(v.is_some());
        let v = v.unwrap();
        assert!((v as f64).abs() < 1e-4);
        assert!((v as f64).abs() > 0.0);
    }

    #[test]
    fn test_parse_float_cell_missing_variants() {
        assert!(parse_float_cell("").is_none());
        assert!(parse_float_cell("NA").is_none());
        assert!(parse_float_cell("na").is_none());
        assert!(parse_float_cell("NaN").is_none());
        assert!(parse_float_cell("null").is_none());
        assert!(parse_float_cell(".").is_none());
    }

    #[test]
    fn test_parse_float_cell_normal_values() {
        assert_eq!(parse_float_cell("1.5"), Some(1.5f32));
        assert_eq!(parse_float_cell("0.0"), Some(0.0f32));
        assert_eq!(parse_float_cell("-1.0"), Some(-1.0f32));
    }

    #[test]
    fn test_parse_cct_file_tumor_fixture() {
        let dir = TempDir::new().unwrap();
        let path = write_cct_fixture(dir.path(), "tumor.cct", TUMOR_FIXTURE);

        let matrix = parse_cct_file(&path).unwrap();

        assert_eq!(matrix.sample_ids, vec!["C3L-00001", "C3L-00002", "C3L-00003"]);
        assert_eq!(matrix.genes.len(), 4);

        // TP53: [1.5, ~-2.48e-05, 0.75]
        let tp53 = matrix.genes.iter().find(|(g, _)| g == "TP53").unwrap();
        assert_eq!(tp53.1[0], Some(1.5f32));
        assert!(tp53.1[1].is_some());
        assert!((tp53.1[1].unwrap() as f64).abs() < 1e-4);
        assert_eq!(tp53.1[2], Some(0.75f32));

        // EGFR: [0.0, None (célula vazia), None (NA)]
        let egfr = matrix.genes.iter().find(|(g, _)| g == "EGFR").unwrap();
        assert_eq!(egfr.1[0], Some(0.0f32));
        assert!(egfr.1[1].is_none());
        assert!(egfr.1[2].is_none());
    }

    #[test]
    fn test_parse_cct_file_normal_fixture() {
        let dir = TempDir::new().unwrap();
        let path = write_cct_fixture(dir.path(), "normal.cct", NORMAL_FIXTURE);

        let matrix = parse_cct_file(&path).unwrap();

        assert_eq!(matrix.sample_ids, vec!["C3N-00001", "C3N-00002"]);
        assert_eq!(matrix.genes.len(), 4);

        // EGFR normal: [NaN→None, 1.0]
        let egfr = matrix.genes.iter().find(|(g, _)| g == "EGFR").unwrap();
        assert!(egfr.1[0].is_none());
        assert_eq!(egfr.1[1], Some(1.0f32));
    }

    #[test]
    fn test_align_matrices_intersection() {
        let dir = TempDir::new().unwrap();
        let tumor_path = write_cct_fixture(dir.path(), "tumor.cct", TUMOR_FIXTURE);
        let normal_path = write_cct_fixture(dir.path(), "normal.cct", NORMAL_FIXTURE);

        let tumor = parse_cct_file(&tumor_path).unwrap();
        let normal = parse_cct_file(&normal_path).unwrap();

        let aligned = align_matrices(&tumor, &normal);

        // Interseção: BRCA1, EGFR, TP53 (3 genes, ordem alfabética)
        assert_eq!(aligned.genes.len(), 3);
        assert_eq!(aligned.genes[0], "BRCA1");
        assert_eq!(aligned.genes[1], "EGFR");
        assert_eq!(aligned.genes[2], "TP53");

        // Não deve conter exclusivos
        assert!(!aligned.genes.contains(&"ONLY_TUMOR".to_string()));
        assert!(!aligned.genes.contains(&"ONLY_NORMAL".to_string()));

        // Colunas tumor: 3 amostras, cada uma com 3 genes
        assert_eq!(aligned.tumor_sample_cols.len(), 3); // n_tumor_samples = 3
        for col in &aligned.tumor_sample_cols {
            assert_eq!(col.len(), 3, "coluna tumor deve ter 3 entradas (n_genes)");
        }

        // Colunas normal: 2 amostras, cada uma com 3 genes
        assert_eq!(aligned.normal_sample_cols.len(), 2); // n_normal_samples = 2
        for col in &aligned.normal_sample_cols {
            assert_eq!(col.len(), 3, "coluna normal deve ter 3 entradas (n_genes)");
        }

        // Genes descartados: ONLY_TUMOR + ONLY_NORMAL = 2
        assert_eq!(aligned.genes_discarded, 2);
    }

    #[test]
    fn test_manifesto_n_features_n_samples() {
        let dir = TempDir::new().unwrap();
        let tumor_path = write_cct_fixture(dir.path(), "tumor.cct", TUMOR_FIXTURE);
        let normal_path = write_cct_fixture(dir.path(), "normal.cct", NORMAL_FIXTURE);

        let tumor = parse_cct_file(&tumor_path).unwrap();
        let normal = parse_cct_file(&normal_path).unwrap();
        let aligned = align_matrices(&tumor, &normal);

        let parquet_path = dir.path().join("test.parquet");
        let checksum = write_parquet(
            &parquet_path,
            &aligned.genes,
            &tumor.sample_ids,
            &normal.sample_ids,
            &aligned.tumor_sample_cols,
            &aligned.normal_sample_cols,
        )
        .unwrap();

        assert!(parquet_path.exists());
        assert_eq!(checksum.len(), 32); // MD5 hex = 32 chars

        let n_features = aligned.genes.len();
        let n_tumor = tumor.sample_ids.len();
        let n_normal = normal.sample_ids.len();
        let n_samples = n_tumor + n_normal;

        assert_eq!(n_features, 3); // BRCA1, EGFR, TP53
        assert_eq!(n_samples, 5); // 3 tumor + 2 normal
        assert_eq!(aligned.genes_discarded, 2);

        // Verificar índices das colunas de amostra
        let mut cols: Vec<SampleColumn> = Vec::new();
        for (i, id) in tumor.sample_ids.iter().enumerate() {
            cols.push(SampleColumn {
                case_id: id.clone(),
                sample_role: "tumor".to_string(),
                column_index: 1 + i,
            });
        }
        for (i, id) in normal.sample_ids.iter().enumerate() {
            cols.push(SampleColumn {
                case_id: id.clone(),
                sample_role: "normal".to_string(),
                column_index: 1 + n_tumor + i,
            });
        }

        // Tumor: cols 1, 2, 3
        assert_eq!(cols[0].column_index, 1);
        assert_eq!(cols[0].sample_role, "tumor");
        assert_eq!(cols[1].column_index, 2);
        assert_eq!(cols[2].column_index, 3);

        // Normal: cols 4, 5
        assert_eq!(cols[3].column_index, 4);
        assert_eq!(cols[3].sample_role, "normal");
        assert_eq!(cols[4].column_index, 5);

        assert_eq!(cols.len(), n_samples);
    }

    #[test]
    fn test_validate_linkedomics_url_accept() {
        assert!(validate_linkedomics_url(
            "https://www.linkedomics.org/data_download/CPTAC-CCRCC/HS_CPTAC_CCRCC_proteome_Tumor.cct"
        )
        .is_ok());
    }

    #[test]
    fn test_validate_linkedomics_url_reject_http() {
        assert!(validate_linkedomics_url(
            "http://www.linkedomics.org/data_download/test.cct"
        )
        .is_err());
    }

    #[test]
    fn test_validate_linkedomics_url_reject_other_host() {
        assert!(validate_linkedomics_url(
            "https://evil.com/data_download/test.cct"
        )
        .is_err());
    }

    #[test]
    fn test_validate_linkedomics_url_reject_path_traversal() {
        assert!(validate_linkedomics_url(
            "https://www.linkedomics.org/../etc/passwd"
        )
        .is_err());
    }

    #[test]
    fn test_parquet_idempotent_checksum() {
        let dir = TempDir::new().unwrap();
        let tumor_path = write_cct_fixture(dir.path(), "tumor.cct", TUMOR_FIXTURE);
        let normal_path = write_cct_fixture(dir.path(), "normal.cct", NORMAL_FIXTURE);

        let tumor = parse_cct_file(&tumor_path).unwrap();
        let normal = parse_cct_file(&normal_path).unwrap();
        let aligned = align_matrices(&tumor, &normal);

        let path1 = dir.path().join("out1.parquet");
        let path2 = dir.path().join("out2.parquet");

        let md5_1 = write_parquet(
            &path1,
            &aligned.genes,
            &tumor.sample_ids,
            &normal.sample_ids,
            &aligned.tumor_sample_cols,
            &aligned.normal_sample_cols,
        )
        .unwrap();

        let md5_2 = write_parquet(
            &path2,
            &aligned.genes,
            &tumor.sample_ids,
            &normal.sample_ids,
            &aligned.tumor_sample_cols,
            &aligned.normal_sample_cols,
        )
        .unwrap();

        assert_eq!(md5_1, md5_2, "Parquet não é idempotente entre duas execuções");
    }
}
