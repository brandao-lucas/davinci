use serde::Deserialize;
use serde_json::{json, Value as JsonValue};

use crate::ncbi::client::NcbiClient;
use crate::omics::models::{DatasetPaperLinkData, OmicDatasetData};
use crate::omics::type_classifier::classify_omic_type;

const ESEARCH_URL: &str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi";
const ESUMMARY_URL: &str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi";

// ─── JSON deserialization structs ────────────────────────────────────────────

#[derive(Deserialize)]
struct EsearchResponse {
    esearchresult: EsearchResult,
}

#[derive(Deserialize)]
struct EsearchResult {
    idlist: Vec<String>,
}

#[derive(Deserialize, Default)]
struct SraSummaryEntry {
    /// Study accession (SRP…) — the stable cross-study identifier
    #[serde(rename = "study_accession")]
    study_accession: Option<String>,
    #[serde(rename = "study_title")]
    study_title: Option<String>,
    #[serde(rename = "study_abstract")]
    study_abstract: Option<String>,
    /// Taxonomy ID
    #[serde(rename = "organism_taxid")]
    organism_taxid: Option<String>,
    #[serde(rename = "organism_name")]
    organism_name: Option<String>,
    /// Library strategy: RNA-Seq, WGS, AMPLICON, ChIP-Seq, etc.
    #[serde(rename = "library_strategy")]
    library_strategy: Option<String>,
    /// Sequencing platform: ILLUMINA, OXFORD_NANOPORE, etc.
    #[serde(rename = "platform")]
    platform: Option<String>,
    /// Sequencing instrument model
    #[serde(rename = "instrument_model")]
    instrument_model: Option<String>,
    /// Number of runs in this study
    #[serde(rename = "runs")]
    runs: Option<JsonValue>,
    /// BioProject accession linked to this study
    #[serde(rename = "bioproject_accession")]
    bioproject_accession: Option<String>,
    /// Associated PubMed ID (single, not an array in SRA)
    #[serde(rename = "pubmed_id")]
    pubmed_id: Option<String>,
    /// Sample count (may be in a nested field)
    #[serde(rename = "sample_count")]
    sample_count: Option<JsonValue>,
}

// ─── Public API ──────────────────────────────────────────────────────────────

/// Fetch SRA study metadata for the given query.
///
/// Returns `(datasets, links)`. Most SRA entries do not embed PMIDs directly,
/// so the returned `links` vec is typically empty. Use elink for PMID discovery.
pub async fn fetch_sra_datasets(
    client: &NcbiClient,
    query: &str,
    max_results: usize,
) -> Result<(Vec<OmicDatasetData>, Vec<DatasetPaperLinkData>), String> {
    // Step 1 — esearch
    let max_str = max_results.to_string();
    let search_params = [
        ("db", "sra"),
        ("term", query),
        ("retmax", &max_str),
        ("retmode", "json"),
    ];
    let search_body = client.fetch_with_retry(ESEARCH_URL, &search_params).await?;
    let search_resp: EsearchResponse =
        serde_json::from_str(&search_body).map_err(|e| format!("SRA esearch parse error: {e}"))?;

    let uids = search_resp.esearchresult.idlist;
    if uids.is_empty() {
        return Ok((vec![], vec![]));
    }

    // Step 2 — esummary in batches of 100
    let mut all_datasets: Vec<OmicDatasetData> = Vec::with_capacity(uids.len());
    let mut all_links: Vec<DatasetPaperLinkData> = Vec::new();

    for chunk in uids.chunks(100) {
        let ids_csv = chunk.join(",");
        let summary_params = [
            ("db", "sra"),
            ("id", ids_csv.as_str()),
            ("retmode", "json"),
        ];
        let summary_body = client.fetch_with_retry(ESUMMARY_URL, &summary_params).await?;

        let raw: serde_json::Value = serde_json::from_str(&summary_body)
            .map_err(|e| format!("SRA esummary parse error: {e}"))?;

        let result_obj = match raw.get("result").and_then(|v| v.as_object()) {
            Some(obj) => obj,
            None => continue,
        };

        for (uid, entry_val) in result_obj {
            if uid == "uids" {
                continue;
            }

            let entry: SraSummaryEntry = match serde_json::from_value(entry_val.clone()) {
                Ok(e) => e,
                Err(_) => continue,
            };

            let accession = match entry.study_accession {
                Some(ref a) if !a.is_empty() => a.clone(),
                _ => continue,
            };

            let title = entry.study_title.unwrap_or_default();
            let summary = entry.study_abstract.unwrap_or_default();
            let library_strategy = entry.library_strategy.unwrap_or_default();

            // Include library_strategy in text for better classification
            let classify_text = format!("{} {} {}", title, summary, library_strategy);
            let classification = classify_omic_type(&classify_text, "");

            let organism = entry.organism_name.unwrap_or_default();
            let tax_id = entry
                .organism_taxid
                .and_then(|t| t.parse::<i32>().ok());

            let platform = entry
                .instrument_model
                .or(entry.platform)
                .unwrap_or_default();

            let n_samples = parse_n_samples(entry.sample_count);
            let runs_count = count_runs(entry.runs);
            let bioproject_id = entry.bioproject_accession.unwrap_or_default();

            let extra_metadata = json!({
                "sra_uid": uid,
                "library_strategy": library_strategy,
                "runs_count": runs_count,
            });

            // SRA occasionally embeds a PMID
            if let Some(pmid_str) = entry.pubmed_id {
                if let Ok(pmid) = pmid_str.parse::<i64>() {
                    all_links.push(DatasetPaperLinkData {
                        dataset_accession: accession.clone(),
                        paper_pmid: pmid,
                        link_source: "elink".to_string(),
                    });
                }
            }

            all_datasets.push(OmicDatasetData {
                accession,
                source_db: "sra".to_string(),
                bioproject_id,
                title,
                summary,
                omic_type: classification.omic_type.to_string(),
                omic_subcategory: classification.omic_subcategory.to_string(),
                organism,
                tax_id,
                n_samples,
                platform,
                extra_metadata,
                is_active: true,
            });
        }
    }

    Ok((all_datasets, all_links))
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

fn parse_n_samples(raw: Option<JsonValue>) -> Option<i32> {
    match raw? {
        JsonValue::Number(n) => n.as_i64().map(|v| v as i32),
        JsonValue::String(s) => s.parse::<i32>().ok(),
        _ => None,
    }
}

fn count_runs(raw: Option<JsonValue>) -> i64 {
    match raw {
        Some(JsonValue::Array(arr)) => arr.len() as i64,
        Some(JsonValue::Number(n)) => n.as_i64().unwrap_or(0),
        _ => 0,
    }
}

/// Returns `(uid, accession)` pairs for datasets that had no embedded PMIDs,
/// to be used for elink discovery.
pub fn datasets_without_pmids<'a>(
    datasets: &'a [OmicDatasetData],
    links: &[DatasetPaperLinkData],
) -> Vec<(String, String)> {
    let linked: std::collections::HashSet<&str> =
        links.iter().map(|l| l.dataset_accession.as_str()).collect();

    datasets
        .iter()
        .filter(|d| !linked.contains(d.accession.as_str()))
        .filter_map(|d| {
            d.extra_metadata
                .get("sra_uid")
                .and_then(|v| v.as_str())
                .map(|uid| (uid.to_string(), d.accession.clone()))
        })
        .collect()
}
