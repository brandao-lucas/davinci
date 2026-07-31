/// Loader do catálogo de papel do gene — OncoKB cancerGeneList.txt → GeneRole (COPY/UPSERT).
///
/// # Fonte
///
/// `https://www.oncokb.org/api/v1/utils/cancerGeneList.txt`
/// TSV público, TOKEN-FREE. Colunas relevantes (por nome, não por índice fixo):
/// - `Hugo Symbol`      → gene_symbol (UPPERCASE)
/// - `Entrez Gene ID`   → entrez_id (BigInt, pode ser vazio)
/// - `Gene Type`        → role mapeado (ONCOGENE → oncogene, TSG → tsg,
///                        ONCOGENE_AND_TSG → oncogene_and_tsg, NEITHER → neither,
///                        INSUFFICIENT_EVIDENCE / vazio → unknown)
/// - `COSMIC CGC (v99)` (ou coluna com substring "CGC") → cgc_flag (Yes/No)
/// - `Vogelstein`       → vogelstein_flag (Yes/No)
///
/// # Saída
///
/// Nenhum artefato em disco. COPY/UPSERT direto em `core_generole`
/// ON CONFLICT (gene_symbol, source) DO UPDATE (idempotente).
///
/// # Segurança
///
/// URL validada via `validate_oncokb_url` antes de qualquer I/O.
/// Nenhum token de API é necessário para este endpoint.

use std::collections::HashMap;
use std::io::{BufRead, BufReader};
use std::path::Path;

use bytes::Bytes;
use chrono::Utc;
use futures::SinkExt;
use std::pin::pin;
use tokio_postgres::Client;

// ─── Manifesto ────────────────────────────────────────────────────────────────

/// Manifesto retornado pelo loader após COPY em GeneRole.
///
/// Handoff para o vitruvio (passo 2.2):
///
/// | Campo | Tipo | Descrição |
/// |---|---|---|
/// | `n_genes` | `usize` | Total de genes processados do TSV. |
/// | `n_oncogene` | `usize` | Genes com role=oncogene. |
/// | `n_tsg` | `usize` | Genes com role=tsg. |
/// | `n_dual` | `usize` | Genes com role=oncogene_and_tsg. |
/// | `n_neither` | `usize` | Genes com role=neither. |
/// | `n_unknown` | `usize` | Genes com role=unknown (INSUFFICIENT_EVIDENCE ou vazio). |
/// | `source_version` | `String` | Versão da fonte (data do download, ex.: "2026-07-11"). |
/// | `n_upserted` | `u64` | Linhas gravadas via COPY UPSERT. |
#[derive(Debug)]
pub struct GeneRoleLoadManifest {
    pub n_genes: usize,
    pub n_oncogene: usize,
    pub n_tsg: usize,
    pub n_dual: usize,
    pub n_neither: usize,
    pub n_unknown: usize,
    pub source_version: String,
    pub n_upserted: u64,
}

// ─── Validação de URL ─────────────────────────────────────────────────────────

/// Valida que a URL pertence exclusivamente ao host `www.oncokb.org` via HTTPS.
///
/// Rejeita qualquer URL com host diferente ou esquema diferente de `https`,
/// prevenindo SSRF caso o caller passe uma URL não confiável.
pub fn validate_oncokb_url(url: &str) -> Result<(), String> {
    if !url.starts_with("https://www.oncokb.org/") {
        return Err(format!(
            "URL rejeitada por política de host: '{}'. \
             Apenas https://www.oncokb.org/ é permitido para o loader OncoKB.",
            url
        ));
    }
    let path_part = &url["https://www.oncokb.org/".len()..];
    if path_part.contains("..") {
        return Err(format!(
            "URL rejeitada: contém path traversal '..' em '{}'",
            url
        ));
    }
    Ok(())
}

// ─── Mapeamento de Gene Type → role ──────────────────────────────────────────

/// Mapeia o valor cru do campo `Gene Type` do OncoKB para o vocabulário canônico.
///
/// | Gene Type (OncoKB) | role |
/// |---|---|
/// | ONCOGENE | oncogene |
/// | TSG | tsg |
/// | ONCOGENE_AND_TSG | oncogene_and_tsg |
/// | NEITHER | neither |
/// | INSUFFICIENT_EVIDENCE | unknown |
/// | (vazio) | unknown |
pub fn map_gene_type_to_role(gene_type: &str) -> &'static str {
    match gene_type.trim() {
        "ONCOGENE" => "oncogene",
        "TSG" => "tsg",
        "ONCOGENE_AND_TSG" => "oncogene_and_tsg",
        "NEITHER" => "neither",
        // INSUFFICIENT_EVIDENCE e strings vazias → unknown
        _ => "unknown",
    }
}

// ─── Struct interno de linha parseada ────────────────────────────────────────

#[derive(Debug)]
pub(crate) struct GeneRoleRow {
    gene_symbol: String,
    entrez_id: Option<i64>,
    role: &'static str,
    cgc_flag: bool,
    vogelstein_flag: bool,
    source_version: String,
}

// ─── Parse TSV streaming ──────────────────────────────────────────────────────

/// Parse streaming do cancerGeneList.txt.
///
/// Uma passada: lê linha a linha sem bufferizar o arquivo inteiro.
/// Detecta colunas por nome (não por índice fixo) — robusto a colunas extras.
pub(crate) fn parse_oncokb_tsv(path: &Path, source_version: &str) -> Result<Vec<GeneRoleRow>, String> {
    let file = std::fs::File::open(path)
        .map_err(|e| format!("Falha ao abrir {:?}: {}", path, e))?;

    let reader = BufReader::new(file);
    let mut lines = reader.lines();

    // Linha 1: cabeçalho
    let header_line = lines
        .next()
        .ok_or_else(|| format!("Arquivo TSV vazio: {:?}", path))?
        .map_err(|e| format!("Erro ao ler cabeçalho de {:?}: {}", path, e))?;

    let header_fields: Vec<String> = header_line
        .split('\t')
        .map(|s| s.trim().to_string())
        .collect();

    if header_fields.is_empty() {
        return Err(format!("Cabeçalho vazio em {:?}", path));
    }

    // Indexar colunas por nome
    let col_index: HashMap<String, usize> = header_fields
        .iter()
        .enumerate()
        .map(|(i, name)| (name.clone(), i))
        .collect();

    // Coluna obrigatória: Hugo Symbol
    let hugo_col = col_index
        .get("Hugo Symbol")
        .copied()
        .ok_or_else(|| format!("Coluna 'Hugo Symbol' não encontrada no cabeçalho de {:?}", path))?;

    // Coluna obrigatória: Entrez Gene ID
    let entrez_col = col_index.get("Entrez Gene ID").copied();

    // Coluna obrigatória: Gene Type
    let gene_type_col = col_index
        .get("Gene Type")
        .copied()
        .ok_or_else(|| format!("Coluna 'Gene Type' não encontrada no cabeçalho de {:?}", path))?;

    // Coluna CGC: procurar por nome exato ou substring "CGC"
    let cgc_col = col_index.get("COSMIC CGC (v99)").copied().or_else(|| {
        header_fields
            .iter()
            .enumerate()
            .find(|(_, name)| name.contains("CGC"))
            .map(|(i, _)| i)
    });

    // Coluna Vogelstein
    let vogelstein_col = col_index.get("Vogelstein").copied();

    let mut rows: Vec<GeneRoleRow> = Vec::new();

    for (line_num, line_result) in lines.enumerate() {
        let line = line_result
            .map_err(|e| format!("Erro ao ler linha {} de {:?}: {}", line_num + 2, path, e))?;

        let line = line.trim_end_matches('\r');
        if line.is_empty() {
            continue;
        }

        let fields: Vec<&str> = line.split('\t').collect();

        // gene_symbol: UPPERCASE
        let gene_symbol = match fields.get(hugo_col) {
            Some(s) => {
                let sym = s.trim().to_uppercase();
                if sym.is_empty() {
                    continue; // pular linhas sem símbolo
                }
                sym
            }
            None => continue,
        };

        // entrez_id: BigInt nullable
        let entrez_id = entrez_col.and_then(|col| {
            fields
                .get(col)
                .and_then(|s| s.trim().parse::<i64>().ok())
        });

        // gene_type cru
        let gene_type_raw = fields
            .get(gene_type_col)
            .map(|s| s.trim().to_string())
            .unwrap_or_default();

        let role = map_gene_type_to_role(&gene_type_raw);

        // cgc_flag
        let cgc_flag = cgc_col
            .and_then(|col| fields.get(col))
            .map(|s| s.trim().eq_ignore_ascii_case("yes"))
            .unwrap_or(false);

        // vogelstein_flag
        let vogelstein_flag = vogelstein_col
            .and_then(|col| fields.get(col))
            .map(|s| s.trim().eq_ignore_ascii_case("yes"))
            .unwrap_or(false);

        rows.push(GeneRoleRow {
            gene_symbol,
            entrez_id,
            role,
            cgc_flag,
            vogelstein_flag,
            source_version: source_version.to_string(),
        });
    }

    Ok(rows)
}

// ─── COPY UPSERT em core_generole ────────────────────────────────────────────

/// COPY UPSERT de GeneRole em `core_generole`.
///
/// ON CONFLICT (gene_symbol, source) DO UPDATE — idempotente.
pub(crate) async fn copy_gene_roles(
    client: &Client,
    rows: &[GeneRoleRow],
) -> Result<u64, tokio_postgres::Error> {
    if rows.is_empty() {
        return Ok(0);
    }

    // Staging table
    client
        .execute("DROP TABLE IF EXISTS _staging_generole", &[])
        .await?;
    client
        .execute(
            "CREATE TEMP TABLE _staging_generole (
                gene_symbol      VARCHAR(50),
                entrez_id        BIGINT,
                role             VARCHAR(30),
                cgc_flag         BOOLEAN,
                vogelstein_flag  BOOLEAN,
                source           VARCHAR(20),
                source_version   VARCHAR(100),
                loaded_at        TIMESTAMPTZ
            )",
            &[],
        )
        .await?;

    let csv = build_gene_role_csv(rows);

    // COPY FROM STDIN
    {
        let sink = client
            .copy_in(
                "COPY _staging_generole (gene_symbol, entrez_id, role, cgc_flag, \
                 vogelstein_flag, source, source_version, loaded_at) \
                 FROM STDIN WITH (FORMAT csv, NULL 'NULL')",
            )
            .await?;
        let mut sink = pin!(sink);
        sink.send(Bytes::from(csv.as_bytes().to_vec())).await?;
        sink.finish().await?;
    }

    // UPSERT com ON CONFLICT (gene_symbol, source)
    let affected = client
        .execute(
            "INSERT INTO core_generole \
                (gene_symbol, entrez_id, role, cgc_flag, vogelstein_flag, \
                 source, source_version, loaded_at)
             SELECT gene_symbol, entrez_id, role, cgc_flag, vogelstein_flag, \
                    source, source_version, loaded_at
             FROM _staging_generole
             ON CONFLICT (gene_symbol, source) DO UPDATE SET
                 entrez_id        = COALESCE(EXCLUDED.entrez_id, core_generole.entrez_id),
                 role             = EXCLUDED.role,
                 cgc_flag         = EXCLUDED.cgc_flag,
                 vogelstein_flag  = EXCLUDED.vogelstein_flag,
                 source_version   = EXCLUDED.source_version,
                 loaded_at        = EXCLUDED.loaded_at",
            &[],
        )
        .await?;

    client
        .execute("DROP TABLE IF EXISTS _staging_generole", &[])
        .await?;

    Ok(affected)
}

// ─── CSV helper ──────────────────────────────────────────────────────────────

fn build_gene_role_csv(rows: &[GeneRoleRow]) -> String {
    let now = Utc::now().format("%Y-%m-%d %H:%M:%S%.6f+00").to_string();
    let mut csv = String::new();
    for r in rows {
        csv.push_str(&format!(
            "{},{},{},{},{},{},{},{}\n",
            escape_csv_field(&r.gene_symbol),
            r.entrez_id.map_or("NULL".to_string(), |v| v.to_string()),
            escape_csv_field(r.role),
            if r.cgc_flag { "t" } else { "f" },
            if r.vogelstein_flag { "t" } else { "f" },
            escape_csv_field("oncokb"),
            escape_csv_field(&r.source_version),
            &now,
        ));
    }
    csv
}

fn escape_csv_field(s: &str) -> String {
    format!("\"{}\"", s.replace('"', "\"\""))
}

// ─── Função principal (async) ─────────────────────────────────────────────────

/// Baixa o cancerGeneList.txt do OncoKB, parseia em streaming e faz COPY UPSERT.
///
/// `source_version` é derivada da data do download (decisão: não há campo de
/// versão estruturado no arquivo; usar ISO date YYYY-MM-DD do momento do fetch).
pub async fn load_oncokb_gene_roles_async(
    url: &str,
    dest_dir: &Path,
    db_url: &str,
) -> Result<GeneRoleLoadManifest, String> {
    // 1. Validar URL (proteção SSRF)
    validate_oncokb_url(url)?;

    // 2. source_version = data do download (ISO date)
    let source_version = Utc::now().format("%Y-%m-%d").to_string();

    // 3. Cliente HTTP com User-Agent de browser (precaução — OncoKB pode verificar)
    //
    // `redirect::Policy::none()` — hardening A4 (laudo 007): checagem empírica
    // confirmou que `www.oncokb.org/api/v1/utils/cancerGeneList.txt` responde
    // 200 direto (sem 3xx), então proibir redirect não quebra a ingestão.
    // Isso impede que a allowlist de host (`validate_oncokb_url`) seja
    // contornada por um redirect para host fora da allowlist.
    let http_client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(120))
        .redirect(reqwest::redirect::Policy::none())
        .user_agent(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) \
             AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        .build()
        .map_err(|e| format!("Falha ao criar HTTP client: {}", e))?;

    // 4. Baixar TSV para disco (streaming)
    tokio::fs::create_dir_all(dest_dir)
        .await
        .map_err(|e| format!("create_dir_all {:?}: {}", dest_dir, e))?;

    let tsv_path = dest_dir.join("oncokb_cancer_gene_list.tsv");
    download_oncokb_tsv(&http_client, url, &tsv_path).await?;

    // 5. Parse streaming (uma passada)
    let rows = parse_oncokb_tsv(&tsv_path, &source_version)?;

    // 6. Contadores do manifesto
    let n_genes = rows.len();
    let n_oncogene = rows.iter().filter(|r| r.role == "oncogene").count();
    let n_tsg = rows.iter().filter(|r| r.role == "tsg").count();
    let n_dual = rows.iter().filter(|r| r.role == "oncogene_and_tsg").count();
    let n_neither = rows.iter().filter(|r| r.role == "neither").count();
    let n_unknown = rows.iter().filter(|r| r.role == "unknown").count();

    eprintln!(
        "[gene_role_loader] {} genes parseados: {} oncogene, {} tsg, {} dual, {} neither, {} unknown",
        n_genes, n_oncogene, n_tsg, n_dual, n_neither, n_unknown
    );

    // 7. Conectar ao banco e fazer COPY UPSERT
    let client = crate::db::connection::connect_db(db_url)
        .await
        .map_err(|e| format!("DB connection failed: {:?}", e))?;

    let n_upserted = copy_gene_roles(&client, &rows)
        .await
        .map_err(|e| format!("copy_gene_roles failed: {:?}", e))?;

    eprintln!(
        "[gene_role_loader] COPY UPSERT concluído: {} linhas gravadas em core_generole",
        n_upserted
    );

    Ok(GeneRoleLoadManifest {
        n_genes,
        n_oncogene,
        n_tsg,
        n_dual,
        n_neither,
        n_unknown,
        source_version,
        n_upserted,
    })
}

/// Download streaming do TSV do OncoKB com User-Agent de browser.
async fn download_oncokb_tsv(
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
            "HTTP {} ao baixar {}. Verifique User-Agent e disponibilidade do servidor.",
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

/// Entry point síncrono (wrapper para PyO3).
pub fn load_oncokb_gene_roles(
    url: &str,
    dest_dir: &str,
    db_url: &str,
) -> Result<GeneRoleLoadManifest, String> {
    let rt = tokio::runtime::Runtime::new()
        .map_err(|e| format!("Falha ao criar runtime Tokio: {}", e))?;
    let dest_path = Path::new(dest_dir);
    rt.block_on(load_oncokb_gene_roles_async(url, dest_path, db_url))
}

// ─── Testes unitários (#[cfg(test)]) — SEM rede ──────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use tempfile::TempDir;

    /// Fixture TSV mínima cobrindo todos os Gene Types + CGC/Vogelstein Yes/No.
    /// Colunas: Hugo Symbol, Entrez Gene ID, Gene Type, COSMIC CGC (v99), Vogelstein
    /// (mais uma coluna extra para testar robustez a colunas adicionais)
    const ONCOKB_FIXTURE: &str = "\
Hugo Symbol\tEntrez Gene ID\tGene Type\tCOSMIC CGC (v99)\tVogelstein\tExtra Col\n\
VHL\t7428\tTSG\tYes\tYes\tfoo\n\
PTEN\t5728\tTSG\tYes\tYes\tbar\n\
TP53\t7157\tTSG\tYes\tYes\t\n\
PIK3CA\t5290\tONCOGENE\tYes\tNo\t\n\
MTOR\t2475\tONCOGENE\tNo\tNo\t\n\
KRAS\t3845\tONCOGENE\tYes\tYes\t\n\
NOTCH1\t4851\tONCOGENE_AND_TSG\tYes\tNo\t\n\
CDKN2A\t1029\tTSG\tYes\tNo\t\n\
HRAS\t3265\tNEITHER\tNo\tNo\t\n\
UNKNOWNGENE\t9999\tINSUFFICIENT_EVIDENCE\tNo\tNo\t\n\
EMPTYGENE\t\t\tNo\tNo\t\n\
";

    fn write_fixture(dir: &std::path::Path, filename: &str, content: &str) -> std::path::PathBuf {
        let path = dir.join(filename);
        let mut f = std::fs::File::create(&path).unwrap();
        f.write_all(content.as_bytes()).unwrap();
        path
    }

    #[test]
    fn test_map_gene_type_to_role_all_variants() {
        assert_eq!(map_gene_type_to_role("ONCOGENE"), "oncogene");
        assert_eq!(map_gene_type_to_role("TSG"), "tsg");
        assert_eq!(map_gene_type_to_role("ONCOGENE_AND_TSG"), "oncogene_and_tsg");
        assert_eq!(map_gene_type_to_role("NEITHER"), "neither");
        assert_eq!(map_gene_type_to_role("INSUFFICIENT_EVIDENCE"), "unknown");
        assert_eq!(map_gene_type_to_role(""), "unknown");
        assert_eq!(map_gene_type_to_role("  "), "unknown");
        // Qualquer valor desconhecido → unknown
        assert_eq!(map_gene_type_to_role("GARBAGE"), "unknown");
    }

    #[test]
    fn test_validate_oncokb_url_accept() {
        assert!(validate_oncokb_url(
            "https://www.oncokb.org/api/v1/utils/cancerGeneList.txt"
        )
        .is_ok());
    }

    #[test]
    fn test_validate_oncokb_url_reject_http() {
        assert!(validate_oncokb_url(
            "http://www.oncokb.org/api/v1/utils/cancerGeneList.txt"
        )
        .is_err());
    }

    #[test]
    fn test_validate_oncokb_url_reject_other_host() {
        assert!(validate_oncokb_url("https://evil.com/cancerGeneList.txt").is_err());
    }

    #[test]
    fn test_validate_oncokb_url_reject_path_traversal() {
        assert!(validate_oncokb_url("https://www.oncokb.org/../etc/passwd").is_err());
    }

    #[test]
    fn test_parse_oncokb_tsv_gene_symbols_uppercase() {
        let dir = TempDir::new().unwrap();
        let path = write_fixture(dir.path(), "oncokb.tsv", ONCOKB_FIXTURE);
        let rows = parse_oncokb_tsv(&path, "2026-07-11").unwrap();

        // Todos os gene_symbol devem ser UPPERCASE
        for row in &rows {
            assert_eq!(
                row.gene_symbol,
                row.gene_symbol.to_uppercase(),
                "gene_symbol não está em UPPERCASE: {}",
                row.gene_symbol
            );
        }
    }

    #[test]
    fn test_parse_oncokb_tsv_tsg_mapping() {
        let dir = TempDir::new().unwrap();
        let path = write_fixture(dir.path(), "oncokb.tsv", ONCOKB_FIXTURE);
        let rows = parse_oncokb_tsv(&path, "2026-07-11").unwrap();

        // VHL, PTEN, TP53 → tsg
        for symbol in &["VHL", "PTEN", "TP53", "CDKN2A"] {
            let row = rows.iter().find(|r| r.gene_symbol == *symbol).unwrap();
            assert_eq!(
                row.role, "tsg",
                "{} deveria ter role=tsg, tem={}",
                symbol, row.role
            );
        }
    }

    #[test]
    fn test_parse_oncokb_tsv_oncogene_mapping() {
        let dir = TempDir::new().unwrap();
        let path = write_fixture(dir.path(), "oncokb.tsv", ONCOKB_FIXTURE);
        let rows = parse_oncokb_tsv(&path, "2026-07-11").unwrap();

        // PIK3CA, MTOR, KRAS → oncogene
        for symbol in &["PIK3CA", "MTOR", "KRAS"] {
            let row = rows.iter().find(|r| r.gene_symbol == *symbol).unwrap();
            assert_eq!(
                row.role, "oncogene",
                "{} deveria ter role=oncogene, tem={}",
                symbol, row.role
            );
        }
    }

    #[test]
    fn test_parse_oncokb_tsv_dual_mapping() {
        let dir = TempDir::new().unwrap();
        let path = write_fixture(dir.path(), "oncokb.tsv", ONCOKB_FIXTURE);
        let rows = parse_oncokb_tsv(&path, "2026-07-11").unwrap();

        let notch1 = rows.iter().find(|r| r.gene_symbol == "NOTCH1").unwrap();
        assert_eq!(notch1.role, "oncogene_and_tsg");
    }

    #[test]
    fn test_parse_oncokb_tsv_neither_mapping() {
        let dir = TempDir::new().unwrap();
        let path = write_fixture(dir.path(), "oncokb.tsv", ONCOKB_FIXTURE);
        let rows = parse_oncokb_tsv(&path, "2026-07-11").unwrap();

        let hras = rows.iter().find(|r| r.gene_symbol == "HRAS").unwrap();
        assert_eq!(hras.role, "neither");
    }

    #[test]
    fn test_parse_oncokb_tsv_unknown_mapping() {
        let dir = TempDir::new().unwrap();
        let path = write_fixture(dir.path(), "oncokb.tsv", ONCOKB_FIXTURE);
        let rows = parse_oncokb_tsv(&path, "2026-07-11").unwrap();

        // INSUFFICIENT_EVIDENCE → unknown
        let unknown = rows.iter().find(|r| r.gene_symbol == "UNKNOWNGENE").unwrap();
        assert_eq!(unknown.role, "unknown");

        // Campo Gene Type vazio → unknown
        let empty_gene = rows.iter().find(|r| r.gene_symbol == "EMPTYGENE").unwrap();
        assert_eq!(empty_gene.role, "unknown");
    }

    #[test]
    fn test_parse_oncokb_tsv_cgc_vogelstein_flags() {
        let dir = TempDir::new().unwrap();
        let path = write_fixture(dir.path(), "oncokb.tsv", ONCOKB_FIXTURE);
        let rows = parse_oncokb_tsv(&path, "2026-07-11").unwrap();

        // VHL: CGC=Yes, Vogelstein=Yes
        let vhl = rows.iter().find(|r| r.gene_symbol == "VHL").unwrap();
        assert!(vhl.cgc_flag, "VHL: cgc_flag deveria ser true");
        assert!(vhl.vogelstein_flag, "VHL: vogelstein_flag deveria ser true");

        // MTOR: CGC=No, Vogelstein=No
        let mtor = rows.iter().find(|r| r.gene_symbol == "MTOR").unwrap();
        assert!(!mtor.cgc_flag, "MTOR: cgc_flag deveria ser false");
        assert!(!mtor.vogelstein_flag, "MTOR: vogelstein_flag deveria ser false");

        // PIK3CA: CGC=Yes, Vogelstein=No
        let pik3ca = rows.iter().find(|r| r.gene_symbol == "PIK3CA").unwrap();
        assert!(pik3ca.cgc_flag, "PIK3CA: cgc_flag deveria ser true");
        assert!(!pik3ca.vogelstein_flag, "PIK3CA: vogelstein_flag deveria ser false");
    }

    #[test]
    fn test_parse_oncokb_tsv_entrez_id() {
        let dir = TempDir::new().unwrap();
        let path = write_fixture(dir.path(), "oncokb.tsv", ONCOKB_FIXTURE);
        let rows = parse_oncokb_tsv(&path, "2026-07-11").unwrap();

        let vhl = rows.iter().find(|r| r.gene_symbol == "VHL").unwrap();
        assert_eq!(vhl.entrez_id, Some(7428i64));

        // Entrez vazio → None
        let empty_gene = rows.iter().find(|r| r.gene_symbol == "EMPTYGENE").unwrap();
        assert_eq!(empty_gene.entrez_id, None);
    }

    #[test]
    fn test_parse_oncokb_tsv_row_count() {
        let dir = TempDir::new().unwrap();
        let path = write_fixture(dir.path(), "oncokb.tsv", ONCOKB_FIXTURE);
        let rows = parse_oncokb_tsv(&path, "2026-07-11").unwrap();

        // 11 genes no fixture
        assert_eq!(rows.len(), 11, "Expected 11 genes no fixture");
    }

    #[test]
    fn test_parse_oncokb_tsv_source_version() {
        let dir = TempDir::new().unwrap();
        let path = write_fixture(dir.path(), "oncokb.tsv", ONCOKB_FIXTURE);
        let rows = parse_oncokb_tsv(&path, "2026-07-11").unwrap();

        for row in &rows {
            assert_eq!(row.source_version, "2026-07-11");
        }
    }

    #[test]
    fn test_parse_oncokb_tsv_extra_column_ignored() {
        // Verifica que a coluna extra "Extra Col" não causa erro
        let dir = TempDir::new().unwrap();
        let path = write_fixture(dir.path(), "oncokb.tsv", ONCOKB_FIXTURE);
        let rows = parse_oncokb_tsv(&path, "2026-07-11").unwrap();
        // Se chegou aqui sem panic, extra col foi ignorada corretamente
        assert!(!rows.is_empty());
    }

    /// Teste com fixture usando coluna CGC por substring (nome diferente de "COSMIC CGC (v99)")
    #[test]
    fn test_parse_oncokb_tsv_cgc_column_by_substring() {
        const FIXTURE_ALT_CGC: &str = "\
Hugo Symbol\tEntrez Gene ID\tGene Type\tCOSMIC CGC (v100)\tVogelstein\n\
BRAF\t673\tONCOGENE\tYes\tNo\n\
";
        let dir = TempDir::new().unwrap();
        let path = write_fixture(dir.path(), "oncokb_alt.tsv", FIXTURE_ALT_CGC);
        let rows = parse_oncokb_tsv(&path, "2026-07-11").unwrap();

        let braf = rows.iter().find(|r| r.gene_symbol == "BRAF").unwrap();
        assert_eq!(braf.role, "oncogene");
        // Coluna "COSMIC CGC (v100)" é detectada por substring "CGC"
        assert!(braf.cgc_flag, "BRAF: cgc_flag deveria ser true com coluna CGC alternativa");
    }
}
