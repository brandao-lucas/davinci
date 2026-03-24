use bytes::Bytes;
use std::collections::{HashMap, HashSet};
use tokio_postgres::Client;

use crate::omics::models::{DatasetPaperLinkData, OmicDatasetData};

// ─── Public API ──────────────────────────────────────────────────────────────

/// Bulk-upsert omics datasets into `core_omicdataset`.
///
/// Uses a temp staging table + `COPY FROM STDIN` + `INSERT … ON CONFLICT DO UPDATE`
/// to achieve upsert semantics with bulk performance.
/// The FTS trigger on `core_omicdataset` automatically updates `search_vector`.
///
/// Returns the number of rows inserted or updated.
pub async fn copy_omic_datasets(
    client: &Client,
    datasets: &[OmicDatasetData],
) -> Result<u64, tokio_postgres::Error> {
    if datasets.is_empty() {
        return Ok(0);
    }

    // Create temp staging table with only the columns we write.
    // ON COMMIT DROP is not needed here since we run outside an explicit transaction;
    // we use DROP IF EXISTS + CREATE to keep it clean across retries.
    client
        .execute("DROP TABLE IF EXISTS _staging_omicdataset", &[])
        .await?;
    client
        .execute(
            "CREATE TEMP TABLE _staging_omicdataset (
                accession        VARCHAR(50),
                source_db        VARCHAR(20),
                bioproject_id    VARCHAR(50),
                title            TEXT,
                summary          TEXT,
                omic_type        VARCHAR(20),
                omic_subcategory VARCHAR(200),
                organism         VARCHAR(200),
                tax_id           INTEGER,
                n_samples        INTEGER,
                platform         VARCHAR(200),
                extra_metadata   JSONB,
                is_active        BOOLEAN
            )",
            &[],
        )
        .await?;

    // Build CSV payload and COPY into staging
    let csv = build_dataset_csv(datasets);
    bulk_insert_csv(
        client,
        "COPY _staging_omicdataset (
            accession, source_db, bioproject_id, title, summary,
            omic_type, omic_subcategory, organism, tax_id, n_samples,
            platform, extra_metadata, is_active
        ) FROM STDIN WITH (FORMAT csv, NULL 'NULL')",
        &csv,
    )
    .await?;

    // Upsert from staging into the real table.
    // search_vector, ingested_at, updated_at handled by Postgres defaults/triggers.
    let affected = client
        .execute(
            "INSERT INTO core_omicdataset (
                accession, source_db, bioproject_id, title, summary,
                omic_type, omic_subcategory, organism, tax_id, n_samples,
                platform, extra_metadata, is_active
            )
            SELECT
                accession, source_db, bioproject_id, title, summary,
                omic_type, omic_subcategory, organism, tax_id, n_samples,
                platform, extra_metadata, is_active
            FROM _staging_omicdataset
            ON CONFLICT (accession) DO UPDATE SET
                title            = EXCLUDED.title,
                summary          = EXCLUDED.summary,
                omic_type        = EXCLUDED.omic_type,
                omic_subcategory = EXCLUDED.omic_subcategory,
                organism         = EXCLUDED.organism,
                tax_id           = EXCLUDED.tax_id,
                n_samples        = EXCLUDED.n_samples,
                platform         = EXCLUDED.platform,
                extra_metadata   = EXCLUDED.extra_metadata,
                updated_at       = NOW()",
            &[],
        )
        .await?;

    client
        .execute("DROP TABLE IF EXISTS _staging_omicdataset", &[])
        .await?;

    Ok(affected)
}

/// Bulk-insert dataset↔paper links into `core_datasetpaperlink`.
///
/// Resolves `dataset_accession` → `dataset_id` and `paper_pmid` → `paper_id`
/// via SQL lookups. Silently skips links where either FK cannot be resolved
/// (i.e. paper not yet ingested or dataset insert failed).
///
/// Uses ON CONFLICT DO NOTHING so re-ingestion is idempotent.
///
/// Returns the number of new links inserted.
pub async fn copy_dataset_paper_links(
    client: &Client,
    links: &[DatasetPaperLinkData],
) -> Result<u64, tokio_postgres::Error> {
    if links.is_empty() {
        return Ok(0);
    }

    // Collect unique accessions and PMIDs for bulk FK resolution
    let accessions: Vec<String> = links
        .iter()
        .map(|l| l.dataset_accession.clone())
        .collect::<HashSet<_>>()
        .into_iter()
        .collect();

    let pmids: Vec<i64> = links
        .iter()
        .map(|l| l.paper_pmid)
        .collect::<HashSet<_>>()
        .into_iter()
        .collect();

    // Resolve accession → dataset_id
    let rows = client
        .query(
            "SELECT id, accession FROM core_omicdataset WHERE accession = ANY($1)",
            &[&accessions],
        )
        .await?;
    let dataset_ids: HashMap<String, i64> = rows
        .into_iter()
        .map(|r| (r.get::<_, String>(1), r.get::<_, i64>(0)))
        .collect();

    // Resolve pmid → paper_id
    let rows = client
        .query(
            "SELECT id, pmid FROM core_paper WHERE pmid = ANY($1)",
            &[&pmids],
        )
        .await?;
    let paper_ids: HashMap<i64, i64> = rows
        .into_iter()
        .map(|r| (r.get::<_, i64>(1), r.get::<_, i64>(0)))
        .collect();

    // Build CSV with only fully-resolved links
    let mut csv = String::new();
    for link in links {
        let dataset_id = match dataset_ids.get(&link.dataset_accession) {
            Some(id) => *id,
            None => continue, // dataset not found
        };
        let paper_id = match paper_ids.get(&link.paper_pmid) {
            Some(id) => *id,
            None => continue, // paper not yet ingested
        };
        csv.push_str(&format!(
            "{},{},{}\n",
            dataset_id,
            paper_id,
            escape_csv_field(&link.link_source)
        ));
    }

    if csv.is_empty() {
        return Ok(0);
    }

    // Staging table for link upsert
    client
        .execute("DROP TABLE IF EXISTS _staging_datasetlink", &[])
        .await?;
    client
        .execute(
            "CREATE TEMP TABLE _staging_datasetlink (
                dataset_id  BIGINT,
                paper_id    BIGINT,
                link_source VARCHAR(50)
            )",
            &[],
        )
        .await?;

    bulk_insert_csv(
        client,
        "COPY _staging_datasetlink (dataset_id, paper_id, link_source) FROM STDIN WITH (FORMAT csv)",
        &csv,
    )
    .await?;

    let affected = client
        .execute(
            "INSERT INTO core_datasetpaperlink (dataset_id, paper_id, link_source)
             SELECT dataset_id, paper_id, link_source FROM _staging_datasetlink
             ON CONFLICT (dataset_id, paper_id) DO NOTHING",
            &[],
        )
        .await?;

    client
        .execute("DROP TABLE IF EXISTS _staging_datasetlink", &[])
        .await?;

    Ok(affected)
}

/// Low-level COPY FROM STDIN helper.
///
/// Sends `csv_data` bytes into the table via the Postgres COPY protocol.
/// The caller provides the complete COPY SQL including column list and options.
///
/// `CopyInSink` is `!Unpin`. We use `std::pin::pin!()` to create a
/// `Pin<&mut CopyInSink<T>>`, which IS `Unpin` (all pinned references are Unpin),
/// enabling `SinkExt::send` and `finish()` (which takes `Pin<&mut Self>`).
pub async fn bulk_insert_csv(
    client: &Client,
    copy_query: &str,
    csv_data: &str,
) -> Result<u64, tokio_postgres::Error> {
    use futures::SinkExt;
    use std::pin::pin;

    let sink = client.copy_in(copy_query).await?;
    let mut sink = pin!(sink); // Pin<&mut CopyInSink<Bytes>>: Unpin

    sink.send(Bytes::from(csv_data.as_bytes().to_vec())).await?;
    sink.finish().await
}

// ─── CSV helpers ─────────────────────────────────────────────────────────────

fn build_dataset_csv(datasets: &[OmicDatasetData]) -> String {
    let mut csv = String::new();
    for d in datasets {
        let extra_str =
            serde_json::to_string(&d.extra_metadata).unwrap_or_else(|_| "{}".to_string());
        let row = format!(
            "{},{},{},{},{},{},{},{},{},{},{},{},{}\n",
            escape_csv_field(&d.accession),
            escape_csv_field(&d.source_db),
            escape_csv_field(&d.bioproject_id),
            escape_csv_field(&d.title),
            escape_csv_field(&d.summary),
            escape_csv_field(&d.omic_type),
            escape_csv_field(&d.omic_subcategory),
            escape_csv_field(&d.organism),
            d.tax_id.map_or("NULL".to_string(), |v| v.to_string()),
            d.n_samples.map_or("NULL".to_string(), |v| v.to_string()),
            escape_csv_field(&d.platform),
            escape_csv_field(&extra_str),
            if d.is_active { "t" } else { "f" },
        );
        csv.push_str(&row);
    }
    csv
}

/// Wrap a CSV field in double-quotes if it contains special characters.
/// Internal double-quotes are escaped by doubling them (RFC 4180).
fn escape_csv_field(s: &str) -> String {
    if s.contains(',') || s.contains('"') || s.contains('\n') || s.contains('\r') {
        format!("\"{}\"", s.replace('"', "\"\""))
    } else {
        s.to_string()
    }
}
