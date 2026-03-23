use reqwest::{Client, StatusCode};
use std::time::Duration;
use tokio::time::sleep;

pub struct NcbiClient {
    client: Client,
    api_key: Option<String>,
}

impl NcbiClient {
    pub fn new(api_key: Option<String>) -> Self {
        Self {
            client: Client::builder().timeout(Duration::from_secs(60)).build().unwrap(),
            api_key,
        }
    }

    pub async fn fetch_with_retry(&self, url: &str, params: &[(&str, &str)]) -> Result<String, String> {
        let mut retries = 0;
        let max_retries = 5;
        let mut backoff = Duration::from_millis(500);

        loop {
            let mut req = self.client.get(url).query(params);
            if let Some(key) = &self.api_key {
                req = req.query(&[("api_key", key)]);
            }

            let response = req.send().await.map_err(|e| e.to_string())?;

            if response.status() == StatusCode::TOO_MANY_REQUESTS {
                if retries >= max_retries {
                    return Err("Max retries exceeded for NCBI API".to_string());
                }
                
                let retry_after = response
                    .headers()
                    .get("retry-after")
                    .and_then(|v| v.to_str().ok())
                    .and_then(|s| s.parse::<u64>().ok())
                    .map(Duration::from_secs)
                    .unwrap_or(backoff);
                
                sleep(retry_after).await;
                retries += 1;
                backoff *= 2;
                continue;
            }

            if !response.status().is_success() {
                return Err(format!("NCBI request failed with status: {}", response.status()));
            }

            return response.text().await.map_err(|e| e.to_string());
        }
    }
}
