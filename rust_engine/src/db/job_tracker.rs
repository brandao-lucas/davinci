use tokio_postgres::Client;

pub async fn update_job_status(
    client: &Client, 
    job_id: &str, 
    status: &str, 
    processed: i32, 
    error: Option<&str>
) -> Result<(), String> {
    let uuid_job_id = uuid::Uuid::parse_str(job_id)
        .map_err(|e| format!("Invalid UUID: {}", e))?;
    
    let err_str = error.unwrap_or("");

    client.execute(
        "UPDATE core_ingestionjob SET status = $1, records_processed = $2, error_message = $3 WHERE id = $4",
        &[&status, &processed, &err_str, &uuid_job_id],
    )
    .await
    .map_err(|e| format!("DB error: {}", e))?;
    Ok(())
}
