use pyo3::prelude::*;
use serde::Deserialize;
use std::collections::HashMap;

use crate::ncbi::client::NcbiClient;

const ESEARCH_URL: &str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi";
const ESUMMARY_URL: &str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi";

// ── MeshSuggestion (PyClass) ──────────────────────────────────────────────────

/// One MeSH descriptor suggestion returned by `mesh_suggest`.
///
/// Fields:
/// | Name                   | Type        | Description |
/// |------------------------|-------------|-------------|
/// | `descriptor`           | `str`       | Human-readable MeSH descriptor name. |
/// | `ui`                   | `str`       | MeSH unique identifier (e.g. "D017497"). |
/// | `tree_numbers`         | `Vec[str]`  | MeSH tree numbers (e.g. ["C17.800.338.365"]). |
/// | `scope_note`           | `str`       | Scope note / definition from the MeSH record. |
/// | `allowable_qualifiers` | `Vec[str]`  | Valid subheadings for this descriptor. |
/// | `pubmed_count`         | `usize`     | Number of PubMed records tagged with this descriptor. |
#[pyclass]
#[derive(Clone, Debug, Default)]
pub struct MeshSuggestion {
    #[pyo3(get)]
    pub descriptor: String,
    #[pyo3(get)]
    pub ui: String,
    #[pyo3(get)]
    pub tree_numbers: Vec<String>,
    #[pyo3(get)]
    pub scope_note: String,
    #[pyo3(get)]
    pub allowable_qualifiers: Vec<String>,
    #[pyo3(get)]
    pub pubmed_count: usize,
}

#[pymethods]
impl MeshSuggestion {
    fn __repr__(&self) -> String {
        format!(
            "MeshSuggestion(descriptor={:?}, ui={:?}, pubmed_count={})",
            self.descriptor, self.ui, self.pubmed_count
        )
    }
}

// ── Deserialization structs for esummary JSON ─────────────────────────────────

#[derive(Deserialize)]
struct EsummaryResponse {
    result: EsummaryResult,
}

#[derive(Deserialize)]
struct EsummaryResult {
    uids: Vec<String>,
    #[serde(flatten)]
    records: HashMap<String, MeshRecord>,
}

#[derive(Deserialize, Default)]
struct IdxLink {
    treenum: String,
}

#[derive(Deserialize, Default)]
struct MeshRecord {
    #[serde(default)]
    ds_meshterms: Vec<String>,
    #[serde(default)]
    ds_meshui: String,
    #[serde(default)]
    ds_scopenote: String,
    #[serde(default)]
    ds_subheading: Vec<String>,
    #[serde(default)]
    ds_idxlinks: Vec<IdxLink>,
    #[serde(default)]
    ds_recordtype: String,
}

// ── esearch_mesh_uids ─────────────────────────────────────────────────────────

/// Search the `mesh` database for UIDs matching `term`.
/// Returns up to `retmax` UID strings.
async fn esearch_mesh_uids(
    client: &NcbiClient,
    term: &str,
    retmax: usize,
) -> Result<Vec<String>, String> {
    #[derive(Deserialize)]
    struct EsearchResp {
        esearchresult: EsearchResult,
    }
    #[derive(Deserialize)]
    struct EsearchResult {
        idlist: Vec<String>,
    }

    let retmax_str = retmax.to_string();
    let params = [
        ("db", "mesh"),
        ("term", term),
        ("retmax", &retmax_str),
        ("retmode", "json"),
    ];

    let body = client.fetch_with_retry(ESEARCH_URL, &params).await?;
    let resp: EsearchResp = serde_json::from_str(&body)
        .map_err(|e| format!("mesh esearch JSON parse error: {e}"))?;

    Ok(resp.esearchresult.idlist)
}

// ── extract_pubmed_mesh_translations ─────────────────────────────────────────

/// Run `esearch db=pubmed` and extract auto-translated MeSH descriptor names
/// from `translationset` / `querytranslation` in the JSON response.
///
/// Returns deduplicated descriptor names found as `"X"[MeSH Terms]` in the
/// PubMed auto-translation (best relevance signal).
async fn extract_pubmed_mesh_translations(
    client: &NcbiClient,
    term: &str,
) -> Result<Vec<String>, String> {
    #[derive(Deserialize)]
    struct EsearchResp {
        esearchresult: EsearchResult,
    }
    #[derive(Deserialize, Default)]
    struct TranslationSet {
        to: Option<String>,
    }
    #[derive(Deserialize)]
    struct EsearchResult {
        #[serde(default)]
        translationset: Vec<TranslationSet>,
        #[serde(default)]
        querytranslation: String,
    }

    let params = [
        ("db", "pubmed"),
        ("term", term),
        ("retmax", "0"),
        ("retmode", "json"),
    ];

    let body = client.fetch_with_retry(ESEARCH_URL, &params).await?;
    let resp: EsearchResp = serde_json::from_str(&body)
        .map_err(|e| format!("pubmed esearch translation JSON parse error: {e}"))?;

    let mut mesh_names: Vec<String> = Vec::new();

    for entry in &resp.esearchresult.translationset {
        if let Some(to_str) = &entry.to {
            extract_mesh_descriptors_from_query(to_str, &mut mesh_names);
        }
    }
    extract_mesh_descriptors_from_query(&resp.esearchresult.querytranslation, &mut mesh_names);

    // Deduplicate preserving order
    let mut seen = std::collections::HashSet::new();
    mesh_names.retain(|n| seen.insert(n.to_lowercase()));

    Ok(mesh_names)
}

/// Extract descriptor names that appear as `"Name"[MeSH Terms]` or `"Name"[majr]`
/// from a PubMed query string.
fn extract_mesh_descriptors_from_query(query: &str, out: &mut Vec<String>) {
    let mesh_markers = ["[mesh terms]", "[mesh]", "[majr]", "[mh]"];
    let lower = query.to_lowercase();

    let mut pos = 0;
    while pos < lower.len() {
        // Find opening quote
        if let Some(open_rel) = lower[pos..].find('"') {
            let open = pos + open_rel;
            // Find matching closing quote
            if let Some(close_rel) = lower[open + 1..].find('"') {
                let close = open + 1 + close_rel;
                let descriptor = &query[open + 1..close];
                // Check what follows the closing quote (after optional whitespace)
                let after = lower[close + 1..].trim_start();
                if mesh_markers.iter().any(|m| after.starts_with(m)) {
                    let name = descriptor.trim().to_string();
                    if !name.is_empty() {
                        out.push(name);
                    }
                }
                pos = close + 1;
            } else {
                break;
            }
        } else {
            break;
        }
    }
}

// ── esummary_mesh ─────────────────────────────────────────────────────────────

/// Fetch MeSH summary records for a list of UIDs via esummary JSON.
/// Returns `MeshSuggestion` structs (without pubmed_count, filled later).
async fn esummary_mesh(
    client: &NcbiClient,
    uids: &[String],
) -> Result<Vec<MeshSuggestion>, String> {
    if uids.is_empty() {
        return Ok(vec![]);
    }

    let ids_str = uids.join(",");
    let params = [
        ("db", "mesh"),
        ("id", ids_str.as_str()),
        ("retmode", "json"),
    ];

    let body = client.fetch_with_retry(ESUMMARY_URL, &params).await?;
    let resp: EsummaryResponse = serde_json::from_str(&body)
        .map_err(|e| format!("mesh esummary JSON parse error: {e}"))?;

    let mut suggestions: Vec<MeshSuggestion> = Vec::new();

    // Iterate in the order the API returned UIDs
    for uid in &resp.result.uids {
        let Some(record) = resp.result.records.get(uid) else {
            continue;
        };
        // Skip supplemental records and non-descriptor entries
        if record.ds_recordtype == "supplemental-record" || record.ds_meshterms.is_empty() {
            continue;
        }

        let descriptor = record.ds_meshterms.first().cloned().unwrap_or_default();
        if descriptor.is_empty() {
            continue;
        }

        let tree_numbers: Vec<String> = record
            .ds_idxlinks
            .iter()
            .map(|l| l.treenum.clone())
            .filter(|t| !t.is_empty() && !t.starts_with('@'))
            .collect();

        suggestions.push(MeshSuggestion {
            descriptor,
            ui: record.ds_meshui.clone(),
            tree_numbers,
            scope_note: record.ds_scopenote.clone(),
            allowable_qualifiers: record.ds_subheading.clone(),
            pubmed_count: 0, // filled in run_mesh_suggest
        });
    }

    Ok(suggestions)
}

// ── pubmed_count_for_mesh ─────────────────────────────────────────────────────

/// Count PubMed records tagged with a given MeSH descriptor.
/// Returns 0 on error (non-fatal, count is informational).
async fn pubmed_count_for_mesh(client: &NcbiClient, descriptor: &str) -> usize {
    #[derive(Deserialize)]
    struct Resp {
        esearchresult: Res,
    }
    #[derive(Deserialize)]
    struct Res {
        count: String,
    }

    let term = format!(r#""{descriptor}"[MeSH Terms]"#);
    let params = [
        ("db", "pubmed"),
        ("term", term.as_str()),
        ("retmax", "0"),
        ("retmode", "json"),
    ];

    match client.fetch_with_retry(ESEARCH_URL, &params).await {
        Ok(body) => {
            if let Ok(r) = serde_json::from_str::<Resp>(&body) {
                r.esearchresult.count.trim().parse().unwrap_or(0)
            } else {
                0
            }
        }
        Err(_) => 0,
    }
}

// ── Public entry point ────────────────────────────────────────────────────────

/// Suggest MeSH descriptors for a free-text term by fusing:
/// 1. PubMed automatic translation (`esearch db=pubmed`, reads `translationset` /
///    `querytranslation` to extract `"X"[MeSH Terms]` mappings — best relevance signal).
/// 2. MeSH database search (`esearch db=mesh` → `esummary db=mesh`):
///    descriptor name, UI, tree numbers, scope note, allowable qualifiers (subheadings).
///
/// Returns `Vec<MeshSuggestion>` ordered by PubMed relevance (descriptors that
/// appear in the PubMed translation come first).
///
/// # Parameters
/// | Name      | Type          | Description |
/// |-----------|---------------|-------------|
/// | `term`    | `str`         | Free-text term to look up (e.g. "hidradenitis"). |
/// | `api_key` | `Option[str]` | NCBI API key (optional, relaxes rate limit). |
#[pyfunction]
#[pyo3(signature = (term, api_key=None))]
pub fn mesh_suggest(term: String, api_key: Option<String>) -> PyResult<Vec<MeshSuggestion>> {
    let rt = tokio::runtime::Runtime::new()
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("Tokio runtime: {e}")))?;

    rt.block_on(async {
        run_mesh_suggest(term, api_key)
            .await
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e))
    })
}

async fn run_mesh_suggest(
    term: String,
    api_key: Option<String>,
) -> Result<Vec<MeshSuggestion>, String> {
    let client = NcbiClient::new(api_key);

    // 1. PubMed translation → MeSH descriptor names (best relevance signal)
    let translated_names = extract_pubmed_mesh_translations(&client, &term).await?;

    // 2. MeSH database search for the raw term → UIDs
    let mesh_uids = esearch_mesh_uids(&client, &term, 10).await?;

    // 3. esummary → full records (descriptor, UI, tree numbers, scope note, subheadings)
    let mut suggestions = esummary_mesh(&client, &mesh_uids).await?;

    // 4. Reorder: put descriptors that appear in the PubMed translation first.
    suggestions.sort_by_key(|s| {
        let pos = translated_names
            .iter()
            .position(|t| t.eq_ignore_ascii_case(&s.descriptor));
        pos.unwrap_or(usize::MAX)
    });

    // 5. Add pubmed_count for each descriptor (non-fatal if a single one fails)
    for s in suggestions.iter_mut() {
        s.pubmed_count = pubmed_count_for_mesh(&client, &s.descriptor).await;
    }

    Ok(suggestions)
}

// ── Tests ─────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_extract_mesh_descriptors_simple() {
        let query = r#""Hidradenitis Suppurativa"[MeSH Terms] OR "Hidradenitis"[MeSH Terms]"#;
        let mut out = Vec::new();
        extract_mesh_descriptors_from_query(query, &mut out);
        assert_eq!(out.len(), 2);
        assert_eq!(out[0], "Hidradenitis Suppurativa");
        assert_eq!(out[1], "Hidradenitis");
    }

    #[test]
    fn test_extract_mesh_descriptors_majr() {
        let query = r#""Skin Diseases"[majr]"#;
        let mut out = Vec::new();
        extract_mesh_descriptors_from_query(query, &mut out);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0], "Skin Diseases");
    }

    #[test]
    fn test_extract_mesh_descriptors_no_mesh() {
        let query = r#"hidradenitis AND suppurativa"#;
        let mut out = Vec::new();
        extract_mesh_descriptors_from_query(query, &mut out);
        assert!(out.is_empty());
    }

    #[test]
    fn test_esummary_parse_descriptor_record() {
        // Minimal esummary JSON matching the real NCBI format
        let json_body = r#"{
            "header": {"type":"esummary","version":"0.3"},
            "result": {
                "uids": ["68017497"],
                "68017497": {
                    "uid": "68017497",
                    "ds_yearintroduced": "1993",
                    "ds_scopenote": "A chronic suppurative disease.",
                    "ds_registrynumber": "",
                    "ds_headingmappedto": "",
                    "ds_meshterms": ["Hidradenitis Suppurativa", "Suppurative Hidradenitis"],
                    "ds_subheading": ["blood", "genetics", "surgery"],
                    "ds_papx": [],
                    "ds_previousindexing": [],
                    "ds_seerelated": [],
                    "ds_palist": [],
                    "ds_idxlinks": [
                        {"parent": 1, "treenum": "C17.800.838.765.420", "children": []},
                        {"parent": 2, "treenum": "C01.150.252.819.420", "children": []}
                    ],
                    "ds_entrydate": "1/01/01 00:00",
                    "ds_revisiondate": "1/01/01 00:00",
                    "ds_headingmappedtolist": [],
                    "ds_recordtype": "descriptor",
                    "ds_meshui": "D017497"
                }
            }
        }"#;

        let resp: EsummaryResponse = serde_json::from_str(json_body).unwrap();
        assert_eq!(resp.result.uids, vec!["68017497"]);
        let rec = &resp.result.records["68017497"];
        assert_eq!(rec.ds_meshterms[0], "Hidradenitis Suppurativa");
        assert_eq!(rec.ds_meshui, "D017497");
        assert_eq!(rec.ds_subheading.len(), 3);
        assert_eq!(rec.ds_idxlinks.len(), 2);
        assert_eq!(rec.ds_idxlinks[0].treenum, "C17.800.838.765.420");
    }

    #[test]
    fn test_supplemental_records_skipped() {
        let json_body = r#"{
            "header": {"type":"esummary","version":"0.3"},
            "result": {
                "uids": ["67538118"],
                "67538118": {
                    "uid": "67538118",
                    "ds_yearintroduced": "",
                    "ds_scopenote": "",
                    "ds_registrynumber": "",
                    "ds_headingmappedto": "",
                    "ds_meshterms": ["Hidradenitis suppurativa, familial"],
                    "ds_subheading": [],
                    "ds_papx": [],
                    "ds_previousindexing": [],
                    "ds_seerelated": [],
                    "ds_palist": [],
                    "ds_idxlinks": [{"parent": 1, "treenum": "@181678", "children": []}],
                    "ds_entrydate": "1/01/01 00:00",
                    "ds_revisiondate": "1/01/01 00:00",
                    "ds_headingmappedtolist": ["68017497"],
                    "ds_recordtype": "supplemental-record",
                    "ds_meshui": "C538118"
                }
            }
        }"#;

        let resp: EsummaryResponse = serde_json::from_str(json_body).unwrap();
        let rec = &resp.result.records["67538118"];
        // supplemental-record should be skipped in esummary_mesh
        assert_eq!(rec.ds_recordtype, "supplemental-record");
    }
}
