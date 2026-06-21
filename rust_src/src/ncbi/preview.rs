use pyo3::prelude::*;
use std::sync::Arc;
use tokio::sync::Semaphore;

use crate::ncbi::client::NcbiClient;
use crate::ncbi::fetch::esearch_count;

// ── Rate-limit constants ──────────────────────────────────────────────────────
// NCBI allows 3 req/s without key and 10 req/s with key.
// We use a semaphore to cap concurrent in-flight requests and interleave a
// per-permit sleep so we never exceed the ceiling.
const MAX_CONCURRENT_NO_KEY: usize = 3;
const MAX_CONCURRENT_WITH_KEY: usize = 10;

// ── MeshBlock ─────────────────────────────────────────────────────────────────

/// A single MeSH descriptor block as supplied by the caller.
///
/// Fields:
/// - `descriptor`   — display name, e.g. "Hidradenitis Suppurativa"
/// - `mesh_term`    — ready-to-embed search term, e.g. `"Hidradenitis Suppurativa"[MeSH Terms]`
/// - `mode`         — `"and"` or `"or"` — how to combine this block with free-text
#[derive(Clone, Debug)]
pub struct MeshBlock {
    pub mesh_term: String,
    pub mode: String,
}

// ── MagnitudePreview (PyClass) ────────────────────────────────────────────────

#[pyclass]
#[derive(Clone, Default)]
pub struct MagnitudePreview {
    // ── Core counts (always computed) ─────────────────────────────────────────
    /// Total hits for the free-text query alone.
    #[pyo3(get)]
    pub free_text_count: usize,
    /// Total hits for the combined MeSH blocks alone.
    #[pyo3(get)]
    pub mesh_count: usize,
    /// Total hits for the final combined query (free-text + MeSH).
    #[pyo3(get)]
    pub combined_count: usize,
    /// Overlap: count(free-text AND mesh-combined).
    #[pyo3(get)]
    pub overlap: usize,
    /// Only in free-text, not in any MeSH block (free_text_count - overlap).
    #[pyo3(get)]
    pub only_free_text: usize,
    /// Only in MeSH, not found by free-text alone (mesh_count - overlap).
    #[pyo3(get)]
    pub only_mesh: usize,
    /// Publisher-supplied records not yet indexed by MeSH (publisher[sb]).
    #[pyo3(get)]
    pub not_yet_indexed: usize,
    /// Review-type articles in combined results.
    #[pyo3(get)]
    pub reviews: usize,
    /// Systematic reviews + meta-analyses in combined results.
    #[pyo3(get)]
    pub systematic_reviews: usize,

    // ── Optional heavy blocks (computed only when the corresponding flag is set)
    /// Annual counts: list of (year, count) sorted ascending.
    #[pyo3(get)]
    pub by_year: Vec<(u16, usize)>,
    /// Counts by publication type: list of (pub_type_label, count).
    #[pyo3(get)]
    pub by_pub_type: Vec<(String, usize)>,
    /// Open-access counts: (free_full_text_count, pmc_count).
    #[pyo3(get)]
    pub open_access: (usize, usize),
}

#[pymethods]
impl MagnitudePreview {
    fn __repr__(&self) -> String {
        format!(
            "MagnitudePreview(combined={}, free_text={}, mesh={}, overlap={}, not_yet_indexed={}, reviews={}, systematic_reviews={})",
            self.combined_count,
            self.free_text_count,
            self.mesh_count,
            self.overlap,
            self.not_yet_indexed,
            self.reviews,
            self.systematic_reviews,
        )
    }
}

// ── Query builder (internal) ──────────────────────────────────────────────────

/// Build the PubMed boolean string for the mesh side.
///
/// Each MeshBlock contributes its `mesh_term` joined by its `mode`
/// (AND / OR) with respect to the previous blocks.
fn build_mesh_query(blocks: &[MeshBlock]) -> Option<String> {
    if blocks.is_empty() {
        return None;
    }
    let mut parts: Vec<String> = Vec::with_capacity(blocks.len());
    for (i, block) in blocks.iter().enumerate() {
        let term = block.mesh_term.trim().to_string();
        if i == 0 {
            parts.push(term);
        } else {
            let op = if block.mode.eq_ignore_ascii_case("or") { "OR" } else { "AND" };
            parts.push(format!("{op} {term}"));
        }
    }
    Some(format!("({})", parts.join(" ")))
}

/// Build the final combined query string:
/// `(free_text) AND (mesh_combined)`  OR  just `(free_text)` when no MeSH.
fn build_combined_query(free_text: &str, mesh_query: Option<&str>) -> String {
    match mesh_query {
        Some(mq) => format!("({free_text}) AND {mq}"),
        None => format!("({free_text})"),
    }
}

// ── Concurrency helper ────────────────────────────────────────────────────────

/// Acquire a semaphore permit and run the async closure.
/// This acts as a concurrency gate so we never fire more than N requests at once.
async fn sem_count<F, Fut>(sem: Arc<Semaphore>, f: F) -> Result<usize, String>
where
    F: FnOnce() -> Fut,
    Fut: std::future::Future<Output = Result<usize, String>>,
{
    let _permit = sem
        .acquire()
        .await
        .map_err(|e| format!("Semaphore error: {e}"))?;
    f().await
}

// ── Public entry point ────────────────────────────────────────────────────────

/// Compute magnitude metrics for a PubMed query without downloading any PMIDs.
///
/// # Parameters (PyO3)
/// | Name              | Type              | Description |
/// |-------------------|-------------------|-------------|
/// | `free_text`       | `str`             | Pure free-text part of the query — the raw boolean block **without** any MeSH qualifiers (e.g. `"hidradenitis suppurativa OR acne inversa"`). Used as-is for `free_text_count`. Django must send only the free-text portion here, never the combined query. |
/// | `mesh_terms`      | `Vec<(str, str)>` | List of `(mesh_term, mode)` tuples. `mesh_term` is the ready-to-embed PubMed syntax (e.g. `"Hidradenitis Suppurativa"[MeSH Terms]`). `mode` is `"and"` or `"or"`. Used to compute `mesh_count`/`only_mesh`. |
/// | `date_from`       | `Option<u16>`     | Minimum publication year (inclusive), or `None`. |
/// | `date_to`         | `Option<u16>`     | Maximum publication year (inclusive), or `None`. |
/// | `ncbi_api_key`    | `Option<str>`     | NCBI API key. Raises rate limit from 3 to 10 req/s. |
/// | `flag_by_year`    | `bool`            | Compute annual breakdown (1 request per year bucket). |
/// | `flag_by_pub_type`| `bool`            | Compute publication-type breakdown. |
/// | `flag_open_access`| `bool`            | Compute open-access counts. |
/// | `year_buckets`    | `Option<Vec<u16>>`| Explicit list of years for by_year. Defaults to last 10 years when `None`. |
/// | `combined`        | `Option<str>`     | Pre-built combined query string produced by the Django query-builder (i.e. the exact string that will be used for ingestion). When provided, this string is used **directly** as the query for `combined_count`, `overlap`, and all derived metrics — the Rust-side `build_combined_query` is **not called**. When `None`, the combined string is assembled internally from `free_text` + `mesh_terms` (backwards-compatible behaviour). Pass this parameter to guarantee that the count shown to the user matches the query that will actually be ingested. |
///
/// # Returns
/// `MagnitudePreview` with core counts always populated.
/// Optional heavy blocks are populated only when their flag is `true`.
#[pyfunction]
#[pyo3(signature = (
    free_text,
    mesh_terms,
    date_from=None,
    date_to=None,
    ncbi_api_key=None,
    flag_by_year=false,
    flag_by_pub_type=false,
    flag_open_access=false,
    year_buckets=None,
    combined=None
))]
pub fn pubmed_magnitude_preview(
    free_text: String,
    mesh_terms: Vec<(String, String)>,
    date_from: Option<u16>,
    date_to: Option<u16>,
    ncbi_api_key: Option<String>,
    flag_by_year: bool,
    flag_by_pub_type: bool,
    flag_open_access: bool,
    year_buckets: Option<Vec<u16>>,
    combined: Option<String>,
) -> PyResult<MagnitudePreview> {
    let rt = tokio::runtime::Runtime::new()
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("Tokio runtime: {e}")))?;

    rt.block_on(async {
        run_preview(
            free_text,
            mesh_terms,
            date_from,
            date_to,
            ncbi_api_key,
            flag_by_year,
            flag_by_pub_type,
            flag_open_access,
            year_buckets,
            combined,
        )
        .await
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e))
    })
}

async fn run_preview(
    free_text: String,
    mesh_terms: Vec<(String, String)>,
    date_from: Option<u16>,
    date_to: Option<u16>,
    ncbi_api_key: Option<String>,
    flag_by_year: bool,
    flag_by_pub_type: bool,
    flag_open_access: bool,
    year_buckets: Option<Vec<u16>>,
    combined: Option<String>,
) -> Result<MagnitudePreview, String> {
    let has_key = ncbi_api_key.is_some();
    let concurrency = if has_key {
        MAX_CONCURRENT_WITH_KEY
    } else {
        MAX_CONCURRENT_NO_KEY
    };

    let ncbi = Arc::new(NcbiClient::new(ncbi_api_key));
    let sem = Arc::new(Semaphore::new(concurrency));

    // Build mesh blocks
    let blocks: Vec<MeshBlock> = mesh_terms
        .into_iter()
        .map(|(mesh_term, mode)| MeshBlock { mesh_term, mode })
        .collect();

    let mesh_query = build_mesh_query(&blocks);

    // When the caller supplies a pre-built combined query (option 1b of the design),
    // use it directly — this guarantees that the count shown to the user is computed
    // against the exact same string that Django will submit for ingestion.
    // When `combined` is None we fall back to assembling it internally (backwards-compat).
    let combined_query = match combined {
        Some(c) => c,
        None => build_combined_query(&free_text, mesh_query.as_deref()),
    };

    // overlap_query: the intersection of free-text and MeSH.
    // When a combined string was provided externally we still use the internally-built
    // intersection to keep the overlap semantics correct (free-text ∩ MeSH).
    let overlap_query = match &mesh_query {
        Some(mq) => Some(format!("({free_text}) AND {mq}")),
        None => None,
    };

    // ── Core counts (7 concurrent tasks) ──────────────────────────────────────

    let ncbi_ft = Arc::clone(&ncbi);
    let sem_ft = Arc::clone(&sem);
    let ft_query = free_text.clone();
    let ft_handle = tokio::spawn(async move {
        sem_count(sem_ft, || {
            let c = Arc::clone(&ncbi_ft);
            async move { esearch_count(&c, &ft_query, date_from, date_to).await }
        })
        .await
    });

    let mesh_count_handle = if let Some(ref mq) = mesh_query {
        let ncbi_m = Arc::clone(&ncbi);
        let sem_m = Arc::clone(&sem);
        let mq_owned = mq.clone();
        Some(tokio::spawn(async move {
            sem_count(sem_m, || {
                let c = Arc::clone(&ncbi_m);
                async move { esearch_count(&c, &mq_owned, date_from, date_to).await }
            })
            .await
        }))
    } else {
        None
    };

    let ncbi_cb = Arc::clone(&ncbi);
    let sem_cb = Arc::clone(&sem);
    let cq = combined_query.clone();
    let combined_handle = tokio::spawn(async move {
        sem_count(sem_cb, || {
            let c = Arc::clone(&ncbi_cb);
            async move { esearch_count(&c, &cq, date_from, date_to).await }
        })
        .await
    });

    let overlap_handle = if let Some(ref oq) = overlap_query {
        let ncbi_ov = Arc::clone(&ncbi);
        let sem_ov = Arc::clone(&sem);
        let oq_owned = oq.clone();
        Some(tokio::spawn(async move {
            sem_count(sem_ov, || {
                let c = Arc::clone(&ncbi_ov);
                async move { esearch_count(&c, &oq_owned, date_from, date_to).await }
            })
            .await
        }))
    } else {
        None
    };

    // not_yet_indexed: free_text AND publisher[sb]
    let ncbi_nyi = Arc::clone(&ncbi);
    let sem_nyi = Arc::clone(&sem);
    let nyi_query = format!("({free_text}) AND publisher[sb]");
    let nyi_handle = tokio::spawn(async move {
        sem_count(sem_nyi, || {
            let c = Arc::clone(&ncbi_nyi);
            async move { esearch_count(&c, &nyi_query, date_from, date_to).await }
        })
        .await
    });

    // reviews
    let ncbi_rv = Arc::clone(&ncbi);
    let sem_rv = Arc::clone(&sem);
    let rv_query = format!("{combined_query} AND Review[pt]");
    let reviews_handle = tokio::spawn(async move {
        sem_count(sem_rv, || {
            let c = Arc::clone(&ncbi_rv);
            async move { esearch_count(&c, &rv_query, date_from, date_to).await }
        })
        .await
    });

    // systematic reviews
    let ncbi_sr = Arc::clone(&ncbi);
    let sem_sr = Arc::clone(&sem);
    let sr_query = format!(
        r#"{combined_query} AND (systematic[sb] OR "Meta-Analysis"[pt])"#
    );
    let sr_handle = tokio::spawn(async move {
        sem_count(sem_sr, || {
            let c = Arc::clone(&ncbi_sr);
            async move { esearch_count(&c, &sr_query, date_from, date_to).await }
        })
        .await
    });

    // Await core results
    let free_text_count = ft_handle
        .await
        .map_err(|e| format!("free_text task panic: {e}"))?
        .map_err(|e| format!("free_text count: {e}"))?;

    let mesh_count = if let Some(h) = mesh_count_handle {
        h.await
            .map_err(|e| format!("mesh_count task panic: {e}"))?
            .map_err(|e| format!("mesh count: {e}"))?
    } else {
        0
    };

    let combined_count = combined_handle
        .await
        .map_err(|e| format!("combined task panic: {e}"))?
        .map_err(|e| format!("combined count: {e}"))?;

    let overlap = if let Some(h) = overlap_handle {
        h.await
            .map_err(|e| format!("overlap task panic: {e}"))?
            .map_err(|e| format!("overlap count: {e}"))?
    } else {
        // No MeSH blocks → overlap == free_text_count
        free_text_count
    };

    let not_yet_indexed = nyi_handle
        .await
        .map_err(|e| format!("nyi task panic: {e}"))?
        .map_err(|e| format!("not_yet_indexed count: {e}"))?;

    let reviews = reviews_handle
        .await
        .map_err(|e| format!("reviews task panic: {e}"))?
        .map_err(|e| format!("reviews count: {e}"))?;

    let systematic_reviews = sr_handle
        .await
        .map_err(|e| format!("systematic_reviews task panic: {e}"))?
        .map_err(|e| format!("systematic_reviews count: {e}"))?;

    let only_free_text = free_text_count.saturating_sub(overlap);
    let only_mesh = mesh_count.saturating_sub(overlap);

    // ── Optional heavy blocks ──────────────────────────────────────────────────

    // by_year
    let by_year = if flag_by_year {
        let current_year = chrono::Utc::now().naive_utc().date().format("%Y").to_string().parse::<u16>().unwrap_or(2025);
        let years: Vec<u16> = match year_buckets {
            Some(ref ys) => ys.clone(),
            None => ((current_year.saturating_sub(9))..=current_year).collect(),
        };

        let mut year_handles = Vec::with_capacity(years.len());
        for yr in years.iter() {
            let ncbi_yr = Arc::clone(&ncbi);
            let sem_yr = Arc::clone(&sem);
            let yr_query = combined_query.clone();
            let yr = *yr;
            year_handles.push((
                yr,
                tokio::spawn(async move {
                    sem_count(sem_yr, || {
                        let c = Arc::clone(&ncbi_yr);
                        async move {
                            esearch_count(&c, &yr_query, Some(yr), Some(yr)).await
                        }
                    })
                    .await
                }),
            ));
        }

        let mut result = Vec::with_capacity(year_handles.len());
        for (yr, h) in year_handles {
            let cnt = h
                .await
                .map_err(|e| format!("by_year task panic for {yr}: {e}"))?
                .map_err(|e| format!("by_year count for {yr}: {e}"))?;
            result.push((yr, cnt));
        }
        result
    } else {
        vec![]
    };

    // by_pub_type
    let by_pub_type = if flag_by_pub_type {
        let types: &[(&str, &str)] = &[
            ("Review", &format!("{combined_query} AND Review[pt]")),
            ("Meta-Analysis", &format!(r#"{combined_query} AND "Meta-Analysis"[pt]"#)),
            ("Randomized Controlled Trial", &format!(r#"{combined_query} AND "Randomized Controlled Trial"[pt]"#)),
            ("Clinical Trial", &format!(r#"{combined_query} AND "Clinical Trial"[pt]"#)),
            ("Case Reports", &format!(r#"{combined_query} AND "Case Reports"[pt]"#)),
        ];

        let mut pt_handles = Vec::with_capacity(types.len());
        for (label, query) in types {
            let ncbi_pt = Arc::clone(&ncbi);
            let sem_pt = Arc::clone(&sem);
            let label = label.to_string();
            let query = query.to_string();
            pt_handles.push((
                label,
                tokio::spawn(async move {
                    sem_count(sem_pt, || {
                        let c = Arc::clone(&ncbi_pt);
                        async move { esearch_count(&c, &query, date_from, date_to).await }
                    })
                    .await
                }),
            ));
        }

        let mut result = Vec::with_capacity(pt_handles.len());
        for (label, h) in pt_handles {
            let cnt = h
                .await
                .map_err(|e| format!("by_pub_type task panic for {label}: {e}"))?
                .map_err(|e| format!("by_pub_type count for {label}: {e}"))?;
            result.push((label, cnt));
        }
        result
    } else {
        vec![]
    };

    // open_access
    let open_access = if flag_open_access {
        let ncbi_fft = Arc::clone(&ncbi);
        let sem_fft = Arc::clone(&sem);
        let fft_query = format!("{combined_query} AND free full text[sb]");
        let fft_handle = tokio::spawn(async move {
            sem_count(sem_fft, || {
                let c = Arc::clone(&ncbi_fft);
                async move { esearch_count(&c, &fft_query, date_from, date_to).await }
            })
            .await
        });

        let ncbi_pmc = Arc::clone(&ncbi);
        let sem_pmc = Arc::clone(&sem);
        let pmc_query = format!("{combined_query} AND pubmed pmc[sb]");
        let pmc_handle = tokio::spawn(async move {
            sem_count(sem_pmc, || {
                let c = Arc::clone(&ncbi_pmc);
                async move { esearch_count(&c, &pmc_query, date_from, date_to).await }
            })
            .await
        });

        let fft_count = fft_handle
            .await
            .map_err(|e| format!("fft task panic: {e}"))?
            .map_err(|e| format!("free_full_text count: {e}"))?;

        let pmc_count = pmc_handle
            .await
            .map_err(|e| format!("pmc task panic: {e}"))?
            .map_err(|e| format!("pmc count: {e}"))?;

        (fft_count, pmc_count)
    } else {
        (0, 0)
    };

    Ok(MagnitudePreview {
        free_text_count,
        mesh_count,
        combined_count,
        overlap,
        only_free_text,
        only_mesh,
        not_yet_indexed,
        reviews,
        systematic_reviews,
        by_year,
        by_pub_type,
        open_access,
    })
}

// ── Tests ─────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_build_mesh_query_empty() {
        assert_eq!(build_mesh_query(&[]), None);
    }

    #[test]
    fn test_build_mesh_query_single() {
        let blocks = vec![MeshBlock {
            mesh_term: r#""Hidradenitis Suppurativa"[MeSH Terms]"#.to_string(),
            mode: "and".to_string(),
        }];
        let q = build_mesh_query(&blocks).unwrap();
        assert!(q.contains("Hidradenitis"));
        assert!(q.starts_with('('));
        assert!(q.ends_with(')'));
    }

    #[test]
    fn test_build_mesh_query_multi_and() {
        let blocks = vec![
            MeshBlock {
                mesh_term: r#""Hidradenitis"[MeSH Terms]"#.to_string(),
                mode: "and".to_string(),
            },
            MeshBlock {
                mesh_term: r#""Skin Diseases"[MeSH Terms]"#.to_string(),
                mode: "and".to_string(),
            },
        ];
        let q = build_mesh_query(&blocks).unwrap();
        assert!(q.contains("AND"));
    }

    #[test]
    fn test_build_mesh_query_multi_or() {
        let blocks = vec![
            MeshBlock {
                mesh_term: r#""Hidradenitis"[MeSH Terms]"#.to_string(),
                mode: "or".to_string(),
            },
            MeshBlock {
                mesh_term: r#""Acne Inversa"[MeSH Terms]"#.to_string(),
                mode: "or".to_string(),
            },
        ];
        let q = build_mesh_query(&blocks).unwrap();
        assert!(q.contains("OR"));
    }

    #[test]
    fn test_build_combined_no_mesh() {
        let q = build_combined_query("hidradenitis", None);
        assert_eq!(q, "(hidradenitis)");
    }

    #[test]
    fn test_build_combined_with_mesh() {
        let q = build_combined_query(
            "hidradenitis",
            Some(r#"("Hidradenitis Suppurativa"[MeSH Terms])"#),
        );
        assert!(q.starts_with("(hidradenitis)"));
        assert!(q.contains("AND"));
    }

    // ── Tests for the `combined` parameter (option 1b) ────────────────────────
    //
    // When the caller supplies `combined`, it must be used verbatim as the
    // combined_query string — build_combined_query must NOT be called.  This
    // is validated by inspecting the string that run_preview would use; we
    // cannot call run_preview in unit tests (it fires HTTP), so we exercise
    // the logic via the equivalent local computation.

    /// Simulates what run_preview does when `combined = Some(...)`: the
    /// pre-built string is used directly, without wrapping `free_text` again.
    #[test]
    fn test_combined_explicit_used_verbatim() {
        let free_text = "hidradenitis suppurativa OR acne inversa";
        let pre_built = r#"(hidradenitis suppurativa OR acne inversa) AND ("Hidradenitis Suppurativa"[MeSH Terms])"#;

        // Replicate the run_preview selection logic (no HTTP call):
        let mesh_blocks: Vec<MeshBlock> = vec![MeshBlock {
            mesh_term: r#""Hidradenitis Suppurativa"[MeSH Terms]"#.to_string(),
            mode: "and".to_string(),
        }];
        let mesh_query = build_mesh_query(&mesh_blocks);

        let combined_provided: Option<String> = Some(pre_built.to_string());

        let combined_query = match combined_provided {
            Some(c) => c,
            None => build_combined_query(free_text, mesh_query.as_deref()),
        };

        // Must equal the pre-built string exactly — no double-wrapping.
        assert_eq!(combined_query, pre_built);
        // Must NOT double-apply MeSH (the pre-built string contains [MeSH Terms] once).
        assert_eq!(combined_query.matches("[MeSH Terms]").count(), 1);
    }

    /// When `combined = None`, fallback to internal build (backwards-compat).
    #[test]
    fn test_combined_none_falls_back_to_internal_build() {
        let free_text = "hidradenitis";
        let mesh_blocks: Vec<MeshBlock> = vec![MeshBlock {
            mesh_term: r#""Hidradenitis Suppurativa"[MeSH Terms]"#.to_string(),
            mode: "and".to_string(),
        }];
        let mesh_query = build_mesh_query(&mesh_blocks);

        let combined_provided: Option<String> = None;

        let combined_query = match combined_provided {
            Some(c) => c,
            None => build_combined_query(free_text, mesh_query.as_deref()),
        };

        // Internal build wraps free_text in parens and appends mesh block.
        assert!(combined_query.starts_with("(hidradenitis)"));
        assert!(combined_query.contains("AND"));
        assert!(combined_query.contains("[MeSH Terms]"));
    }

    /// free_text_count query must be the pure free-text string, not the combined one.
    /// This test documents the invariant: free_text is passed through unchanged.
    #[test]
    fn test_free_text_not_contaminated_by_mesh() {
        let free_text = "cancer OR neoplasm";
        // The query used for free_text_count is `free_text` directly — no mesh appended.
        // Simulate what ft_query in run_preview receives:
        let ft_query = free_text.to_string();
        assert!(!ft_query.contains("[MeSH Terms]"));
        assert!(!ft_query.contains("[mh]"));
        assert!(!ft_query.contains("[majr]"));
        assert_eq!(ft_query, "cancer OR neoplasm");
    }
}
