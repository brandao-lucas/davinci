/// GEO supplementary file download.
///
/// # URL scheme
///
/// NCBI GEO supplementary files live at:
/// `https://ftp.ncbi.nlm.nih.gov/geo/series/<prefix>/<accession>/suppl/`
///
/// Where `<prefix>` is the accession with the last three digits replaced by
/// "nnn" (e.g. GSE12345 → GSE12nnn, GSE1234 → GSE1nnn).
///
/// # Directory listing
///
/// NCBI serves the FTP tree over HTTPS as a plain Apache/nginx directory index.
/// The index HTML contains `<a href="filename">` anchor tags for each file entry.
/// We parse only `<a href=...>` lines, skipping:
///   - Parent directory link (`../`)
///   - Query strings / anchors
///   - Entries with a trailing `/` (sub-directories — GEO suppl/ has none in practice)
///
/// # Behaviour on missing suppl/
///
/// If the suppl/ URL returns 404 or an empty listing, the function returns an
/// empty Vec without error — many GEO datasets have no supplementary files.
///
/// # File_kind parametrisation (F2 hook)
///
/// `file_kind` is accepted as a parameter and forwarded to the COPY writer so
/// that F2 (FASTQ/ENA) can call the same pyfunction with `file_kind="fastq"`
/// and route to a different fetch path without changing the DB schema or the
/// Python contract.

use std::path::{Path, PathBuf};

use crate::ncbi::client::{FileResult, NcbiClient};

/// A single supplementary file resolved from GEO.
pub struct GeoSupplFile {
    /// Remote HTTPS URL of the file on the NCBI FTP mirror.
    pub remote_url: String,
    /// File name (basename), e.g. `GSE12345_raw_counts.txt.gz`.
    pub file_name: String,
    /// Result after download (path, size, checksum).
    pub result: FileResult,
}

/// Build the HTTPS URL for the suppl/ directory of a GSE accession.
///
/// GSE12345   → https://ftp.ncbi.nlm.nih.gov/geo/series/GSE12nnn/GSE12345/suppl/
/// GSE1234    → https://ftp.ncbi.nlm.nih.gov/geo/series/GSE1nnn/GSE1234/suppl/
/// GSE123456  → https://ftp.ncbi.nlm.nih.gov/geo/series/GSE123nnn/GSE123456/suppl/
pub fn geo_suppl_url(accession: &str) -> String {
    // Strip the "GSE" prefix, replace last 3 digits with "nnn"
    let acc_upper = accession.to_uppercase();
    let prefix = if acc_upper.starts_with("GSE") { "GSE" } else { "" };
    let digits = &acc_upper[prefix.len()..];

    let nnn_prefix = if digits.len() > 3 {
        format!("{}{}", prefix, &digits[..digits.len() - 3])
    } else {
        prefix.to_string()
    };

    format!(
        "https://ftp.ncbi.nlm.nih.gov/geo/series/{}nnn/{}/suppl/",
        nnn_prefix, acc_upper
    )
}

/// Parse an Apache/nginx directory-index HTML page and extract file names.
///
/// Returns a Vec of bare file names (not full URLs).
/// Entries ending in `/` (sub-directories) and `../` (parent) are excluded.
///
/// This is the unit-testable pure function — no I/O.
pub fn parse_suppl_listing(html: &str) -> Vec<String> {
    let mut files = Vec::new();

    for line in html.lines() {
        // Look for <a href="..."> patterns
        let mut search_from = 0;
        while let Some(href_pos) = line[search_from..].to_lowercase().find("href=\"") {
            let abs_pos = search_from + href_pos + 6; // skip href="
            let rest = &line[abs_pos..];
            let end = match rest.find('"') {
                Some(p) => p,
                None => break,
            };
            let href = &rest[..end];
            search_from = abs_pos + end + 1;

            // Skip parent dir, query strings, anchors, sub-directories
            if href == "../"
                || href.starts_with('?')
                || href.starts_with('#')
                || href.starts_with("http")
                || href.ends_with('/')
            {
                continue;
            }

            // Decode simple %xx sequences (e.g. %20 for space)
            let decoded = percent_decode(href);
            if !decoded.is_empty() {
                files.push(decoded);
            }
        }
    }

    files
}

/// Minimal percent-decoder for file names (only handles %XX hex pairs).
fn percent_decode(s: &str) -> String {
    let mut result = String::with_capacity(s.len());
    let bytes = s.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let Ok(hex) = std::str::from_utf8(&bytes[i + 1..i + 3]) {
                if let Ok(byte_val) = u8::from_str_radix(hex, 16) {
                    result.push(byte_val as char);
                    i += 3;
                    continue;
                }
            }
        }
        result.push(bytes[i] as char);
        i += 1;
    }
    result
}

/// Download all supplementary files for a GEO dataset to `dest_dir`.
///
/// Steps:
/// 1. Fetch the suppl/ directory listing (HTTPS).
/// 2. Parse HTML → list of file names.
/// 3. For each file: call `NcbiClient::fetch_to_disk` to stream to `dest_dir/<file_name>`.
///
/// Returns `(files, errors)`:
/// - `files`: successfully downloaded `GeoSupplFile` entries.
/// - `errors`: non-fatal per-file error strings (empty = full success).
///
/// Empty listing (404 or blank) → `files` is empty, no error added.
pub async fn download_geo_supplementary(
    client: &NcbiClient,
    accession: &str,
    dest_dir: &Path,
) -> (Vec<GeoSupplFile>, Vec<String>) {
    let suppl_url = geo_suppl_url(accession);
    eprintln!("[downloader] GEO suppl listing: {}", suppl_url);

    // Fetch directory listing (empty body = no files)
    let listing_html = match client.fetch_with_retry(&suppl_url, &[]).await {
        Ok(html) => html,
        Err(e) => {
            // 404 or network error → treat as "no files", non-fatal
            if e.contains("404") || e.contains("404") {
                eprintln!("[downloader] {} suppl/ not found (404) — skipping", accession);
                return (vec![], vec![]);
            }
            eprintln!("[downloader] listing fetch error for {}: {}", accession, e);
            return (vec![], vec![format!("suppl listing error for {}: {}", accession, e)]);
        }
    };

    let file_names = parse_suppl_listing(&listing_html);
    if file_names.is_empty() {
        eprintln!("[downloader] {} suppl/ listing is empty", accession);
        return (vec![], vec![]);
    }

    eprintln!("[downloader] {} suppl/ has {} file(s)", accession, file_names.len());

    let mut downloaded: Vec<GeoSupplFile> = Vec::new();
    let mut errors: Vec<String> = Vec::new();

    for file_name in &file_names {
        let remote_url = format!("{}{}", suppl_url, file_name);
        let dest_path: PathBuf = dest_dir.join(file_name);

        eprintln!("[downloader] downloading {} → {:?}", remote_url, dest_path);
        match client.fetch_to_disk(&remote_url, &dest_path).await {
            Ok(result) => {
                eprintln!(
                    "[downloader] OK {} bytes, md5={}", result.size_bytes, result.checksum_md5
                );
                downloaded.push(GeoSupplFile {
                    remote_url,
                    file_name: file_name.clone(),
                    result,
                });
            }
            Err(e) => {
                eprintln!("[downloader] FAIL {}: {}", file_name, e);
                errors.push(format!("download failed for {}: {}", file_name, e));
            }
        }
    }

    (downloaded, errors)
}

// ─── ENA FASTQ download (F2) ─────────────────────────────────────────────────

/// One FASTQ file entry resolved from the ENA Portal filereport TSV.
///
/// A single SRR accession may have 1 file (single-end) or 2 files (R1 / R2,
/// paired-end). The ENA `fastq_ftp` column separates multiple URLs with `;`.
pub struct EnaFastqEntry {
    /// SRR / ERR / DRR accession (e.g. `SRR1234567`).
    pub run_accession: String,
    /// HTTPS URL constructed from the ENA FTP path.
    pub url: String,
    /// Declared file name (basename of the URL).
    pub file_name: String,
    /// Natural key for ON CONFLICT: `<run_accession>_<R-index>` (e.g. `SRR1234567_1`).
    pub accession_key: String,
    /// Expected MD5 as declared by ENA (lowercase hex); `None` if absent.
    pub expected_md5: Option<String>,
    /// Declared file size in bytes; `None` if absent.
    pub declared_bytes: Option<i64>,
}

/// ENA Portal filereport endpoint.
///
/// Returns a TSV with columns:
///   `run_accession  fastq_ftp  fastq_md5  fastq_bytes`
///
/// `fastq_ftp` may contain multiple paths separated by `;` (R1/R2).
/// `fastq_md5` and `fastq_bytes` mirror that with `;`-separated values.
///
/// Example line (condensed):
/// ```text
/// SRR1234567\tftp.sra.ebi.ac.uk/vol1/fastq/SRR123/007/SRR1234567/SRR1234567_1.fastq.gz;ftp.sra.ebi.ac.uk/.../SRR1234567_2.fastq.gz\tabc123;def456\t1234567;9876543
/// ```
pub fn ena_filereport_url(run_accession: &str) -> String {
    format!(
        "https://www.ebi.ac.uk/ena/portal/api/filereport\
         ?accession={}&result=read_run&fields=run_accession,fastq_ftp,fastq_md5,fastq_bytes&format=tsv",
        run_accession
    )
}

/// Convert an ENA FTP path to a downloadable HTTPS URL.
///
/// ENA `fastq_ftp` values look like:
///   `ftp.sra.ebi.ac.uk/vol1/fastq/SRR123/007/SRR1234567/SRR1234567_1.fastq.gz`
///
/// We serve them over HTTPS by replacing the FTP hostname with the HTTPS mirror:
///   `https://ftp.sra.ebi.ac.uk/vol1/fastq/...`
///
/// If the value already starts with `http://` or `https://`, it is returned
/// unchanged (future-proof for ENA API changes).
pub fn ena_ftp_to_https(ftp_path: &str) -> String {
    let p = ftp_path.trim();
    if p.starts_with("http://") || p.starts_with("https://") || p.starts_with("ftp://") {
        // Already has a scheme — return as-is (callers handle ftp:// if needed;
        // reqwest supports both ftp:// and https://).
        p.to_string()
    } else {
        // Bare hostname path: prefix with https://
        format!("https://{}", p)
    }
}

/// Parse the TSV body returned by the ENA Portal filereport API.
///
/// Expects a header row followed by data rows. Returns one `EnaFastqEntry`
/// per individual FASTQ file (i.e. R1 and R2 produce two entries per row).
///
/// Rows without a `fastq_ftp` value (column empty or `-`) are skipped
/// silently — these are runs where ENA has no FASTQ (e.g. bam-only uploads).
///
/// Column order is determined by the header, so the function is robust to
/// ENA adding or reordering columns in the future.
pub fn parse_ena_filereport_tsv(tsv: &str) -> Vec<EnaFastqEntry> {
    let mut entries = Vec::new();
    let mut lines = tsv.lines();

    // Parse header to find column indices
    let header_line = match lines.next() {
        Some(h) => h,
        None => return entries,
    };
    let headers: Vec<&str> = header_line.split('\t').collect();

    let col = |name: &str| headers.iter().position(|h| h.trim() == name);

    let idx_run = match col("run_accession") {
        Some(i) => i,
        None => return entries, // malformed header
    };
    let idx_ftp = col("fastq_ftp");
    let idx_md5 = col("fastq_md5");
    let idx_bytes = col("fastq_bytes");

    for line in lines {
        if line.trim().is_empty() {
            continue;
        }
        let fields: Vec<&str> = line.split('\t').collect();

        let run_accession = match fields.get(idx_run) {
            Some(s) if !s.trim().is_empty() => s.trim().to_string(),
            _ => continue,
        };

        // fastq_ftp may be absent or empty ("-" means no FASTQ from ENA)
        let ftp_raw = idx_ftp
            .and_then(|i| fields.get(i))
            .map(|s| s.trim())
            .unwrap_or("");

        if ftp_raw.is_empty() || ftp_raw == "-" {
            // No FASTQ available from ENA for this run
            continue;
        }

        let ftp_urls: Vec<&str> = ftp_raw.split(';').filter(|s| !s.trim().is_empty()).collect();
        let md5_vals: Vec<&str> = idx_md5
            .and_then(|i| fields.get(i))
            .map(|s| s.split(';').collect())
            .unwrap_or_default();
        let byte_vals: Vec<&str> = idx_bytes
            .and_then(|i| fields.get(i))
            .map(|s| s.split(';').collect())
            .unwrap_or_default();

        for (r_idx, ftp_path) in ftp_urls.iter().enumerate() {
            let url = ena_ftp_to_https(ftp_path.trim());
            // Basename = last path segment
            let file_name = url
                .split('/')
                .last()
                .unwrap_or(&url)
                .to_string();
            // Natural key: SRRxxxxxxx_<1-based index>
            let accession_key = format!("{}_{}", run_accession, r_idx + 1);

            let expected_md5 = md5_vals
                .get(r_idx)
                .map(|s| s.trim())
                .filter(|s| !s.is_empty() && *s != "-")
                .map(|s| s.to_lowercase());

            let declared_bytes = byte_vals
                .get(r_idx)
                .and_then(|s| s.trim().parse::<i64>().ok());

            entries.push(EnaFastqEntry {
                run_accession: run_accession.clone(),
                url,
                file_name,
                accession_key,
                expected_md5,
                declared_bytes,
            });
        }
    }

    entries
}

/// Result for a single FASTQ file download.
pub struct FastqFileResult {
    pub entry: EnaFastqEntry,
    /// Path on disk after successful download.
    pub local_path: String,
    /// Actual size in bytes on disk.
    pub size_bytes: u64,
    /// Computed MD5 (lowercase hex).
    pub checksum_md5: String,
    /// FK to `core_omicsample.id` (resolved from `entry.run_accession`).
    pub sample_id: i64,
}

/// Download all FASTQ files for the given list of (sample_id, srr_accession) pairs.
///
/// For each SRR:
/// 1. Query ENA Portal filereport to get FASTQ URLs and checksums.
/// 2. Download each file (R1/R2) via `NcbiClient::fetch_to_disk_resumable`.
/// 3. Validate MD5 against ENA's declared value when available.
///
/// Returns `(results, errors)`:
/// - `results`: successfully downloaded entries (may be empty).
/// - `errors`: non-fatal per-file/per-run error strings.
///
/// SRR runs for which ENA has no FASTQ are skipped without error — a note
/// is printed to stderr so operators can investigate (sra-tools fallback is
/// out of scope for F2).
pub async fn download_fastq_for_samples(
    client: &NcbiClient,
    samples: &[(i64, String)], // (sample_id, srr_accession)
    dest_dir: &Path,
) -> (Vec<FastqFileResult>, Vec<String>) {
    let mut results: Vec<FastqFileResult> = Vec::new();
    let mut errors: Vec<String> = Vec::new();

    for (sample_id, run_accession) in samples {
        eprintln!(
            "[downloader] ENA filereport for {}",
            run_accession
        );

        let filereport_url = ena_filereport_url(run_accession);
        let tsv = match client.fetch_with_retry(&filereport_url, &[]).await {
            Ok(body) => body,
            Err(e) => {
                let msg = format!(
                    "ENA filereport fetch failed for {}: {}",
                    run_accession, e
                );
                eprintln!("[downloader] {}", msg);
                errors.push(msg);
                continue;
            }
        };

        let entries = parse_ena_filereport_tsv(&tsv);

        if entries.is_empty() {
            eprintln!(
                "[downloader] {} has no FASTQ in ENA (bam-only or not indexed yet) — skipping",
                run_accession
            );
            // Non-fatal: note in stderr, not in errors (sra-tools fallback is future)
            continue;
        }

        eprintln!(
            "[downloader] {} → {} FASTQ file(s)",
            run_accession,
            entries.len()
        );

        for entry in entries {
            let dest_path = dest_dir.join(&entry.file_name);
            eprintln!(
                "[downloader] downloading {} → {:?}",
                entry.url, dest_path
            );

            // Check how many bytes are already on disk (for `bytes_downloaded` tracking)
            let pre_existing = tokio::fs::metadata(&dest_path)
                .await
                .map(|m| m.len())
                .unwrap_or(0);
            eprintln!(
                "[downloader] {} pre-existing bytes on disk: {}",
                entry.accession_key, pre_existing
            );

            match client.fetch_to_disk_resumable(&entry.url, &dest_path, None).await {
                Ok(file_result) => {
                    // Validate MD5 if ENA declared one
                    if let Some(ref expected) = entry.expected_md5 {
                        if !expected.is_empty() && file_result.checksum_md5 != *expected {
                            let msg = format!(
                                "MD5 mismatch for {}: expected {}, got {}",
                                entry.accession_key, expected, file_result.checksum_md5
                            );
                            eprintln!("[downloader] {}", msg);
                            errors.push(msg);
                            // Still record the file but with failed status
                            // (handled at call site by presence in errors)
                        } else {
                            eprintln!(
                                "[downloader] {} MD5 OK ({})",
                                entry.accession_key, file_result.checksum_md5
                            );
                        }
                    }

                    results.push(FastqFileResult {
                        entry,
                        local_path: file_result.path,
                        size_bytes: file_result.size_bytes,
                        checksum_md5: file_result.checksum_md5,
                        sample_id: *sample_id,
                    });
                }
                Err(e) => {
                    let msg = format!(
                        "FASTQ download failed for {}: {}",
                        entry.accession_key, e
                    );
                    eprintln!("[downloader] {}", msg);
                    errors.push(msg);
                }
            }
        }
    }

    (results, errors)
}

/// Fetch (sample_id, accession) pairs for all SRR samples belonging to `dataset_id`.
///
/// Queries `core_omicsample` for rows where `dataset_id = $1` and the `accession`
/// starts with one of the SRA prefixes (SRR, ERR, DRR). Returns only rows that
/// have a SRA-style run accession so that GEO GSM-only samples are excluded.
pub async fn fetch_srr_samples_for_dataset(
    db_client: &tokio_postgres::Client,
    dataset_id: i64,
) -> Result<Vec<(i64, String)>, String> {
    let rows = db_client
        .query(
            "SELECT id, accession FROM core_omicsample \
             WHERE dataset_id = $1 \
               AND (accession LIKE 'SRR%' OR accession LIKE 'ERR%' OR accession LIKE 'DRR%')",
            &[&dataset_id],
        )
        .await
        .map_err(|e| format!("DB query for SRR samples failed: {:?}", e))?;

    Ok(rows
        .into_iter()
        .map(|r| (r.get::<_, i64>(0), r.get::<_, String>(1)))
        .collect())
}

// ─── Unit tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_geo_suppl_url_5digit() {
        let url = geo_suppl_url("GSE12345");
        assert_eq!(
            url,
            "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE12nnn/GSE12345/suppl/"
        );
    }

    #[test]
    fn test_geo_suppl_url_4digit() {
        let url = geo_suppl_url("GSE1234");
        assert_eq!(
            url,
            "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE1nnn/GSE1234/suppl/"
        );
    }

    #[test]
    fn test_geo_suppl_url_6digit() {
        let url = geo_suppl_url("GSE123456");
        assert_eq!(
            url,
            "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE123nnn/GSE123456/suppl/"
        );
    }

    #[test]
    fn test_geo_suppl_url_lowercase_normalized() {
        let url = geo_suppl_url("gse12345");
        assert_eq!(
            url,
            "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE12nnn/GSE12345/suppl/"
        );
    }

    #[test]
    fn test_parse_suppl_listing_typical() {
        let html = r#"
<!DOCTYPE HTML>
<html>
<head><title>Index of /geo/series/GSE12nnn/GSE12345/suppl</title></head>
<body>
<h1>Index of /geo/series/GSE12nnn/GSE12345/suppl</h1>
<pre>      <a href="?C=N;O=D">Name</a>  <a href="?C=M;O=A">Last modified</a>  Size
<hr><a href="../">../</a>
<a href="GSE12345_raw_counts.txt.gz">GSE12345_raw_counts.txt.gz</a>  2023-01-15 10:22   1.2M
<a href="GSE12345_metadata.csv.gz">GSE12345_metadata.csv.gz</a>    2023-01-15 10:22   45K
<hr></pre>
</body>
</html>"#;
        let files = parse_suppl_listing(html);
        assert_eq!(files.len(), 2);
        assert!(files.contains(&"GSE12345_raw_counts.txt.gz".to_string()));
        assert!(files.contains(&"GSE12345_metadata.csv.gz".to_string()));
    }

    #[test]
    fn test_parse_suppl_listing_empty() {
        let html = r#"
<html><body>
<a href="../">../</a>
</body></html>"#;
        let files = parse_suppl_listing(html);
        assert!(files.is_empty());
    }

    #[test]
    fn test_parse_suppl_listing_skips_subdirs() {
        let html = r#"
<a href="../">../</a>
<a href="subdir/">subdir/</a>
<a href="file.txt.gz">file.txt.gz</a>
"#;
        let files = parse_suppl_listing(html);
        assert_eq!(files.len(), 1);
        assert_eq!(files[0], "file.txt.gz");
    }

    #[test]
    fn test_parse_suppl_listing_skips_query_anchors() {
        // Use r##"..."## so the inner "#top" does not close the raw string
        let html = r##"
<a href="?sort=name">Sort</a>
<a href="#top">Top</a>
<a href="real_file.tar.gz">real_file.tar.gz</a>
"##;
        let files = parse_suppl_listing(html);
        assert_eq!(files.len(), 1);
        assert_eq!(files[0], "real_file.tar.gz");
    }

    #[test]
    fn test_parse_suppl_listing_skips_absolute_urls() {
        let html = r#"
<a href="https://example.com/file.gz">external</a>
<a href="local_file.gz">local_file.gz</a>
"#;
        let files = parse_suppl_listing(html);
        assert_eq!(files.len(), 1);
        assert_eq!(files[0], "local_file.gz");
    }

    #[test]
    fn test_parse_suppl_listing_percent_decode() {
        let html = r#"<a href="GSE12345%20raw.txt.gz">GSE12345 raw.txt.gz</a>"#;
        let files = parse_suppl_listing(html);
        assert_eq!(files.len(), 1);
        assert_eq!(files[0], "GSE12345 raw.txt.gz");
    }

    #[test]
    fn test_geo_suppl_url_3digit() {
        // Edge case: fewer than 3 digits — prefix becomes just "GSE"
        let url = geo_suppl_url("GSE99");
        assert_eq!(
            url,
            "https://ftp.ncbi.nlm.nih.gov/geo/series/GSEnnn/GSE99/suppl/"
        );
    }

    // ─── ENA parser tests ─────────────────────────────────────────────────────

    #[test]
    fn test_ena_ftp_to_https_bare_path() {
        let url = ena_ftp_to_https(
            "ftp.sra.ebi.ac.uk/vol1/fastq/SRR123/007/SRR1234567/SRR1234567_1.fastq.gz",
        );
        assert_eq!(
            url,
            "https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR123/007/SRR1234567/SRR1234567_1.fastq.gz"
        );
    }

    #[test]
    fn test_ena_ftp_to_https_already_https() {
        let url = ena_ftp_to_https("https://example.com/file.fastq.gz");
        assert_eq!(url, "https://example.com/file.fastq.gz");
    }

    #[test]
    fn test_ena_ftp_to_https_ftp_scheme() {
        let url = ena_ftp_to_https("ftp://ftp.sra.ebi.ac.uk/vol1/fastq/file.fastq.gz");
        assert_eq!(url, "ftp://ftp.sra.ebi.ac.uk/vol1/fastq/file.fastq.gz");
    }

    #[test]
    fn test_ena_filereport_url() {
        let url = ena_filereport_url("SRR1234567");
        assert!(url.contains("accession=SRR1234567"));
        assert!(url.contains("fastq_ftp"));
        assert!(url.contains("fastq_md5"));
        assert!(url.contains("fastq_bytes"));
    }

    /// Paired-end run: two FASTQ files (R1/R2) separated by `;`.
    #[test]
    fn test_parse_ena_tsv_paired_end() {
        let tsv = "run_accession\tfastq_ftp\tfastq_md5\tfastq_bytes\n\
            SRR1234567\t\
            ftp.sra.ebi.ac.uk/vol1/fastq/SRR123/007/SRR1234567/SRR1234567_1.fastq.gz;\
            ftp.sra.ebi.ac.uk/vol1/fastq/SRR123/007/SRR1234567/SRR1234567_2.fastq.gz\t\
            aabbcc112233aabbcc112233aabbcc11;ddeeff445566ddeeff445566ddeeff44\t\
            1234567;9876543\n";

        let entries = parse_ena_filereport_tsv(tsv);
        assert_eq!(entries.len(), 2, "should produce 2 entries for R1/R2");

        let r1 = &entries[0];
        assert_eq!(r1.run_accession, "SRR1234567");
        assert_eq!(r1.accession_key, "SRR1234567_1");
        assert_eq!(
            r1.url,
            "https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR123/007/SRR1234567/SRR1234567_1.fastq.gz"
        );
        assert_eq!(r1.file_name, "SRR1234567_1.fastq.gz");
        assert_eq!(
            r1.expected_md5,
            Some("aabbcc112233aabbcc112233aabbcc11".to_string())
        );
        assert_eq!(r1.declared_bytes, Some(1234567));

        let r2 = &entries[1];
        assert_eq!(r2.accession_key, "SRR1234567_2");
        assert_eq!(
            r2.url,
            "https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR123/007/SRR1234567/SRR1234567_2.fastq.gz"
        );
        assert_eq!(
            r2.expected_md5,
            Some("ddeeff445566ddeeff445566ddeeff44".to_string())
        );
        assert_eq!(r2.declared_bytes, Some(9876543));
    }

    /// Single-end run: one FASTQ file, no semicolon.
    #[test]
    fn test_parse_ena_tsv_single_end() {
        let tsv = "run_accession\tfastq_ftp\tfastq_md5\tfastq_bytes\n\
            SRR9999001\t\
            ftp.sra.ebi.ac.uk/vol1/fastq/SRR999/001/SRR9999001/SRR9999001.fastq.gz\t\
            ff00ff00ff00ff00ff00ff00ff00ff00\t\
            55000000\n";

        let entries = parse_ena_filereport_tsv(tsv);
        assert_eq!(entries.len(), 1);
        let e = &entries[0];
        assert_eq!(e.accession_key, "SRR9999001_1");
        assert_eq!(e.file_name, "SRR9999001.fastq.gz");
        assert_eq!(e.declared_bytes, Some(55000000));
    }

    /// Run with no FASTQ in ENA (empty fastq_ftp column).
    #[test]
    fn test_parse_ena_tsv_no_fastq_empty() {
        let tsv = "run_accession\tfastq_ftp\tfastq_md5\tfastq_bytes\n\
            SRR0000001\t\t\t\n";
        let entries = parse_ena_filereport_tsv(tsv);
        assert!(entries.is_empty(), "empty fastq_ftp should produce no entries");
    }

    /// Run with fastq_ftp set to dash placeholder.
    #[test]
    fn test_parse_ena_tsv_no_fastq_dash() {
        let tsv = "run_accession\tfastq_ftp\tfastq_md5\tfastq_bytes\n\
            SRR0000002\t-\t-\t-\n";
        let entries = parse_ena_filereport_tsv(tsv);
        assert!(entries.is_empty(), "'-' fastq_ftp should produce no entries");
    }

    /// Multiple runs in a single TSV (batch filereport not used in F2, but
    /// parser should handle it correctly for robustness).
    #[test]
    fn test_parse_ena_tsv_multiple_runs() {
        let tsv = "run_accession\tfastq_ftp\tfastq_md5\tfastq_bytes\n\
            SRR0000010\tftp.sra.ebi.ac.uk/a/SRR0000010.fastq.gz\tabc\t100\n\
            SRR0000011\tftp.sra.ebi.ac.uk/b/SRR0000011_1.fastq.gz;ftp.sra.ebi.ac.uk/b/SRR0000011_2.fastq.gz\tdef;ghi\t200;300\n";
        let entries = parse_ena_filereport_tsv(tsv);
        assert_eq!(entries.len(), 3); // 1 + 2
        assert_eq!(entries[0].accession_key, "SRR0000010_1");
        assert_eq!(entries[1].accession_key, "SRR0000011_1");
        assert_eq!(entries[2].accession_key, "SRR0000011_2");
    }

    /// MD5 absent (no md5 column) — should not panic.
    #[test]
    fn test_parse_ena_tsv_missing_md5_column() {
        let tsv = "run_accession\tfastq_ftp\tfastq_bytes\n\
            SRR5555555\tftp.sra.ebi.ac.uk/x/SRR5555555.fastq.gz\t999\n";
        let entries = parse_ena_filereport_tsv(tsv);
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].expected_md5, None);
        assert_eq!(entries[0].declared_bytes, Some(999));
    }

    /// Empty TSV (just header, no data rows).
    #[test]
    fn test_parse_ena_tsv_empty_body() {
        let tsv = "run_accession\tfastq_ftp\tfastq_md5\tfastq_bytes\n";
        let entries = parse_ena_filereport_tsv(tsv);
        assert!(entries.is_empty());
    }

    /// Completely empty string.
    #[test]
    fn test_parse_ena_tsv_totally_empty() {
        let entries = parse_ena_filereport_tsv("");
        assert!(entries.is_empty());
    }
}
