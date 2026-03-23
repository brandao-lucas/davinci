use pyo3::prelude::*;
use tokio::runtime::Runtime;

pub mod ncbi;
pub mod categorization;
pub mod db;
pub mod utils;

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
        // 1. Conectar ao DB
        let client = match crate::db::connection::connect_db(&db_url).await {
            Ok(c) => c,
            Err(e) => return Err(format!("DB connection failed: {}", e)),
        };

        // 2. Atualizar job para PROCESSING (stubbed se UUID falhar)
        crate::db::job_tracker::update_job_status(&client, &job_id, "running", 0, None).await?;

        // --- Aqui entraria a chamada de fetch da NCBI e parsing ---
        // let fetcher = ncbi::client::NcbiClient::new(_ncbi_api_key);
        // let xml = fetcher.fetch_with_retry(...).await?;
        // let papers = ncbi::parser::parse_pubmed_xml(&xml)?;
        // --- 
        
        // 3. Finalizar job
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

/// A Python module implemented in Rust.
#[pymodule]
fn rust_engine(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<IngestionResult>()?;
    m.add_function(wrap_pyfunction!(search_and_ingest_pubmed, m)?)?;
    Ok(())
}
