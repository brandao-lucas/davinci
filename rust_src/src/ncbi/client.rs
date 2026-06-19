use futures::StreamExt;
use md5::{Digest, Md5};
use reqwest::{Client, StatusCode};
use std::path::Path;
use std::time::Duration;
use tokio::io::AsyncWriteExt;
use tokio::time::sleep;

pub struct NcbiClient {
    client: Client,
    api_key: Option<String>,
}

/// Result returned by `fetch_to_disk`.
pub struct FileResult {
    /// Absolute path where the file was written on disk.
    pub path: String,
    /// Number of bytes written.
    pub size_bytes: u64,
    /// Lowercase hex MD5 of the downloaded content.
    pub checksum_md5: String,
}

impl NcbiClient {
    pub fn new(api_key: Option<String>) -> Self {
        Self {
            client: Client::builder()
                .timeout(Duration::from_secs(600)) // 10 min for large files
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

    /// Download a URL to `dest_path` with **resume support** (HTTP Range request).
    ///
    /// If `dest_path` already exists and has N > 0 bytes, sends
    /// `Range: bytes=N-` and appends the response body to the file.
    ///
    /// # MD5 strategy on resume
    ///
    /// MD5 is **not incremental across restarts**: a partial file has no stored
    /// hasher state. On resume we re-read all bytes already on disk to reconstruct
    /// the hasher state, then continue feeding incoming chunks. This is correct and
    /// simple — the re-read overhead is proportional to the already-downloaded size,
    /// which in the worst case is the full file size minus the last chunk. For
    /// FASTQ files (GB range) this is a one-time cost per resume and is acceptable
    /// given that resumes are rare (network failures mid-download).
    ///
    /// If the server returns `200 OK` instead of `206 Partial Content` (i.e. it
    /// ignored the Range header), we truncate the file and restart from scratch
    /// to avoid data corruption.
    ///
    /// # Progress tracking
    ///
    /// Returns `FileResult.size_bytes` = total bytes on disk after completion.
    /// Callers that want to persist progress mid-download should call
    /// `fetch_to_disk_resumable` (same implementation, alias) and read the file
    /// size from disk; no in-memory progress counter is exposed.
    ///
    /// Returns `FileResult { path, size_bytes, checksum_md5 }`.
    pub async fn fetch_to_disk_resumable(
        &self,
        url: &str,
        dest_path: &Path,
        // Callback invoked after each chunk: (bytes_written_so_far).
        // Use `None` when progress tracking is not needed.
        mut progress_cb: Option<&mut (dyn FnMut(u64) + Send)>,
    ) -> Result<FileResult, String> {
        let mut retries = 0usize;
        let max_retries = 5;
        let mut backoff = Duration::from_millis(500);

        loop {
            // Check for existing partial file
            let existing_bytes = match tokio::fs::metadata(dest_path).await {
                Ok(m) if m.len() > 0 => m.len(),
                _ => 0,
            };

            // Build request — with Range header if resuming
            let mut req = self.client.get(url);
            if existing_bytes > 0 {
                req = req.header("Range", format!("bytes={}-", existing_bytes));
            }

            let response = match req.send().await {
                Ok(r) => r,
                Err(e) => {
                    if retries >= max_retries {
                        return Err(format!(
                            "fetch_to_disk_resumable: request failed after {} retries: {}",
                            max_retries, e
                        ));
                    }
                    eprintln!(
                        "[ncbi_client] fetch_to_disk_resumable request error (retry {}/{}): {}",
                        retries + 1,
                        max_retries,
                        e
                    );
                    sleep(backoff).await;
                    retries += 1;
                    backoff *= 2;
                    continue;
                }
            };

            let status = response.status();

            if status == StatusCode::TOO_MANY_REQUESTS {
                if retries >= max_retries {
                    return Err("fetch_to_disk_resumable: max retries exceeded (429)".to_string());
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

            // Handle resume vs. full download
            let (mut file, mut hasher, mut size_bytes) = if status == StatusCode::PARTIAL_CONTENT
                && existing_bytes > 0
            {
                // Server honoured Range — open file for append and seed hasher
                // by re-reading the bytes already on disk.
                eprintln!(
                    "[ncbi_client] resuming {} from byte {}",
                    url, existing_bytes
                );

                // Reconstruct MD5 state from partial file
                let mut hasher = Md5::new();
                {
                    let existing_data = tokio::fs::read(dest_path)
                        .await
                        .map_err(|e| format!("re-read partial file {:?}: {}", dest_path, e))?;
                    hasher.update(&existing_data);
                }

                // Open for append
                let file = tokio::fs::OpenOptions::new()
                    .append(true)
                    .open(dest_path)
                    .await
                    .map_err(|e| format!("open for append {:?}: {}", dest_path, e))?;

                (file, hasher, existing_bytes)
            } else if status.is_success() {
                // Either 200 OK (server ignored Range) or fresh download —
                // truncate and start over.
                if existing_bytes > 0 {
                    eprintln!(
                        "[ncbi_client] server returned {} (not 206) for Range request — restarting {}",
                        status, url
                    );
                }
                if let Some(parent) = dest_path.parent() {
                    tokio::fs::create_dir_all(parent)
                        .await
                        .map_err(|e| format!("create_dir_all {:?}: {}", parent, e))?;
                }
                let file = tokio::fs::File::create(dest_path)
                    .await
                    .map_err(|e| format!("create file {:?}: {}", dest_path, e))?;
                (file, Md5::new(), 0u64)
            } else {
                return Err(format!(
                    "fetch_to_disk_resumable: HTTP {} for {}",
                    status, url
                ));
            };

            let mut stream = response.bytes_stream();

            'stream: loop {
                match stream.next().await {
                    Some(Ok(chunk)) => {
                        size_bytes += chunk.len() as u64;
                        hasher.update(&chunk);
                        if let Err(e) = file.write_all(&chunk).await {
                            return Err(format!("write chunk to {:?}: {}", dest_path, e));
                        }
                        if let Some(cb) = progress_cb.as_mut() {
                            cb(size_bytes);
                        }
                    }
                    Some(Err(e)) => {
                        if retries >= max_retries {
                            return Err(format!(
                                "fetch_to_disk_resumable: stream error after {} retries: {}",
                                max_retries, e
                            ));
                        }
                        eprintln!(
                            "[ncbi_client] stream error (retry {}/{}): {}",
                            retries + 1,
                            max_retries,
                            e
                        );
                        drop(file);
                        sleep(backoff).await;
                        retries += 1;
                        backoff *= 2;
                        break 'stream; // retry outer loop
                    }
                    None => {
                        // Stream complete
                        file.flush()
                            .await
                            .map_err(|e| format!("flush {:?}: {}", dest_path, e))?;
                        let digest = hasher.finalize();
                        let checksum_md5 = format!("{:x}", digest);
                        let path = dest_path
                            .to_str()
                            .ok_or_else(|| "dest_path is not valid UTF-8".to_string())?
                            .to_string();
                        return Ok(FileResult {
                            path,
                            size_bytes,
                            checksum_md5,
                        });
                    }
                }
            }
            // 'stream broke → retry outer 'retry loop
        }
    }

    /// Download a URL to `dest_path` using streaming (chunk-by-chunk).
    ///
    /// Never buffers the full body in memory: chunks are written incrementally
    /// via `AsyncWriteExt` and fed to an incremental MD5 hasher.
    ///
    /// Reuses the same 429 / connection-error retry logic as `fetch_with_retry`.
    /// GEO FTP/HTTPS does not use `api_key` — the key is intentionally NOT added
    /// to GEO URLs to avoid polluting NCBI API quotas with binary downloads.
    ///
    /// Returns `FileResult { path, size_bytes, checksum_md5 }`.
    pub async fn fetch_to_disk(&self, url: &str, dest_path: &Path) -> Result<FileResult, String> {
        let mut retries = 0usize;
        let max_retries = 5;
        let mut backoff = Duration::from_millis(500);

        loop {
            // Build request — deliberately no api_key for FTP/GEO binary endpoints
            let response = match self.client.get(url).send().await {
                Ok(r) => r,
                Err(e) => {
                    if retries >= max_retries {
                        return Err(format!("Download request failed after {} retries: {}", max_retries, e));
                    }
                    eprintln!("[ncbi_client] fetch_to_disk request error (retry {}/{}): {}", retries + 1, max_retries, e);
                    sleep(backoff).await;
                    retries += 1;
                    backoff *= 2;
                    continue;
                }
            };

            if response.status() == StatusCode::TOO_MANY_REQUESTS {
                if retries >= max_retries {
                    return Err("fetch_to_disk: max retries exceeded (429)".to_string());
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
                return Err(format!("fetch_to_disk: HTTP {} for {}", response.status(), url));
            }

            // Open destination file (create / truncate)
            if let Some(parent) = dest_path.parent() {
                tokio::fs::create_dir_all(parent)
                    .await
                    .map_err(|e| format!("create_dir_all {:?}: {}", parent, e))?;
            }
            let mut file = tokio::fs::File::create(dest_path)
                .await
                .map_err(|e| format!("create file {:?}: {}", dest_path, e))?;

            let mut hasher = Md5::new();
            let mut size_bytes: u64 = 0;
            let mut stream = response.bytes_stream();

            loop {
                match stream.next().await {
                    Some(Ok(chunk)) => {
                        size_bytes += chunk.len() as u64;
                        hasher.update(&chunk);
                        file.write_all(&chunk)
                            .await
                            .map_err(|e| format!("write chunk to {:?}: {}", dest_path, e))?;
                    }
                    Some(Err(e)) => {
                        // Body stream error — retry from the top (file is truncated on next open)
                        if retries >= max_retries {
                            return Err(format!("fetch_to_disk stream error after {} retries: {}", max_retries, e));
                        }
                        eprintln!("[ncbi_client] stream error (retry {}/{}): {}", retries + 1, max_retries, e);
                        drop(file);
                        sleep(backoff).await;
                        retries += 1;
                        backoff *= 2;
                        break; // break inner loop → retry outer loop
                    }
                    None => {
                        // Stream exhausted — flush and return
                        file.flush()
                            .await
                            .map_err(|e| format!("flush {:?}: {}", dest_path, e))?;
                        let digest = hasher.finalize();
                        let checksum_md5 = format!("{:x}", digest);
                        let path = dest_path
                            .to_str()
                            .ok_or_else(|| "dest_path is not valid UTF-8".to_string())?
                            .to_string();
                        return Ok(FileResult { path, size_bytes, checksum_md5 });
                    }
                }
            }
            // Only reached when inner loop `break`s for retry
        }
    }
}
