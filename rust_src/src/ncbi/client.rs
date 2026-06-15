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
            client: Client::builder()
                .timeout(Duration::from_secs(120))
                .build()
                .unwrap(),
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

            let response = match req.send().await {
                Ok(r) => r,
                Err(e) => {
                    // Retry on connection/timeout errors
                    if retries >= max_retries {
                        return Err(format!("NCBI request failed after {} retries: {}", max_retries, e));
                    }
                    eprintln!("[ncbi_client] Request error (retry {}/{}): {}", retries + 1, max_retries, e);
                    sleep(backoff).await;
                    retries += 1;
                    backoff *= 2;
                    continue;
                }
            };

            if response.status() == StatusCode::TOO_MANY_REQUESTS {
                if retries >= max_retries {
                    return Err("Max retries exceeded for NCBI API (429)".to_string());
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

            match response.bytes().await {
                Ok(bytes) => return Ok(String::from_utf8_lossy(&bytes).into_owned()),
                Err(e) => {
                    // Retry on body decode errors
                    if retries >= max_retries {
                        return Err(format!("NCBI response body read failed after {} retries: {}", max_retries, e));
                    }
                    eprintln!("[ncbi_client] Body read error (retry {}/{}): {}", retries + 1, max_retries, e);
                    sleep(backoff).await;
                    retries += 1;
                    backoff *= 2;
                    continue;
                }
            }
        }
    }
}
