use reqwest::Client;
use serde::Deserialize;
use serde_json::{json, Value as JsonValue};
use std::time::Duration;
use tokio::time::sleep;

use crate::omics::models::{DatasetPaperLinkData, OmicDatasetData};

const PRIDE_BASE_URL: &str = "https://www.ebi.ac.uk/pride/ws/archive/v3";

// Accession UNIMOD para fosforilação (Phospho / HexNAc(2)PhosphoHex)
// O mais comum é UNIMOD:21 (Phospho). A deteção por nome também cobre
// variantes como "Phospho (S/T/Y)", "phosphoSerine", etc.
const PHOSPHO_UNIMOD_ACC: &str = "UNIMOD:21";

// ─── JSON deserialization structs (PRIDE Archive REST v3) ─────────────────────

// Nota: a resposta de /search/projects é parseada via serde_json::Value (genérico)
// porque o formato do array muda entre versões da API PRIDE v3.
// Não há structs tipadas para a busca — ver parse_search_page().

/// Detalhe completo de `/projects/{accession}`
#[derive(Deserialize, Default)]
#[serde(rename_all = "camelCase")]
struct PrideProjectDetail {
    title: Option<String>,
    project_description: Option<String>,
    submission_type: Option<String>,   // "COMPLETE" | "PARTIAL"
    organisms: Option<Vec<JsonValue>>, // lista CvParam (defensivo: JsonValue)
    organism_parts: Option<Vec<JsonValue>>,
    diseases: Option<Vec<JsonValue>>,
    references: Option<Vec<PrideReference>>,
    doi: Option<String>,               // DOI do projeto
    identified_ptm_strings: Option<Vec<JsonValue>>,
    instruments: Option<Vec<JsonValue>>,
    keywords: Option<Vec<String>>,
    sample_processing_protocol: Option<String>,
}

#[derive(Deserialize, Default)]
#[serde(rename_all = "camelCase")]
struct PrideReference {
    pubmed_id: Option<serde_json::Number>,
    doi: Option<String>,
}

/// Arquivo de `/projects/{accession}/files`
#[derive(Deserialize, Default)]
#[serde(rename_all = "camelCase")]
struct PrideFile {
    file_category: Option<PrideCvParam>,
    public_file_locations: Option<Vec<PrideFileLocation>>,
    file_name: Option<String>,
}

#[derive(Deserialize, Default)]
#[serde(rename_all = "camelCase")]
struct PrideCvParam {
    value: Option<String>, // "RAW" | "PEAK" | "RESULT" | "SEARCH" | "OTHER"
}

#[derive(Deserialize, Default)]
#[serde(rename_all = "camelCase")]
struct PrideFileLocation {
    value: Option<String>, // URL FTP ou Aspera
}

/// Resposta de `/projects/{accession}/files`
#[derive(Deserialize, Default)]
#[serde(rename_all = "camelCase")]
struct PrideFilesResponse {
    #[serde(rename = "_embedded", default)]
    embedded: Option<PrideFilesEmbedded>,
}

#[derive(Deserialize, Default)]
struct PrideFilesEmbedded {
    files: Option<Vec<PrideFile>>,
}

// ─── Public API ──────────────────────────────────────────────────────────────

/// Pesquisa e ingere datasets do PRIDE Archive.
///
/// Estratégia de fetch:
/// 1. Usa `/search/projects?keyword=<query>` (paginado, 100/página) para obter accessions.
/// 2. Para cada accession faz GET `/projects/{accession}` (metadado COMPLETO).
/// 3. Para cada accession faz GET `/projects/{accession}/files?pageSize=300` para
///    derivar `matrix_pointer` e `proteomics_modality`.
///
/// Rate limit: 1 req/s conservador (PRIDE não publica limites, EBI recomenda cortesia).
pub async fn fetch_pride_datasets(
    query: &str,
    max_results: usize,
) -> Result<(Vec<OmicDatasetData>, Vec<DatasetPaperLinkData>), String> {
    let client = Client::builder()
        .timeout(Duration::from_secs(60))
        .user_agent("DaVinci/1.0 (biohub.solutions; bioinformatics)")
        .build()
        .map_err(|e| format!("Failed to build PRIDE HTTP client: {e}"))?;

    // 1. Coletar accessions via busca paginada
    let accessions = search_pride_accessions(&client, query, max_results).await?;

    if accessions.is_empty() {
        return Ok((vec![], vec![]));
    }

    let mut all_datasets: Vec<OmicDatasetData> = Vec::with_capacity(accessions.len());
    let mut all_links: Vec<DatasetPaperLinkData> = Vec::new();

    // 2. Para cada accession: fetch detalhe + files
    for accession in &accessions {
        // Rate limit cortês: 1 req/s
        sleep(Duration::from_millis(1_000)).await;

        let detail = match fetch_project_detail(&client, accession).await {
            Ok(d) => d,
            Err(e) => {
                eprintln!("[pride] Failed to fetch detail for {accession}: {e}");
                continue;
            }
        };

        // 3. Fetch files (1 chamada por projeto)
        sleep(Duration::from_millis(500)).await;
        let files = fetch_project_files(&client, accession).await.unwrap_or_default();

        // 4. Derivar campos estruturais
        let submission_type = detail
            .submission_type
            .as_deref()
            .unwrap_or("PARTIAL")
            .to_uppercase();

        let data_format = if submission_type == "COMPLETE" {
            "processed".to_string()
        } else {
            "raw".to_string()
        };

        // 5. Derivar matrix_pointer (melhor arquivo RESULT para COMPLETE, RAW para PARTIAL)
        let matrix_pointer = derive_matrix_pointer(&files, &submission_type);

        // 6. Derivar proteomics_modality a partir de PTMs
        let ptm_strings: Vec<String> = extract_cv_names(&detail.identified_ptm_strings);
        let proteomics_modality = if is_phospho(&ptm_strings, &detail.identified_ptm_strings) {
            "phospho".to_string()
        } else {
            "global".to_string()
        };

        // 7. Colheita de tecidos, doenças, referências
        let tissue_raw: Vec<String> = extract_cv_names(&detail.organism_parts);
        let disease_raw: Vec<String> = extract_cv_names(&detail.diseases);

        let mut ref_pmids: Vec<i64> = Vec::new();
        let mut ref_dois: Vec<String> = Vec::new();

        if let Some(refs) = &detail.references {
            for r in refs {
                if let Some(pmid_num) = &r.pubmed_id {
                    if let Ok(pmid) = pmid_num.to_string().parse::<i64>() {
                        if pmid > 0 {
                            ref_pmids.push(pmid);
                        }
                    }
                }
                if let Some(doi) = &r.doi {
                    if !doi.is_empty() {
                        ref_dois.push(doi.clone());
                    }
                }
            }
        }
        // Incluir DOI do próprio projeto
        if let Some(proj_doi) = &detail.doi {
            if !proj_doi.is_empty() && !ref_dois.contains(proj_doi) {
                ref_dois.push(proj_doi.clone());
            }
        }

        // 8. Organismo (primeiro elemento da lista, se presente)
        let organism = extract_first_cv_name(&detail.organisms).unwrap_or_default();
        let tax_id = extract_newt_taxid(&detail.organisms);

        // 9. extra_metadata com sub-objeto "contract"
        let contract = json!({
            "matrix_pointer": matrix_pointer,
            "proteomics_modality": proteomics_modality,
            "tissue_raw": tissue_raw,
            "disease_raw": disease_raw,
            "ref_pmids": ref_pmids,
            "ref_dois": ref_dois,
            "proteomexchange_acc": accession,
        });

        let extra_metadata = json!({
            "submission_type": submission_type,
            "instruments": extract_cv_names(&detail.instruments),
            "ptm_strings": ptm_strings,
            "keywords": detail.keywords.unwrap_or_default(),
            "contract": contract,
        });

        let title = detail.title.clone().unwrap_or_else(|| accession.clone());
        let summary = detail
            .project_description
            .clone()
            .or_else(|| detail.sample_processing_protocol.clone())
            .unwrap_or_default();

        // 10. Construir links paper↔dataset
        for pmid in &ref_pmids {
            all_links.push(DatasetPaperLinkData {
                dataset_accession: accession.clone(),
                paper_pmid: *pmid,
                link_source: "pride_archive".to_string(),
            });
        }

        all_datasets.push(OmicDatasetData {
            accession: accession.clone(),
            source_db: "pride_archive".to_string(),
            bioproject_id: String::new(),
            title,
            summary,
            omic_type: "proteomic".to_string(),
            omic_subcategory: String::new(),
            organism,
            tax_id,
            n_samples: None,
            platform: String::new(),
            extra_metadata,
            is_active: true,
            // --- Campos do contrato OmnisPathway ---
            omics_layers: vec!["proteomic".to_string()],
            omics_count: Some(1),
            data_format,
            access_type: "public".to_string(),
        });
    }

    Ok((all_datasets, all_links))
}

// ─── Helpers de fetch ─────────────────────────────────────────────────────────

/// Coleta accessions via `/search/projects?keyword=<q>`, paginado, até max_results.
async fn search_pride_accessions(
    client: &Client,
    query: &str,
    max_results: usize,
) -> Result<Vec<String>, String> {
    let mut accessions: Vec<String> = Vec::new();
    let page_size = 100usize;
    let mut page = 0usize;

    loop {
        if accessions.len() >= max_results {
            break;
        }

        let url = format!(
            "{}/search/projects?keyword={}&pageSize={}&page={}",
            PRIDE_BASE_URL,
            urlencoded(query),
            page_size,
            page
        );

        sleep(Duration::from_millis(500)).await;

        let resp = client
            .get(&url)
            .header("Accept", "application/json")
            .send()
            .await
            .map_err(|e| format!("PRIDE search request failed (page {page}): {e}"))?;

        if resp.status() == 404 {
            // Sem resultados
            break;
        }

        if !resp.status().is_success() {
            return Err(format!(
                "PRIDE search returned HTTP {} for URL: {}",
                resp.status(),
                url
            ));
        }

        let body = resp
            .text()
            .await
            .map_err(|e| format!("PRIDE search response read error: {e}"))?;

        let page_accessions = parse_search_page(&body);

        // Página vazia = fim dos resultados
        if page_accessions.is_empty() {
            break;
        }

        let page_count = page_accessions.len();

        for acc in page_accessions {
            if accessions.len() >= max_results {
                break;
            }
            accessions.push(acc);
        }

        // Parar se esta página veio incompleta (última página) ou se já atingimos o limite
        if page_count < page_size || accessions.len() >= max_results {
            break;
        }

        page += 1;
    }

    Ok(accessions)
}

/// Extrai accessions da string de resposta JSON de `/search/projects`.
///
/// A PRIDE API v3 mudou o formato da resposta entre versões sem versionamento
/// explícito. São tratados três formatos, em ordem de prioridade:
///
/// Formato A (atual, confirmado empiricamente):
///   Array puro no topo: `[{"accession":"PXD…", …}, …]`
///
/// Formato B (HAL-like):
///   Objeto com campo "projects": `{"projects":[{"accession":"PXD…"},...], …}`
///
/// Formato C (HAL _embedded):
///   `{"_embedded":{"compactprojects":[{"accession":"PXD…"},...]}}`
///
/// Qualquer elemento sem "accession" ou com valor vazio é silenciosamente ignorado.
fn parse_search_page(body: &str) -> Vec<String> {
    let value: JsonValue = match serde_json::from_str(body) {
        Ok(v) => v,
        Err(_) => return vec![],
    };

    // Formato A — array puro no topo
    if let Some(arr) = value.as_array() {
        let accs: Vec<String> = arr
            .iter()
            .filter_map(|item| {
                item.get("accession")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string())
                    .filter(|s| !s.is_empty())
            })
            .collect();
        if !accs.is_empty() {
            return accs;
        }
    }

    // Formato B — objeto com campo "projects"
    if let Some(arr) = value.get("projects").and_then(|v| v.as_array()) {
        let accs: Vec<String> = arr
            .iter()
            .filter_map(|item| {
                item.get("accession")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string())
                    .filter(|s| !s.is_empty())
            })
            .collect();
        if !accs.is_empty() {
            return accs;
        }
    }

    // Formato C — _embedded.compactprojects
    if let Some(arr) = value
        .get("_embedded")
        .and_then(|e| e.get("compactprojects"))
        .and_then(|v| v.as_array())
    {
        let accs: Vec<String> = arr
            .iter()
            .filter_map(|item| {
                item.get("accession")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string())
                    .filter(|s| !s.is_empty())
            })
            .collect();
        if !accs.is_empty() {
            return accs;
        }
    }

    vec![]
}

/// Fetch detalhe completo de `/projects/{accession}`.
async fn fetch_project_detail(client: &Client, accession: &str) -> Result<PrideProjectDetail, String> {
    let url = format!("{}/projects/{}", PRIDE_BASE_URL, accession);

    let resp = client
        .get(&url)
        .header("Accept", "application/json")
        .send()
        .await
        .map_err(|e| format!("PRIDE detail request failed for {accession}: {e}"))?;

    if !resp.status().is_success() {
        return Err(format!(
            "PRIDE detail returned HTTP {} for {}",
            resp.status(),
            accession
        ));
    }

    let body = resp
        .text()
        .await
        .map_err(|e| format!("PRIDE detail response read error for {accession}: {e}"))?;

    serde_json::from_str::<PrideProjectDetail>(&body)
        .map_err(|e| format!("PRIDE detail parse error for {accession}: {e}"))
}

/// Fetch lista de arquivos de `/projects/{accession}/files`.
/// Retorna vetor vazio em caso de erro (não-fatal).
async fn fetch_project_files(client: &Client, accession: &str) -> Result<Vec<PrideFile>, String> {
    let url = format!(
        "{}/projects/{}/files?pageSize=300",
        PRIDE_BASE_URL, accession
    );

    let resp = client
        .get(&url)
        .header("Accept", "application/json")
        .send()
        .await
        .map_err(|e| format!("PRIDE files request failed for {accession}: {e}"))?;

    if !resp.status().is_success() {
        return Err(format!(
            "PRIDE files returned HTTP {} for {}",
            resp.status(),
            accession
        ));
    }

    let body = resp
        .text()
        .await
        .map_err(|e| format!("PRIDE files response read error for {accession}: {e}"))?;

    let files_resp: PrideFilesResponse = serde_json::from_str(&body)
        .map_err(|e| format!("PRIDE files parse error for {accession}: {e}"))?;

    Ok(files_resp
        .embedded
        .and_then(|e| e.files)
        .unwrap_or_default())
}

// ─── Helpers de derivação ─────────────────────────────────────────────────────

/// Seleciona o melhor URL de arquivo para `matrix_pointer`.
/// Para COMPLETE: prefere RESULT (mzTab > mzIdentML > qualquer RESULT).
/// Para PARTIAL: prefere RAW (qualquer arquivo RAW).
fn derive_matrix_pointer(files: &[PrideFile], submission_type: &str) -> Option<String> {
    let target_category = if submission_type == "COMPLETE" { "RESULT" } else { "RAW" };

    let mut best_url: Option<String> = None;
    let mut best_score = -1i32;

    for file in files {
        let category = file
            .file_category
            .as_ref()
            .and_then(|c| c.value.as_deref())
            .unwrap_or("");

        if category != target_category {
            continue;
        }

        let file_name = file.file_name.as_deref().unwrap_or("").to_lowercase();

        // Score de preferência para RESULT
        let score: i32 = if file_name.ends_with(".mztab")
            || file_name.ends_with(".mztab.gz")
        {
            3
        } else if file_name.ends_with(".mzidentml")
            || file_name.ends_with(".mzid")
            || file_name.ends_with(".mzid.gz")
        {
            2
        } else {
            1
        };

        if score > best_score {
            // Pegar a primeira URL FTP/HTTPS disponível
            if let Some(url) = file
                .public_file_locations
                .as_ref()
                .and_then(|locs| locs.first())
                .and_then(|loc| loc.value.clone())
            {
                best_score = score;
                best_url = Some(url);
            }
        }
    }

    best_url
}

/// Detecta fosforilação: verifica accession UNIMOD:21 ou nome contendo "phospho"
/// na lista de PTMs (defensivo contra elementos não-dict).
fn is_phospho(ptm_names: &[String], ptm_list: &Option<Vec<JsonValue>>) -> bool {
    // Verificar nomes extraídos
    for name in ptm_names {
        if name.to_lowercase().contains("phospho") {
            return true;
        }
    }

    // Verificar accessions no JSON bruto (para pegar UNIMOD:21)
    if let Some(list) = ptm_list {
        for item in list {
            if let Some(obj) = item.as_object() {
                // Tentar campo "accession" ou "cvLabel:accession"
                let acc = obj
                    .get("accession")
                    .or_else(|| obj.get("cvLabel"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                if acc == PHOSPHO_UNIMOD_ACC {
                    return true;
                }
            }
        }
    }

    false
}

/// Extrai `name` de uma lista de CvParam em formato JsonValue.
/// Defensivo: ignora elementos que não sejam objetos com campo "name".
fn extract_cv_names(list: &Option<Vec<JsonValue>>) -> Vec<String> {
    let Some(items) = list else {
        return vec![];
    };
    items
        .iter()
        .filter_map(|v| {
            v.as_object()
                .and_then(|o| o.get("name").or_else(|| o.get("value")))
                .and_then(|n| n.as_str())
                .map(|s| s.to_string())
                .filter(|s| !s.is_empty())
        })
        .collect()
}

/// Retorna o nome do primeiro elemento da lista de organismos (ou None).
fn extract_first_cv_name(list: &Option<Vec<JsonValue>>) -> Option<String> {
    let names = extract_cv_names(list);
    names.into_iter().next()
}

/// Extrai tax_id do campo NEWT (accession começa com "NEWT:").
/// Ex.: {"accession": "NEWT:9606", "name": "Homo sapiens"} → Some(9606)
fn extract_newt_taxid(list: &Option<Vec<JsonValue>>) -> Option<i32> {
    let items = list.as_ref()?;
    for item in items {
        if let Some(obj) = item.as_object() {
            let acc = obj
                .get("accession")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            if let Some(id_str) = acc.strip_prefix("NEWT:") {
                if let Ok(id) = id_str.parse::<i32>() {
                    return Some(id);
                }
            }
        }
    }
    None
}

// ─── Helpers de URL ───────────────────────────────────────────────────────────

/// Minimal percent-encoding para valores de query URL.
fn urlencoded(s: &str) -> String {
    s.chars()
        .map(|c| match c {
            ' ' => "%20".to_string(),
            '&' => "%26".to_string(),
            '+' => "%2B".to_string(),
            '#' => "%23".to_string(),
            '%' => "%25".to_string(),
            _ => c.to_string(),
        })
        .collect()
}
