use serde::Deserialize;
use std::time::Duration;
use tokio::time::sleep;

use crate::ncbi::client::NcbiClient;

const ESEARCH_URL: &str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi";
const EFETCH_URL: &str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi";

// ── NCBI rate limits ──────────────────────────────────────────────────────────
// Without API key: 3 req/s  → 340ms between requests is safe
// With API key:   10 req/s  → 100ms between requests is safe
const DELAY_NO_KEY_MS: u64 = 340;
const DELAY_WITH_KEY_MS: u64 = 110;

// ── esearch response structs ──────────────────────────────────────────────────

#[derive(Deserialize)]
struct EsearchResponse {
    esearchresult: EsearchResult,
}

#[derive(Deserialize)]
struct EsearchResult {
    idlist: Vec<String>,
    count: String,
}

// ── Public API ────────────────────────────────────────────────────────────────

/// Return only the total hit count for a PubMed query, without downloading PMIDs.
///
/// Calls esearch with `retmax=0` and reads `esearchresult.count`.
/// Used by the magnitude-preview pipeline where only counts are needed.
pub async fn esearch_count(
    client: &NcbiClient,
    term: &str,
    date_from: Option<u16>,
    date_to: Option<u16>,
) -> Result<usize, String> {
    let date_from_str = date_from.map(|y| y.to_string()).unwrap_or_default();
    let date_to_str = date_to.map(|y| y.to_string()).unwrap_or_default();

    let mut params: Vec<(&str, &str)> = vec![
        ("db", "pubmed"),
        ("term", term),
        ("retmax", "0"),
        ("retmode", "json"),
    ];

    if !date_from_str.is_empty() || !date_to_str.is_empty() {
        params.push(("datetype", "pdat"));
        if !date_from_str.is_empty() {
            params.push(("mindate", &date_from_str));
        }
        if !date_to_str.is_empty() {
            params.push(("maxdate", &date_to_str));
        }
    }

    let body = client.fetch_with_retry(ESEARCH_URL, &params).await?;
    let resp: EsearchResponse = serde_json::from_str(&body)
        .map_err(|e| format!("PubMed esearch count JSON parse error: {e}"))?;

    let count: usize = resp.esearchresult.count.trim().parse().unwrap_or(0);
    Ok(count)
}

/// Search PubMed for PMIDs matching the query with optional date range.
///
/// `max_results` caps how many PMIDs to retrieve. NCBI supports up to 100 000
/// per call; practical limits are set by the caller based on API key presence.
///
/// Returns `(pmids, total_count)`.
pub async fn esearch_pubmed(
    client: &NcbiClient,
    query: &str,
    date_from: Option<u16>,
    date_to: Option<u16>,
    max_results: usize,
) -> Result<(Vec<String>, usize), String> {
    let max_str = max_results.to_string();
    let date_from_str = date_from.map(|y| y.to_string()).unwrap_or_default();
    let date_to_str = date_to.map(|y| y.to_string()).unwrap_or_default();

    let mut params: Vec<(&str, &str)> = vec![
        ("db", "pubmed"),
        ("term", query),
        ("retmax", &max_str),
        ("retmode", "json"),
        ("usehistory", "n"),
    ];

    if !date_from_str.is_empty() || !date_to_str.is_empty() {
        params.push(("datetype", "pdat"));
        if !date_from_str.is_empty() {
            params.push(("mindate", &date_from_str));
        }
        if !date_to_str.is_empty() {
            params.push(("maxdate", &date_to_str));
        }
    }

    let body = client.fetch_with_retry(ESEARCH_URL, &params).await?;
    let resp: EsearchResponse = serde_json::from_str(&body)
        .map_err(|e| format!("PubMed esearch JSON parse error: {e}"))?;

    let total: usize = resp.esearchresult.count.trim().parse().unwrap_or(0);
    Ok((resp.esearchresult.idlist, total))
}

/// Fetch PubMed XML for the given list of PMIDs in batches of 100.
///
/// Respects NCBI rate limits:
/// - No API key → 340 ms between batches (≤ 3 req/s)
/// - API key    → 110 ms between batches (≤ 10 req/s)
///
/// Returns a valid `<PubmedArticleSet>…</PubmedArticleSet>` XML string.
pub async fn efetch_pubmed_xml(
    client: &NcbiClient,
    pmids: &[String],
    has_api_key: bool,
) -> Result<String, String> {
    let delay = Duration::from_millis(if has_api_key {
        DELAY_WITH_KEY_MS
    } else {
        DELAY_NO_KEY_MS
    });

    let mut all_xml = String::from("<PubmedArticleSet>");

    for (i, chunk) in pmids.chunks(100).enumerate() {
        // Throttle from the second batch onwards
        if i > 0 {
            sleep(delay).await;
        }

        let ids = chunk.join(",");
        let params = [
            ("db", "pubmed"),
            ("id", ids.as_str()),
            ("retmode", "xml"),
            ("rettype", "abstract"),
        ];

        let xml = client.fetch_with_retry(EFETCH_URL, &params).await?;
        all_xml.push_str(&extract_articles(&xml));
    }

    all_xml.push_str("</PubmedArticleSet>");
    Ok(all_xml)
}

/// Extract the PubmedArticle elements from a single efetch response.
fn extract_articles(xml: &str) -> String {
    let start = xml.find("<PubmedArticle").unwrap_or(0);
    let end = xml
        .rfind("</PubmedArticle>")
        .map(|i| i + "</PubmedArticle>".len())
        .unwrap_or(xml.len());
    xml[start..end].to_string()
}
