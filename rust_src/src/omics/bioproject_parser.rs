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
#[serde(rename_all = "snake_case")]
struct BioProjectSummaryEntry {
    /// Project accession: PRJNA…
    project_acc: Option<String>,
    project_title: Option<String>,
    /// Short description (often the project name repeated)
    description_name: Option<String>,
    /// Longer project description
    description_title: Option<String>,
    /// Organism name
    organism_name: Option<String>,
    /// NCBI taxonomy ID
    organism_taxid: Option<JsonValue>,
    /// Target scope: monoisolate, multiisolate, multispecies, environment, synthetic, other
    target_scope: Option<String>,
    /// Biological material type: genome, transcriptome, metagenome, etc.
    target_material: Option<String>,
    /// Experimental method: sequencing, array, mass_spec, etc.
    method_type: Option<String>,
    /// Number of samples/biosample count
    biosample_count: Option<JsonValue>,
    /// BioProject rarely embeds PMIDs but it can
    #[serde(default)]
    pubmed_ids: Vec<String>,
}

// ─── Public API ──────────────────────────────────────────────────────────────

/// Fetch BioProject metadata for the given query.
///
/// BioProject entries rarely have embedded PMIDs; almost all PMID links
/// come from the elink step. The returned `links` vec will typically be empty.
pub async fn fetch_bioproject_datasets(
    client: &NcbiClient,
    query: &str,
    max_results: usize,
) -> Result<(Vec<OmicDatasetData>, Vec<DatasetPaperLinkData>), String> {
    // Step 1 — esearch
    let max_str = max_results.to_string();
    let search_params = [
        ("db", "bioproject"),
        ("term", query),
        ("retmax", &max_str),
        ("retmode", "json"),
    ];
    let search_body = client.fetch_with_retry(ESEARCH_URL, &search_params).await?;
    let search_resp: EsearchResponse = serde_json::from_str(&search_body)
        .map_err(|e| format!("BioProject esearch parse error: {e}"))?;

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
            ("db", "bioproject"),
            ("id", ids_csv.as_str()),
            ("retmode", "json"),
        ];
        let summary_body = client.fetch_with_retry(ESUMMARY_URL, &summary_params).await?;

        let raw: serde_json::Value = serde_json::from_str(&summary_body)
            .map_err(|e| format!("BioProject esummary parse error: {e}"))?;

        let result_obj = match raw.get("result").and_then(|v| v.as_object()) {
            Some(obj) => obj,
            None => continue,
        };

        for (uid, entry_val) in result_obj {
            if uid == "uids" {
                continue;
            }

            let entry: BioProjectSummaryEntry = match serde_json::from_value(entry_val.clone()) {
                Ok(e) => e,
                Err(_) => continue,
            };

            let accession = match entry.project_acc {
                Some(ref a) if !a.is_empty() => a.clone(),
                _ => continue,
            };

            let title = entry.project_title.unwrap_or_default();
            // Use description_title (longer) over description_name (shorter)
            let summary = entry
                .description_title
                .or(entry.description_name)
                .unwrap_or_default();

            let method_type = entry.method_type.unwrap_or_default();
            let target_material = entry.target_material.clone().unwrap_or_default();

            // Combine method_type and target_material to help classification
            let classify_hint = format!("{} {} {} {}", title, summary, method_type, target_material);
            let classifications = classify_omic_type(&classify_hint, "");
            let omic_type = classifications.iter().map(|c| c.omic_type).collect::<Vec<_>>().join(",");
            let omic_subcategory = classifications.iter().map(|c| c.omic_subcategory).collect::<Vec<_>>().join(",");

            let organism = entry.organism_name.unwrap_or_default();
            let tax_id = parse_tax_id(entry.organism_taxid);
            let n_samples = parse_count(entry.biosample_count);

            let extra_metadata = json!({
                "bioproject_uid": uid,
                "target_scope": entry.target_scope,
                "target_material": entry.target_material,
                "method_type": method_type,
            });

            // Occasionally BioProject embeds PMIDs
            for pmid_str in &entry.pubmed_ids {
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
                source_db: "bioproject".to_string(),
                bioproject_id: String::new(), // BioProject IS the bioproject
                title,
                summary,
                omic_type,
                omic_subcategory,
                organism,
                tax_id,
                n_samples,
                platform: String::new(),
                extra_metadata,
                is_active: true,
            });
        }
    }

    Ok((all_datasets, all_links))
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

fn parse_tax_id(raw: Option<JsonValue>) -> Option<i32> {
    match raw? {
        JsonValue::Number(n) => n.as_i64().map(|v| v as i32),
        JsonValue::String(s) => s.parse::<i32>().ok(),
        _ => None,
    }
}

fn parse_count(raw: Option<JsonValue>) -> Option<i32> {
    match raw? {
        JsonValue::Number(n) => n.as_i64().map(|v| v as i32),
        JsonValue::String(s) => s.parse::<i32>().ok(),
        _ => None,
    }
}

/// Returns `(uid, accession)` pairs for entries with no embedded PMIDs,
/// to be used for NCBI elink discovery.
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
                .get("bioproject_uid")
                .and_then(|v| v.as_str())
                .map(|uid| (uid.to_string(), d.accession.clone()))
        })
        .collect()
}
