/// Loader de matriz CNV gene×amostra (tumor-only) — LinkedOmics .cct → Parquet + manifesto.
///
/// # Fonte
///
/// `https://www.linkedomics.org/data_download/CPTAC-CCRCC/HS_CPTAC_CCRCC_CNV_gene_Tumor.cct`
/// (~26.7 MB, público, requer User-Agent de browser para evitar 403)
///
/// # Formato .cct (tumor-only)
///
/// TSV tab-delimited, sem BOM:
/// - Linha 1 (cabeçalho): célula 1 = `gene`; colunas seguintes = Case_IDs (C3L-/C3N-...).
/// - Linhas 2..: célula 1 = gene symbol; colunas seguintes = log-ratio contínuo (pode ser vazio/NaN).
///
/// # Diferença em relação ao matrix_loader.rs (proteoma)
///
/// O proteoma tem DOIS arquivos (tumor + normal) que são alinhados por interseção.
/// O CNV tem UM arquivo só (tumor). Por isso:
/// - Não há alinhamento de interseção — todos os genes do arquivo são mantidos.
/// - Todas as amostras recebem `sample_role = "tumor"`.
/// - O Parquet usa prefixo `T_<case_id>` (sem `N_` columns).
///
/// # Saída Parquet
///
/// Um único arquivo `<dest_dir>/<parquet_name>`:
/// - Coluna 0: `feature` (Utf8/String) — símbolo do gene.
/// - Colunas 1..N: `T_<case_id>` (Float32, nullable) — amostras tumor.
///
/// # Manifesto
///
/// `CnvMatrixManifest`: `parquet_path`, `checksum_md5`, `n_features`, `n_samples`,
/// `sample_columns` (todos com `sample_role = "tumor"`).
///
/// # Segurança
///
/// URLs validadas via `validate_linkedomics_url` (reúsa do matrix_loader).
/// User-Agent de browser obrigatório.

use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use arrow::array::{ArrayRef, Float32Array, StringArray};
use arrow::datatypes::{DataType, Field, Schema};
use arrow::record_batch::RecordBatch;
use md5::{Digest, Md5};
use parquet::arrow::ArrowWriter;
use parquet::file::properties::WriterProperties;

use crate::omics::matrix_loader::{
    validate_linkedomics_url, SampleColumn,
};

// ─── Manifesto ────────────────────────────────────────────────────────────────

/// Manifesto retornado pelo loader CNV após gravar o Parquet.
///
/// Handoff para o vitruvio (passo 2.3):
///
/// | Campo | Tipo | Descrição |
/// |---|---|---|
/// | `parquet_path` | `str` | Caminho absoluto do Parquet gravado. |
/// | `checksum_md5` | `str` | MD5 lowercase-hex do arquivo Parquet. |
/// | `n_features` | `usize` | Número de genes (features = linhas do .cct). |
/// | `n_samples` | `usize` | Número de amostras (todas tumor). |
/// | `sample_columns` | `Vec<SampleColumn>` | Todas com `sample_role = "tumor"`. |
#[derive(Debug)]
pub struct CnvMatrixManifest {
    pub parquet_path: String,
    pub checksum_md5: String,
    pub n_features: usize,
    pub n_samples: usize,
    /// Todas as entradas têm `sample_role = "tumor"`.
    pub sample_columns: Vec<SampleColumn>,
}

// ─── Parse streaming do .cct (tumor-only) ────────────────────────────────────

/// Resultado do parse de um arquivo .cct tumor-only.
struct CnvCctMatrix {
    /// IDs de amostra (col 1 em diante do cabeçalho).
    sample_ids: Vec<String>,
    /// Genes: `(gene_symbol, Vec<Option<f32>>)`.
    genes: Vec<(String, Vec<Option<f32>>)>,
}

/// Parse streaming de um arquivo .cct tumor-only.
///
/// Uma passada: lê linha a linha sem bufferizar o arquivo inteiro em String.
/// Reutiliza a lógica de `parse_cct_file` do `matrix_loader.rs` — mesmos
/// valores aceitos para missing: `""`, `NA`, `NaN`, `null`, `.`.
fn parse_cnv_cct_file(path: &Path) -> Result<CnvCctMatrix, String> {
    let file = std::fs::File::open(path)
        .map_err(|e| format!("Falha ao abrir {:?}: {}", path, e))?;

    let reader = BufReader::new(file);
    let mut lines = reader.lines();

    // Linha 1: cabeçalho
    let header_line = lines
        .next()
        .ok_or_else(|| format!("Arquivo .cct CNV vazio: {:?}", path))?
        .map_err(|e| format!("Erro ao ler cabeçalho de {:?}: {}", path, e))?;

    let header_fields: Vec<&str> = header_line.split('\t').collect();
    if header_fields.is_empty() {
        return Err(format!("Cabeçalho vazio em {:?}", path));
    }

    let feature_col_name = header_fields[0].trim();
    if feature_col_name != "gene" {
        eprintln!(
            "[cnv_loader] aviso: primeira coluna do cabeçalho é '{}', esperava 'gene'",
            feature_col_name
        );
    }

    let sample_ids: Vec<String> = header_fields[1..]
        .iter()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect();

    let n_samples = sample_ids.len();
    if n_samples == 0 {
        return Err(format!(
            "Nenhuma amostra encontrada no cabeçalho de {:?}",
            path
        ));
    }

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

        let values: Vec<Option<f32>> = (0..n_samples)
            .map(|i| {
                let cell = fields.get(i + 1).map(|s| s.trim()).unwrap_or("");
                parse_cnv_float_cell(cell)
            })
            .collect();

        genes.push((gene_symbol, values));
    }

    Ok(CnvCctMatrix { sample_ids, genes })
}

/// Converte uma célula TSV em `Option<f32>` para valores CNV log-ratio.
///
/// Aceita notação decimal e científica. Retorna `None` para missing/inválido.
#[inline]
fn parse_cnv_float_cell(cell: &str) -> Option<f32> {
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

// ─── Transposição para layout coluna-por-amostra (Arrow-native) ──────────────

struct CnvAligned {
    genes: Vec<String>,
    /// `sample_cols[sample_idx]` = vetor de n_genes valores para aquela amostra
    sample_cols: Vec<Vec<Option<f32>>>,
}

/// Transpõe a matriz CNV (linha-por-gene) para layout coluna-por-amostra (Arrow-native).
fn transpose_cnv_matrix(matrix: &CnvCctMatrix) -> CnvAligned {
    let n_genes = matrix.genes.len();
    let n_samples = matrix.sample_ids.len();

    let mut sample_cols: Vec<Vec<Option<f32>>> =
        vec![Vec::with_capacity(n_genes); n_samples];

    for (_, values) in &matrix.genes {
        for s in 0..n_samples {
            sample_cols[s].push(values.get(s).copied().flatten());
        }
    }

    CnvAligned {
        genes: matrix.genes.iter().map(|(g, _)| g.clone()).collect(),
        sample_cols,
    }
}

// ─── Escrita do Parquet ───────────────────────────────────────────────────────

/// Grava a matriz CNV em formato Parquet (Arrow columnar).
///
/// Schema:
/// - `feature` (Utf8): gene symbol
/// - `T_<case_id>` (Float32, nullable): log-ratio tumor para cada amostra
///
/// Retorna o MD5 do arquivo gravado.
fn write_cnv_parquet(
    dest_path: &Path,
    aligned: &CnvAligned,
    sample_ids: &[String],
) -> Result<String, String> {
    let n_genes = aligned.genes.len();

    // Schema Arrow
    let mut fields: Vec<Field> = Vec::new();
    fields.push(Field::new("feature", DataType::Utf8, false));

    for id in sample_ids {
        fields.push(Field::new(
            format!("T_{}", id),
            DataType::Float32,
            true, // nullable — log-ratio pode ser missing
        ));
    }

    let schema = Arc::new(Schema::new(fields));

    // Coluna feature
    let feature_array: ArrayRef = Arc::new(StringArray::from(
        aligned.genes.iter().map(|s| s.as_str()).collect::<Vec<_>>(),
    ));

    let mut arrays: Vec<ArrayRef> = vec![feature_array];

    // Amostras tumor: uma coluna por amostra, n_genes linhas
    for col in &aligned.sample_cols {
        debug_assert_eq!(col.len(), n_genes);
        arrays.push(Arc::new(Float32Array::from(col.clone())));
    }

    let batch = RecordBatch::try_new(schema.clone(), arrays)
        .map_err(|e| format!("Falha ao criar RecordBatch CNV: {}", e))?;

    if let Some(parent) = dest_path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("Falha ao criar diretório {:?}: {}", parent, e))?;
    }

    let props = WriterProperties::builder()
        .set_created_by("ferris/cnv_loader v0.1".to_string())
        .build();

    let file = std::fs::File::create(dest_path)
        .map_err(|e| format!("Falha ao criar {:?}: {}", dest_path, e))?;

    let mut writer = ArrowWriter::try_new(file, schema, Some(props))
        .map_err(|e| format!("Falha ao inicializar ArrowWriter CNV: {}", e))?;

    writer
        .write(&batch)
        .map_err(|e| format!("Falha ao escrever RecordBatch CNV: {}", e))?;

    writer
        .close()
        .map_err(|e| format!("Falha ao fechar ArrowWriter CNV: {}", e))?;

    // MD5
    let parquet_bytes = std::fs::read(dest_path)
        .map_err(|e| format!("Falha ao ler Parquet CNV para MD5: {}", e))?;
    let mut hasher = Md5::new();
    hasher.update(&parquet_bytes);
    let checksum = format!("{:x}", hasher.finalize());

    Ok(checksum)
}

// ─── Função principal (async) ─────────────────────────────────────────────────

/// Baixa o arquivo CNV .cct do LinkedOmics, parseia em streaming e grava Parquet.
///
/// `url` deve apontar para `https://www.linkedomics.org/…`.
/// O User-Agent de browser é obrigatório (LinkedOmics retorna 403 sem ele).
pub async fn load_cnv_matrix_async(
    url: &str,
    dest_dir: &Path,
    parquet_name: &str,
) -> Result<CnvMatrixManifest, String> {
    // 1. Validar URL (proteção SSRF — reúsa validador do matrix_loader)
    validate_linkedomics_url(url)?;

    // 2. Cliente HTTP com User-Agent de browser
    let http_client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(600))
        .user_agent(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) \
             AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        .build()
        .map_err(|e| format!("Falha ao criar HTTP client: {}", e))?;

    // 3. Baixar o arquivo CNV .cct
    tokio::fs::create_dir_all(dest_dir)
        .await
        .map_err(|e| format!("create_dir_all {:?}: {}", dest_dir, e))?;

    let cct_path = dest_dir.join("cptac_ccrcc_cnv_tumor.cct");
    eprintln!("[cnv_loader] baixando CNV: {}", url);
    download_cnv_cct(&http_client, url, &cct_path).await?;
    eprintln!("[cnv_loader] CNV gravado em {:?}", cct_path);

    // 4. Parse streaming
    eprintln!("[cnv_loader] parseando .cct CNV...");
    let matrix = parse_cnv_cct_file(&cct_path)?;
    eprintln!(
        "[cnv_loader] {} genes × {} amostras tumor",
        matrix.genes.len(),
        matrix.sample_ids.len()
    );

    // 5. Transpor para layout coluna-por-amostra (Arrow-native)
    let aligned = transpose_cnv_matrix(&matrix);

    let n_features = aligned.genes.len();
    let n_samples = matrix.sample_ids.len();

    // 6. Gravar Parquet
    let parquet_path: PathBuf = dest_dir.join(parquet_name);
    eprintln!("[cnv_loader] gravando Parquet em {:?}", parquet_path);

    let checksum_md5 = write_cnv_parquet(&parquet_path, &aligned, &matrix.sample_ids)?;

    eprintln!(
        "[cnv_loader] Parquet gravado. n_features={} n_samples={} MD5={}",
        n_features, n_samples, checksum_md5
    );

    // 7. Construir manifesto — todas as amostras são tumor
    let sample_columns: Vec<SampleColumn> = matrix
        .sample_ids
        .iter()
        .enumerate()
        .map(|(i, case_id)| SampleColumn {
            case_id: case_id.clone(),
            sample_role: "tumor".to_string(),
            column_index: 1 + i, // col 0 = 'feature'
        })
        .collect();

    Ok(CnvMatrixManifest {
        parquet_path: parquet_path.to_string_lossy().into_owned(),
        checksum_md5,
        n_features,
        n_samples,
        sample_columns,
    })
}

/// Download streaming com User-Agent de browser — mesmo padrão do matrix_loader.
async fn download_cnv_cct(
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
            "HTTP {} ao baixar {}. Verifique User-Agent (LinkedOmics requer browser UA).",
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

// ─── Entry point síncrono ─────────────────────────────────────────────────────

/// Carrega a matriz CNV CPTAC CCRCC e grava Parquet. Wrapper síncrono para PyO3.
pub fn load_cnv_matrix(
    url: &str,
    dest_dir: &str,
    parquet_name: &str,
) -> Result<CnvMatrixManifest, String> {
    let rt = tokio::runtime::Runtime::new()
        .map_err(|e| format!("Falha ao criar runtime Tokio: {}", e))?;
    let dest_path = Path::new(dest_dir);
    rt.block_on(load_cnv_matrix_async(url, dest_path, parquet_name))
}

// ─── Testes unitários (#[cfg(test)]) — SEM rede ──────────────────────────────

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

    /// Fixture tumor-only com 3 amostras e 5 genes.
    /// Inclui NaN, vazio e valor zero para testar handling de missing.
    const CNV_FIXTURE: &str = "\
gene\tC3L-00004\tC3L-00026\tC3N-00150\n\
VHL\t-1.5\t-2.0\t0.1\n\
PTEN\t-0.8\t\tNaN\n\
KRAS\t0.5\t1.2\t0.0\n\
PIK3CA\t0.0\tNA\t1.8\n\
TP53\t-1.1\t-0.9\t-0.5\n\
";

    #[test]
    fn test_parse_cnv_float_cell_values() {
        assert_eq!(parse_cnv_float_cell("1.5"), Some(1.5f32));
        assert_eq!(parse_cnv_float_cell("-2.0"), Some(-2.0f32));
        assert_eq!(parse_cnv_float_cell("0.0"), Some(0.0f32));
    }

    #[test]
    fn test_parse_cnv_float_cell_missing() {
        assert!(parse_cnv_float_cell("").is_none());
        assert!(parse_cnv_float_cell("NA").is_none());
        assert!(parse_cnv_float_cell("NaN").is_none());
        assert!(parse_cnv_float_cell("nan").is_none());
        assert!(parse_cnv_float_cell("null").is_none());
        assert!(parse_cnv_float_cell(".").is_none());
    }

    #[test]
    fn test_parse_cnv_float_cell_scientific() {
        let v = parse_cnv_float_cell("1.5e-2");
        assert!(v.is_some());
        let v = v.unwrap();
        assert!((v - 0.015f32).abs() < 1e-5);
    }

    #[test]
    fn test_parse_cnv_cct_file_sample_ids() {
        let dir = TempDir::new().unwrap();
        let path = write_cct_fixture(dir.path(), "cnv.cct", CNV_FIXTURE);
        let matrix = parse_cnv_cct_file(&path).unwrap();

        assert_eq!(
            matrix.sample_ids,
            vec!["C3L-00004", "C3L-00026", "C3N-00150"]
        );
    }

    #[test]
    fn test_parse_cnv_cct_file_gene_count() {
        let dir = TempDir::new().unwrap();
        let path = write_cct_fixture(dir.path(), "cnv.cct", CNV_FIXTURE);
        let matrix = parse_cnv_cct_file(&path).unwrap();

        // 5 genes no fixture
        assert_eq!(matrix.genes.len(), 5, "Esperava 5 genes no fixture CNV");
    }

    #[test]
    fn test_parse_cnv_cct_file_vhl_values() {
        let dir = TempDir::new().unwrap();
        let path = write_cct_fixture(dir.path(), "cnv.cct", CNV_FIXTURE);
        let matrix = parse_cnv_cct_file(&path).unwrap();

        let vhl = matrix.genes.iter().find(|(g, _)| g == "VHL").unwrap();
        assert_eq!(vhl.1[0], Some(-1.5f32));
        assert_eq!(vhl.1[1], Some(-2.0f32));
        assert_eq!(vhl.1[2], Some(0.1f32));
    }

    #[test]
    fn test_parse_cnv_cct_file_missing_values() {
        let dir = TempDir::new().unwrap();
        let path = write_cct_fixture(dir.path(), "cnv.cct", CNV_FIXTURE);
        let matrix = parse_cnv_cct_file(&path).unwrap();

        // PTEN: [-0.8, None (vazio), None (NaN)]
        let pten = matrix.genes.iter().find(|(g, _)| g == "PTEN").unwrap();
        assert_eq!(pten.1[0], Some(-0.8f32));
        assert!(pten.1[1].is_none(), "PTEN[1] deveria ser None (célula vazia)");
        assert!(pten.1[2].is_none(), "PTEN[2] deveria ser None (NaN)");

        // PIK3CA: [0.0, None (NA), 1.8]
        let pik3ca = matrix.genes.iter().find(|(g, _)| g == "PIK3CA").unwrap();
        assert_eq!(pik3ca.1[0], Some(0.0f32));
        assert!(pik3ca.1[1].is_none(), "PIK3CA[1] deveria ser None (NA)");
        assert_eq!(pik3ca.1[2], Some(1.8f32));
    }

    #[test]
    fn test_transpose_cnv_matrix_shape() {
        let dir = TempDir::new().unwrap();
        let path = write_cct_fixture(dir.path(), "cnv.cct", CNV_FIXTURE);
        let matrix = parse_cnv_cct_file(&path).unwrap();
        let aligned = transpose_cnv_matrix(&matrix);

        // 5 genes no fixture
        assert_eq!(aligned.genes.len(), 5);
        // 3 amostras → 3 colunas
        assert_eq!(aligned.sample_cols.len(), 3);
        // Cada coluna tem 5 entradas (n_genes)
        for col in &aligned.sample_cols {
            assert_eq!(col.len(), 5, "Cada coluna deve ter n_genes=5 entradas");
        }
    }

    #[test]
    fn test_manifesto_sample_roles_all_tumor() {
        let dir = TempDir::new().unwrap();
        let path = write_cct_fixture(dir.path(), "cnv.cct", CNV_FIXTURE);
        let matrix = parse_cnv_cct_file(&path).unwrap();
        let aligned = transpose_cnv_matrix(&matrix);

        let parquet_path = dir.path().join("cnv_test.parquet");
        let checksum = write_cnv_parquet(&parquet_path, &aligned, &matrix.sample_ids).unwrap();

        assert!(parquet_path.exists(), "Parquet CNV deve existir após escrita");
        assert_eq!(checksum.len(), 32, "MD5 hex deve ter 32 chars");

        // Construir manifesto e verificar que todos os roles são "tumor"
        let sample_columns: Vec<SampleColumn> = matrix
            .sample_ids
            .iter()
            .enumerate()
            .map(|(i, case_id)| SampleColumn {
                case_id: case_id.clone(),
                sample_role: "tumor".to_string(),
                column_index: 1 + i,
            })
            .collect();

        for sc in &sample_columns {
            assert_eq!(
                sc.sample_role, "tumor",
                "Todas as amostras CNV devem ter role='tumor'"
            );
        }

        // n_features = 5, n_samples = 3
        assert_eq!(aligned.genes.len(), 5);
        assert_eq!(matrix.sample_ids.len(), 3);
        assert_eq!(sample_columns.len(), 3);

        // Índices das colunas: 1, 2, 3 (col 0 = feature)
        assert_eq!(sample_columns[0].column_index, 1);
        assert_eq!(sample_columns[1].column_index, 2);
        assert_eq!(sample_columns[2].column_index, 3);
    }

    #[test]
    fn test_manifesto_n_features_n_samples() {
        let dir = TempDir::new().unwrap();
        let path = write_cct_fixture(dir.path(), "cnv.cct", CNV_FIXTURE);
        let matrix = parse_cnv_cct_file(&path).unwrap();
        let aligned = transpose_cnv_matrix(&matrix);

        // Verifique que n_features e n_samples estão corretos
        assert_eq!(aligned.genes.len(), 5, "n_features deve ser 5");
        assert_eq!(matrix.sample_ids.len(), 3, "n_samples deve ser 3");
    }

    #[test]
    fn test_parquet_idempotent_checksum() {
        let dir = TempDir::new().unwrap();
        let path = write_cct_fixture(dir.path(), "cnv.cct", CNV_FIXTURE);
        let matrix = parse_cnv_cct_file(&path).unwrap();
        let aligned = transpose_cnv_matrix(&matrix);

        let path1 = dir.path().join("cnv1.parquet");
        let path2 = dir.path().join("cnv2.parquet");

        let md5_1 = write_cnv_parquet(&path1, &aligned, &matrix.sample_ids).unwrap();
        let md5_2 = write_cnv_parquet(&path2, &aligned, &matrix.sample_ids).unwrap();

        assert_eq!(
            md5_1, md5_2,
            "Parquet CNV não é idempotente entre duas execuções"
        );
    }

    #[test]
    fn test_validate_linkedomics_url_cnv() {
        // URL real do CNV deve ser aceita
        assert!(validate_linkedomics_url(
            "https://www.linkedomics.org/data_download/CPTAC-CCRCC/HS_CPTAC_CCRCC_CNV_gene_Tumor.cct"
        )
        .is_ok());

        // URL de outro host deve ser rejeitada
        assert!(
            validate_linkedomics_url("https://evil.com/HS_CPTAC_CCRCC_CNV_gene_Tumor.cct")
                .is_err()
        );
    }

    #[test]
    fn test_parse_cnv_cct_file_gene_names_preserved() {
        let dir = TempDir::new().unwrap();
        let path = write_cct_fixture(dir.path(), "cnv.cct", CNV_FIXTURE);
        let matrix = parse_cnv_cct_file(&path).unwrap();

        // Genes devem preservar o nome como está no arquivo (case-sensitive)
        let gene_names: Vec<&str> = matrix.genes.iter().map(|(g, _)| g.as_str()).collect();
        assert!(gene_names.contains(&"VHL"));
        assert!(gene_names.contains(&"PTEN"));
        assert!(gene_names.contains(&"KRAS"));
        assert!(gene_names.contains(&"PIK3CA"));
        assert!(gene_names.contains(&"TP53"));
    }

    #[test]
    fn test_parse_cnv_cct_empty_lines_skipped() {
        const FIXTURE_WITH_EMPTY: &str = "\
gene\tC3L-00004\tC3L-00026\n\
VHL\t-1.5\t-2.0\n\
\n\
KRAS\t0.5\t1.2\n\
";
        let dir = TempDir::new().unwrap();
        let path = write_cct_fixture(dir.path(), "cnv_empty.cct", FIXTURE_WITH_EMPTY);
        let matrix = parse_cnv_cct_file(&path).unwrap();

        // Linhas vazias devem ser ignoradas → 2 genes
        assert_eq!(matrix.genes.len(), 2, "Linhas vazias devem ser ignoradas");
    }
}
