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

// ─── PubMed ingestion ─────────────────────────────────────────────────────────

/// Search PubMed via NCBI E-utilities and ingest results into the database.
///
/// Pipeline:
/// 1. esearch → list of PMIDs
/// 2. efetch in batches of 100 → PubMed XML
/// 3. parse XML → Vec<PaperData>
/// 4. NER on abstracts (genes)
/// 5. copy_papers (upsert) + child tables
/// 6. link_project_papers (core_projectpaper)
/// 7. update job status → completed
#[pyfunction]
#[pyo3(signature = (job_id, query, db_url, project_id, date_from=None, date_to=None, ncbi_api_key=None))]
fn search_and_ingest_pubmed(
    job_id: String,
    query: String,
    db_url: String,
    project_id: String,
    date_from: Option<u16>,
    date_to: Option<u16>,
    ncbi_api_key: Option<String>,
) -> PyResult<IngestionResult> {
    let rt = Runtime::new().unwrap();

    let result: Result<IngestionResult, String> = rt.block_on(async {
        // 1. Connect to DB and mark job running
        let client = match crate::db::connection::connect_db(&db_url).await {
            Ok(c) => c,
            Err(e) => return Err(format!("DB connection failed: {e}")),
        };
        let project_id_uuid = uuid::Uuid::parse_str(&project_id)
            .map_err(|e| format!("Invalid project_id UUID '{}': {e}", project_id))?;

        crate::db::job_tracker::update_job_status(&client, &job_id, "running", 0, 0, 0, None).await?;

        // 2. esearch → PMID list
        // Without API key NCBI allows 3 req/s → cap at 5 000 to finish in ~30 min.
        // With API key it allows 10 req/s → cap at 10 000.
        let has_api_key = ncbi_api_key.is_some();
        let max_results: usize = if has_api_key { 10_000 } else { 5_000 };

        let ncbi = crate::ncbi::client::NcbiClient::new(ncbi_api_key);
        let (pmids, total_hits) =
            crate::ncbi::fetch::esearch_pubmed(&ncbi, &query, date_from, date_to, max_results)
                .await
                .map_err(|e| format!("esearch failed: {e}"))?;

        if pmids.is_empty() {
            crate::db::job_tracker::update_job_status(&client, &job_id, "completed", 0, 0, 0, None)
                .await?;
            return Ok(IngestionResult {
                records_processed: 0,
                records_inserted: 0,
                records_updated: 0,
                errors: if total_hits > max_results {
                    vec![format!(
                        "Query matched {} results; retrieved {} (limit). Add an NCBI API key to raise the limit.",
                        total_hits, max_results
                    )]
                } else {
                    vec![]
                },
            });
        }

        let mut errors: Vec<String> = Vec::new();
        if total_hits > max_results {
            errors.push(format!(
                "Query matched {} results; retrieved {} (limit {}). Add an NCBI API key to raise the limit.",
                total_hits, pmids.len(), max_results
            ));
        }

        // 3. efetch → XML → PaperData (rate-limited)
        let xml = crate::ncbi::fetch::efetch_pubmed_xml(&ncbi, &pmids, has_api_key)
            .await
            .map_err(|e| format!("efetch failed: {e}"))?;

        let papers = crate::ncbi::parser::parse_pubmed_xml(&xml)
            .map_err(|e| format!("XML parse failed: {e}"))?;

        let records_processed = papers.len() as u64;

        // 4. Gene NER on abstracts
        let mut gene_mentions: Vec<(i64, String, i32)> = Vec::new();
        for paper in &papers {
            let genes = crate::categorization::gene_ner::extract_genes(&paper.abstract_text);
            for (symbol, count) in genes {
                gene_mentions.push((paper.pmid, symbol, count));
            }
        }

        // 4b. Drug NER on abstracts
        let mut drug_mentions: Vec<(i64, String, String, i32)> = Vec::new();
        for paper in &papers {
            let drugs = crate::categorization::drug_ner::extract_drugs(&paper.abstract_text);
            for (name, name_lower, count) in drugs {
                drug_mentions.push((paper.pmid, name, name_lower, count));
            }
        }

        // 5. Bulk upsert papers + resolve pmid→paper_id
        let (records_inserted, pmid_to_id) =
            crate::db::copy_writer::copy_papers(&client, &papers)
                .await
                .map_err(|e| format!("copy_papers failed: {:?}", e))?;

        // 6. Child tables (non-fatal)

        if let Err(e) =
            crate::db::copy_writer::copy_paper_authors(&client, &papers, &pmid_to_id).await
        {
            errors.push(format!("copy_paper_authors: {:?}", e));
        }
        if let Err(e) =
            crate::db::copy_writer::copy_paper_keywords(&client, &papers, &pmid_to_id).await
        {
            errors.push(format!("copy_paper_keywords: {:?}", e));
        }
        if let Err(e) =
            crate::db::copy_writer::copy_paper_mesh(&client, &papers, &pmid_to_id).await
        {
            errors.push(format!("copy_paper_mesh: {:?}", e));
        }
        if let Err(e) =
            crate::db::copy_writer::copy_paper_genes(&client, &gene_mentions, &pmid_to_id).await
        {
            errors.push(format!("copy_paper_genes: {:?}", e));
        }
        if let Err(e) =
            crate::db::copy_writer::copy_paper_drugs(&client, &drug_mentions, &pmid_to_id).await
        {
            errors.push(format!("copy_paper_drugs: {:?}", e));
        }

        // 6b. Resolve any pending dataset-paper links (from prior omics runs)
        if let Err(e) = crate::db::copy_writer::resolve_pending_links(&client).await {
            errors.push(format!("resolve_pending_links: {:?}", e));
        }

        // 7. Link papers to project
        let pmid_list: Vec<i64> = papers.iter().map(|p| p.pmid).collect();
        if let Err(e) =
            crate::db::copy_writer::link_project_papers(&client, project_id_uuid, &pmid_list).await
        {
            errors.push(format!("link_project_papers: {:?}", e));
        }

        // 8. Mark job completed
        let records_updated = records_processed.saturating_sub(records_inserted);
        let err_summary = if errors.is_empty() { None } else { Some(errors.join("; ")) };
        crate::db::job_tracker::update_job_status(
            &client,
            &job_id,
            "completed",
            records_processed as i32,
            records_inserted as i32,
            records_updated as i32,
            err_summary.as_deref(),
        )
        .await?;

        Ok(IngestionResult {
            records_processed,
            records_inserted,
            records_updated: records_processed.saturating_sub(records_inserted),
            errors,
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
/// * `project_id`     - UUID of the DaVinciProject to link datasets to
/// * `sources`        - Subset of ["geo", "sra", "bioproject", "gwas"]
/// * `max_per_source` - Maximum datasets to fetch per source (default: 500)
/// * `ncbi_api_key`   - Optional NCBI API key (raises rate limit 3→10 req/s)
#[pyfunction]
#[pyo3(signature = (job_id, query, db_url, project_id, sources, max_per_source=10_000, ncbi_api_key=None, synonyms=None))]
fn search_and_ingest_omics(
    job_id: String,
    query: String,
    db_url: String,
    project_id: String,
    sources: Vec<String>,
    max_per_source: usize,
    ncbi_api_key: Option<String>,
    synonyms: Option<Vec<String>>,
) -> PyResult<OmicsResult> {
    let rt = Runtime::new().unwrap();

    let result: Result<OmicsResult, String> = rt.block_on(async {
        // 1. Connect and mark job running
        let db_client = match crate::db::connection::connect_db(&db_url).await {
            Ok(c) => c,
            Err(e) => return Err(format!("DB connection failed: {e}")),
        };
        let project_id_uuid = uuid::Uuid::parse_str(&project_id)
            .map_err(|e| format!("Invalid project_id UUID '{}': {e}", project_id))?;

        crate::db::job_tracker::update_job_status(&db_client, &job_id, "running", 0, 0, 0, None).await?;

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
                    // GWAS EBI API uses exact trait matching — try query + each synonym
                    let mut gwas_terms: Vec<String> = vec![query.clone()];
                    if let Some(ref syns) = synonyms {
                        for s in syns {
                            if !s.is_empty() && s != &query {
                                gwas_terms.push(s.clone());
                            }
                        }
                    }
                    let per_term = (max_per_source / gwas_terms.len().max(1)).max(1);
                    for term in &gwas_terms {
                        match crate::omics::gwas_parser::fetch_gwas_datasets(term, per_term)
                            .await
                        {
                            Ok((datasets, links)) => {
                                all_datasets.extend(datasets);
                                all_links.extend(links);
                            }
                            Err(e) => errors.push(format!("GWAS fetch error for '{}': {e}", term)),
                        }
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
                    let msg = format!("COPY datasets failed: {:?}", e);
                    crate::db::job_tracker::update_job_status(
                        &db_client, &job_id, "failed", 0, 0, 0, Some(&msg),
                    )
                    .await?;
                    return Err(msg);
                }
            };

        // 5. Link datasets to project (non-fatal)
        let accessions: Vec<String> = all_datasets.iter().map(|d| d.accession.clone()).collect();
        if let Err(e) = crate::db::copy_writer::link_project_datasets(
            &db_client, project_id_uuid, &accessions,
        )
        .await
        {
            errors.push(format!("link_project_datasets: {:?}", e));
        }

        // 6. Store links as pending (deferred FK resolution)
        // Also try direct insertion for links where both FKs exist
        let mut links_inserted = 0u64;

        // First store all as pending
        if let Err(e) = crate::db::copy_writer::store_pending_links(&db_client, &all_links).await {
            errors.push(format!("store_pending_links failed: {:?}", e));
        }

        // Then try to resolve any that can be resolved now
        match crate::db::copy_writer::resolve_pending_links(&db_client).await {
            Ok(n) => links_inserted = n,
            Err(e) => errors.push(format!("resolve_pending_links failed: {e}")),
        }

        // 7. Mark job completed
        let err_summary = if errors.is_empty() { None } else { Some(errors.join("; ")) };
        crate::db::job_tracker::update_job_status(
            &db_client,
            &job_id,
            "completed",
            datasets_processed as i32,
            datasets_inserted as i32,
            0,
            err_summary.as_deref(),
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

// ─── Pending link resolution ──────────────────────────────────────────────────

/// Resolve pending dataset-paper links that were stored during omics ingestion.
///
/// Call this after both PubMed and omics ingestion have completed to ensure
/// all FK references can be resolved.
#[pyfunction]
fn resolve_pending_links(db_url: String) -> PyResult<u64> {
    let rt = Runtime::new().unwrap();

    let result: Result<u64, String> = rt.block_on(async {
        let client = crate::db::connection::connect_db(&db_url)
            .await
            .map_err(|e| format!("DB connection failed: {e}"))?;

        crate::db::copy_writer::resolve_pending_links(&client)
            .await
            .map_err(|e| format!("resolve_pending_links failed: {e}"))
    });

    match result {
        Ok(n) => Ok(n),
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
    m.add_function(wrap_pyfunction!(resolve_pending_links, m)?)?;
    Ok(())
}
