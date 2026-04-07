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

/// GEO esummary returns a flat object keyed by GDS UID, plus a "uids" array.
/// We deserialize the `result` map into a HashMap and filter out the "uids" key.
#[derive(Deserialize, Default)]
struct GeoSummaryEntry {
    accession: Option<String>,  // "GSE12345" — the GSE accession
    title: Option<String>,
    summary: Option<String>,
    organism: Option<String>,
    taxon: Option<String>,
    #[serde(rename = "n_samples")]
    n_samples_raw: Option<JsonValue>, // can be int or string in the API
    gpl: Option<String>,
    gse: Option<String>,
    bioproject: Option<String>,
    #[serde(rename = "pubmed_ids", default)]
    pubmed_ids: Vec<String>,
}

// ─── Public API ──────────────────────────────────────────────────────────────

/// Fetch GEO dataset metadata for the given query.
///
/// Returns `(datasets, links)` where `links` are PMID connections embedded
/// in the GEO esummary response (`pubmed_ids` field).
/// Entries without embedded PMIDs are candidates for elink discovery.
pub async fn fetch_geo_datasets(
    client: &NcbiClient,
    query: &str,
    max_results: usize,
) -> Result<(Vec<OmicDatasetData>, Vec<DatasetPaperLinkData>), String> {
    // Step 1 — esearch: get GDS UIDs
    let max_str = max_results.to_string();
    let search_params = [
        ("db", "gds"),
        ("term", query),
        ("retmax", &max_str),
        ("retmode", "json"),
    ];
    let search_body = client.fetch_with_retry(ESEARCH_URL, &search_params).await?;
    let search_resp: EsearchResponse =
        serde_json::from_str(&search_body).map_err(|e| format!("GEO esearch parse error: {e}"))?;

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
            ("db", "gds"),
            ("id", ids_csv.as_str()),
            ("retmode", "json"),
        ];
        let summary_body = client.fetch_with_retry(ESUMMARY_URL, &summary_params).await?;

        let raw: serde_json::Value = serde_json::from_str(&summary_body)
            .map_err(|e| format!("GEO esummary parse error: {e}"))?;

        let result_obj = match raw.get("result").and_then(|v| v.as_object()) {
            Some(obj) => obj,
            None => continue,
        };

        for (uid, entry_val) in result_obj {
            if uid == "uids" {
                continue; // skip the "uids" metadata key
            }

            // Deserialize each entry individually so one bad entry doesn't abort
            let entry: GeoSummaryEntry = match serde_json::from_value(entry_val.clone()) {
                Ok(e) => e,
                Err(_) => continue,
            };

            let title = entry.title.unwrap_or_default();
            let summary = entry.summary.unwrap_or_default();

            let classifications = classify_omic_type(&title, &summary);
            let omic_type = classifications.iter().map(|c| c.omic_type).collect::<Vec<_>>().join(",");
            let omic_subcategory = classifications.iter().map(|c| c.omic_subcategory).collect::<Vec<_>>().join(",");

            let gpl = entry.gpl.unwrap_or_default();
            let bioproject_id = entry.bioproject.unwrap_or_default();
            let original_accession = entry.gse.or(entry.accession).unwrap_or_default();

            if original_accession.is_empty() {
                continue;
            }

            // Unification: if bioproject_id is present, use it as the primary accession.
            // This allows merging GEO and BioProject records on the same study ID.
            let accession = if !bioproject_id.is_empty() {
                bioproject_id.clone()
            } else {
                original_accession.clone()
            };

            let organism = entry.organism.unwrap_or_default();
            let tax_id = entry
                .taxon
                .and_then(|t| t.parse::<i32>().ok());
            let n_samples = parse_n_samples(entry.n_samples_raw);

            let extra_metadata: JsonValue = json!({
                "gds_uid": uid,
                "gse": original_accession,
                "gpl": gpl,
                "bioproject": bioproject_id,
            });

            // Build DatasetPaperLink for each embedded PMID
            for pmid_str in &entry.pubmed_ids {
                if let Ok(pmid) = pmid_str.parse::<i64>() {
                    all_links.push(DatasetPaperLinkData {
                        dataset_accession: accession.clone(),
                        paper_pmid: pmid,
                        link_source: "geo_xml".to_string(),
                    });
                }
            }

            all_datasets.push(OmicDatasetData {
                accession,
                source_db: "geo".to_string(),
                bioproject_id,
                title,
                summary,
                omic_type,
                omic_subcategory,
                organism,
                tax_id,
                n_samples,
                platform: gpl,
                extra_metadata,
                is_active: true,
            });
        }
    }

    Ok((all_datasets, all_links))
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

/// GEO esummary can return n_samples as an integer or a string.
fn parse_n_samples(raw: Option<JsonValue>) -> Option<i32> {
    match raw? {
        JsonValue::Number(n) => n.as_i64().map(|v| v as i32),
        JsonValue::String(s) => s.parse::<i32>().ok(),
        _ => None,
    }
}

/// Returns `(uid, accession)` pairs for datasets that had no embedded PMIDs.
/// These pairs are used to call elink for PMID discovery.
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
            // GDS UID is stored in extra_metadata.gds_uid
            d.extra_metadata
                .get("gds_uid")
                .and_then(|v| v.as_str())
                .map(|uid| (uid.to_string(), d.accession.clone()))
        })
        .collect()
}
