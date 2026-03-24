use pyo3::prelude::*;
use tokio::runtime::Runtime;

pub mod categorization;
pub mod db;
pub mod ncbi;
pub mod omics;
pub mod utils;

// ─── Shared result types ──────────────────────────────────────────────────────

#[pyclass]
#[derive(Clone)]
pub struct IngestionResult {
    #[pyo3(get, set)]
    pub records_processed: u64,
    #[pyo3(get, set)]
    pub records_inserted: u64,
    #[pyo3(get, set)]
    pub records_updated: u64,
    #[pyo3(get, set)]
    pub errors: Vec<String>,
}

#[pymethods]
impl IngestionResult {
    #[new]
    fn new() -> Self {
        IngestionResult {
            records_processed: 0,
            records_inserted: 0,
            records_updated: 0,
            errors: Vec::new(),
        }
    }
}

#[pyclass]
#[derive(Clone)]
pub struct OmicsResult {
    #[pyo3(get, set)]
    pub datasets_processed: u64,
    #[pyo3(get, set)]
    pub datasets_inserted: u64,
    #[pyo3(get, set)]
    pub links_inserted: u64,
    #[pyo3(get, set)]
    pub errors: Vec<String>,
}

#[pymethods]
impl OmicsResult {
    #[new]
    fn new() -> Self {
        OmicsResult {
            datasets_processed: 0,
            datasets_inserted: 0,
            links_inserted: 0,
            errors: Vec::new(),
        }
    }
}

// ─── PubMed ingestion (Phase 2 — stub) ───────────────────────────────────────

/// Dispara a busca e ingestão no PubMed.
#[pyfunction]
#[pyo3(signature = (job_id, query, db_url, date_from=None, date_to=None, ncbi_api_key=None))]
fn search_and_ingest_pubmed(
    job_id: String,
    query: String,
    db_url: String,
    date_from: Option<u16>,
    date_to: Option<u16>,
    ncbi_api_key: Option<String>,
) -> PyResult<IngestionResult> {
    let rt = Runtime::new().unwrap();

    let result: Result<IngestionResult, String> = rt.block_on(async {
        let client = match crate::db::connection::connect_db(&db_url).await {
            Ok(c) => c,
            Err(e) => return Err(format!("DB connection failed: {}", e)),
        };

        crate::db::job_tracker::update_job_status(&client, &job_id, "running", 0, None).await?;

        // Phase 2 implementation (ncbi/parser.rs) not yet complete.
        let _ = (query, date_from, date_to, ncbi_api_key);

        crate::db::job_tracker::update_job_status(&client, &job_id, "completed", 0, None).await?;

        Ok(IngestionResult {
            records_processed: 0,
            records_inserted: 0,
            records_updated: 0,
            errors: vec![],
        })
    });

    match result {
        Ok(res) => Ok(res),
        Err(e) => Err(pyo3::exceptions::PyRuntimeError::new_err(e)),
    }
}

// ─── Omics metadata ingestion (Phase 3) ──────────────────────────────────────

/// Triggers omics metadata search and ingestion for GEO, SRA, BioProject,
/// and/or GWAS Catalog. Results are stored in `core_omicdataset` and
/// `core_datasetpaperlink`.
///
/// # Arguments
/// * `job_id`         - UUID of the IngestionJob to track
/// * `query`          - Search term (e.g. "cardiovascular disease")
/// * `db_url`         - PostgreSQL connection string
/// * `sources`        - Subset of ["geo", "sra", "bioproject", "gwas"]
/// * `max_per_source` - Maximum datasets to fetch per source (default: 500)
/// * `ncbi_api_key`   - Optional NCBI API key (raises rate limit 3→10 req/s)
#[pyfunction]
#[pyo3(signature = (job_id, query, db_url, sources, max_per_source=500, ncbi_api_key=None))]
fn search_and_ingest_omics(
    job_id: String,
    query: String,
    db_url: String,
    sources: Vec<String>,
    max_per_source: usize,
    ncbi_api_key: Option<String>,
) -> PyResult<OmicsResult> {
    let rt = Runtime::new().unwrap();

    let result: Result<OmicsResult, String> = rt.block_on(async {
        // 1. Connect and mark job running
        let db_client = match crate::db::connection::connect_db(&db_url).await {
            Ok(c) => c,
            Err(e) => return Err(format!("DB connection failed: {e}")),
        };
        crate::db::job_tracker::update_job_status(&db_client, &job_id, "running", 0, None).await?;

        // 2. Shared NCBI client (GEO, SRA, BioProject, elink)
        let ncbi_client = crate::ncbi::client::NcbiClient::new(ncbi_api_key);

        let mut all_datasets: Vec<crate::omics::models::OmicDatasetData> = Vec::new();
        let mut all_links: Vec<crate::omics::models::DatasetPaperLinkData> = Vec::new();
        let mut errors: Vec<String> = Vec::new();

        // 3. Fetch from each source (errors are non-fatal — partial results are kept)
        for source in &sources {
            match source.as_str() {
                "geo" => {
                    match crate::omics::geo_parser::fetch_geo_datasets(
                        &ncbi_client, &query, max_per_source,
                    )
                    .await
                    {
                        Ok((datasets, links)) => {
                            let needs_elink =
                                crate::omics::geo_parser::datasets_without_pmids(&datasets, &links);
                            all_datasets.extend(datasets);
                            all_links.extend(links);
                            if !needs_elink.is_empty() {
                                match crate::omics::elink::discover_links_via_elink(
                                    &ncbi_client, &needs_elink, "gds",
                                )
                                .await
                                {
                                    Ok(elinks) => all_links.extend(elinks),
                                    Err(e) => errors.push(format!("GEO elink error: {e}")),
                                }
                            }
                        }
                        Err(e) => errors.push(format!("GEO fetch error: {e}")),
                    }
                }
                "sra" => {
                    match crate::omics::sra_parser::fetch_sra_datasets(
                        &ncbi_client, &query, max_per_source,
                    )
                    .await
                    {
                        Ok((datasets, links)) => {
                            let needs_elink =
                                crate::omics::sra_parser::datasets_without_pmids(&datasets, &links);
                            all_datasets.extend(datasets);
                            all_links.extend(links);
                            if !needs_elink.is_empty() {
                                match crate::omics::elink::discover_links_via_elink(
                                    &ncbi_client, &needs_elink, "sra",
                                )
                                .await
                                {
                                    Ok(elinks) => all_links.extend(elinks),
                                    Err(e) => errors.push(format!("SRA elink error: {e}")),
                                }
                            }
                        }
                        Err(e) => errors.push(format!("SRA fetch error: {e}")),
                    }
                }
                "bioproject" => {
                    match crate::omics::bioproject_parser::fetch_bioproject_datasets(
                        &ncbi_client, &query, max_per_source,
                    )
                    .await
                    {
                        Ok((datasets, links)) => {
                            let needs_elink =
                                crate::omics::bioproject_parser::datasets_without_pmids(
                                    &datasets, &links,
                                );
                            all_datasets.extend(datasets);
                            all_links.extend(links);
                            if !needs_elink.is_empty() {
                                match crate::omics::elink::discover_links_via_elink(
                                    &ncbi_client, &needs_elink, "bioproject",
                                )
                                .await
                                {
                                    Ok(elinks) => all_links.extend(elinks),
                                    Err(e) => errors.push(format!("BioProject elink error: {e}")),
                                }
                            }
                        }
                        Err(e) => errors.push(format!("BioProject fetch error: {e}")),
                    }
                }
                "gwas" => {
                    match crate::omics::gwas_parser::fetch_gwas_datasets(&query, max_per_source)
                        .await
                    {
                        Ok((datasets, links)) => {
                            all_datasets.extend(datasets);
                            all_links.extend(links);
                            // GWAS Catalog embeds PMIDs directly; no elink needed
                        }
                        Err(e) => errors.push(format!("GWAS fetch error: {e}")),
                    }
                }
                unknown => {
                    errors.push(format!(
                        "Unknown source: '{unknown}'. Valid: geo, sra, bioproject, gwas"
                    ));
                }
            }
        }

        let datasets_processed = all_datasets.len() as u64;

        // 4. Bulk COPY datasets (fatal on DB error)
        let datasets_inserted =
            match crate::db::copy_writer::copy_omic_datasets(&db_client, &all_datasets).await {
                Ok(n) => n,
                Err(e) => {
                    let msg = format!("COPY datasets failed: {e}");
                    crate::db::job_tracker::update_job_status(
                        &db_client, &job_id, "failed", 0, Some(&msg),
                    )
                    .await?;
                    return Err(msg);
                }
            };

        // 5. Bulk COPY links (non-fatal — links are supplementary)
        let links_inserted =
            match crate::db::copy_writer::copy_dataset_paper_links(&db_client, &all_links).await {
                Ok(n) => n,
                Err(e) => {
                    errors.push(format!("COPY links failed: {e}"));
                    0
                }
            };

        // 6. Mark job completed
        crate::db::job_tracker::update_job_status(
            &db_client,
            &job_id,
            "completed",
            datasets_inserted as i32,
            None,
        )
        .await?;

        Ok(OmicsResult {
            datasets_processed,
            datasets_inserted,
            links_inserted,
            errors,
        })
    });

    match result {
        Ok(res) => Ok(res),
        Err(e) => Err(pyo3::exceptions::PyRuntimeError::new_err(e)),
    }
}

// ─── Python module registration ───────────────────────────────────────────────

#[pymodule]
fn rust_engine(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<IngestionResult>()?;
    m.add_class::<OmicsResult>()?;
    m.add_function(wrap_pyfunction!(search_and_ingest_pubmed, m)?)?;
    m.add_function(wrap_pyfunction!(search_and_ingest_omics, m)?)?;
    Ok(())
}
