/// Loader de regulons CollecTRI via OmniPath → arquivo intermediário local.
///
/// # Fluxo
///
/// 1. Fetch `https://omnipathdb.org/interactions?datasets=collectri&genesymbols=1`
///    (TSV, streaming, teto de bytes M2).
/// 2. Parse TSV em uma passada; filtro na origem: manter apenas linhas onde
///    `source_genesymbol` ∈ `tf_allowlist`.
/// 3. Derivar `mode_of_regulation`: +1 (is_stimulation=1), -1 (is_inhibition=1),
///    0 (conflito ou nenhum).
/// 4. Gravar arquivo JSONL em `dest_dir/collectri_regulons.jsonl` (um JSON por linha).
///    **NÃO** insere em tabela PG (Decisão 3.3: sem consumidor além do mapeamento 3.4).
///
/// # Formato do arquivo de saída (JSONL)
///
/// Uma linha por regulon (par TF→alvo):
/// ```json
/// {"tf":"TP53","target":"MDM2","mode":1,"sources":"CollecTRI","n_references":5}
/// ```
///
/// | Campo | Tipo | Descrição |
/// |---|---|---|
/// | `tf` | String | Símbolo UPPERCASE do TF (`source_genesymbol`). |
/// | `target` | String | Símbolo UPPERCASE do alvo (`target_genesymbol`). |
/// | `mode` | i8 | +1 (estimulação), -1 (inibição), 0 (conflito/neutro). |
/// | `sources` | String | Coluna `sources` cru (ex.: "CollecTRI"). |
/// | `n_references` | usize | Número de referências da coluna `references` (delimitada por `;`). |
///
/// # Manifesto retornado
///
/// `Regulon LoadManifest { n_tfs, n_targets, n_edges, n_positive, n_negative, n_neutral, source_version, regulon_path }`
///
/// # Segurança (M2/M3)
///
/// - `validate_omnipath_url`: prefixo `https://omnipathdb.org/`, rejeita `..`.
/// - Teto de bytes: 200 MB (CollecTRI completo é ~50 MB; cap generoso).
/// - `tf_allowlist`: só TFs das 3 vias — filtra na origem para não puxar reguloma inteiro.
/// - Sem token/credencial (OmniPath é público).
/// - Fixtures de teste **SINTÉTICAS** — não commitar CollecTRI bruto.

use std::collections::HashSet;
use std::io::{BufRead, BufReader, Write};
use std::path::Path;

use futures::StreamExt;

// ─── Teto de bytes ────────────────────────────────────────────────────────────
const OMNIPATH_MAX_BYTES: usize = 200 * 1024 * 1024; // 200 MB

// ─── Manifesto público ────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct RegulonLoadManifest {
    /// Número de TFs únicos no arquivo de saída.
    pub n_tfs: usize,
    /// Número de alvos únicos (target_genesymbol únicos).
    pub n_targets: usize,
    /// Número total de linhas (pares TF→alvo) gravados.
    pub n_edges: usize,
    /// Pares com mode_of_regulation = +1.
    pub n_positive: usize,
    /// Pares com mode_of_regulation = -1.
    pub n_negative: usize,
    /// Pares com mode_of_regulation = 0 (conflito ou neutro).
    pub n_neutral: usize,
    /// Versão da fonte: data do download.
    pub source_version: String,
    /// Caminho absoluto do arquivo JSONL de regulons gravado.
    pub regulon_path: String,
}

// ─── Validação de URL ─────────────────────────────────────────────────────────

/// Valida que a URL pertence ao host `omnipathdb.org` via HTTPS.
///
/// Rejeita esquema ≠ `https`, host diferente, ou path traversal `..`.
/// Espelha o padrão M2/M3 (`validate_kegg_url` / `validate_linkedomics_url`).
pub fn validate_omnipath_url(url: &str) -> Result<(), String> {
    if !url.starts_with("https://omnipathdb.org/") {
        return Err(format!(
            "URL rejeitada por política de host: '{}'. \
             Apenas https://omnipathdb.org/ é permitido para o loader OmniPath.",
            url
        ));
    }
    let path_part = &url["https://omnipathdb.org/".len()..];
    if path_part.contains("..") {
        return Err(format!(
            "URL rejeitada: contém path traversal '..' em '{}'",
            url
        ));
    }
    Ok(())
}

// ─── Struct interno de linha parseada ────────────────────────────────────────

#[derive(Debug)]
pub struct ReguloRow {
    tf: String,
    target: String,
    mode: i8,
    sources: String,
    n_references: usize,
}

// ─── Derivar mode_of_regulation ──────────────────────────────────────────────

/// Parseia um valor booleano vindo do TSV OmniPath.
///
/// O OmniPath codifica booleanos de formas diferentes conforme o endpoint/versão:
/// `"1"`/`"0"` (estilo numérico) ou `"True"`/`"False"` (estilo Python, capitalizado).
/// Aceita ambas as codificações, case-insensitive, para não recair no bug de
/// silenciosamente zerar todo o sinal quando a fonte muda de formato.
///
/// Qualquer valor não reconhecido (incluindo vazio) é tratado como falso.
fn parse_omnipath_bool(value: &str) -> bool {
    matches!(value.trim().to_ascii_lowercase().as_str(), "1" | "true")
}

/// Deriva `mode_of_regulation` das colunas `is_stimulation` e `is_inhibition`.
///
/// | is_stimulation | is_inhibition | mode |
/// |---|---|---|
/// | 1 / True | 0 / False | +1 |
/// | 0 / False | 1 / True | -1 |
/// | 1 / True | 1 / True | 0 (conflito) |
/// | 0 / False | 0 / False | 0 (neutro/desconhecido) |
pub fn derive_mode(is_stimulation: &str, is_inhibition: &str) -> i8 {
    let stim = parse_omnipath_bool(is_stimulation);
    let inhib = parse_omnipath_bool(is_inhibition);
    match (stim, inhib) {
        (true, false) => 1,
        (false, true) => -1,
        _ => 0, // conflito ou neutro
    }
}

// ─── Parse TSV em uma passada ────────────────────────────────────────────────

/// Parse do TSV CollecTRI (streaming por linha).
///
/// Colunas esperadas (posição por nome, robusto a colunas extras):
/// - `source_genesymbol` → tf (UPPERCASE)
/// - `target_genesymbol` → target (UPPERCASE)
/// - `is_stimulation` → contribui para mode
/// - `is_inhibition` → contribui para mode
/// - `sources` → cru
/// - `references` → contagem delimitada por `;` (real do OmniPath; `sources`
///   também usa `;`, mas essa coluna é gravada crua, sem contagem)
///
/// Filtra na origem: só linhas com `source_genesymbol` ∈ `tf_allowlist`.
pub fn parse_collectri_tsv(
    content: &[u8],
    tf_allowlist: &HashSet<String>,
) -> Result<Vec<ReguloRow>, String> {
    let reader = BufReader::new(content);
    let mut lines = reader.lines();

    // Linha 1: cabeçalho
    let header_line = lines
        .next()
        .ok_or("CollecTRI TSV vazio")?
        .map_err(|e| format!("Erro ao ler cabeçalho CollecTRI: {}", e))?;

    let header_fields: Vec<String> = header_line
        .split('\t')
        .map(|s| s.trim().to_string())
        .collect();

    // Indexar colunas por nome
    let col: std::collections::HashMap<String, usize> = header_fields
        .iter()
        .enumerate()
        .map(|(i, name)| (name.clone(), i))
        .collect();

    // Colunas obrigatórias
    let src_sym_col = col.get("source_genesymbol").copied().ok_or(
        "Coluna 'source_genesymbol' não encontrada no TSV CollecTRI. \
         Verifique o endpoint e os parâmetros (genesymbols=1).",
    )?;
    let tgt_sym_col = col.get("target_genesymbol").copied().ok_or(
        "Coluna 'target_genesymbol' não encontrada no TSV CollecTRI.",
    )?;
    let stim_col = col.get("is_stimulation").copied().ok_or(
        "Coluna 'is_stimulation' não encontrada no TSV CollecTRI.",
    )?;
    let inhib_col = col.get("is_inhibition").copied().ok_or(
        "Coluna 'is_inhibition' não encontrada no TSV CollecTRI.",
    )?;

    // Colunas opcionais
    let sources_col = col.get("sources").copied();
    let refs_col = col.get("references").copied();

    let mut rows: Vec<ReguloRow> = Vec::new();

    for (line_num, line_result) in lines.enumerate() {
        let line = line_result
            .map_err(|e| format!("Erro ao ler linha {} do CollecTRI TSV: {}", line_num + 2, e))?;

        let line = line.trim_end_matches('\r');
        if line.is_empty() {
            continue;
        }

        let fields: Vec<&str> = line.split('\t').collect();

        // TF symbol
        let tf = match fields.get(src_sym_col) {
            Some(s) => {
                let sym = s.trim().to_uppercase();
                if sym.is_empty() {
                    continue;
                }
                sym
            }
            None => continue,
        };

        // Filtro na origem: só TFs da allowlist
        if !tf_allowlist.is_empty() && !tf_allowlist.contains(&tf) {
            continue;
        }

        // Target symbol
        let target = match fields.get(tgt_sym_col) {
            Some(s) => {
                let sym = s.trim().to_uppercase();
                if sym.is_empty() {
                    continue;
                }
                sym
            }
            None => continue,
        };

        // Mode
        let is_stim = fields.get(stim_col).map(|s| s.trim()).unwrap_or("0");
        let is_inhib = fields.get(inhib_col).map(|s| s.trim()).unwrap_or("0");
        let mode = derive_mode(is_stim, is_inhib);

        // Sources (opcional)
        let sources = sources_col
            .and_then(|c| fields.get(c))
            .map(|s| s.trim().to_string())
            .unwrap_or_default();

        // Referências: contam-se por ';' (delimitador real do OmniPath, ex.:
        // "CollecTRI2:11562751;CollecTRI:10914736;..."). Entradas vazias
        // (string vazia, ou geradas por ';' duplicado/trailing) são ignoradas.
        let n_references = refs_col
            .and_then(|c| fields.get(c))
            .map(|s| {
                let s = s.trim();
                if s.is_empty() {
                    0
                } else {
                    s.split(';').filter(|part| !part.trim().is_empty()).count()
                }
            })
            .unwrap_or(0);

        rows.push(ReguloRow {
            tf,
            target,
            mode,
            sources,
            n_references,
        });
    }

    Ok(rows)
}

// ─── Gravar JSONL ────────────────────────────────────────────────────────────

/// Grava os regulons em formato JSONL (um objeto JSON por linha).
///
/// Formato de cada linha:
/// `{"tf":"TP53","target":"MDM2","mode":1,"sources":"CollecTRI","n_references":5}`
fn write_regulon_jsonl(path: &Path, rows: &[ReguloRow]) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("create_dir_all {:?}: {}", parent, e))?;
    }

    let mut file = std::fs::File::create(path)
        .map_err(|e| format!("Falha ao criar {:?}: {}", path, e))?;

    for row in rows {
        // Escapar strings simples (sem chars especiais esperados em símbolos de gene)
        let line = format!(
            "{{\"tf\":\"{}\",\"target\":\"{}\",\"mode\":{},\"sources\":\"{}\",\"n_references\":{}}}\n",
            row.tf.replace('"', "\\\""),
            row.target.replace('"', "\\\""),
            row.mode,
            row.sources.replace('"', "\\\"").replace('\n', "").replace('\r', ""),
            row.n_references,
        );
        file.write_all(line.as_bytes())
            .map_err(|e| format!("Erro ao escrever JSONL em {:?}: {}", path, e))?;
    }

    file.flush()
        .map_err(|e| format!("Erro ao fechar {:?}: {}", path, e))?;

    Ok(())
}

// ─── Download streaming ───────────────────────────────────────────────────────

/// Baixa o endpoint OmniPath em streaming com teto de bytes.
async fn download_omnipath(
    client: &reqwest::Client,
    url: &str,
) -> Result<Vec<u8>, String> {
    let response = client
        .get(url)
        .send()
        .await
        .map_err(|e| format!("HTTP GET falhou para {}: {}", url, e))?;

    let status = response.status();
    if !status.is_success() {
        return Err(format!(
            "HTTP {} ao baixar CollecTRI de {}. \
             Verifique se o serviço está disponível.",
            status, url
        ));
    }

    let mut stream = response.bytes_stream();
    let mut content: Vec<u8> = Vec::new();
    let mut total_bytes = 0usize;

    while let Some(chunk_result) = stream.next().await {
        let chunk = chunk_result
            .map_err(|e| format!("Erro ao ler chunk de {}: {}", url, e))?;
        total_bytes += chunk.len();
        if total_bytes > OMNIPATH_MAX_BYTES {
            return Err(format!(
                "Resposta OmniPath excede teto de {} MB",
                OMNIPATH_MAX_BYTES / 1024 / 1024
            ));
        }
        content.extend_from_slice(&chunk);
    }

    Ok(content)
}

// ─── Função principal assíncrona ─────────────────────────────────────────────

/// Carrega os regulons CollecTRI filtrados pelos TFs das 3 vias.
pub async fn load_collectri_regulons_async(
    tf_allowlist: Vec<String>,
    dest_dir: &Path,
) -> Result<RegulonLoadManifest, String> {
    let source_version = chrono::Utc::now().format("%Y-%m-%d").to_string();

    // Normalizar allowlist para UPPERCASE
    let tf_set: HashSet<String> = tf_allowlist
        .into_iter()
        .map(|s| s.trim().to_uppercase())
        .filter(|s| !s.is_empty())
        .collect();

    eprintln!(
        "[regulon_loader] tf_allowlist: {} TFs",
        tf_set.len()
    );

    // URL do CollecTRI via OmniPath
    let url = "https://omnipathdb.org/interactions?datasets=collectri&genesymbols=1&fields=sources,references";
    validate_omnipath_url(url)?;

    // HTTP client
    let http_client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(300))
        .user_agent(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) \
             AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        .build()
        .map_err(|e| format!("Falha ao criar HTTP client: {}", e))?;

    eprintln!("[regulon_loader] baixando CollecTRI de {}", url);
    let content = download_omnipath(&http_client, url).await?;
    eprintln!(
        "[regulon_loader] download concluído: {} bytes",
        content.len()
    );

    // Parse TSV com filtro de TF
    let rows = parse_collectri_tsv(&content, &tf_set)?;
    eprintln!("[regulon_loader] regulons filtrados: {} pares", rows.len());

    // Estatísticas
    let n_positive = rows.iter().filter(|r| r.mode == 1).count();
    let n_negative = rows.iter().filter(|r| r.mode == -1).count();
    let n_neutral = rows.iter().filter(|r| r.mode == 0).count();

    let n_tfs: HashSet<&str> = rows.iter().map(|r| r.tf.as_str()).collect();
    let n_targets: HashSet<&str> = rows.iter().map(|r| r.target.as_str()).collect();

    let n_tfs = n_tfs.len();
    let n_targets = n_targets.len();
    let n_edges = rows.len();

    // Gravar JSONL
    let jsonl_path = dest_dir.join("collectri_regulons.jsonl");
    eprintln!(
        "[regulon_loader] gravando {} regulons em {:?}",
        n_edges, jsonl_path
    );
    write_regulon_jsonl(&jsonl_path, &rows)?;

    eprintln!(
        "[regulon_loader] concluído: {} TFs, {} alvos, {} pares (+{}/-{}/0:{})",
        n_tfs, n_targets, n_edges, n_positive, n_negative, n_neutral
    );

    Ok(RegulonLoadManifest {
        n_tfs,
        n_targets,
        n_edges,
        n_positive,
        n_negative,
        n_neutral,
        source_version,
        regulon_path: jsonl_path.to_string_lossy().into_owned(),
    })
}

/// Entry point síncrono para PyO3.
pub fn load_collectri_regulons(
    tf_allowlist: Vec<String>,
    dest_dir: &str,
) -> Result<RegulonLoadManifest, String> {
    let rt = tokio::runtime::Runtime::new()
        .map_err(|e| format!("Falha ao criar runtime Tokio: {}", e))?;
    rt.block_on(load_collectri_regulons_async(
        tf_allowlist,
        Path::new(dest_dir),
    ))
}

// ─── Testes unitários ─────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    // ─── Fixture TSV sintética (NÃO é CollecTRI bruto — licença GPL/acadêmica) ─
    // Representa a estrutura real do endpoint OmniPath CollecTRI.
    // Codificação numérica ("1"/"0") — mantida por retrocompatibilidade.

    const COLLECTRI_FIXTURE: &str = "\
source\ttarget\tsource_genesymbol\ttarget_genesymbol\tis_directed\tis_stimulation\tis_inhibition\tconsensus_stimulation\tconsensus_inhibition\tsources\treferences\n\
6667\t4193\tTP53\tMDM2\t1\t1\t0\t1\t0\tCollecTRI\tPMID:10000;PMID:10001;PMID:10002\n\
6667\t595\tTP53\tCCND1\t1\t0\t1\t0\t1\tCollecTRI\tPMID:20000\n\
6667\t1026\tTP53\tCDKN1A\t1\t1\t0\t1\t0\tCollecTRI\tPMID:30000;PMID:30001\n\
4609\t6667\tMYC\tTP53\t1\t0\t1\t0\t1\tCollecTRI\tPMID:40000\n\
4609\t7157\tMYC\tTP53\t1\t1\t1\t0\t0\tCollecTRI\tPMID:50000\n\
1387\t595\tCREBBP\tCCND1\t1\t1\t0\t1\t0\tCollecTRI\tPMID:60000\n\
9999\t1234\tUNKNOWN_TF\tSOME_GENE\t1\t1\t0\t1\t0\tCollecTRI\tPMID:70000\n\
";

    // ─── Fixture TSV com a codificação REAL do OmniPath ("True"/"False") ──────
    // Estrutura idêntica à fixture acima, mas com booleanos estilo Python.
    // Regressão do bug: derive_mode comparava só contra "1"/"0" e zerava
    // 100% do sinal quando a fonte retornava "True"/"False".

    const COLLECTRI_FIXTURE_TRUE_FALSE: &str = "\
source\ttarget\tsource_genesymbol\ttarget_genesymbol\tis_directed\tis_stimulation\tis_inhibition\tconsensus_stimulation\tconsensus_inhibition\tsources\treferences\n\
6667\t4193\tTP53\tMDM2\tTrue\tTrue\tFalse\tTrue\tFalse\tCollecTRI\tPMID:10000;PMID:10001;PMID:10002\n\
6667\t595\tTP53\tCCND1\tTrue\tFalse\tTrue\tFalse\tTrue\tCollecTRI\tPMID:20000\n\
4609\t7157\tMYC\tTP53\tTrue\tTrue\tTrue\tFalse\tFalse\tCollecTRI\tPMID:50000\n\
1387\t595\tCREBBP\tCCND1\tTrue\tFalse\tFalse\tFalse\tFalse\tCollecTRI\tPMID:60000\n\
";

    // ─── Linha real do OmniPath (dado público, 1 exemplo — não é recorte do
    // dataset licenciado, apenas uma linha de amostra confirmada via curl) ───

    const OMNIPATH_REAL_LINE_SMAD3_JUN: &str = "\
source\ttarget\tsource_genesymbol\ttarget_genesymbol\tis_directed\tis_stimulation\tis_inhibition\tconsensus_direction\tconsensus_stimulation\tconsensus_inhibition\tsources\treferences\n\
P84022\tP05412\tSMAD3\tJUN\tTrue\tTrue\tFalse\tTrue\tTrue\tFalse\tCollecTRI;TFactS_DoRothEA\tPMID:99999\n\
";

    // ─── Linha real do OmniPath: MYC→TERT (recorte de referências, formato
    // real "recurso:pmid" delimitado por ';'; confirmado via curl live) ──────
    // A resposta live completa tem ~180 referências; aqui usamos um recorte
    // de 6 para travar a contagem correta com o delimitador real.

    const OMNIPATH_REAL_LINE_MYC_TERT: &str = "\
source\ttarget\tsource_genesymbol\ttarget_genesymbol\tis_directed\tis_stimulation\tis_inhibition\tconsensus_direction\tconsensus_stimulation\tconsensus_inhibition\tsources\treferences\n\
P01106\tO14746\tMYC\tTERT\tTrue\tTrue\tFalse\tTrue\tTrue\tFalse\tCollecTRI;TRRUST_CollecTRI;TRRUST_DoRothEA;Pavlidis_CollecTRI2\tCollecTRI2:11562751;CollecTRI2:22617349;CollecTRI:10914736;TRRUST:22207128;TRRUST:9450935;Pavlidis:15489916\n\
";

    // ─── Testes de derive_mode ────────────────────────────────────────────────

    #[test]
    fn test_derive_mode_stimulation() {
        assert_eq!(derive_mode("1", "0"), 1);
    }

    #[test]
    fn test_derive_mode_inhibition() {
        assert_eq!(derive_mode("0", "1"), -1);
    }

    #[test]
    fn test_derive_mode_conflict() {
        assert_eq!(derive_mode("1", "1"), 0);
    }

    #[test]
    fn test_derive_mode_neutral() {
        assert_eq!(derive_mode("0", "0"), 0);
    }

    #[test]
    fn test_derive_mode_whitespace() {
        assert_eq!(derive_mode(" 1 ", " 0 "), 1);
        assert_eq!(derive_mode(" 0 ", " 1 "), -1);
    }

    // ─── Regressão: codificação real "True"/"False" do OmniPath ─────────────

    #[test]
    fn test_derive_mode_true_false_stimulation() {
        assert_eq!(derive_mode("True", "False"), 1);
    }

    #[test]
    fn test_derive_mode_true_false_inhibition() {
        assert_eq!(derive_mode("False", "True"), -1);
    }

    #[test]
    fn test_derive_mode_true_false_conflict() {
        assert_eq!(derive_mode("True", "True"), 0);
    }

    #[test]
    fn test_derive_mode_true_false_neutral() {
        assert_eq!(derive_mode("False", "False"), 0);
    }

    #[test]
    fn test_derive_mode_true_false_case_insensitive() {
        // Variações de capitalização (TRUE, true, tRuE) devem ser aceitas.
        assert_eq!(derive_mode("TRUE", "FALSE"), 1);
        assert_eq!(derive_mode("true", "false"), 1);
        assert_eq!(derive_mode("FALSE", "TRUE"), -1);
        assert_eq!(derive_mode("false", "true"), -1);
    }

    #[test]
    fn test_derive_mode_true_false_whitespace() {
        assert_eq!(derive_mode(" True ", " False "), 1);
        assert_eq!(derive_mode(" False ", " True "), -1);
    }

    #[test]
    fn test_derive_mode_empty_is_false() {
        // Valor vazio/ausente não deve ser tratado como verdadeiro.
        assert_eq!(derive_mode("", ""), 0);
        assert_eq!(derive_mode("", "True"), -1);
        assert_eq!(derive_mode("True", ""), 1);
    }

    // ─── Testes de parse_collectri_tsv ───────────────────────────────────────

    #[test]
    fn test_parse_collectri_all_tfs_no_filter() {
        // Allowlist vazia = sem filtro (aceitar tudo)
        let allowlist: HashSet<String> = HashSet::new();
        let rows = parse_collectri_tsv(COLLECTRI_FIXTURE.as_bytes(), &allowlist).unwrap();
        // 7 linhas de dados
        assert_eq!(rows.len(), 7);
    }

    #[test]
    fn test_parse_collectri_filter_tp53_only() {
        let mut allowlist = HashSet::new();
        allowlist.insert("TP53".to_string());

        let rows = parse_collectri_tsv(COLLECTRI_FIXTURE.as_bytes(), &allowlist).unwrap();
        // Apenas linhas com source_genesymbol = TP53 (3 linhas)
        assert_eq!(rows.len(), 3);
        assert!(rows.iter().all(|r| r.tf == "TP53"));
    }

    #[test]
    fn test_parse_collectri_filter_reduces_volume() {
        let mut allowlist = HashSet::new();
        allowlist.insert("TP53".to_string());
        allowlist.insert("MYC".to_string());

        let rows_filtered =
            parse_collectri_tsv(COLLECTRI_FIXTURE.as_bytes(), &allowlist).unwrap();

        let allowlist_all: HashSet<String> = HashSet::new();
        let rows_all =
            parse_collectri_tsv(COLLECTRI_FIXTURE.as_bytes(), &allowlist_all).unwrap();

        // Filtrado deve ter menos linhas
        assert!(rows_filtered.len() < rows_all.len());
        // UNKNOWN_TF e CREBBP devem estar ausentes
        assert!(!rows_filtered.iter().any(|r| r.tf == "UNKNOWN_TF"));
        assert!(!rows_filtered.iter().any(|r| r.tf == "CREBBP"));
    }

    #[test]
    fn test_parse_collectri_mode_positive() {
        let mut allowlist = HashSet::new();
        allowlist.insert("TP53".to_string());

        let rows = parse_collectri_tsv(COLLECTRI_FIXTURE.as_bytes(), &allowlist).unwrap();

        // TP53→MDM2: is_stimulation=1, is_inhibition=0 → mode=+1
        let tp53_mdm2 = rows.iter().find(|r| r.target == "MDM2").unwrap();
        assert_eq!(tp53_mdm2.mode, 1);
    }

    #[test]
    fn test_parse_collectri_mode_negative() {
        let mut allowlist = HashSet::new();
        allowlist.insert("TP53".to_string());

        let rows = parse_collectri_tsv(COLLECTRI_FIXTURE.as_bytes(), &allowlist).unwrap();

        // TP53→CCND1: is_stimulation=0, is_inhibition=1 → mode=-1
        let tp53_ccnd1 = rows.iter().find(|r| r.target == "CCND1").unwrap();
        assert_eq!(tp53_ccnd1.mode, -1);
    }

    #[test]
    fn test_parse_collectri_mode_conflict_zero() {
        let mut allowlist = HashSet::new();
        allowlist.insert("MYC".to_string());

        let rows = parse_collectri_tsv(COLLECTRI_FIXTURE.as_bytes(), &allowlist).unwrap();

        // MYC→TP53 (segundo par): is_stimulation=1, is_inhibition=1 → mode=0 (conflito)
        // Note: a fixture tem dois MYC→TP53 entries (linhas 4 e 5)
        // Linha 5: source_genesymbol=MYC, target_genesymbol=TP53, is_stimulation=1, is_inhibition=1
        let conflict_row = rows
            .iter()
            .find(|r| r.tf == "MYC" && r.target == "TP53" && r.mode == 0);
        assert!(
            conflict_row.is_some(),
            "Esperava par MYC→TP53 com mode=0 (conflito)"
        );
    }

    #[test]
    fn test_parse_collectri_n_references() {
        let allowlist: HashSet<String> = HashSet::new();
        let rows = parse_collectri_tsv(COLLECTRI_FIXTURE.as_bytes(), &allowlist).unwrap();

        // TP53→MDM2: "PMID:10000;PMID:10001;PMID:10002" → 3 referências
        let tp53_mdm2 = rows.iter().find(|r| r.tf == "TP53" && r.target == "MDM2").unwrap();
        assert_eq!(tp53_mdm2.n_references, 3);

        // TP53→CCND1: "PMID:20000" → 1 referência
        let tp53_ccnd1 = rows.iter().find(|r| r.tf == "TP53" && r.target == "CCND1").unwrap();
        assert_eq!(tp53_ccnd1.n_references, 1);
    }

    // ─── Regressão: contagem de referências delimitadas por ';' (não '|') ────

    #[test]
    fn test_parse_collectri_n_references_semicolon_real_format() {
        // Confirma a contagem correta com o delimitador real do OmniPath (';')
        // e o formato real de entrada ("recurso:pmid"), usando o recorte
        // MYC→TERT observado na ingestão live (6 referências no recorte).
        let allowlist: HashSet<String> = HashSet::new();
        let rows = parse_collectri_tsv(OMNIPATH_REAL_LINE_MYC_TERT.as_bytes(), &allowlist).unwrap();

        assert_eq!(rows.len(), 1);
        let myc_tert = &rows[0];
        assert_eq!(myc_tert.tf, "MYC");
        assert_eq!(myc_tert.target, "TERT");
        assert_eq!(myc_tert.n_references, 6);
        // Sinal: is_stimulation=True, is_inhibition=False → mode=+1
        assert_eq!(myc_tert.mode, 1);
    }

    #[test]
    fn test_parse_collectri_n_references_ignores_empty_segments() {
        // ';' duplicado ou trailing não deve inflar a contagem com entradas vazias.
        let tsv = "\
source\ttarget\tsource_genesymbol\ttarget_genesymbol\tis_directed\tis_stimulation\tis_inhibition\tsources\treferences\n\
6667\t4193\tTP53\tMDM2\t1\t1\t0\tCollecTRI\tPMID:1;;PMID:2;\n\
";
        let allowlist: HashSet<String> = HashSet::new();
        let rows = parse_collectri_tsv(tsv.as_bytes(), &allowlist).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].n_references, 2);
    }

    #[test]
    fn test_parse_collectri_uppercase_normalization() {
        // Criar fixture com símbolos lowercase
        let tsv = "\
source\ttarget\tsource_genesymbol\ttarget_genesymbol\tis_directed\tis_stimulation\tis_inhibition\tsources\treferences\n\
6667\t4193\ttp53\tmdm2\t1\t1\t0\tCollecTRI\tPMID:10000\n\
";
        let mut allowlist = HashSet::new();
        allowlist.insert("TP53".to_string()); // allowlist em UPPERCASE

        let rows = parse_collectri_tsv(tsv.as_bytes(), &allowlist).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].tf, "TP53"); // normalizado
        assert_eq!(rows[0].target, "MDM2"); // normalizado
    }

    // ─── Testes ponta-a-ponta com a codificação real "True"/"False" ──────────
    // Regressão do bug: 100% dos regulons saíam com mode=0 (n_neutral=5915)
    // porque derive_mode só reconhecia "1"/"0".

    #[test]
    fn test_parse_collectri_true_false_encoding_end_to_end() {
        let allowlist: HashSet<String> = HashSet::new();
        let rows = parse_collectri_tsv(COLLECTRI_FIXTURE_TRUE_FALSE.as_bytes(), &allowlist).unwrap();

        assert_eq!(rows.len(), 4);

        let tp53_mdm2 = rows.iter().find(|r| r.tf == "TP53" && r.target == "MDM2").unwrap();
        assert_eq!(tp53_mdm2.mode, 1, "is_stimulation=True/is_inhibition=False deve dar mode=+1");

        let tp53_ccnd1 = rows.iter().find(|r| r.tf == "TP53" && r.target == "CCND1").unwrap();
        assert_eq!(tp53_ccnd1.mode, -1, "is_stimulation=False/is_inhibition=True deve dar mode=-1");

        let myc_tp53 = rows.iter().find(|r| r.tf == "MYC" && r.target == "TP53").unwrap();
        assert_eq!(myc_tp53.mode, 0, "is_stimulation=True/is_inhibition=True (conflito) deve dar mode=0");

        let crebbp_ccnd1 = rows.iter().find(|r| r.tf == "CREBBP" && r.target == "CCND1").unwrap();
        assert_eq!(crebbp_ccnd1.mode, 0, "is_stimulation=False/is_inhibition=False (neutro) deve dar mode=0");

        // Nenhuma linha deve virar mode=0 apenas por má-interpretação da codificação:
        // das 4 linhas, 2 têm sinal (TP53→MDM2 e TP53→CCND1).
        let n_signed = rows.iter().filter(|r| r.mode != 0).count();
        assert_eq!(n_signed, 2);
    }

    #[test]
    fn test_parse_collectri_real_smad3_jun_line() {
        // Linha real do OmniPath (SMAD3→JUN), confirmada via curl live.
        let allowlist: HashSet<String> = HashSet::new();
        let rows = parse_collectri_tsv(OMNIPATH_REAL_LINE_SMAD3_JUN.as_bytes(), &allowlist).unwrap();

        assert_eq!(rows.len(), 1);
        let row = &rows[0];
        assert_eq!(row.tf, "SMAD3");
        assert_eq!(row.target, "JUN");
        // is_stimulation=True, is_inhibition=False → mode=+1
        assert_eq!(row.mode, 1);
        assert_eq!(row.sources, "CollecTRI;TFactS_DoRothEA");
        assert_eq!(row.n_references, 1);
    }

    // ─── Testes de JSONL ─────────────────────────────────────────────────────

    #[test]
    fn test_write_regulon_jsonl_content() {
        use tempfile::TempDir;

        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test_regulons.jsonl");

        let rows = vec![
            ReguloRow {
                tf: "TP53".to_string(),
                target: "MDM2".to_string(),
                mode: 1,
                sources: "CollecTRI".to_string(),
                n_references: 3,
            },
            ReguloRow {
                tf: "TP53".to_string(),
                target: "CCND1".to_string(),
                mode: -1,
                sources: "CollecTRI".to_string(),
                n_references: 1,
            },
        ];

        write_regulon_jsonl(&path, &rows).unwrap();

        let content = std::fs::read_to_string(&path).unwrap();
        let lines: Vec<&str> = content.lines().collect();

        assert_eq!(lines.len(), 2);
        assert!(lines[0].contains("\"tf\":\"TP53\""));
        assert!(lines[0].contains("\"target\":\"MDM2\""));
        assert!(lines[0].contains("\"mode\":1"));
        assert!(lines[0].contains("\"n_references\":3"));
        assert!(lines[1].contains("\"mode\":-1"));
    }

    // ─── Testes de validação de URL ──────────────────────────────────────────

    #[test]
    fn test_validate_omnipath_url_accept() {
        assert!(validate_omnipath_url(
            "https://omnipathdb.org/interactions?datasets=collectri&genesymbols=1"
        )
        .is_ok());
        assert!(validate_omnipath_url("https://omnipathdb.org/interactions").is_ok());
    }

    #[test]
    fn test_validate_omnipath_url_reject_http() {
        assert!(
            validate_omnipath_url("http://omnipathdb.org/interactions").is_err()
        );
    }

    #[test]
    fn test_validate_omnipath_url_reject_wrong_host() {
        assert!(validate_omnipath_url("https://evil.com/interactions").is_err());
        assert!(
            validate_omnipath_url("https://www.omnipathdb.org/interactions").is_err()
        );
        assert!(validate_omnipath_url("https://rest.kegg.jp/get/hsa04151/kgml").is_err());
    }

    #[test]
    fn test_validate_omnipath_url_reject_path_traversal() {
        assert!(
            validate_omnipath_url("https://omnipathdb.org/../etc/passwd").is_err()
        );
    }

    #[test]
    fn test_validate_omnipath_url_reject_linkedomics() {
        // host diferente não deve ser aceito
        assert!(validate_omnipath_url(
            "https://www.linkedomics.org/data_download/test.cct"
        )
        .is_err());
    }
}
