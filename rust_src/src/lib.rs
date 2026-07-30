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
            Err(e) => {
                eprintln!("[db] connection error: {e}");
                return Err("DB connection failed".to_string());
            }
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
            Err(e) => {
                eprintln!("[db] connection error: {e}");
                return Err("DB connection failed".to_string());
            }
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
            Err(e) => {
                eprintln!("[db] connection error: {e}");
                return Err("DB connection failed".to_string());
            }
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
            .map_err(|e| { eprintln!("[db] connection error: {e}"); "DB connection failed".to_string() })?;

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
            Err(e) => {
                eprintln!("[db] connection error: {e}");
                return Err("DB connection failed".to_string());
            }
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
#[pyo3(signature = (job_id, dataset_id, dataset_accession, source_db, file_kind, dest_dir, db_url, ncbi_api_key=None, sample_ids=None))]
fn download_dataset_files(
    job_id: String,
    dataset_id: i64,
    dataset_accession: String,
    source_db: String,
    file_kind: String,
    dest_dir: String,
    db_url: String,
    ncbi_api_key: Option<String>,
    sample_ids: Option<Vec<i64>>,
) -> PyResult<DownloadResult> {
    let rt = Runtime::new().unwrap();

    let result: Result<DownloadResult, String> = rt.block_on(async {
        // 1. Connect to DB and mark job running
        let db_client = match crate::db::connection::connect_db(&db_url).await {
            Ok(c) => c,
            Err(e) => {
                eprintln!("[db] connection error: {e}");
                return Err("DB connection failed".to_string());
            }
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

                    // GEO→FASTQ via ENA: expand GSM sra_runs → SRR pairs then
                    // use the same download pipeline as the native SRA branch.
                    "fastq" => {
                        // 1. Expand GSM samples → (gsm_id, srr_accession) pairs
                        //    using extra_metadata['sra_runs'] written by resolve_sra_runs_for_dataset.
                        let (srr_pairs, skipped) =
                            match crate::omics::downloader::fetch_runs_from_geo_samples(
                                &db_client,
                                dataset_id,
                                sample_ids.as_deref(),
                            )
                            .await
                            {
                                Ok(v) => v,
                                Err(e) => {
                                    let msg =
                                        format!("fetch_runs_from_geo_samples failed: {}", e);
                                    crate::db::job_tracker::update_job_status(
                                        &db_client,
                                        &job_id,
                                        "failed",
                                        0,
                                        0,
                                        0,
                                        Some(&msg),
                                    )
                                    .await?;
                                    return Err(msg);
                                }
                            };

                        if skipped > 0 {
                            eprintln!(
                                "[download] dataset_id={} (GEO): {} GSM sample(s) skipped \
                                 (no sra_runs resolved — run resolve_sra_runs_for_dataset first \
                                 or sample is microarray-only)",
                                dataset_id, skipped
                            );
                        }

                        eprintln!(
                            "[download] dataset_id={} (GEO→ENA): {} SRR run(s) to download",
                            dataset_id,
                            srr_pairs.len()
                        );

                        if srr_pairs.is_empty() {
                            errors.push(format!(
                                "No resolved SRR runs found for GEO dataset_id={} \
                                 (accession={}). Run resolve_sra_runs_for_dataset before \
                                 downloading FASTQ, or dataset may be microarray-only.",
                                dataset_id, dataset_accession
                            ));
                        } else {
                            // 2. Download via ENA — identical pipeline to the SRA branch
                            let (fastq_results, download_errors) =
                                crate::omics::downloader::download_fastq_for_samples(
                                    &ncbi,
                                    &srr_pairs,
                                    dest_path,
                                )
                                .await;

                            errors.extend(download_errors);

                            // 3. Build DB rows — sample_id = gsm_id, dataset_id = NULL (XOR)
                            for fr in &fastq_results {
                                let md5_ok =
                                    fr.entry.expected_md5.as_ref().map_or(true, |expected| {
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
                                    dataset_id: None, // XOR: FASTQ belongs to sample (GSM)
                                    sample_id: Some(fr.sample_id),
                                });
                            }
                        }
                    }

                    unknown => {
                        errors.push(format!(
                            "Unknown file_kind '{}' for source 'geo'. \
                             Expected: geo_supplementary, fastq",
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
                        //    sample_ids=Some(vec) → subset; None → all SRR samples
                        let srr_samples = match crate::omics::downloader::fetch_srr_samples_for_dataset(
                            &db_client,
                            dataset_id,
                            sample_ids.as_deref(),
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

// ─── GEO → SRA run resolution (MVP-B) ────────────────────────────────────────

/// Result returned by `resolve_sra_runs_for_dataset`.
#[pyclass]
#[derive(Clone)]
pub struct SraResolutionResult {
    /// Number of GSM samples whose `extra_metadata['sra_runs']` was written.
    #[pyo3(get)]
    pub samples_updated: u64,
    /// Non-fatal error messages (rate-limit, missing SRX, etc.).
    #[pyo3(get)]
    pub errors: Vec<String>,
}

#[pymethods]
impl SraResolutionResult {
    #[new]
    fn new() -> Self {
        SraResolutionResult {
            samples_updated: 0,
            errors: Vec::new(),
        }
    }
}

/// Resolve GSM → SRR run accessions for all GEO samples in `dataset_id`.
///
/// For each sample with accession `GSM*`:
/// 1. Read `extra_metadata['relation']` (captured by the GEO SOFT parser).
/// 2. Extract `SRX*` / `ERX*` / `DRX*` accessions.
/// 3. Query the ENA Portal filereport API: `SRX → [SRR, ...]`.
/// 4. UPDATE `core_omicsample.extra_metadata['sra_runs']` with the resolved list.
///
/// Tolerant of missing `relation`, microarray GSMs, and SRX with no ENA FASTQ.
/// Respects ENA rate limits via `NcbiClient` backoff (429 handling).
///
/// **Security:** `ncbi_api_key` is used only in HTTP headers — never logged.
///
/// # Returns
///
/// `SraResolutionResult { samples_updated, errors }`.
/// Raises `PyRuntimeError` only on fatal errors (DB connection failure).
/// Per-sample errors are non-fatal and appear in `errors`.
#[pyfunction]
#[pyo3(signature = (dataset_id, db_url, ncbi_api_key=None))]
fn resolve_sra_runs_for_dataset(
    dataset_id: i64,
    db_url: String,
    ncbi_api_key: Option<String>,
) -> PyResult<SraResolutionResult> {
    let rt = Runtime::new().unwrap();

    let result: Result<SraResolutionResult, String> = rt.block_on(async {
        let db_client = match crate::db::connection::connect_db(&db_url).await {
            Ok(c) => c,
            Err(e) => {
                eprintln!("[db] connection error: {e}");
                return Err("DB connection failed".to_string());
            }
        };

        // ncbi_api_key is intentionally NOT logged anywhere in this function
        let ncbi = crate::ncbi::client::NcbiClient::new(ncbi_api_key);

        let (samples_updated, errors) =
            crate::omics::downloader::resolve_sra_runs_for_geo_samples(
                &ncbi,
                &db_client,
                dataset_id,
            )
            .await;

        Ok(SraResolutionResult {
            samples_updated,
            errors,
        })
    });

    match result {
        Ok(res) => Ok(res),
        Err(e) => Err(pyo3::exceptions::PyRuntimeError::new_err(e)),
    }
}

// ─── Inc-1: FASTQ URL resolution (destination='client') ──────────────────────

/// One FASTQ URL entry returned by `resolve_fastq_urls`.
///
/// `fastq_url` contains one or two HTTPS URLs separated by `;` for paired-end
/// runs.  An empty string signals a bam-only run (no public FASTQ on ENA).
#[pyclass]
#[derive(Clone)]
pub struct FastqUrlEntry {
    /// SRR / ERR / DRR accession.
    #[pyo3(get)]
    pub run_accession: String,
    /// HTTPS FASTQ URL(s), `;`-joined.  Empty string = no public FASTQ (bam-only).
    #[pyo3(get)]
    pub fastq_url: String,
    /// Basename(s) derived from `fastq_url`, `;`-joined.  Empty when URL is empty.
    #[pyo3(get)]
    pub file_name: String,
    /// Declared total size in bytes (R1 + R2 for paired-end).  `None` when unknown.
    #[pyo3(get)]
    pub size_bytes: Option<i64>,
    /// MD5 checksum(s), `;`-joined, lowercase.  `None` when unavailable.
    #[pyo3(get)]
    pub checksum_md5: Option<String>,
}

#[pymethods]
impl FastqUrlEntry {
    #[new]
    fn new() -> Self {
        FastqUrlEntry {
            run_accession: String::new(),
            fastq_url: String::new(),
            file_name: String::new(),
            size_bytes: None,
            checksum_md5: None,
        }
    }

    fn __repr__(&self) -> String {
        format!(
            "FastqUrlEntry(run_accession={:?}, fastq_url={:?}, size_bytes={:?})",
            self.run_accession, self.fastq_url, self.size_bytes
        )
    }
}

/// Resolve public ENA FASTQ URLs for a dataset without downloading anything.
///
/// Intended for `destination='client'` (Inc-1): the Django view calls this
/// synchronously, receives the list, and returns it directly in the HTTP
/// response.  The browser then fetches each URL directly from ENA — no quota
/// consumed, no object-storage writes, no `DatasetFile` records created.
///
/// # Source routing
///
/// - `source_db="geo"`: reads cached URLs from `core_samplesrarun` (no HTTP).
/// - `source_db="sra"|"ena"`: queries ENA Portal filereport per run (HTTP).
///
/// # Bam-only runs
///
/// Runs where ENA has no public FASTQ are included with `fastq_url=""` so
/// the caller can surface a clear "no public FASTQ" status.
///
/// # Security
///
/// `ncbi_api_key` is forwarded to `NcbiClient` for rate-limited HTTP calls
/// only.  It is **never** written to logs, return values, or error messages.
///
/// # Returns
///
/// `list[FastqUrlEntry]` — one entry per resolved run.
/// Raises `PyRuntimeError` only on fatal errors (DB connection failure).
/// Per-run HTTP errors are non-fatal and written to stderr.
#[pyfunction]
#[pyo3(signature = (dataset_id, source_db, db_url, sample_ids=None, ncbi_api_key=None))]
fn resolve_fastq_urls(
    dataset_id: i64,
    source_db: String,
    db_url: String,
    sample_ids: Option<Vec<i64>>,
    ncbi_api_key: Option<String>,
) -> PyResult<Vec<FastqUrlEntry>> {
    let rt = Runtime::new().unwrap();

    let result: Result<Vec<FastqUrlEntry>, String> = rt.block_on(async {
        let db_client = match crate::db::connection::connect_db(&db_url).await {
            Ok(c) => c,
            Err(e) => {
                eprintln!("[db] connection error: {e}");
                return Err("DB connection failed".to_string());
            }
        };

        // ncbi_api_key forwarded to NcbiClient only — never logged
        let ncbi = crate::ncbi::client::NcbiClient::new(ncbi_api_key);

        let (records, errors) =
            crate::omics::downloader::resolve_fastq_urls_for_dataset(
                &ncbi,
                &db_client,
                dataset_id,
                &source_db,
                sample_ids.as_deref(),
            )
            .await;

        // Log non-fatal errors to stderr (operator visibility)
        for e in &errors {
            eprintln!("[resolve_fastq_urls] non-fatal: {}", e);
        }

        let entries = records
            .into_iter()
            .map(|r| FastqUrlEntry {
                run_accession: r.run_accession,
                fastq_url: r.fastq_url,
                file_name: r.file_name,
                size_bytes: r.size_bytes,
                checksum_md5: r.checksum_md5,
            })
            .collect();

        Ok(entries)
    });

    match result {
        Ok(entries) => Ok(entries),
        Err(e) => Err(pyo3::exceptions::PyRuntimeError::new_err(e)),
    }
}

// ─── CPTAC matrix loader (Obj 2 — Fase 0, passo 0.2) ────────────────────────

/// Metadados de uma coluna de amostra no manifesto do Parquet.
///
/// Campos:
/// - `case_id`: ID original da amostra (ex.: `C3L-00004`), sem prefixo de role.
/// - `sample_role`: `"tumor"` ou `"normal"`.
/// - `column_index`: posição 0-based da coluna no Parquet (col 0 = `feature`; amostras ≥ 1).
#[pyclass]
#[derive(Clone)]
pub struct CptacSampleColumn {
    #[pyo3(get)]
    pub case_id: String,
    #[pyo3(get)]
    pub sample_role: String,
    #[pyo3(get)]
    pub column_index: usize,
}

#[pymethods]
impl CptacSampleColumn {
    #[new]
    fn new() -> Self {
        CptacSampleColumn {
            case_id: String::new(),
            sample_role: String::new(),
            column_index: 0,
        }
    }

    fn __repr__(&self) -> String {
        format!(
            "CptacSampleColumn(case_id={:?}, sample_role={:?}, column_index={})",
            self.case_id, self.sample_role, self.column_index
        )
    }
}

/// Manifesto retornado por `load_cptac_matrix`.
///
/// Contrato de handoff para o vitruvio (passo 0.3):
///
/// | Campo | Tipo | Descrição |
/// |---|---|---|
/// | `parquet_path` | `str` | Caminho absoluto do Parquet gravado em `dest_dir`. |
/// | `checksum_md5` | `str` | MD5 lowercase-hex do arquivo Parquet. |
/// | `n_features` | `int` | Genes na interseção tumor ∩ normal. |
/// | `n_samples` | `int` | Total de amostras (tumor + normal). |
/// | `sample_columns` | `list[CptacSampleColumn]` | Colunas de amostra ordenadas: tumor primeiro, depois normal. |
/// | `genes_discarded` | `int` | Genes presentes em apenas um arquivo (não na interseção). |
///
/// O vitruvio usa `parquet_path` para fazer upload via `default_storage` e
/// `sample_columns` para criar os registros `OmicMatrixSample`.
#[pyclass]
pub struct CptacMatrixManifest {
    #[pyo3(get)]
    pub parquet_path: String,
    #[pyo3(get)]
    pub checksum_md5: String,
    #[pyo3(get)]
    pub n_features: usize,
    #[pyo3(get)]
    pub n_samples: usize,
    #[pyo3(get)]
    pub sample_columns: Vec<CptacSampleColumn>,
    #[pyo3(get)]
    pub genes_discarded: usize,
}

#[pymethods]
impl CptacMatrixManifest {
    fn __repr__(&self) -> String {
        format!(
            "CptacMatrixManifest(n_features={}, n_samples={}, genes_discarded={}, checksum_md5={:?})",
            self.n_features, self.n_samples, self.genes_discarded, self.checksum_md5
        )
    }
}

/// Baixa a matriz proteômica CPTAC CCRCC do LinkedOmics, normaliza e grava Parquet.
///
/// # Fluxo
///
/// 1. Valida `tumor_url` e `normal_url` (devem ser `https://www.linkedomics.org/…`).
/// 2. Baixa os dois arquivos `.cct` para `dest_dir` com `User-Agent` de browser
///    (LinkedOmics bloqueia sem UA) via streaming chunked.
/// 3. Parseia cada arquivo em streaming, uma passada, sem bufferizar em String:
///    extrai cabeçalho (Case_IDs) e dados (gene symbol + valores float).
///    Células vazias / `NA` / `NaN` → `null` no Parquet.
/// 4. Alinha os dois arquivos por gene symbol (interseção), em ordem alfabética.
///    Genes exclusivos de um arquivo são descartados e contados em `genes_discarded`.
/// 5. Grava UM arquivo Parquet em `dest_dir/<parquet_name>`:
///    - Coluna `feature` (Utf8): gene symbol.
///    - Colunas `T_<case_id>` (Float32, nullable): amostras tumor.
///    - Colunas `N_<case_id>` (Float32, nullable): amostras normal.
/// 6. Retorna `CptacMatrixManifest` com caminho, MD5, `n_features`, `n_samples`
///    e lista de `CptacSampleColumn` com `case_id`, `sample_role`, `column_index`.
///
/// # Segurança
///
/// URLs são validadas contra `https://www.linkedomics.org/` antes de qualquer
/// fetch (proteção SSRF). Path traversal (`..`) é rejeitado.
///
/// # Idempotência
///
/// Re-rodar com o mesmo `dest_dir` regrava os arquivos de forma determinística.
/// Mesmo conteúdo → mesmo MD5 do Parquet.
///
/// # Não faz
///
/// - Não toca o Postgres (nem COPY, nem ORM).
/// - Não cria `OmicMatrix` / `OmicMatrixSample` — isso é responsabilidade do vitruvio (0.3).
///
/// # Argumentos
///
/// | Parâmetro | Tipo | Descrição |
/// |---|---|---|
/// | `tumor_url` | `str` | URL do `.cct` tumor (deve ser `https://www.linkedomics.org/…`). |
/// | `normal_url` | `str` | URL do `.cct` normal (idem). |
/// | `dest_dir` | `str` | Diretório local onde os arquivos e o Parquet serão gravados. |
/// | `parquet_name` | `str` | Nome do arquivo Parquet de saída (ex.: `"cptac_ccrcc_proteome.parquet"`). |
///
/// # Retorno
///
/// `CptacMatrixManifest` — ver campos acima.
/// Lança `PyRuntimeError` em caso de erro fatal (URL inválida, falha HTTP, falha de I/O).
#[pyfunction]
#[pyo3(signature = (tumor_url, normal_url, dest_dir, parquet_name = "cptac_ccrcc_proteome.parquet"))]
fn load_cptac_matrix(
    tumor_url: String,
    normal_url: String,
    dest_dir: String,
    parquet_name: &str,
) -> PyResult<CptacMatrixManifest> {
    match crate::omics::matrix_loader::load_cptac_matrix(
        &tumor_url,
        &normal_url,
        &dest_dir,
        parquet_name,
    ) {
        Ok(manifest) => {
            let sample_columns: Vec<CptacSampleColumn> = manifest
                .sample_columns
                .into_iter()
                .map(|sc| CptacSampleColumn {
                    case_id: sc.case_id,
                    sample_role: sc.sample_role,
                    column_index: sc.column_index,
                })
                .collect();

            Ok(CptacMatrixManifest {
                parquet_path: manifest.parquet_path,
                checksum_md5: manifest.checksum_md5,
                n_features: manifest.n_features,
                n_samples: manifest.n_samples,
                sample_columns,
                genes_discarded: manifest.genes_discarded,
            })
        }
        Err(e) => Err(pyo3::exceptions::PyRuntimeError::new_err(e)),
    }
}

// ─── OncoKB gene role loader (Obj 2 — Fase 2, passo 2.2) ────────────────────

/// Manifesto retornado por `load_oncokb_gene_roles`.
///
/// | Campo | Tipo | Descrição |
/// |---|---|---|
/// | `n_genes` | `int` | Total de genes parseados do TSV. |
/// | `n_oncogene` | `int` | Genes com role=oncogene. |
/// | `n_tsg` | `int` | Genes com role=tsg. |
/// | `n_dual` | `int` | Genes com role=oncogene_and_tsg. |
/// | `n_neither` | `int` | Genes com role=neither. |
/// | `n_unknown` | `int` | Genes com role=unknown (INSUFFICIENT_EVIDENCE ou vazio). |
/// | `source_version` | `str` | Data do download ISO (ex.: "2026-07-11"). |
/// | `n_upserted` | `int` | Linhas gravadas via COPY UPSERT em core_generole. |
#[pyclass]
pub struct GeneRoleManifest {
    #[pyo3(get)]
    pub n_genes: usize,
    #[pyo3(get)]
    pub n_oncogene: usize,
    #[pyo3(get)]
    pub n_tsg: usize,
    #[pyo3(get)]
    pub n_dual: usize,
    #[pyo3(get)]
    pub n_neither: usize,
    #[pyo3(get)]
    pub n_unknown: usize,
    #[pyo3(get)]
    pub source_version: String,
    #[pyo3(get)]
    pub n_upserted: u64,
}

#[pymethods]
impl GeneRoleManifest {
    fn __repr__(&self) -> String {
        format!(
            "GeneRoleManifest(n_genes={}, n_oncogene={}, n_tsg={}, n_dual={}, \
             n_neither={}, n_unknown={}, source_version={:?}, n_upserted={})",
            self.n_genes,
            self.n_oncogene,
            self.n_tsg,
            self.n_dual,
            self.n_neither,
            self.n_unknown,
            self.source_version,
            self.n_upserted,
        )
    }
}

/// Baixa o catálogo de papel do gene do OncoKB (cancerGeneList.txt — TOKEN-FREE),
/// parseia em streaming e faz COPY UPSERT na tabela `core_generole`.
///
/// # Fonte
///
/// `https://www.oncokb.org/api/v1/utils/cancerGeneList.txt` (TSV público, ~138KB).
/// Nenhum token é necessário para este arquivo.
///
/// # Mapeamento Gene Type → role
///
/// | Gene Type (OncoKB) | role |
/// |---|---|
/// | ONCOGENE | oncogene |
/// | TSG | tsg |
/// | ONCOGENE_AND_TSG | oncogene_and_tsg |
/// | NEITHER | neither |
/// | INSUFFICIENT_EVIDENCE / vazio | unknown |
///
/// # COPY UPSERT
///
/// ON CONFLICT (gene_symbol, source) DO UPDATE — idempotente.
/// `source_version` = data ISO do download (não há campo de versão no arquivo).
///
/// # Argumentos
///
/// | Parâmetro | Tipo | Descrição |
/// |---|---|---|
/// | `url` | `str` | URL do TSV OncoKB (deve ser `https://www.oncokb.org/…`). |
/// | `dest_dir` | `str` | Diretório onde o TSV baixado é temporariamente salvo. |
/// | `db_url` | `str` | PostgreSQL connection string. |
///
/// # Retorno
///
/// `GeneRoleManifest` com contagens por role, source_version e n_upserted.
/// Lança `PyRuntimeError` em caso de erro fatal.
#[pyfunction]
#[pyo3(signature = (url, dest_dir, db_url))]
fn load_oncokb_gene_roles(
    url: String,
    dest_dir: String,
    db_url: String,
) -> PyResult<GeneRoleManifest> {
    match crate::omics::gene_role_loader::load_oncokb_gene_roles(&url, &dest_dir, &db_url) {
        Ok(m) => Ok(GeneRoleManifest {
            n_genes: m.n_genes,
            n_oncogene: m.n_oncogene,
            n_tsg: m.n_tsg,
            n_dual: m.n_dual,
            n_neither: m.n_neither,
            n_unknown: m.n_unknown,
            source_version: m.source_version,
            n_upserted: m.n_upserted,
        }),
        Err(e) => Err(pyo3::exceptions::PyRuntimeError::new_err(e)),
    }
}

// ─── CNV matrix loader (Obj 2 — Fase 2, passo 2.3) ──────────────────────────

/// Manifesto retornado por `load_cnv_matrix`.
///
/// Handoff para o vitruvio (passo 2.3):
///
/// | Campo | Tipo | Descrição |
/// |---|---|---|
/// | `parquet_path` | `str` | Caminho absoluto do Parquet gravado em `dest_dir`. |
/// | `checksum_md5` | `str` | MD5 lowercase-hex do arquivo Parquet. |
/// | `n_features` | `int` | Número de genes (linhas do .cct). |
/// | `n_samples` | `int` | Número de amostras (todas tumor). |
/// | `sample_columns` | `list[CptacSampleColumn]` | Todas com `sample_role = "tumor"`. |
///
/// O vitruvio usa `parquet_path` para upload via `default_storage` e
/// `sample_columns` para criar registros `OmicMatrixSample` + `OmicMatrix`
/// com `omics_layer` adequado (handoff — ver nota de handoff abaixo).
#[pyclass]
pub struct CnvMatrixManifest {
    #[pyo3(get)]
    pub parquet_path: String,
    #[pyo3(get)]
    pub checksum_md5: String,
    #[pyo3(get)]
    pub n_features: usize,
    #[pyo3(get)]
    pub n_samples: usize,
    #[pyo3(get)]
    pub sample_columns: Vec<CptacSampleColumn>,
}

#[pymethods]
impl CnvMatrixManifest {
    fn __repr__(&self) -> String {
        format!(
            "CnvMatrixManifest(n_features={}, n_samples={}, checksum_md5={:?})",
            self.n_features, self.n_samples, self.checksum_md5
        )
    }
}

/// Baixa a matriz CNV CPTAC CCRCC do LinkedOmics, parseia em streaming e grava Parquet.
///
/// # Fonte
///
/// `https://www.linkedomics.org/data_download/CPTAC-CCRCC/HS_CPTAC_CCRCC_CNV_gene_Tumor.cct`
/// (~26.7 MB, público). Requer User-Agent de browser (LinkedOmics retorna 403 sem ele).
///
/// # Diferença em relação a `load_cptac_matrix`
///
/// O loader do proteoma (Fase 0) alinha DOIS arquivos (tumor + normal) por interseção.
/// O CNV tem UM arquivo só (tumor): todas as amostras são `sample_role = "tumor"`,
/// e não há alinhamento de interseção — todos os genes do arquivo são incluídos.
///
/// # Parquet de saída
///
/// - Coluna `feature` (Utf8): gene symbol.
/// - Colunas `T_<case_id>` (Float32, nullable): log-ratio tumor por amostra.
///
/// # Não faz
///
/// - Não cria `OmicMatrix` / `OmicMatrixSample` (responsabilidade do vitruvio).
/// - Não faz COPY de metadados CNV — apenas Parquet + manifesto.
///
/// # HANDOFF para vitruvio (passo 2.3)
///
/// O vitruvio precisa saber qual valor usar para `OmicMatrix.omics_layer` ao criar
/// o registro para o CNV. O vocabulário atual de `omics_layer` pode não incluir
/// 'copy_number' ou 'cnv' — verificar CheckConstraint antes de usar (handoff cartografo
/// se necessário — não inventar valor que viole a constraint).
///
/// # Argumentos
///
/// | Parâmetro | Tipo | Descrição |
/// |---|---|---|
/// | `url` | `str` | URL do .cct CNV (deve ser `https://www.linkedomics.org/…`). |
/// | `dest_dir` | `str` | Diretório local para o .cct baixado e o Parquet. |
/// | `parquet_name` | `str` | Nome do arquivo Parquet de saída. |
///
/// # Retorno
///
/// `CnvMatrixManifest` — ver campos acima.
/// Lança `PyRuntimeError` em caso de erro fatal.
#[pyfunction]
#[pyo3(signature = (url, dest_dir, parquet_name = "cptac_ccrcc_cnv.parquet"))]
fn load_cnv_matrix(
    url: String,
    dest_dir: String,
    parquet_name: &str,
) -> PyResult<CnvMatrixManifest> {
    match crate::omics::cnv_loader::load_cnv_matrix(&url, &dest_dir, parquet_name) {
        Ok(manifest) => {
            let sample_columns: Vec<CptacSampleColumn> = manifest
                .sample_columns
                .into_iter()
                .map(|sc| CptacSampleColumn {
                    case_id: sc.case_id,
                    sample_role: sc.sample_role,
                    column_index: sc.column_index,
                })
                .collect();

            Ok(CnvMatrixManifest {
                parquet_path: manifest.parquet_path,
                checksum_md5: manifest.checksum_md5,
                n_features: manifest.n_features,
                n_samples: manifest.n_samples,
                sample_columns,
            })
        }
        Err(e) => Err(pyo3::exceptions::PyRuntimeError::new_err(e)),
    }
}

// ─── CNV seed derivation (Obj 2 — Fase 2, slice CNV-seed) ───────────────────

/// Manifesto retornado por `seed_cnv_from_parquet`.
///
/// Contrato de handoff para o vitruvio que chama esta função:
///
/// | Campo | Tipo | Descrição |
/// |---|---|---|
/// | `n_seeds_written` | `int` | Linhas gravadas via COPY em `core_varianteffectseed`. |
/// | `n_activator` | `int` | Seeds com direction='activator' (oncogene amplificado). |
/// | `n_inactivator` | `int` | Seeds com direction='inactivator' (TSG deletado). |
/// | `n_neutral_skipped` | `int` | Células puladas: oncogene_and_tsg ambíguo. |
/// | `n_genes_with_role` | `int` | Tamanho do mapa GeneRole consultado (oncogene+tsg+dual). |
/// | `n_genes_skipped_no_role` | `int` | Genes sem role relevante (neither/unknown/ausente). |
/// | `n_cells_subthreshold` | `int` | Células com |cnv| abaixo do threshold. |
/// | `n_cells_null` | `int` | Células com valor null/missing no Parquet. |
#[pyclass]
pub struct CnvSeedManifest {
    #[pyo3(get)]
    pub n_seeds_written: u64,
    #[pyo3(get)]
    pub n_activator: u64,
    #[pyo3(get)]
    pub n_inactivator: u64,
    #[pyo3(get)]
    pub n_neutral_skipped: u64,
    #[pyo3(get)]
    pub n_genes_with_role: u64,
    #[pyo3(get)]
    pub n_genes_skipped_no_role: u64,
    #[pyo3(get)]
    pub n_cells_subthreshold: u64,
    #[pyo3(get)]
    pub n_cells_null: u64,
}

#[pymethods]
impl CnvSeedManifest {
    fn __repr__(&self) -> String {
        format!(
            "CnvSeedManifest(n_seeds_written={}, n_activator={}, n_inactivator={}, \
             n_neutral_skipped={}, n_genes_with_role={}, n_genes_skipped_no_role={}, \
             n_cells_subthreshold={}, n_cells_null={})",
            self.n_seeds_written,
            self.n_activator,
            self.n_inactivator,
            self.n_neutral_skipped,
            self.n_genes_with_role,
            self.n_genes_skipped_no_role,
            self.n_cells_subthreshold,
            self.n_cells_null,
        )
    }
}

/// Lê o Parquet CNV local, cruza com `GeneRole`+`OmicMatrixSample` do DB,
/// aplica a regra dosagem×papel (heurística CNV v1) e faz COPY UPSERT
/// em `core_varianteffectseed`.
///
/// # Pré-requisitos
///
/// - `parquet_path`: caminho local do Parquet CNV (Django fez o download do object storage
///   e passa o caminho; I/O de storage é Django).
/// - `core_generole` populado para `source='oncokb'` (load_oncokb_gene_roles já rodou).
/// - `core_omicmatrixsample` populado para `matrix_id` (vitruvio registrou o manifesto).
///
/// # Regra de sinal (dosagem × papel — heurística v1)
///
/// | Role | CNV value | Decisão |
/// |---|---|---|
/// | oncogene | >= gain_threshold | activator |
/// | tsg | <= loss_threshold | inactivator |
/// | oncogene_and_tsg | qualquer | ambíguo → pular |
/// | neither / unknown | qualquer | sem papel → pular |
/// | sub-threshold | qualquer | pular |
/// | null | — | pular |
///
/// Guarda apenas `activator`/`inactivator` (neutro não é seed de perturbação).
/// `magnitude = |cnv_value|`; `confidence = 0.6` (heurística v1 declarada).
/// `evidence_type = 'cnv'`; `variant_key = ''` (nível-gene); `effect_source = 'cnv_dosage_x_oncokb'`.
///
/// # Segurança
///
/// `db_url` nunca logado.
///
/// # Argumentos
///
/// | Parâmetro | Tipo | Descrição |
/// |---|---|---|
/// | `parquet_path` | `str` | Caminho local do Parquet CNV (absoluto). |
/// | `matrix_id` | `int` | PK de `OmicMatrix` da matriz CNV (FK nas seeds). |
/// | `db_url` | `str` | PostgreSQL connection string. |
/// | `method_version` | `str` | Versão do método (default: "fase2-cnv-v1"). |
/// | `gain_threshold` | `float` | Limiar de amplificação (default: 0.3). |
/// | `loss_threshold` | `float` | Limiar de deleção (default: -0.3, deve ser negativo). |
///
/// # Retorno
///
/// `CnvSeedManifest` — ver campos acima.
/// Lança `PyRuntimeError` em caso de erro fatal.
#[pyfunction]
#[pyo3(signature = (
    parquet_path,
    matrix_id,
    db_url,
    method_version = "fase2-cnv-v1",
    gain_threshold = 0.3,
    loss_threshold = -0.3
))]
fn seed_cnv_from_parquet(
    parquet_path: String,
    matrix_id: i64,
    db_url: String,
    method_version: &str,
    gain_threshold: f32,
    loss_threshold: f32,
) -> PyResult<CnvSeedManifest> {
    match crate::omics::cnv_seed_derivation::seed_cnv_from_parquet(
        &parquet_path,
        matrix_id,
        &db_url,
        method_version,
        gain_threshold,
        loss_threshold,
    ) {
        Ok(m) => Ok(CnvSeedManifest {
            n_seeds_written: m.n_seeds_written,
            n_activator: m.n_activator,
            n_inactivator: m.n_inactivator,
            n_neutral_skipped: m.n_neutral_skipped,
            n_genes_with_role: m.n_genes_with_role,
            n_genes_skipped_no_role: m.n_genes_skipped_no_role,
            n_cells_subthreshold: m.n_cells_subthreshold,
            n_cells_null: m.n_cells_null,
        }),
        Err(e) => Err(pyo3::exceptions::PyRuntimeError::new_err(e)),
    }
}

// ─── Variant effect loaders (Obj 2 — Fase 2, passo 2.4) ─────────────────────

/// Manifesto retornado por `load_clinvar_effects`.
///
/// | Campo | Tipo | Descrição |
/// |---|---|---|
/// | `n_variants_processed` | `int` | Linhas VCF parseadas (excluindo comentários/cabeçalho). |
/// | `n_kept` | `int` | Variantes cujo gene está na allowlist e foram gravadas. |
/// | `n_skipped_offlist` | `int` | Variantes descartadas (gene não está na allowlist). |
/// | `n_skipped_no_gene` | `int` | Linhas sem campo GENEINFO no INFO. |
/// | `n_upserted` | `int` | Linhas gravadas via COPY UPSERT em core_varianteffectraw. |
/// | `source_version` | `str` | Versão da fonte extraída do cabeçalho VCF. |
/// | `errors` | `list[str]` | Erros não-fatais durante o parse. |
#[pyclass]
pub struct ClinVarLoadManifest {
    #[pyo3(get)]
    pub n_variants_processed: usize,
    #[pyo3(get)]
    pub n_kept: usize,
    #[pyo3(get)]
    pub n_skipped_offlist: usize,
    #[pyo3(get)]
    pub n_skipped_no_gene: usize,
    #[pyo3(get)]
    pub n_upserted: u64,
    #[pyo3(get)]
    pub source_version: String,
    #[pyo3(get)]
    pub errors: Vec<String>,
}

#[pymethods]
impl ClinVarLoadManifest {
    fn __repr__(&self) -> String {
        format!(
            "ClinVarLoadManifest(n_processed={}, n_kept={}, n_offlist={}, \
             n_no_gene={}, n_upserted={}, source_version={:?}, n_errors={})",
            self.n_variants_processed, self.n_kept, self.n_skipped_offlist,
            self.n_skipped_no_gene, self.n_upserted, self.source_version,
            self.errors.len()
        )
    }
}

/// Manifesto retornado por `load_alphamissense_effects`.
///
/// | Campo | Tipo | Descrição |
/// |---|---|---|
/// | `n_variants_processed` | `int` | Linhas TSV parseadas (excluindo cabeçalho). |
/// | `n_kept` | `int` | Variantes cujo gene está no mapa e foram gravadas. |
/// | `n_skipped_no_map` | `int` | Variantes descartadas (uniprot_id não mapeado). |
/// | `n_upserted` | `int` | Linhas gravadas via COPY UPSERT em core_varianteffectraw. |
/// | `source_version` | `str` | Versão da fonte (data do download). |
/// | `errors` | `list[str]` | Erros não-fatais. |
/// | `handoff_required` | `bool` | `True` quando mapa uniprot→gene não foi fornecido (AM não carregado). |
#[pyclass]
pub struct AlphaMissenseLoadManifest {
    #[pyo3(get)]
    pub n_variants_processed: usize,
    #[pyo3(get)]
    pub n_kept: usize,
    #[pyo3(get)]
    pub n_skipped_no_map: usize,
    #[pyo3(get)]
    pub n_upserted: u64,
    #[pyo3(get)]
    pub source_version: String,
    #[pyo3(get)]
    pub errors: Vec<String>,
    /// `True` quando o mapa uniprot→gene não foi fornecido.
    /// O caller (vitruvio) deve fornecer o mapa para carregar AM no v2.
    #[pyo3(get)]
    pub handoff_required: bool,
}

#[pymethods]
impl AlphaMissenseLoadManifest {
    fn __repr__(&self) -> String {
        format!(
            "AlphaMissenseLoadManifest(n_processed={}, n_kept={}, n_no_map={}, \
             n_upserted={}, source_version={:?}, handoff_required={}, n_errors={})",
            self.n_variants_processed, self.n_kept, self.n_skipped_no_map,
            self.n_upserted, self.source_version, self.handoff_required,
            self.errors.len()
        )
    }
}

/// Baixa o VCF ClinVar GRCh38 (clinvar.vcf.gz — ~192MB), parseia em streaming e
/// faz COPY UPSERT em `core_varianteffectraw`.
///
/// # Fonte
///
/// `https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/clinvar.vcf.gz`
/// (domínio público, NCBI FTP).
///
/// # Filtro de escala v1
///
/// Somente variantes cujo `gene_symbol` (campo `GENEINFO` do INFO VCF) esteja em
/// `gene_allowlist` são gravadas. A allowlist deve conter os genes do `GeneRole`
/// (carregados por `load_oncokb_gene_roles` no passo 2.2). Descarte em streaming —
/// sem bufferizar o VCF na memória.
///
/// # Normalização de variant_key (Decisão F)
///
/// `variant_key` = `CHROM:POS:REF:ALT` GRCh38, CHROM sem prefixo `chr`.
/// ClinVar VCF GRCh38 já vem sem `chr` — nenhuma transformação necessária.
/// Indels: strip de base de ancoragem VCF (left-anchor único, v1).
///
/// # COPY UPSERT
///
/// ON CONFLICT (variant_key, source, gene_symbol) DO UPDATE — idempotente.
/// `source='clinvar'`; `raw_magnitude=NULL` (ClinVar é categórico); `confidence=0.9`.
///
/// # Segurança
///
/// URL validada: apenas `https://ftp.ncbi.nlm.nih.gov/` é permitido.
/// `db_url` nunca logado.
///
/// # Argumentos
///
/// | Parâmetro | Tipo | Descrição |
/// |---|---|---|
/// | `url` | `str` | URL do VCF GRCh38 gzip (deve ser `https://ftp.ncbi.nlm.nih.gov/…`). |
/// | `dest_dir` | `str` | Diretório onde o `.vcf.gz` baixado é salvo temporariamente. |
/// | `db_url` | `str` | PostgreSQL connection string. |
/// | `gene_allowlist` | `list[str]` | Symbols dos genes do GeneRole (UPPERCASE). |
///
/// # Retorno
///
/// `ClinVarLoadManifest` com contagens e `source_version`.
/// Lança `PyRuntimeError` em caso de erro fatal.
#[pyfunction]
#[pyo3(signature = (url, dest_dir, db_url, gene_allowlist))]
fn load_clinvar_effects(
    url: String,
    dest_dir: String,
    db_url: String,
    gene_allowlist: Vec<String>,
) -> PyResult<ClinVarLoadManifest> {
    match crate::omics::variant_effect_loader::load_clinvar_effects(
        &url, &dest_dir, &db_url, gene_allowlist,
    ) {
        Ok(m) => Ok(ClinVarLoadManifest {
            n_variants_processed: m.n_variants_processed,
            n_kept: m.n_kept,
            n_skipped_offlist: m.n_skipped_offlist,
            n_skipped_no_gene: m.n_skipped_no_gene,
            n_upserted: m.n_upserted,
            source_version: m.source_version,
            errors: m.errors,
        }),
        Err(e) => Err(pyo3::exceptions::PyRuntimeError::new_err(e)),
    }
}

/// Baixa o TSV AlphaMissense hg38 (AlphaMissense_hg38.tsv.gz — ~643MB), parseia
/// em streaming e faz COPY UPSERT em `core_varianteffectraw`.
///
/// # Decisão de filtro (HANDOFF v1)
///
/// AlphaMissense não tem `gene_symbol` — só `uniprot_id`. Para filtrar por gene,
/// o caller deve fornecer `uniprot_to_gene` (mapa `uniprot_id → gene_symbol`,
/// derivado de UniProt/Ensembl para os genes do GeneRole).
///
/// **Se `uniprot_to_gene` estiver vazio**: a função retorna imediatamente com
/// `handoff_required=True` sem baixar o arquivo. Isso é o comportamento correto
/// para o v1 — AM fica como handoff. O vitruvio deve fornecer o mapa no v2.
///
/// # Fonte
///
/// `https://storage.googleapis.com/dm_alphamissense/AlphaMissense_hg38.tsv.gz`
/// (CC BY-NC-SA 4.0, Google DeepMind). Mirror Zenodo: `https://zenodo.org/record/8208688/…`.
///
/// # Normalização de variant_key (Decisão F)
///
/// AlphaMissense vem com `CHROM` com prefixo `chr` → REMOVIDO. `POS` 1-based.
/// Resultado idêntico ao ClinVar para a mesma variante SNV.
///
/// # Segurança
///
/// URL validada: apenas `storage.googleapis.com` e `zenodo.org` são permitidos.
/// `db_url` nunca logado. Licença AM: NÃO commitar recortes do arquivo real —
/// fixtures nos testes são sintéticas.
///
/// # Argumentos
///
/// | Parâmetro | Tipo | Descrição |
/// |---|---|---|
/// | `url` | `str` | URL do TSV gzip AlphaMissense. |
/// | `dest_dir` | `str` | Diretório onde o `.tsv.gz` é salvo temporariamente. |
/// | `db_url` | `str` | PostgreSQL connection string. |
/// | `uniprot_to_gene` | `dict[str,str]` | Mapa `uniprot_id → gene_symbol` para genes do GeneRole. Vazio → `handoff_required=True`. |
/// | `gene_allowlist` | `list[str]` | Symbols dos genes do GeneRole (UPPERCASE). |
///
/// # Retorno
///
/// `AlphaMissenseLoadManifest` com contagens, `source_version` e `handoff_required`.
/// Lança `PyRuntimeError` em caso de erro fatal.
#[pyfunction]
#[pyo3(signature = (url, dest_dir, db_url, uniprot_to_gene, gene_allowlist))]
fn load_alphamissense_effects(
    url: String,
    dest_dir: String,
    db_url: String,
    uniprot_to_gene: std::collections::HashMap<String, String>,
    gene_allowlist: Vec<String>,
) -> PyResult<AlphaMissenseLoadManifest> {
    match crate::omics::variant_effect_loader::load_alphamissense_effects(
        &url, &dest_dir, &db_url, uniprot_to_gene, gene_allowlist,
    ) {
        Ok(m) => Ok(AlphaMissenseLoadManifest {
            n_variants_processed: m.n_variants_processed,
            n_kept: m.n_kept,
            n_skipped_no_map: m.n_skipped_no_map,
            n_upserted: m.n_upserted,
            source_version: m.source_version,
            errors: m.errors,
            handoff_required: m.handoff_required,
        }),
        Err(e) => Err(pyo3::exceptions::PyRuntimeError::new_err(e)),
    }
}

// ─── Somatic MAF loader (Obj 2 — Fase 2, passo 2.6) ─────────────────────────

/// Entrada do mapa `file_id → sample_accession` passado pelo Django.
///
/// O Django resolve o mapa via GDC API (`/files?fields=cases.submitter_id`)
/// e passa uma lista de `{file_id: str, sample_accession: str}`. O loader
/// associa cada variante à `sample_accession` pelo `file_id` que está baixando —
/// sem derivar o case_id do Tumor_Sample_Barcode (que é aliquot CPTAC interno).
#[pyclass]
#[derive(Clone)]
pub struct SomaticMafFileEntry {
    #[pyo3(get, set)]
    pub file_id: String,
    #[pyo3(get, set)]
    pub sample_accession: String,
}

#[pymethods]
impl SomaticMafFileEntry {
    #[new]
    fn new(file_id: String, sample_accession: String) -> Self {
        SomaticMafFileEntry { file_id, sample_accession }
    }

    fn __repr__(&self) -> String {
        format!(
            "SomaticMafFileEntry(file_id={:?}, sample_accession={:?})",
            self.file_id, self.sample_accession
        )
    }
}

/// Metadados de uma coluna de amostra no Parquet somatic MAF.
///
/// Campos:
/// - `case_id`: accession da amostra (ex.: `"C3L-00004_tumor"`).
/// - `sample_role`: sempre `"tumor"` para MAFs somáticos CPTAC.
/// - `column_index`: posição 0-based da coluna no Parquet (col 0 = `feature`; amostras ≥ 1).
#[pyclass]
#[derive(Clone)]
pub struct SomaticMafSampleColumn {
    #[pyo3(get)]
    pub case_id: String,
    #[pyo3(get)]
    pub sample_role: String,
    #[pyo3(get)]
    pub column_index: usize,
}

#[pymethods]
impl SomaticMafSampleColumn {
    #[new]
    fn new() -> Self {
        SomaticMafSampleColumn {
            case_id: String::new(),
            sample_role: String::new(),
            column_index: 0,
        }
    }

    fn __repr__(&self) -> String {
        format!(
            "SomaticMafSampleColumn(case_id={:?}, sample_role={:?}, column_index={})",
            self.case_id, self.sample_role, self.column_index
        )
    }
}

/// Manifesto retornado por `load_somatic_maf`.
///
/// Handoff para o vitruvio (passo 2.6):
///
/// | Campo | Tipo | Descrição |
/// |---|---|---|
/// | `parquet_path` | `str` | Caminho absoluto do Parquet burden (`feature × T_<sample>`, Int32). |
/// | `checksum_md5` | `str` | MD5 lowercase-hex do Parquet. |
/// | `n_features` | `int` | Número de genes (features = linhas do Parquet). |
/// | `n_samples` | `int` | Número de amostras únicas. |
/// | `sample_columns` | `list[SomaticMafSampleColumn]` | Todas com `sample_role = "tumor"`. |
/// | `occurrences_path` | `str` | Caminho do TSV `(sample_accession, variant_key, gene_symbol, variant_classification)`. |
/// | `n_files_processed` | `int` | Arquivos MAF baixados e parseados com sucesso. |
/// | `n_occurrences` | `int` | Ocorrências únicas `(sample, variant_key)` após dedupe. |
/// | `n_variants_skipped_synonymous` | `int` | Variantes descartadas por serem sinônimas. |
/// | `n_variants_skipped_offlist` | `int` | Variantes descartadas (gene não na allowlist). |
/// | `errors` | `list[str]` | Erros não-fatais por arquivo MAF. |
#[pyclass]
pub struct SomaticMafManifest {
    #[pyo3(get)]
    pub parquet_path: String,
    #[pyo3(get)]
    pub checksum_md5: String,
    #[pyo3(get)]
    pub n_features: usize,
    #[pyo3(get)]
    pub n_samples: usize,
    #[pyo3(get)]
    pub sample_columns: Vec<SomaticMafSampleColumn>,
    #[pyo3(get)]
    pub occurrences_path: String,
    #[pyo3(get)]
    pub n_files_processed: usize,
    #[pyo3(get)]
    pub n_occurrences: usize,
    #[pyo3(get)]
    pub n_variants_skipped_synonymous: usize,
    #[pyo3(get)]
    pub n_variants_skipped_offlist: usize,
    #[pyo3(get)]
    pub errors: Vec<String>,
}

#[pymethods]
impl SomaticMafManifest {
    fn __repr__(&self) -> String {
        format!(
            "SomaticMafManifest(n_features={}, n_samples={}, n_files_processed={}, \
             n_occurrences={}, n_skipped_syn={}, n_skipped_off={}, n_errors={})",
            self.n_features,
            self.n_samples,
            self.n_files_processed,
            self.n_occurrences,
            self.n_variants_skipped_synonymous,
            self.n_variants_skipped_offlist,
            self.errors.len(),
        )
    }
}

/// Baixa e parseia arquivos GDC masked MAF em lote, produzindo dois artefatos locais:
///
/// 1. **Parquet burden** (`feature × T_<sample>`, Int32) — uma linha por gene, uma
///    coluna por amostra, valor = contagem de variantes não-sinônimas (`omics_layer='genomic'`,
///    `data_format_level='counts'`). Usado pelo vitruvio para criar `OmicMatrix`.
/// 2. **TSV de ocorrências** (`sample_accession, variant_key, gene_symbol,
///    variant_classification`) — consumido pelo `snv_seed_service` (vitruvio)
///    para cruzar com `VariantEffectResolved` e materializar `VariantEffectSeed`.
///
/// # Contrato do file_map
///
/// O Django resolve `file_id → sample_accession` via GDC API (`/files?fields=cases.submitter_id`)
/// antes de chamar esta função. O mapa é passado como lista de `SomaticMafFileEntry`.
/// O loader **não** tenta derivar o case_id do `Tumor_Sample_Barcode` in-file (que é
/// um aliquot CPTAC interno como `CPT0077490009`, sem relação direta com C3L-/C3N-).
///
/// # Variantes não-sinônimas (lista exata)
///
/// Missense_Mutation, Nonsense_Mutation, Frame_Shift_Del, Frame_Shift_Ins,
/// In_Frame_Del, In_Frame_Ins, Splice_Site, Translation_Start_Site, Nonstop_Mutation.
/// Silent, Intron, UTR, IGR, RNA → descartados.
///
/// # variant_key
///
/// Reutiliza `normalize_variant_key` / `strip_chr_prefix` de `variant_effect_loader.rs`
/// (strip de `chr` + strip de base de ancoragem VCF para indels). Consistente com
/// ClinVar/AlphaMissense — crítico para o cruzamento do seed.
///
/// # Dedupe
///
/// Mesma `(sample_accession, variant_key)` em múltiplos arquivos conta 1× no burden
/// e 1× no TSV. CPTAC tem casos com múltiplos MAFs por amostra.
///
/// # Download
///
/// GET `https://api.gdc.cancer.gov/data/{file_id}` (não HEAD — HEAD retorna 400).
/// Host validado: apenas `api.gdc.cancer.gov`. Teto de 512 MB por arquivo.
/// Arquivo gzip descomprimido em streaming. Timeout 30 min.
///
/// # Segurança
///
/// `db_url` nunca logado. Host allowlist. Teto de bytes. Sem SQL cru.
///
/// # Argumentos
///
/// | Parâmetro | Tipo | Descrição |
/// |---|---|---|
/// | `file_map` | `list[SomaticMafFileEntry]` | Mapa `file_id → sample_accession` (Django resolve). |
/// | `dest_dir` | `str` | Diretório local onde os artefatos serão gravados. |
/// | `db_url` | `str` | PostgreSQL connection string (reservado; não usado no loader). |
/// | `gene_allowlist` | `list[str]` | Symbols dos genes do GeneRole (UPPERCASE). |
/// | `parquet_name` | `str` | Nome do Parquet de saída (default: `"cptac_ccrcc_somatic.parquet"`). |
///
/// # Retorno
///
/// `SomaticMafManifest` — ver campos acima.
/// Lança `PyRuntimeError` em caso de erro fatal (diretório, Parquet). Erros por arquivo
/// são não-fatais e acumulados em `errors`.
#[pyfunction]
#[pyo3(signature = (file_map, dest_dir, db_url, gene_allowlist, parquet_name = "cptac_ccrcc_somatic.parquet"))]
fn load_somatic_maf(
    file_map: Vec<SomaticMafFileEntry>,
    dest_dir: String,
    db_url: String,
    gene_allowlist: Vec<String>,
    parquet_name: &str,
) -> PyResult<SomaticMafManifest> {
    let rust_entries: Vec<crate::omics::maf_loader::MafFileEntry> = file_map
        .into_iter()
        .map(|e| crate::omics::maf_loader::MafFileEntry {
            file_id: e.file_id,
            sample_accession: e.sample_accession,
        })
        .collect();

    match crate::omics::maf_loader::load_somatic_maf(
        rust_entries,
        &dest_dir,
        &db_url,
        gene_allowlist,
        parquet_name,
    ) {
        Ok(m) => {
            let sample_columns: Vec<SomaticMafSampleColumn> = m
                .sample_columns
                .into_iter()
                .map(|sc| SomaticMafSampleColumn {
                    case_id: sc.case_id,
                    sample_role: sc.sample_role,
                    column_index: sc.column_index,
                })
                .collect();
            Ok(SomaticMafManifest {
                parquet_path: m.parquet_path,
                checksum_md5: m.checksum_md5,
                n_features: m.n_features,
                n_samples: m.n_samples,
                sample_columns,
                occurrences_path: m.occurrences_path,
                n_files_processed: m.n_files_processed,
                n_occurrences: m.n_occurrences,
                n_variants_skipped_synonymous: m.n_variants_skipped_synonymous,
                n_variants_skipped_offlist: m.n_variants_skipped_offlist,
                errors: m.errors,
            })
        }
        Err(e) => Err(pyo3::exceptions::PyRuntimeError::new_err(e)),
    }
}

// ─── Fosfoproteoma loader (Obj 2 — Fase 3, passo 3.1) ───────────────────────

/// Manifesto retornado por `load_cptac_phospho_matrix`.
///
/// Mesmo contrato que `CptacMatrixManifest` (proteoma, Fase 0) — o vitruvio
/// usa os mesmos campos para criar `OmicMatrix(omics_layer='proteomic',
/// feature_axis='phospho_site', data_format_level='intensities')`.
///
/// | Campo | Tipo | Descrição |
/// |---|---|---|
/// | `parquet_path` | `str` | Caminho absoluto do Parquet gravado em `dest_dir`. |
/// | `checksum_md5` | `str` | MD5 lowercase-hex do Parquet. |
/// | `n_features` | `int` | Número de features (genes na interseção tumor ∩ normal). |
/// | `n_samples` | `int` | Total de amostras (tumor + normal). |
/// | `sample_columns` | `list[CptacSampleColumn]` | Colunas de amostra (tumor primeiro). |
/// | `genes_discarded` | `int` | Genes descartados fora da interseção. |
#[pyclass]
pub struct PhosphoMatrixManifest {
    #[pyo3(get)]
    pub parquet_path: String,
    #[pyo3(get)]
    pub checksum_md5: String,
    #[pyo3(get)]
    pub n_features: usize,
    #[pyo3(get)]
    pub n_samples: usize,
    #[pyo3(get)]
    pub sample_columns: Vec<CptacSampleColumn>,
    #[pyo3(get)]
    pub genes_discarded: usize,
}

#[pymethods]
impl PhosphoMatrixManifest {
    fn __repr__(&self) -> String {
        format!(
            "PhosphoMatrixManifest(n_features={}, n_samples={}, genes_discarded={}, checksum_md5={:?})",
            self.n_features, self.n_samples, self.genes_discarded, self.checksum_md5
        )
    }
}

/// Baixa o fosfoproteoma CPTAC CCRCC (gene-level, Tumor + Normal) do LinkedOmics,
/// parseia e grava Parquet. Reusa `matrix_loader` sem nova allowlist de host.
///
/// # Discovery (2026-07-16)
///
/// Arquivos: `HS_CPTAC_CCRCC_phosphoproteome_gene_Tumor.cct` /
/// `HS_CPTAC_CCRCC_phosphoproteome_gene_Normal.cct` (gene-level, não site-level).
/// Feature = símbolo de gene UPPERCASE (mesmo formato do proteoma).
/// `feature_axis = 'phospho_site'` é o vocabulário do `OmicMatrix` (já existe desde a Fase 0);
/// host `www.linkedomics.org` já validado por `validate_linkedomics_url`.
///
/// # Argumentos
///
/// | Parâmetro | Tipo | Descrição |
/// |---|---|---|
/// | `tumor_url` | `str` | URL do `.cct` tumor (deve ser `https://www.linkedomics.org/…`). |
/// | `normal_url` | `str` | URL do `.cct` normal (deve ser `https://www.linkedomics.org/…`). |
/// | `dest_dir` | `str` | Diretório onde Parquet e cache `.cct` são gravados. |
/// | `parquet_name` | `str` | Nome do arquivo Parquet (default: `"cptac_ccrcc_phospho.parquet"`). |
///
/// # Retorno
///
/// `PhosphoMatrixManifest` — mesmo contrato do proteoma.
/// O vitruvio cria `OmicMatrix(omics_layer='proteomic', feature_axis='phospho_site',
/// data_format_level='intensities')` + `OmicMatrixSample` com base no manifesto.
#[pyfunction]
#[pyo3(signature = (tumor_url, normal_url, dest_dir, parquet_name = "cptac_ccrcc_phospho.parquet"))]
fn load_cptac_phospho_matrix(
    tumor_url: String,
    normal_url: String,
    dest_dir: String,
    parquet_name: &str,
) -> PyResult<PhosphoMatrixManifest> {
    match crate::omics::matrix_loader::load_cptac_phospho_matrix(
        &tumor_url,
        &normal_url,
        &dest_dir,
        parquet_name,
    ) {
        Ok(manifest) => {
            let sample_columns: Vec<CptacSampleColumn> = manifest
                .sample_columns
                .into_iter()
                .map(|sc| CptacSampleColumn {
                    case_id: sc.case_id,
                    sample_role: sc.sample_role,
                    column_index: sc.column_index,
                })
                .collect();

            Ok(PhosphoMatrixManifest {
                parquet_path: manifest.parquet_path,
                checksum_md5: manifest.checksum_md5,
                n_features: manifest.n_features,
                n_samples: manifest.n_samples,
                sample_columns,
                genes_discarded: manifest.genes_discarded,
            })
        }
        Err(e) => Err(pyo3::exceptions::PyRuntimeError::new_err(e)),
    }
}

// ─── Catálogo de features de matriz (Obj 2 — Fase 3, promoção A→C) ─────────

/// Manifesto retornado por `catalog_matrix_features`.
///
/// | Campo | Tipo | Descrição |
/// |---|---|---|
/// | `n_features` | `int` | Features distintas (UPPERCASE, dedup) catalogadas em `core_omicmatrixfeature`. |
/// | `n_inserted` | `int` | Linhas gravadas como INSERT novo (normal: = n_features). |
/// | `n_updated` | `int` | Linhas que colidiram em `(matrix_id, feature_key)` no COPY final (defesa; normalmente 0). |
#[pyclass]
pub struct FeatureCatalogManifest {
    #[pyo3(get)]
    pub n_features: u64,
    #[pyo3(get)]
    pub n_inserted: u64,
    #[pyo3(get)]
    pub n_updated: u64,
}

#[pymethods]
impl FeatureCatalogManifest {
    fn __repr__(&self) -> String {
        format!(
            "FeatureCatalogManifest(n_features={}, n_inserted={}, n_updated={})",
            self.n_features, self.n_inserted, self.n_updated
        )
    }
}

/// Lê o eixo de linhas (coluna `feature`, 0-based) de um Parquet de matriz
/// já carregado (proteoma/fosfoproteoma CPTAC hoje) e popula
/// `core_omicmatrixfeature`: uma linha por `(matrix_id, feature_key) → row_index`.
///
/// Normaliza `feature_key` para UPPERCASE. Idempotente: rodar duas vezes com
/// o mesmo Parquet não duplica — o catálogo antigo da matriz é limpo (DELETE)
/// antes de repopular, evitando colisão em `(matrix_id, row_index)` quando a
/// posição de uma feature muda entre versões do loader (ver decisão completa
/// no cabeçalho de `matrix_feature_catalog.rs`).
///
/// # Pré-requisitos
///
/// - `parquet_path`: caminho local do Parquet (Django baixou do object storage).
/// - `matrix_id`: PK bigint de `OmicMatrix` já registrado.
///
/// # Argumentos
///
/// | Parâmetro | Tipo | Descrição |
/// |---|---|---|
/// | `parquet_path` | `str` | Caminho local do Parquet da matriz (absoluto). |
/// | `matrix_id` | `int` | PK de `OmicMatrix` (FK bigint em `OmicMatrixFeature`). |
/// | `db_url` | `str` | PostgreSQL connection string. |
///
/// # Retorno
///
/// `FeatureCatalogManifest` — ver campos acima. Lança `PyRuntimeError` em caso de erro fatal.
#[pyfunction]
fn catalog_matrix_features(
    parquet_path: String,
    matrix_id: i64,
    db_url: String,
) -> PyResult<FeatureCatalogManifest> {
    match crate::omics::matrix_feature_catalog::catalog_matrix_features(&parquet_path, matrix_id, &db_url) {
        Ok(m) => Ok(FeatureCatalogManifest {
            n_features: m.n_features,
            n_inserted: m.n_inserted,
            n_updated: m.n_updated,
        }),
        Err(e) => Err(pyo3::exceptions::PyRuntimeError::new_err(e)),
    }
}

// ─── KEGG topology loader (Obj 2 — Fase 3, passo 3.2) ───────────────────────

/// Manifesto retornado por `load_kegg_topology`.
///
/// | Campo | Tipo | Descrição |
/// |---|---|---|
/// | `n_pathways` | `int` | Vias inseridas/atualizadas em `core_pathway`. |
/// | `n_nodes` | `int` | Total de nós inseridos (todos os pathways). |
/// | `n_edges` | `int` | Total de arestas inseridas. |
/// | `n_signed` | `int` | Arestas com sign ≠ 0 (+1 ou -1). |
/// | `n_unsigned` | `int` | Arestas com sign = 0 (associação/desconhecido). |
/// | `n_orphan_symbols` | `int` | Nós `gene`/`ortholog` sem `gene_symbol` derivável. |
/// | `source_version` | `str` | Data do download (ex.: "2026-07-16"). |
#[pyclass]
pub struct KeggTopologyManifest {
    #[pyo3(get)]
    pub n_pathways: usize,
    #[pyo3(get)]
    pub n_nodes: usize,
    #[pyo3(get)]
    pub n_edges: usize,
    #[pyo3(get)]
    pub n_signed: usize,
    #[pyo3(get)]
    pub n_unsigned: usize,
    #[pyo3(get)]
    pub n_orphan_symbols: usize,
    #[pyo3(get)]
    pub source_version: String,
}

#[pymethods]
impl KeggTopologyManifest {
    fn __repr__(&self) -> String {
        format!(
            "KeggTopologyManifest(n_pathways={}, n_nodes={}, n_edges={}, \
             n_signed={}, n_unsigned={}, n_orphan_symbols={}, source_version={:?})",
            self.n_pathways,
            self.n_nodes,
            self.n_edges,
            self.n_signed,
            self.n_unsigned,
            self.n_orphan_symbols,
            self.source_version,
        )
    }
}

/// Baixa as topologias KGML das vias KEGG listadas, parseia XML em uma passada,
/// aplica §Sinal (subtype.name → sign ±1/0) e faz COPY UPSERT em
/// `core_pathway` / `core_pathwaynode` / `core_pathwayedge`.
///
/// # Fonte (travada)
///
/// `https://rest.kegg.jp/get/<kegg_id>/kgml` para cada via.
/// Host allowlist: apenas `rest.kegg.jp`. Rate limit: ≤ 3 req/s (3 vias = 3 chamadas).
/// Cache local em `<dest_dir>/kegg/<kegg_id>.kgml`.
///
/// # §Sinal
///
/// | subtype.name | sign | interaction |
/// |---|---|---|
/// | activation, phosphorylation | +1 | activation |
/// | expression | +1 | expression |
/// | inhibition, dephosphorylation | -1 | inhibition |
/// | repression | -1 | repression |
/// | binding/association, state change | 0 | binding |
/// | indirect effect | 0 | indirect |
/// | (sem subtype) | 0 | unknown |
/// | conflito (activation+inhibition) | 0 | unknown |
///
/// # Argumentos
///
/// | Parâmetro | Tipo | Descrição |
/// |---|---|---|
/// | `pathway_kegg_ids` | `list[str]` | IDs KEGG das vias (ex.: `["hsa04151","hsa04010","hsa04115"]`). |
/// | `dest_dir` | `str` | Diretório de cache local (KGML raw, gitignored). |
/// | `db_url` | `str` | PostgreSQL connection string. |
///
/// # Retorno
///
/// `KeggTopologyManifest` — ver campos acima. `readout_role` fica `none` em todos os
/// nós (marcado pelo vitruvio no passo 3.4).
#[pyfunction]
#[pyo3(signature = (pathway_kegg_ids, dest_dir, db_url))]
fn load_kegg_topology(
    pathway_kegg_ids: Vec<String>,
    dest_dir: String,
    db_url: String,
) -> PyResult<KeggTopologyManifest> {
    match crate::omics::kegg_topology_loader::load_kegg_topology(
        &pathway_kegg_ids,
        &dest_dir,
        &db_url,
    ) {
        Ok(m) => Ok(KeggTopologyManifest {
            n_pathways: m.n_pathways,
            n_nodes: m.n_nodes,
            n_edges: m.n_edges,
            n_signed: m.n_signed,
            n_unsigned: m.n_unsigned,
            n_orphan_symbols: m.n_orphan_symbols,
            source_version: m.source_version,
        }),
        Err(e) => Err(pyo3::exceptions::PyRuntimeError::new_err(e)),
    }
}

// ─── CollecTRI regulon loader (Obj 2 — Fase 3, passo 3.3) ───────────────────

/// Manifesto retornado por `load_collectri_regulons`.
///
/// | Campo | Tipo | Descrição |
/// |---|---|---|
/// | `n_tfs` | `int` | TFs únicos no arquivo de saída. |
/// | `n_targets` | `int` | Alvos únicos. |
/// | `n_edges` | `int` | Total de pares TF→alvo gravados no JSONL. |
/// | `n_positive` | `int` | Pares com mode=+1 (estimulação). |
/// | `n_negative` | `int` | Pares com mode=-1 (inibição). |
/// | `n_neutral` | `int` | Pares com mode=0 (conflito ou neutro). |
/// | `source_version` | `str` | Data do download. |
/// | `regulon_path` | `str` | Caminho absoluto do arquivo JSONL de regulons. |
#[pyclass]
pub struct RegulonLoadManifest {
    #[pyo3(get)]
    pub n_tfs: usize,
    #[pyo3(get)]
    pub n_targets: usize,
    #[pyo3(get)]
    pub n_edges: usize,
    #[pyo3(get)]
    pub n_positive: usize,
    #[pyo3(get)]
    pub n_negative: usize,
    #[pyo3(get)]
    pub n_neutral: usize,
    #[pyo3(get)]
    pub source_version: String,
    /// Caminho absoluto do JSONL de regulons (input do passo 3.4 do vitruvio).
    #[pyo3(get)]
    pub regulon_path: String,
}

#[pymethods]
impl RegulonLoadManifest {
    fn __repr__(&self) -> String {
        format!(
            "RegulonLoadManifest(n_tfs={}, n_targets={}, n_edges={}, \
             n_positive={}, n_negative={}, n_neutral={}, \
             source_version={:?}, regulon_path={:?})",
            self.n_tfs,
            self.n_targets,
            self.n_edges,
            self.n_positive,
            self.n_negative,
            self.n_neutral,
            self.source_version,
            self.regulon_path,
        )
    }
}

/// Baixa os regulons CollecTRI via OmniPath, filtra pelos TFs das 3 vias
/// e grava arquivo JSONL intermediário (consumido pelo vitruvio no passo 3.4).
///
/// **NÃO** insere em tabela PG (Decisão 3.3: arquivo intermediário local —
/// sem consumidor além do mapeamento 3.4).
///
/// # Fonte
///
/// `https://omnipathdb.org/interactions?datasets=collectri&genesymbols=1&fields=sources,references`
/// TSV público (GPL/acadêmico). Host allowlist: apenas `omnipathdb.org`.
/// Teto: 200 MB.
///
/// # Filtro
///
/// Apenas linhas onde `source_genesymbol` ∈ `tf_allowlist`. O vitruvio
/// deriva a allowlist dos `gene_symbol` dos nós das 3 vias após 3.2.
///
/// # Formato JSONL de saída
///
/// `{"tf":"TP53","target":"MDM2","mode":1,"sources":"CollecTRI","n_references":3}`
///
/// # Argumentos
///
/// | Parâmetro | Tipo | Descrição |
/// |---|---|---|
/// | `tf_allowlist` | `list[str]` | Símbolos UPPERCASE dos TFs das 3 vias (derivado pelo vitruvio). |
/// | `dest_dir` | `str` | Diretório onde o JSONL é gravado. |
///
/// # Retorno
///
/// `RegulonLoadManifest` com contagens e `regulon_path` (caminho do JSONL).
#[pyfunction]
#[pyo3(signature = (tf_allowlist, dest_dir))]
fn load_collectri_regulons(
    tf_allowlist: Vec<String>,
    dest_dir: String,
) -> PyResult<RegulonLoadManifest> {
    match crate::omics::regulon_loader::load_collectri_regulons(
        tf_allowlist,
        &dest_dir,
    ) {
        Ok(m) => Ok(RegulonLoadManifest {
            n_tfs: m.n_tfs,
            n_targets: m.n_targets,
            n_edges: m.n_edges,
            n_positive: m.n_positive,
            n_negative: m.n_negative,
            n_neutral: m.n_neutral,
            source_version: m.source_version,
            regulon_path: m.regulon_path,
        }),
        Err(e) => Err(pyo3::exceptions::PyRuntimeError::new_err(e)),
    }
}

// ─── NER bindings ─────────────────────────────────────────────────────────────

/// Extrai genes mencionados em `text` usando o dicionário HGNC canônico.
///
/// Retorna lista de tuplas `(gene_symbol, mention_count)` em ordem
/// decrescente de frequência. Função puramente síncrona, sem I/O.
#[pyfunction]
fn extract_genes(text: &str) -> Vec<(String, i32)> {
    let mut results = crate::categorization::gene_ner::extract_genes(text);
    results.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
    results
}

// ─── Python module registration ───────────────────────────────────────────────

#[pymodule]
fn rust_engine(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<IngestionResult>()?;
    m.add_class::<OmicsResult>()?;
    m.add_class::<SampleIngestionResult>()?;
    m.add_class::<DownloadResult>()?;
    m.add_class::<SraResolutionResult>()?;
    m.add_class::<FastqUrlEntry>()?;
    // MeSH / preview types and functions
    m.add_class::<MagnitudePreview>()?;
    m.add_class::<MeshSuggestion>()?;
    m.add_function(wrap_pyfunction!(search_and_ingest_pubmed, m)?)?;
    m.add_function(wrap_pyfunction!(search_and_ingest_omics, m)?)?;
    m.add_function(wrap_pyfunction!(search_and_ingest_pride, m)?)?;
    m.add_function(wrap_pyfunction!(resolve_pending_links, m)?)?;
    m.add_function(wrap_pyfunction!(ingest_samples_for_dataset, m)?)?;
    m.add_function(wrap_pyfunction!(download_dataset_files, m)?)?;
    m.add_function(wrap_pyfunction!(resolve_sra_runs_for_dataset, m)?)?;
    m.add_function(wrap_pyfunction!(resolve_fastq_urls, m)?)?;
    m.add_function(wrap_pyfunction!(pubmed_magnitude_preview, m)?)?;
    m.add_function(wrap_pyfunction!(mesh_suggest, m)?)?;
    m.add_function(wrap_pyfunction!(extract_genes, m)?)?;
    // CPTAC matrix loader (Obj 2 — Fase 0)
    m.add_class::<CptacSampleColumn>()?;
    m.add_class::<CptacMatrixManifest>()?;
    m.add_function(wrap_pyfunction!(load_cptac_matrix, m)?)?;
    // OncoKB gene role loader (Obj 2 — Fase 2, passo 2.2)
    m.add_class::<GeneRoleManifest>()?;
    m.add_function(wrap_pyfunction!(load_oncokb_gene_roles, m)?)?;
    // CNV matrix loader (Obj 2 — Fase 2, passo 2.3)
    m.add_class::<CnvMatrixManifest>()?;
    m.add_function(wrap_pyfunction!(load_cnv_matrix, m)?)?;
    // CNV seed derivation (Obj 2 — Fase 2, slice CNV-seed)
    m.add_class::<CnvSeedManifest>()?;
    m.add_function(wrap_pyfunction!(seed_cnv_from_parquet, m)?)?;
    // Variant effect loaders — ClinVar + AlphaMissense (Obj 2 — Fase 2, passo 2.4)
    m.add_class::<ClinVarLoadManifest>()?;
    m.add_class::<AlphaMissenseLoadManifest>()?;
    m.add_function(wrap_pyfunction!(load_clinvar_effects, m)?)?;
    m.add_function(wrap_pyfunction!(load_alphamissense_effects, m)?)?;
    // Somatic MAF loader — GDC masked MAF → Parquet burden + ocorrências (Obj 2 — Fase 2, passo 2.6)
    m.add_class::<SomaticMafFileEntry>()?;
    m.add_class::<SomaticMafSampleColumn>()?;
    m.add_class::<SomaticMafManifest>()?;
    m.add_function(wrap_pyfunction!(load_somatic_maf, m)?)?;
    // Fosfoproteoma loader (Obj 2 — Fase 3, passo 3.1)
    m.add_class::<PhosphoMatrixManifest>()?;
    m.add_function(wrap_pyfunction!(load_cptac_phospho_matrix, m)?)?;
    // KEGG topology loader (Obj 2 — Fase 3, passo 3.2)
    m.add_class::<KeggTopologyManifest>()?;
    m.add_function(wrap_pyfunction!(load_kegg_topology, m)?)?;
    // CollecTRI regulon loader (Obj 2 — Fase 3, passo 3.3)
    m.add_class::<RegulonLoadManifest>()?;
    m.add_function(wrap_pyfunction!(load_collectri_regulons, m)?)?;
    // Catálogo de features de matriz — promoção A→C (Obj 2 — Fase 3)
    m.add_class::<FeatureCatalogManifest>()?;
    m.add_function(wrap_pyfunction!(catalog_matrix_features, m)?)?;
    Ok(())
}
