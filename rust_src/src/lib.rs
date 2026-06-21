use pyo3::prelude::*;
use tokio::runtime::Runtime;

// Re-export PyO3 types from sub-modules so they are visible at crate root for the pymodule.
pub use crate::ncbi::mesh::{mesh_suggest, MeshSuggestion};
pub use crate::ncbi::preview::{pubmed_magnitude_preview, MagnitudePreview};

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

        // 4c. Variant NER on abstracts (rs-numbers)
        let mut variant_mentions: Vec<(i64, String, i32)> = Vec::new();
        for paper in &papers {
            let variants =
                crate::categorization::variant_ner::extract_variants(&paper.abstract_text);
            for (rs_number, count) in variants {
                variant_mentions.push((paper.pmid, rs_number, count));
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
        if let Err(e) =
            crate::db::copy_writer::copy_paper_variants(&client, &variant_mentions, &pmid_to_id)
                .await
        {
            errors.push(format!("copy_paper_variants: {:?}", e));
        }

        // 6b. Resolve any pending dataset-paper links (from prior omics runs)
        if let Err(e) = crate::db::copy_writer::resolve_pending_links(&client).await {
            errors.push(format!("resolve_pending_links: {:?}", e));
        }

        // 7. Link papers to project
        let pmid_list: Vec<i64> = papers.iter().map(|p| p.pmid).collect();
        if let Err(e) =
            crate::db::copy_writer::link_project_papers(&client, project_id_uuid, &pmid_list, &job_id).await
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
            &db_client, project_id_uuid, &accessions, &job_id,
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

// ─── PRIDE Archive ingestion ──────────────────────────────────────────────────

/// Search PRIDE Archive proteomics datasets and ingest results into the database.
///
/// Pipeline:
/// 1. GET /search/projects?keyword=<query> (paginado) → lista de accessions
/// 2. Para cada accession: GET /projects/{accession} → metadado completo
/// 3. Para cada accession: GET /projects/{accession}/files → matrix_pointer + modality
/// 4. Derivação: omics_layers=["proteomic"], omics_count=1, access_type="public"
///    data_format: COMPLETE→"processed", PARTIAL→"raw"
/// 5. copy_omic_datasets (COPY bulk upsert com anti-clobber COALESCE/NULLIF)
/// 6. link_project_datasets (core_projectdataset)
/// 7. store_pending_links + resolve_pending_links (paper↔dataset)
/// 8. update IngestionJob → completed
///
/// # Arguments
///
/// | Parameter      | Type          | Description |
/// |----------------|---------------|-------------|
/// | `job_id`       | `str`         | UUID do IngestionJob (pride_search). |
/// | `query`        | `str`         | Termo de busca (ex: "hidradenitis suppurativa"). |
/// | `db_url`       | `str`         | PostgreSQL connection string. |
/// | `project_id`   | `str`         | UUID do DaVinciProject para vincular datasets. |
/// | `max_results`  | `int`         | Máximo de datasets PRIDE a ingerir (default: 500). |
///
/// # Returns
///
/// `OmicsResult { datasets_processed, datasets_inserted, links_inserted, errors }`.
/// Raises `PyRuntimeError` apenas em erro fatal (DB, COPY).
#[pyfunction]
#[pyo3(signature = (job_id, query, db_url, project_id, max_results=500))]
fn search_and_ingest_pride(
    job_id: String,
    query: String,
    db_url: String,
    project_id: String,
    max_results: usize,
) -> PyResult<OmicsResult> {
    let rt = Runtime::new().unwrap();

    let result: Result<OmicsResult, String> = rt.block_on(async {
        // 1. Conectar ao banco e marcar job como running
        let db_client = match crate::db::connection::connect_db(&db_url).await {
            Ok(c) => c,
            Err(e) => return Err(format!("DB connection failed: {e}")),
        };
        let project_id_uuid = uuid::Uuid::parse_str(&project_id)
            .map_err(|e| format!("Invalid project_id UUID '{}': {e}", project_id))?;

        crate::db::job_tracker::update_job_status(&db_client, &job_id, "running", 0, 0, 0, None)
            .await?;

        let mut errors: Vec<String> = Vec::new();

        // 2. Fetch PRIDE Archive
        let (all_datasets, all_links) =
            match crate::omics::pride_parser::fetch_pride_datasets(&query, max_results).await {
                Ok((d, l)) => (d, l),
                Err(e) => {
                    let msg = format!("PRIDE fetch error: {e}");
                    crate::db::job_tracker::update_job_status(
                        &db_client, &job_id, "failed", 0, 0, 0, Some(&msg),
                    )
                    .await?;
                    return Err(msg);
                }
            };

        let datasets_processed = all_datasets.len() as u64;

        // 3. Bulk COPY datasets (fatal em erro de DB)
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

        // 4. Vincular datasets ao projeto
        let accessions: Vec<String> = all_datasets.iter().map(|d| d.accession.clone()).collect();
        if let Err(e) = crate::db::copy_writer::link_project_datasets(
            &db_client, project_id_uuid, &accessions, &job_id,
        )
        .await
        {
            errors.push(format!("link_project_datasets: {:?}", e));
        }

        // 5. Armazenar links pendentes + resolver imediatamente os que puderem
        let mut links_inserted = 0u64;

        if let Err(e) = crate::db::copy_writer::store_pending_links(&db_client, &all_links).await {
            errors.push(format!("store_pending_links failed: {:?}", e));
        }

        match crate::db::copy_writer::resolve_pending_links(&db_client).await {
            Ok(n) => links_inserted = n,
            Err(e) => errors.push(format!("resolve_pending_links failed: {e}")),
        }

        // 6. Marcar job como completed
        let err_summary = if errors.is_empty() {
            None
        } else {
            Some(errors.join("; "))
        };
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

// ─── Sample ingestion (Op 4.2) ────────────────────────────────────────────────

/// Result type returned by `ingest_samples_for_dataset`.
#[pyclass]
#[derive(Clone)]
pub struct SampleIngestionResult {
    /// Number of samples fetched from the remote API before deduplication.
    #[pyo3(get, set)]
    pub samples_fetched: u64,
    /// Number of rows written (inserted + updated) in `core_omicsample`.
    #[pyo3(get, set)]
    pub samples_written: u64,
    /// Non-fatal errors encountered during fetch/parse/write.
    #[pyo3(get, set)]
    pub errors: Vec<String>,
}

#[pymethods]
impl SampleIngestionResult {
    #[new]
    fn new() -> Self {
        SampleIngestionResult {
            samples_fetched: 0,
            samples_written: 0,
            errors: Vec::new(),
        }
    }
}

/// Fetch and ingest per-sample metadata for a single OmicDataset.
///
/// Designed to be called **on demand** by the backend when a dataset is curated
/// as `included` (Op 4.3 — `run_sample_ingestion` Celery task).
///
/// # Arguments
///
/// | Parameter           | Type           | Description |
/// |---------------------|----------------|-------------|
/// | `dataset_id`        | `i64`          | Integer PK of the OmicDataset row in `core_omicdataset`. Written as FK in every `core_omicsample` row. |
/// | `dataset_accession` | `str`          | External accession (e.g. "GSE12345", "SRP123456"). Drives the NCBI fetch. |
/// | `source_db`         | `str`          | One of "geo", "sra", "bioproject", "gwas_catalog". Selects the fetch/parse path. |
/// | `db_url`            | `str`          | PostgreSQL connection string (same as the other functions). |
/// | `ncbi_api_key`      | `Option[str]`  | NCBI API key (raises rate limit 3→10 req/s). Pass `None` when absent. |
///
/// # Returns
///
/// `SampleIngestionResult` with:
/// - `samples_fetched`: count of samples returned by the remote API.
/// - `samples_written`: rows inserted or updated in `core_omicsample` (COPY result).
/// - `errors`: list of non-fatal error strings (empty on full success).
///
/// On fatal error (DB connection, COPY failure) raises `PyRuntimeError`.
///
/// # Notes on fetch strategy
///
/// - **GEO:** `efetch db=gse acc=GSExxxxx rettype=soft retmode=text`
///   SOFT is line-delimited and compact; one download per dataset.
/// - **SRA:** `efetch db=sra acc=SRPxxxxxx rettype=xml retmode=xml`
///   Returns `<EXPERIMENT_PACKAGE_SET>`; each package has a `<SAMPLE>` element.
/// - **bioproject / gwas_catalog:** no per-sample API exists; returns empty, no error.
#[pyfunction]
#[pyo3(signature = (dataset_id, dataset_accession, source_db, db_url, ncbi_api_key=None))]
fn ingest_samples_for_dataset(
    dataset_id: i64,
    dataset_accession: String,
    source_db: String,
    db_url: String,
    ncbi_api_key: Option<String>,
) -> PyResult<SampleIngestionResult> {
    let rt = Runtime::new().unwrap();

    let result: Result<SampleIngestionResult, String> = rt.block_on(async {
        // 1. Connect to DB
        let db_client = match crate::db::connection::connect_db(&db_url).await {
            Ok(c) => c,
            Err(e) => return Err(format!("DB connection failed: {e}")),
        };

        let mut errors: Vec<String> = Vec::new();

        // 2. Fetch samples from NCBI (or return empty for unsupported sources)
        let ncbi = crate::ncbi::client::NcbiClient::new(ncbi_api_key);
        let samples = match crate::omics::sample_parser::fetch_samples_for_dataset(
            &ncbi,
            dataset_id,
            &dataset_accession,
            &source_db,
        )
        .await
        {
            Ok(s) => s,
            Err(e) => {
                // Fetch failed — non-fatal, return empty with error recorded
                errors.push(format!("sample fetch error: {e}"));
                vec![]
            }
        };

        let samples_fetched = samples.len() as u64;

        if samples.is_empty() {
            return Ok(SampleIngestionResult {
                samples_fetched: 0,
                samples_written: 0,
                errors,
            });
        }

        // 3. COPY samples into core_omicsample (fatal on DB error)
        let samples_written =
            match crate::db::copy_writer::copy_omic_samples(&db_client, &samples).await {
                Ok(n) => n,
                Err(e) => {
                    return Err(format!("copy_omic_samples failed: {:?}", e));
                }
            };

        Ok(SampleIngestionResult {
            samples_fetched,
            samples_written,
            errors,
        })
    });

    match result {
        Ok(res) => Ok(res),
        Err(e) => Err(pyo3::exceptions::PyRuntimeError::new_err(e)),
    }
}

// ─── Dataset file download (F1 — GEO supplementary) ─────────────────────────

/// Result returned by `download_dataset_files`.
#[pyclass]
#[derive(Clone)]
pub struct DownloadResult {
    /// Number of files successfully downloaded and written to `dest_dir`.
    #[pyo3(get, set)]
    pub files_downloaded: u64,
    /// Total bytes written across all downloaded files.
    #[pyo3(get, set)]
    pub bytes_total: u64,
    /// Non-fatal per-file error strings.
    #[pyo3(get, set)]
    pub errors: Vec<String>,
}

#[pymethods]
impl DownloadResult {
    #[new]
    fn new() -> Self {
        DownloadResult {
            files_downloaded: 0,
            bytes_total: 0,
            errors: Vec::new(),
        }
    }
}

/// Download dataset files to disk and register metadata in `core_datasetfile`.
///
/// # Arguments
///
/// | Parameter           | Type          | Description |
/// |---------------------|---------------|-------------|
/// | `job_id`            | `str`         | UUID of the `IngestionJob` row to update throughout the run. |
/// | `dataset_id`        | `i64`         | PK of `core_omicdataset` — written as FK in every `core_datasetfile` row. |
/// | `dataset_accession` | `str`         | GEO accession, e.g. `"GSE12345"`. Used to resolve the suppl/ URL. |
/// | `source_db`         | `str`         | Currently `"geo"`. Reserved for F2 (`"sra"`, `"ena"`). |
/// | `file_kind`         | `str`         | `"geo_supplementary"` for F1. F2 will pass `"fastq"`. |
/// | `dest_dir`          | `str`         | Absolute path to the local temp directory where files are written. Django reads this to find the files for object-storage upload. |
/// | `db_url`            | `str`         | PostgreSQL connection string. |
/// | `ncbi_api_key`      | `Option[str]` | NCBI API key (not used for GEO FTP downloads; reserved for rate-limited E-utilities calls in F2). |
///
/// # Storage-key contract with Django (D3)
///
/// Rust writes each file to `<dest_dir>/<filename>` and records
/// `storage_key = "<dest_dir>/<filename>"` (the full local path) in
/// `core_datasetfile`. Django's post-job step reads `storage_key` to locate
/// the file on disk, uploads it via `default_storage`, and overwrites
/// `storage_key` with the final object-storage path.
///
/// This means:
/// - `storage_key` is **never NULL** (NOT NULL constraint); Rust always fills it.
/// - After Django's upload step, `storage_key` is the canonical object-storage key.
/// - Before upload, `storage_key` holds the temporary local path (safe to use for upload).
///
/// # Returns
///
/// `DownloadResult { files_downloaded, bytes_total, errors }`.
/// Raises `PyRuntimeError` only on fatal errors (DB connection, COPY failure).
/// Per-file errors (network, parse) are non-fatal and appear in `errors`.
#[pyfunction]
#[pyo3(signature = (job_id, dataset_id, dataset_accession, source_db, file_kind, dest_dir, db_url, ncbi_api_key=None))]
fn download_dataset_files(
    job_id: String,
    dataset_id: i64,
    dataset_accession: String,
    source_db: String,
    file_kind: String,
    dest_dir: String,
    db_url: String,
    ncbi_api_key: Option<String>,
) -> PyResult<DownloadResult> {
    let rt = Runtime::new().unwrap();

    let result: Result<DownloadResult, String> = rt.block_on(async {
        // 1. Connect to DB and mark job running
        let db_client = match crate::db::connection::connect_db(&db_url).await {
            Ok(c) => c,
            Err(e) => return Err(format!("DB connection failed: {e}")),
        };

        crate::db::job_tracker::update_job_status(&db_client, &job_id, "running", 0, 0, 0, None)
            .await?;

        let ncbi = crate::ncbi::client::NcbiClient::new(ncbi_api_key);
        let dest_path = std::path::Path::new(&dest_dir);

        let mut errors: Vec<String> = Vec::new();
        let mut db_rows: Vec<crate::db::copy_writer::DatasetFileRow> = Vec::new();

        match source_db.as_str() {
            "geo" => {
                match file_kind.as_str() {
                    "geo_supplementary" | "supplementary" => {
                        // Resolve and download GEO supplementary files
                        let (files, download_errors) =
                            crate::omics::downloader::download_geo_supplementary(
                                &ncbi,
                                &dataset_accession,
                                dest_path,
                            )
                            .await;

                        errors.extend(download_errors);

                        for f in &files {
                            // Natural key: <GSE_accession>__<filename>
                            let accession_key =
                                format!("{}_{}", dataset_accession.to_uppercase(), f.file_name);

                            db_rows.push(crate::db::copy_writer::DatasetFileRow {
                                accession: accession_key,
                                file_type: "supplementary".to_string(),
                                source: "geo_ftp".to_string(),
                                remote_url: f.remote_url.clone(),
                                // Full local path — Django reads this to upload to object storage
                                storage_key: f.result.path.clone(),
                                size_bytes: Some(f.result.size_bytes as i64),
                                checksum_md5: Some(f.result.checksum_md5.clone()),
                                download_status: "downloaded".to_string(),
                                bytes_downloaded: f.result.size_bytes as i64,
                                error_message: String::new(),
                                dataset_id: Some(dataset_id),
                                sample_id: None,
                            });
                        }

                        // Also record failed files so Django knows they exist
                        // (errors list already carries the messages above)
                    }
                    unknown => {
                        errors.push(format!(
                            "Unknown file_kind '{}' for source 'geo'. Expected: geo_supplementary",
                            unknown
                        ));
                    }
                }
            }
            // F2 branch — ENA FASTQ download
            "sra" | "ena" => {
                match file_kind.as_str() {
                    "fastq" => {
                        // 1. Resolve SRR samples for this dataset from the DB
                        let srr_samples = match crate::omics::downloader::fetch_srr_samples_for_dataset(
                            &db_client,
                            dataset_id,
                        )
                        .await
                        {
                            Ok(s) => s,
                            Err(e) => {
                                let msg = format!("fetch_srr_samples_for_dataset failed: {}", e);
                                crate::db::job_tracker::update_job_status(
                                    &db_client, &job_id, "failed", 0, 0, 0, Some(&msg),
                                )
                                .await?;
                                return Err(msg);
                            }
                        };

                        eprintln!(
                            "[download] dataset_id={} has {} SRR sample(s)",
                            dataset_id,
                            srr_samples.len()
                        );

                        if srr_samples.is_empty() {
                            errors.push(format!(
                                "No SRR/ERR/DRR samples found for dataset_id={} (accession={}). \
                                 Ensure sample ingestion ran before FASTQ download.",
                                dataset_id, dataset_accession
                            ));
                        } else {
                            // 2. Download FASTQ files via ENA FTP (resumable)
                            let (fastq_results, download_errors) =
                                crate::omics::downloader::download_fastq_for_samples(
                                    &ncbi,
                                    &srr_samples,
                                    dest_path,
                                )
                                .await;

                            errors.extend(download_errors);

                            // 3. Build DB rows — sample_id filled, dataset_id NULL (XOR constraint)
                            for fr in &fastq_results {
                                // Determine download_status: check if this accession_key appears
                                // in errors (MD5 mismatch) — mark as failed; otherwise downloaded.
                                let md5_ok = fr.entry.expected_md5.as_ref().map_or(true, |expected| {
                                    expected.is_empty() || *expected == fr.checksum_md5
                                });
                                let dl_status = if md5_ok { "downloaded" } else { "failed" };
                                let err_msg = if md5_ok {
                                    String::new()
                                } else {
                                    format!(
                                        "MD5 mismatch: expected {:?}, got {}",
                                        fr.entry.expected_md5, fr.checksum_md5
                                    )
                                };

                                db_rows.push(crate::db::copy_writer::DatasetFileRow {
                                    accession: fr.entry.accession_key.clone(),
                                    file_type: "fastq".to_string(),
                                    source: "ena_ftp".to_string(),
                                    remote_url: fr.entry.url.clone(),
                                    storage_key: fr.local_path.clone(),
                                    size_bytes: Some(fr.size_bytes as i64),
                                    checksum_md5: Some(fr.checksum_md5.clone()),
                                    download_status: dl_status.to_string(),
                                    bytes_downloaded: fr.size_bytes as i64,
                                    error_message: err_msg,
                                    dataset_id: None, // XOR: FASTQ belongs to sample
                                    sample_id: Some(fr.sample_id),
                                });
                            }
                        }
                    }
                    unknown => {
                        errors.push(format!(
                            "Unknown file_kind '{}' for source '{}'. Expected: fastq",
                            unknown, source_db
                        ));
                    }
                }
            }
            unknown => {
                errors.push(format!(
                    "Unknown source_db '{}'. Valid: geo, sra, ena",
                    unknown
                ));
            }
        }

        // 2. COPY metadata to DB (even if some downloads failed — record what we have)
        let files_downloaded = db_rows.len() as u64;
        let bytes_total: u64 = db_rows
            .iter()
            .filter_map(|r| r.size_bytes.map(|b| b as u64))
            .sum();

        if !db_rows.is_empty() {
            match crate::db::copy_writer::copy_dataset_files(&db_client, &db_rows).await {
                Ok(n) => eprintln!("[download] COPY core_datasetfile: {} rows upserted", n),
                Err(e) => {
                    let msg = format!("copy_dataset_files failed: {:?}", e);
                    crate::db::job_tracker::update_job_status(
                        &db_client, &job_id, "failed", 0, 0, 0, Some(&msg),
                    )
                    .await?;
                    return Err(msg);
                }
            }
        }

        // 3. Update IngestionJob
        let err_summary = if errors.is_empty() {
            None
        } else {
            Some(errors.join("; "))
        };
        let status = if errors.is_empty() { "completed" } else { "completed_with_errors" };
        crate::db::job_tracker::update_job_status(
            &db_client,
            &job_id,
            status,
            files_downloaded as i32,
            files_downloaded as i32,
            0,
            err_summary.as_deref(),
        )
        .await?;

        Ok(DownloadResult {
            files_downloaded,
            bytes_total,
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
    m.add_class::<SampleIngestionResult>()?;
    m.add_class::<DownloadResult>()?;
    // MeSH / preview types and functions
    m.add_class::<MagnitudePreview>()?;
    m.add_class::<MeshSuggestion>()?;
    m.add_function(wrap_pyfunction!(search_and_ingest_pubmed, m)?)?;
    m.add_function(wrap_pyfunction!(search_and_ingest_omics, m)?)?;
    m.add_function(wrap_pyfunction!(search_and_ingest_pride, m)?)?;
    m.add_function(wrap_pyfunction!(resolve_pending_links, m)?)?;
    m.add_function(wrap_pyfunction!(ingest_samples_for_dataset, m)?)?;
    m.add_function(wrap_pyfunction!(download_dataset_files, m)?)?;
    m.add_function(wrap_pyfunction!(pubmed_magnitude_preview, m)?)?;
    m.add_function(wrap_pyfunction!(mesh_suggest, m)?)?;
    Ok(())
}
