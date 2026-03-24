use futures::future::join_all;
use serde::Deserialize;
use std::collections::HashMap;

use crate::ncbi::client::NcbiClient;
use crate::omics::models::DatasetPaperLinkData;

const ELINK_URL: &str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi";

// ─── JSON deserialization structs ────────────────────────────────────────────

#[derive(Deserialize)]
struct ElinkResponse {
    #[serde(default)]
    linksets: Vec<ElinkLinkset>,
}

#[derive(Deserialize)]
struct ElinkLinkset {
    /// The query IDs sent in this request (parallel to linksetdbs)
    #[serde(default)]
    ids: Vec<String>,
    #[serde(default)]
    linksetdbs: Vec<ElinkLinksetDb>,
}

#[derive(Deserialize)]
struct ElinkLinksetDb {
    /// The target database ("pubmed")
    #[allow(dead_code)]
    dbto: Option<String>,
    #[serde(default)]
    links: Vec<String>,
}

// ─── Public API ──────────────────────────────────────────────────────────────

/// Discover PubMed links for the given dataset UIDs via NCBI elink.
///
/// # Arguments
/// * `client` - Shared NcbiClient with rate limiting and retry logic
/// * `uid_accession_pairs` - `(numeric_uid, dataset_accession)` pairs.
///   The numeric UID is the NCBI internal ID (e.g. GDS UID, not GSE accession).
///   It is stored in `extra_metadata.gds_uid` / `sra_uid` / `bioproject_uid`
///   by the respective parsers.
/// * `db_from` - Source database: `"gds"`, `"sra"`, or `"bioproject"`
///
/// Returns `DatasetPaperLinkData` entries for every discovered PMID.
pub async fn discover_links_via_elink(
    client: &NcbiClient,
    uid_accession_pairs: &[(String, String)],
    db_from: &str,
) -> Result<Vec<DatasetPaperLinkData>, String> {
    if uid_accession_pairs.is_empty() {
        return Ok(vec![]);
    }

    // Build a UID → accession lookup map
    let uid_to_accession: HashMap<&str, &str> = uid_accession_pairs
        .iter()
        .map(|(uid, acc)| (uid.as_str(), acc.as_str()))
        .collect();

    // NCBI elink hard limit: max 20 IDs per request
    let chunks: Vec<&[(String, String)]> = uid_accession_pairs.chunks(20).collect();

    // Build futures for all chunks (concurrent, not sequential).
    // ids_csv is owned (String) so the async future does not borrow a local.
    let fetch_futures: Vec<_> = chunks
        .iter()
        .map(|chunk| {
            let ids_csv: String = chunk
                .iter()
                .map(|(uid, _)| uid.as_str())
                .collect::<Vec<_>>()
                .join(",");
            fetch_elink_batch(client, ids_csv, db_from)
        })
        .collect();

    let results = join_all(fetch_futures).await;

    let mut all_links: Vec<DatasetPaperLinkData> = Vec::new();

    for (chunk, batch_result) in chunks.iter().zip(results.into_iter()) {
        match batch_result {
            Err(e) => {
                // Log the error but don't abort — partial results are valuable
                eprintln!("[elink] batch error for db_from={db_from}: {e}");
                continue;
            }
            Ok(linksets) => {
                for linkset in linksets {
                    // Resolve the UID to its dataset accession
                    // elink returns `ids` as the query UIDs for this linkset
                    let accession = linkset
                        .ids
                        .iter()
                        .find_map(|id| uid_to_accession.get(id.as_str()).copied())
                        .or_else(|| {
                            // Fallback: if only one UID was in the batch chunk, use it
                            if chunk.len() == 1 {
                                uid_to_accession.get(chunk[0].0.as_str()).copied()
                            } else {
                                None
                            }
                        });

                    let accession = match accession {
                        Some(a) => a,
                        None => continue,
                    };

                    for linksetdb in &linkset.linksetdbs {
                        for pmid_str in &linksetdb.links {
                            if let Ok(pmid) = pmid_str.parse::<i64>() {
                                all_links.push(DatasetPaperLinkData {
                                    dataset_accession: accession.to_string(),
                                    paper_pmid: pmid,
                                    link_source: "elink".to_string(),
                                });
                            }
                        }
                    }
                }
            }
        }
    }

    Ok(all_links)
}

// ─── Internal helpers ─────────────────────────────────────────────────────────

async fn fetch_elink_batch(
    client: &NcbiClient,
    ids_csv: String, // owned to avoid lifetime issues with join_all
    db_from: &str,
) -> Result<Vec<ElinkLinkset>, String> {
    let params = [
        ("dbfrom", db_from),
        ("db", "pubmed"),
        ("id", ids_csv.as_str()),
        ("retmode", "json"),
    ];

    let body = client.fetch_with_retry(ELINK_URL, &params).await?;

    let resp: ElinkResponse =
        serde_json::from_str(&body).map_err(|e| format!("elink parse error: {e}"))?;

    Ok(resp.linksets)
}
