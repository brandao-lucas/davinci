use tokio_postgres::{Client, NoTls, Error};

pub async fn connect_db(url: &str) -> Result<Client, Error> {
    let (client, connection) = tokio_postgres::connect(url, NoTls).await?;
    
    // Spawn task to manage connection background work
    tokio::spawn(async move {
        if let Err(e) = connection.await {
            eprintln!("connection error: {}", e);
        }
    });

    Ok(client)
}
