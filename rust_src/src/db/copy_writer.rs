use bytes::Bytes;
use chrono::Utc;
use std::collections::{HashMap, HashSet};
use tokio_postgres::Client;

use crate::ncbi::models::PaperData;
use crate::omics::models::{DatasetPaperLinkData, OmicDatasetData, OmicSampleData};

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

    // --- Step 1: Merge within the batch ---
    let mut merged: HashMap<String, OmicDatasetData> = HashMap::new();
    for d in datasets {
        merged
            .entry(d.accession.clone())
            .and_modify(|existing| {
                // Merge omic_type strings (comma-separated sets)
                let mut types: HashSet<&str> = existing.omic_type.split(',').filter(|s| !s.is_empty()).collect();
                for t in d.omic_type.split(',').filter(|s| !s.is_empty()) {
                    types.insert(t);
                }
                let mut types_vec: Vec<_> = types.into_iter().collect();
                types_vec.sort();
                existing.omic_type = types_vec.join(",");

                // Merge subcategories
                let mut subs: HashSet<&str> = existing.omic_subcategory.split(',').filter(|s| !s.is_empty()).collect();
                for s in d.omic_subcategory.split(',').filter(|s| !s.is_empty()) {
                    subs.insert(s);
                }
                let mut subs_vec: Vec<_> = subs.into_iter().collect();
                subs_vec.sort();
                existing.omic_subcategory = subs_vec.join(",");

                // Keep longest title/summary
                if d.title.len() > existing.title.len() {
                    existing.title = d.title.clone();
                }
                if d.summary.len() > existing.summary.len() {
                    existing.summary = d.summary.clone();
                }

                // Merge extra_metadata JSON objects
                if let (Some(obj_e), Some(obj_d)) = (existing.extra_metadata.as_object_mut(), d.extra_metadata.as_object()) {
                    for (k, v) in obj_d {
                        obj_e.insert(k.clone(), v.clone());
                    }
                }
            })
            .or_insert(d.clone());
    }
    let datasets_vec: Vec<_> = merged.into_values().collect();

    // Create temp staging table
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
                omic_type        VARCHAR(200),
                omic_subcategory TEXT,
                organism         VARCHAR(200),
                tax_id           INTEGER,
                n_samples        INTEGER,
                platform         VARCHAR(200),
                extra_metadata   JSONB,
                is_active        BOOLEAN,
                ingested_at      TIMESTAMPTZ,
                updated_at       TIMESTAMPTZ
            )",
            &[],
        )
        .await?;

    // Build CSV payload and COPY into staging
    let csv = build_dataset_csv(&datasets_vec);
    bulk_insert_csv(
        client,
        "COPY _staging_omicdataset (
            accession, source_db, bioproject_id, title, summary,
            omic_type, omic_subcategory, organism, tax_id, n_samples,
            platform, extra_metadata, is_active, ingested_at, updated_at
        ) FROM STDIN WITH (FORMAT csv, NULL 'NULL')",
        &csv,
    )
    .await?;

    // --- Step 2: Merge with existing records in DB ---
    let affected = client
        .execute(
            "INSERT INTO core_omicdataset (
                accession, source_db, bioproject_id, title, summary,
                omic_type, omic_subcategory, organism, tax_id, n_samples,
                platform, extra_metadata, is_active, ingested_at, updated_at
            )
            SELECT
                accession, source_db, bioproject_id, title, summary,
                omic_type, omic_subcategory, organism, tax_id, n_samples,
                platform, extra_metadata, is_active, ingested_at, updated_at
            FROM _staging_omicdataset
            ON CONFLICT (accession) DO UPDATE SET
                -- Keep longest title/summary
                title = CASE WHEN length(EXCLUDED.title) > length(core_omicdataset.title) THEN EXCLUDED.title ELSE core_omicdataset.title END,
                summary = CASE WHEN length(EXCLUDED.summary) > length(core_omicdataset.summary) THEN EXCLUDED.summary ELSE core_omicdataset.summary END,
                
                -- Merge and de-duplicate omic types using arrays
                omic_type = COALESCE((
                    SELECT string_agg(distinct t, ',') 
                    FROM unnest(string_to_array(COALESCE(core_omicdataset.omic_type, '') || ',' || COALESCE(EXCLUDED.omic_type, ''), ',')) t 
                    WHERE t != ''
                ), ''),
                omic_subcategory = COALESCE((
                    SELECT string_agg(distinct s, ',') 
                    FROM unnest(string_to_array(COALESCE(core_omicdataset.omic_subcategory, '') || ',' || COALESCE(EXCLUDED.omic_subcategory, ''), ',')) s 
                    WHERE s != ''
                ), ''),
                
                organism         = COALESCE(NULLIF(EXCLUDED.organism, ''), core_omicdataset.organism),
                tax_id           = COALESCE(EXCLUDED.tax_id, core_omicdataset.tax_id),
                n_samples        = COALESCE(EXCLUDED.n_samples, core_omicdataset.n_samples),
                platform         = CASE WHEN length(EXCLUDED.platform) > length(core_omicdataset.platform) THEN EXCLUDED.platform ELSE core_omicdataset.platform END,
                
                -- Merge metadata objects
                extra_metadata   = core_omicdataset.extra_metadata || EXCLUDED.extra_metadata,
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
            escape_csv_field(&sanitize_str(&link.link_source, 50))
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
            "INSERT INTO core_datasetpaperlink (dataset_id, paper_id, link_source, created_at)
             SELECT dataset_id, paper_id, link_source, NOW() FROM _staging_datasetlink
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
    let now = Utc::now().format("%Y-%m-%d %H:%M:%S%.6f+00").to_string();
    let mut csv = String::new();
    for d in datasets {
        let extra_str =
            serde_json::to_string(&d.extra_metadata).unwrap_or_else(|_| "{}".to_string());
        let row = format!(
            "{},{},{},{},{},{},{},{},{},{},{},{},{},{},{}\n",
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
            &now,
            &now,
        );
        csv.push_str(&row);
    }
    csv
}

/// Wrap a CSV field in double-quotes and escape internal quotes.
/// This ensures empty strings are treated as "" (empty) rather than NULL in Postgres CSV format.
fn escape_csv_field(s: &str) -> String {
    format!("\"{}\"", s.replace('"', "\"\""))
}

/// Strip NULL bytes and truncate to `max_len`.
fn sanitize_str(s: &str, max_len: usize) -> String {
    let sanitized = s.replace('\0', "");
    if sanitized.len() <= max_len {
        sanitized
    } else {
        // Truncate safely on char boundary
        let mut end = max_len;
        while !sanitized.is_char_boundary(end) && end > 0 {
            end -= 1;
        }
        sanitized[..end].to_string()
    }
}

// ─── PubMed paper writers ─────────────────────────────────────────────────────

/// Bulk-upsert papers into `core_paper`.
///
/// Returns `(rows_affected, pmid→paper_id map)`. The map is needed by the
/// child-table writers (`copy_paper_authors`, etc.).
pub async fn copy_papers(
    client: &Client,
    papers: &[PaperData],
) -> Result<(u64, HashMap<i64, i64>), tokio_postgres::Error> {
    if papers.is_empty() {
        return Ok((0, HashMap::new()));
    }

    // Deduplicate by PMID — keep the entry with the longest abstract
    let mut deduped: HashMap<i64, &PaperData> = HashMap::new();
    for p in papers {
        deduped
            .entry(p.pmid)
            .and_modify(|existing| {
                if p.abstract_text.len() > existing.abstract_text.len() {
                    *existing = p;
                }
            })
            .or_insert(p);
    }
    let papers: Vec<&PaperData> = deduped.into_values().collect();

    client
        .execute("DROP TABLE IF EXISTS _staging_paper", &[])
        .await?;
    client
        .execute(
            "CREATE TEMP TABLE _staging_paper (
                pmid          BIGINT,
                pmc_id        TEXT,
                doi           TEXT,
                title         TEXT,
                abstract      TEXT,
                journal       TEXT,
                pub_year      SMALLINT,
                pub_month     SMALLINT,
                pub_type      TEXT,
                raw_xml_hash  TEXT,
                ingested_at   TIMESTAMPTZ,
                updated_at    TIMESTAMPTZ
            )",
            &[],
        )
        .await?;

    let now = Utc::now().format("%Y-%m-%d %H:%M:%S%.6f+00").to_string();
    let mut csv = String::new();
    for p in &papers {
        csv.push_str(&format!(
            "{},{},{},{},{},{},{},{},{},{},{},{}\n",
            p.pmid,
            escape_csv_field(p.pmc_id.as_deref().unwrap_or("")),
            escape_csv_field(p.doi.as_deref().unwrap_or("")),
            escape_csv_field(&p.title.replace('\0', "")),
            escape_csv_field(&p.abstract_text.replace('\0', "")),
            escape_csv_field(&sanitize_str(&p.journal, 255)),
            p.pub_year.map_or("NULL".to_string(), |v| v.to_string()),
            p.pub_month.map_or("NULL".to_string(), |v| v.to_string()),
            escape_csv_field(&sanitize_str(&p.pub_type, 100)),
            escape_csv_field(&p.raw_xml_hash),
            &now,
            &now,
        ));
    }

    bulk_insert_csv(
        client,
        "COPY _staging_paper (pmid, pmc_id, doi, title, abstract, journal, pub_year, pub_month, pub_type, raw_xml_hash, ingested_at, updated_at) \
         FROM STDIN WITH (FORMAT csv, NULL 'NULL')",
        &csv,
    )
    .await?;

    let affected = client
        .execute(
            r#"INSERT INTO core_paper (pmid, pmc_id, doi, title, "abstract", journal, pub_year, pub_month, pub_type, raw_xml_hash, ingested_at, updated_at)
               SELECT pmid, pmc_id, doi, title, abstract, journal, pub_year, pub_month, pub_type, raw_xml_hash, ingested_at, updated_at
               FROM _staging_paper
               ON CONFLICT (pmid) DO UPDATE SET
                   pmc_id       = EXCLUDED.pmc_id,
                   doi          = EXCLUDED.doi,
                   title        = EXCLUDED.title,
                   "abstract"   = EXCLUDED."abstract",
                   journal      = EXCLUDED.journal,
                   pub_year     = EXCLUDED.pub_year,
                   pub_month    = EXCLUDED.pub_month,
                   pub_type     = EXCLUDED.pub_type,
                   raw_xml_hash = EXCLUDED.raw_xml_hash,
                   updated_at   = NOW()"#,
            &[],
        )
        .await?;

    client
        .execute("DROP TABLE IF EXISTS _staging_paper", &[])
        .await?;

    // Resolve pmid → paper_id
    let pmids: Vec<i64> = papers.iter().map(|p| p.pmid).collect();
    let rows = client
        .query("SELECT id, pmid FROM core_paper WHERE pmid = ANY($1)", &[&pmids])
        .await?;
    let pmid_to_id: HashMap<i64, i64> = rows
        .into_iter()
        .map(|r| (r.get::<_, i64>(1), r.get::<_, i64>(0)))
        .collect();

    Ok((affected, pmid_to_id))
}

/// Bulk-insert paper authors into `core_paperauthor`.
///
/// ON CONFLICT (paper_id, position) DO NOTHING — idempotent on re-ingestion.
pub async fn copy_paper_authors(
    client: &Client,
    papers: &[PaperData],
    pmid_to_id: &HashMap<i64, i64>,
) -> Result<u64, tokio_postgres::Error> {
    let mut csv = String::new();
    for p in papers {
        let paper_id = match pmid_to_id.get(&p.pmid) {
            Some(id) => *id,
            None => continue,
        };
        for (pos, author) in p.authors.iter().enumerate() {
            csv.push_str(&format!(
                "{},{},{},{},{},{}\n",
                paper_id,
                pos + 1,
                escape_csv_field(&sanitize_str(&author.last_name, 255)),
                escape_csv_field(&sanitize_str(&author.initials, 20)),
                escape_csv_field(&author.affiliation.replace('\0', "")),
                escape_csv_field(&sanitize_str(&author.country, 100)),
            ));
        }
    }
    if csv.is_empty() {
        return Ok(0);
    }

    client
        .execute("DROP TABLE IF EXISTS _staging_author", &[])
        .await?;
    client
        .execute(
            "CREATE TEMP TABLE _staging_author (
                paper_id    BIGINT,
                position    SMALLINT,
                last_name   TEXT,
                initials    TEXT,
                affiliation TEXT,
                country     TEXT
            )",
            &[],
        )
        .await?;

    bulk_insert_csv(
        client,
        "COPY _staging_author (paper_id, position, last_name, initials, affiliation, country) \
         FROM STDIN WITH (FORMAT csv, NULL 'NULL')",
        &csv,
    )
    .await?;

    let affected = client
        .execute(
            "INSERT INTO core_paperauthor (paper_id, position, last_name, initials, affiliation, country)
             SELECT paper_id, position, last_name, initials, affiliation, country FROM _staging_author
             ON CONFLICT (paper_id, position) DO NOTHING",
            &[],
        )
        .await?;

    client
        .execute("DROP TABLE IF EXISTS _staging_author", &[])
        .await?;
    Ok(affected)
}

/// Bulk-insert paper keywords into `core_paperkeyword`.
///
/// ON CONFLICT (paper_id, keyword_lower) DO NOTHING.
pub async fn copy_paper_keywords(
    client: &Client,
    papers: &[PaperData],
    pmid_to_id: &HashMap<i64, i64>,
) -> Result<u64, tokio_postgres::Error> {
    let mut csv = String::new();
    for p in papers {
        let paper_id = match pmid_to_id.get(&p.pmid) {
            Some(id) => *id,
            None => continue,
        };
        for kw in &p.keywords {
            let kw_lower = kw.to_lowercase();
            csv.push_str(&format!(
                "{},{},{}\n",
                paper_id,
                escape_csv_field(&sanitize_str(kw, 255)),
                escape_csv_field(&sanitize_str(&kw_lower, 255)),
            ));
        }
    }
    if csv.is_empty() {
        return Ok(0);
    }

    client
        .execute("DROP TABLE IF EXISTS _staging_keyword", &[])
        .await?;
    client
        .execute(
            "CREATE TEMP TABLE _staging_keyword (
                paper_id      BIGINT,
                keyword       TEXT,
                keyword_lower TEXT
            )",
            &[],
        )
        .await?;

    bulk_insert_csv(
        client,
        "COPY _staging_keyword (paper_id, keyword, keyword_lower) FROM STDIN WITH (FORMAT csv, NULL 'NULL')",
        &csv,
    )
    .await?;

    let affected = client
        .execute(
            "INSERT INTO core_paperkeyword (paper_id, keyword, keyword_lower)
             SELECT paper_id, keyword, keyword_lower FROM _staging_keyword
             ON CONFLICT (paper_id, keyword_lower) DO NOTHING",
            &[],
        )
        .await?;

    client
        .execute("DROP TABLE IF EXISTS _staging_keyword", &[])
        .await?;
    Ok(affected)
}

/// Bulk-insert MeSH terms into `core_papermeshterm`.
///
/// ON CONFLICT (paper_id, descriptor, qualifier) DO NOTHING.
pub async fn copy_paper_mesh(
    client: &Client,
    papers: &[PaperData],
    pmid_to_id: &HashMap<i64, i64>,
) -> Result<u64, tokio_postgres::Error> {
    let mut csv = String::new();
    for p in papers {
        let paper_id = match pmid_to_id.get(&p.pmid) {
            Some(id) => *id,
            None => continue,
        };
        for mesh in &p.mesh_terms {
            csv.push_str(&format!(
                "{},{},{},{}\n",
                paper_id,
                escape_csv_field(&sanitize_str(&mesh.descriptor, 255)),
                escape_csv_field(&sanitize_str(&mesh.qualifier, 255)),
                if mesh.is_major { "t" } else { "f" },
            ));
        }
    }
    if csv.is_empty() {
        return Ok(0);
    }

    client
        .execute("DROP TABLE IF EXISTS _staging_mesh", &[])
        .await?;
    client
        .execute(
            "CREATE TEMP TABLE _staging_mesh (
                paper_id     BIGINT,
                descriptor   TEXT,
                qualifier    TEXT,
                is_major_topic BOOLEAN
            )",
            &[],
        )
        .await?;

    bulk_insert_csv(
        client,
        "COPY _staging_mesh (paper_id, descriptor, qualifier, is_major_topic) \
         FROM STDIN WITH (FORMAT csv, NULL 'NULL')",
        &csv,
    )
    .await?;

    let affected = client
        .execute(
            "INSERT INTO core_papermeshterm (paper_id, descriptor, qualifier, is_major_topic)
             SELECT paper_id, descriptor, qualifier, is_major_topic FROM _staging_mesh
             ON CONFLICT (paper_id, descriptor, qualifier) DO NOTHING",
            &[],
        )
        .await?;

    client
        .execute("DROP TABLE IF EXISTS _staging_mesh", &[])
        .await?;
    Ok(affected)
}

/// Bulk-upsert gene mentions into `core_papergene`.
///
/// `genes` is a list of `(pmid, gene_symbol, mention_count)` tuples.
///
/// ON CONFLICT (paper_id, gene_symbol) DO UPDATE mention_count.
pub async fn copy_paper_genes(
    client: &Client,
    genes: &[(i64, String, i32)],
    pmid_to_id: &HashMap<i64, i64>,
) -> Result<u64, tokio_postgres::Error> {
    let mut csv = String::new();
    for (pmid, symbol, count) in genes {
        let paper_id = match pmid_to_id.get(pmid) {
            Some(id) => *id,
            None => continue,
        };
        csv.push_str(&format!(
            "{},{},{}\n",
            paper_id,
            escape_csv_field(&sanitize_str(symbol, 100)),
            count,
        ));
    }
    if csv.is_empty() {
        return Ok(0);
    }

    client
        .execute("DROP TABLE IF EXISTS _staging_gene", &[])
        .await?;
    client
        .execute(
            "CREATE TEMP TABLE _staging_gene (
                paper_id      BIGINT,
                gene_symbol   TEXT,
                mention_count INTEGER
            )",
            &[],
        )
        .await?;

    bulk_insert_csv(
        client,
        "COPY _staging_gene (paper_id, gene_symbol, mention_count) FROM STDIN WITH (FORMAT csv, NULL 'NULL')",
        &csv,
    )
    .await?;

    let affected = client
        .execute(
            "INSERT INTO core_papergene (paper_id, gene_symbol, mention_count)
             SELECT paper_id, gene_symbol, SUM(mention_count) FROM _staging_gene
             GROUP BY paper_id, gene_symbol
             ON CONFLICT (paper_id, gene_symbol) DO UPDATE SET mention_count = EXCLUDED.mention_count",
            &[],
        )
        .await?;

    client
        .execute("DROP TABLE IF EXISTS _staging_gene", &[])
        .await?;
    Ok(affected)
}

/// Bulk-upsert drug mentions into `core_paperdrug`.
///
/// `drugs` is a list of `(pmid, drug_name, drug_name_lower, mention_count)` tuples.
///
/// ON CONFLICT (paper_id, drug_name_lower) DO UPDATE mention_count.
pub async fn copy_paper_drugs(
    client: &Client,
    drugs: &[(i64, String, String, i32)],
    pmid_to_id: &HashMap<i64, i64>,
) -> Result<u64, tokio_postgres::Error> {
    let mut csv = String::new();
    for (pmid, name, name_lower, count) in drugs {
        let paper_id = match pmid_to_id.get(pmid) {
            Some(id) => *id,
            None => continue,
        };
        csv.push_str(&format!(
            "{},{},{},{},{}\n",
            paper_id,
            escape_csv_field(&sanitize_str(name, 255)),
            escape_csv_field(&sanitize_str(name_lower, 255)),
            count,
            escape_csv_field(""), // drugbank_id — empty for now
        ));
    }
    if csv.is_empty() {
        return Ok(0);
    }

    client
        .execute("DROP TABLE IF EXISTS _staging_drug", &[])
        .await?;
    client
        .execute(
            "CREATE TEMP TABLE _staging_drug (
                paper_id        BIGINT,
                drug_name       TEXT,
                drug_name_lower TEXT,
                mention_count   INTEGER,
                drugbank_id     TEXT
            )",
            &[],
        )
        .await?;

    bulk_insert_csv(
        client,
        "COPY _staging_drug (paper_id, drug_name, drug_name_lower, mention_count, drugbank_id) FROM STDIN WITH (FORMAT csv, NULL 'NULL')",
        &csv,
    )
    .await?;

    let affected = client
        .execute(
            "INSERT INTO core_paperdrug (paper_id, drug_name, drug_name_lower, mention_count, drugbank_id)
             SELECT paper_id, MIN(drug_name), drug_name_lower, SUM(mention_count), MIN(drugbank_id) FROM _staging_drug
             GROUP BY paper_id, drug_name_lower
             ON CONFLICT (paper_id, drug_name_lower) DO UPDATE SET mention_count = EXCLUDED.mention_count",
            &[],
        )
        .await?;

    client
        .execute("DROP TABLE IF EXISTS _staging_drug", &[])
        .await?;
    Ok(affected)
}

/// Bulk-upsert variant (rs-number) mentions into `core_papervariant`.
///
/// `variants` is a list of `(pmid, rs_number, mention_count)` tuples produced
/// by `categorization::variant_ner::extract_variants`.
///
/// The staging table groups by `(paper_id, rs_number)` with `SUM(mention_count)`
/// before the final upsert, so duplicate rows within the same batch are safe.
///
/// ON CONFLICT (paper_id, rs_number) DO UPDATE overwrites mention_count with
/// EXCLUDED.mention_count (the SUM already computed in the staging SELECT),
/// identical to the genes/drugs pattern. This is idempotent: re-ingesting the
/// same paper replaces the count rather than doubling it.
pub async fn copy_paper_variants(
    client: &Client,
    variants: &[(i64, String, i32)],
    pmid_to_id: &HashMap<i64, i64>,
) -> Result<u64, tokio_postgres::Error> {
    let mut csv = String::new();
    for (pmid, rs_number, count) in variants {
        let paper_id = match pmid_to_id.get(pmid) {
            Some(id) => *id,
            None => continue,
        };
        csv.push_str(&format!(
            "{},{},{}\n",
            paper_id,
            escape_csv_field(&sanitize_str(rs_number, 20)),
            count,
        ));
    }
    if csv.is_empty() {
        return Ok(0);
    }

    client
        .execute("DROP TABLE IF EXISTS _staging_variant", &[])
        .await?;
    client
        .execute(
            "CREATE TEMP TABLE _staging_variant (
                paper_id      BIGINT,
                rs_number     TEXT,
                mention_count INTEGER
            )",
            &[],
        )
        .await?;

    bulk_insert_csv(
        client,
        "COPY _staging_variant (paper_id, rs_number, mention_count) FROM STDIN WITH (FORMAT csv, NULL 'NULL')",
        &csv,
    )
    .await?;

    let affected = client
        .execute(
            "INSERT INTO core_papervariant (paper_id, rs_number, mention_count)
             SELECT paper_id, rs_number, SUM(mention_count) FROM _staging_variant
             GROUP BY paper_id, rs_number
             ON CONFLICT (paper_id, rs_number) DO UPDATE
                 SET mention_count = EXCLUDED.mention_count",
            &[],
        )
        .await?;

    client
        .execute("DROP TABLE IF EXISTS _staging_variant", &[])
        .await?;
    Ok(affected)
}

/// Store dataset-paper links in the pending table for deferred FK resolution.
///
/// This avoids the ordering problem where omics ingestion runs before PubMed
/// ingestion — links are stored without FK constraints and resolved later.
pub async fn store_pending_links(
    client: &Client,
    links: &[DatasetPaperLinkData],
) -> Result<u64, tokio_postgres::Error> {
    if links.is_empty() {
        return Ok(0);
    }

    let mut csv = String::new();
    for link in links {
        csv.push_str(&format!(
            "{},{},{}\n",
            escape_csv_field(&sanitize_str(&link.dataset_accession, 50)),
            link.paper_pmid,
            escape_csv_field(&sanitize_str(&link.link_source, 50)),
        ));
    }

    client
        .execute("DROP TABLE IF EXISTS _staging_pending_link", &[])
        .await?;
    client
        .execute(
            "CREATE TEMP TABLE _staging_pending_link (
                dataset_accession VARCHAR(50),
                paper_pmid        BIGINT,
                link_source       VARCHAR(50)
            )",
            &[],
        )
        .await?;

    bulk_insert_csv(
        client,
        "COPY _staging_pending_link (dataset_accession, paper_pmid, link_source) FROM STDIN WITH (FORMAT csv)",
        &csv,
    )
    .await?;

    let affected = client
        .execute(
            "INSERT INTO core_datasetpaperlinkpending (dataset_accession, paper_pmid, link_source, created_at)
             SELECT dataset_accession, paper_pmid, MIN(link_source), NOW() FROM _staging_pending_link
             GROUP BY dataset_accession, paper_pmid
             ON CONFLICT (dataset_accession, paper_pmid) DO NOTHING",
            &[],
        )
        .await?;

    client
        .execute("DROP TABLE IF EXISTS _staging_pending_link", &[])
        .await?;

    eprintln!("[copy_writer] Stored {} pending links", affected);
    Ok(affected)
}

/// Resolve pending dataset-paper links by joining with existing datasets and papers.
///
/// Moves resolved links from `core_datasetpaperlinkpending` to `core_datasetpaperlink`,
/// then deletes the resolved rows from the pending table.
///
/// Returns the number of links resolved and inserted.
pub async fn resolve_pending_links(
    client: &Client,
) -> Result<u64, tokio_postgres::Error> {
    // Insert resolved links
    let affected = client
        .execute(
            "INSERT INTO core_datasetpaperlink (dataset_id, paper_id, link_source, created_at)
             SELECT d.id, p.id, pl.link_source, NOW()
             FROM core_datasetpaperlinkpending pl
             JOIN core_omicdataset d ON d.accession = pl.dataset_accession
             JOIN core_paper p ON p.pmid = pl.paper_pmid
             ON CONFLICT (dataset_id, paper_id) DO NOTHING",
            &[],
        )
        .await?;

    // Delete resolved rows (those where both FKs exist)
    client
        .execute(
            "DELETE FROM core_datasetpaperlinkpending pl
             WHERE EXISTS (
                SELECT 1 FROM core_omicdataset d WHERE d.accession = pl.dataset_accession
             ) AND EXISTS (
                SELECT 1 FROM core_paper p WHERE p.pmid = pl.paper_pmid
             )",
            &[],
        )
        .await?;

    eprintln!("[copy_writer] Resolved {} pending links", affected);
    Ok(affected)
}

/// Link omics datasets to a project via `core_projectdataset`.
///
/// Inserts rows with `curation_status = 'pending'` for every accession that
/// exists in `core_omicdataset`. ON CONFLICT DO NOTHING for idempotency.
///
/// Returns the number of new links inserted.
pub async fn link_project_datasets(
    client: &Client,
    project_id: uuid::Uuid,
    accessions: &[String],
    job_id: &str,
) -> Result<u64, tokio_postgres::Error> {
    if accessions.is_empty() {
        return Ok(0);
    }

    let job_id_uuid: Option<uuid::Uuid> = uuid::Uuid::parse_str(job_id).ok();

    let affected = client
        .execute(
            "INSERT INTO core_projectdataset \
                (project_id, dataset_id, curation_status, exclusion_reason, notes, added_at, ingestion_job_id)
             SELECT $1, id, 'pending', '', '', NOW(), $3
             FROM core_omicdataset WHERE accession = ANY($2)
             ON CONFLICT (project_id, dataset_id) DO NOTHING",
            &[&project_id, &accessions, &job_id_uuid],
        )
        .await?;

    Ok(affected)
}

/// Link papers to a project via `core_projectpaper`.
///
/// Inserts rows with `curation_status = 'pending'` for every PMID that
/// exists in `core_paper`. ON CONFLICT DO NOTHING for idempotency.
///
/// Returns the number of new links inserted.
pub async fn link_project_papers(
    client: &Client,
    project_id: uuid::Uuid,
    pmids: &[i64],
    job_id: &str,
) -> Result<u64, tokio_postgres::Error> {
    if pmids.is_empty() {
        return Ok(0);
    }

    let job_id_uuid: Option<uuid::Uuid> = uuid::Uuid::parse_str(job_id).ok();

    let affected = client
        .execute(
            "INSERT INTO core_projectpaper \
                (project_id, paper_id, curation_status, exclusion_reason, notes, added_at, ingestion_job_id)
             SELECT $1, id, 'pending', '', '', NOW(), $3
             FROM core_paper WHERE pmid = ANY($2)
             ON CONFLICT (project_id, paper_id) DO NOTHING",
            &[&project_id, &pmids, &job_id_uuid],
        )
        .await?;

    Ok(affected)
}

// ─── DatasetFile bulk writer ──────────────────────────────────────────────────

/// One row to be inserted/updated in `core_datasetfile`.
pub struct DatasetFileRow {
    /// Natural key — used for ON CONFLICT (accession) DO UPDATE.
    /// Convention for GEO supplementary: `<GSE_accession>__<filename>`
    /// (e.g. `GSE12345__GSE12345_raw_counts.txt.gz`).
    pub accession: String,
    /// `series_matrix | supplementary | cel | fastq | sra`
    pub file_type: String,
    /// `geo_ftp | ena_ftp | sra_tools`
    pub source: String,
    /// Remote HTTPS/FTP URL.
    pub remote_url: String,
    /// Path on local disk where the file was written (relative to dest_dir).
    /// Django reads this to find the file before uploading to object storage.
    /// After upload Django overwrites this with the final `storage_key`.
    pub storage_key: String,
    /// File size in bytes (None if unknown).
    pub size_bytes: Option<i64>,
    /// Hex MD5 checksum (None if not yet computed).
    pub checksum_md5: Option<String>,
    /// `pending | queued | downloading | downloaded | failed`
    pub download_status: String,
    /// Bytes downloaded so far (for resumable downloads; 0 when complete).
    pub bytes_downloaded: i64,
    /// Error message if status = failed; empty string otherwise.
    pub error_message: String,
    /// FK to `core_omicdataset.id` (set for dataset-level files like GEO suppl).
    pub dataset_id: Option<i64>,
    /// FK to `core_omicsample.id` (set for sample-level files like FASTQ).
    pub sample_id: Option<i64>,
}

/// Bulk-upsert dataset file records into `core_datasetfile`.
///
/// Uses a temp staging table + COPY FROM STDIN + INSERT … ON CONFLICT DO UPDATE.
///
/// Conflict key: UNIQUE on `accession`.
/// On conflict: updates storage_key, size_bytes, checksum_md5, download_status,
/// bytes_downloaded, error_message, updated_at. Fields that should survive
/// re-ingestion unchanged (remote_url, file_type, source, dataset_id, sample_id,
/// created_at) are intentionally excluded from the UPDATE clause.
///
/// NOT NULL / default constraints respected:
/// - `storage_key`: never NULL — send empty string `""` until Django uploads.
/// - `checksum_algo`: always `'md5'` (hardcoded in INSERT).
/// - `bytes_downloaded`, `error_message`: never NULL.
///
/// Returns the number of rows inserted or updated.
pub async fn copy_dataset_files(
    client: &Client,
    rows: &[DatasetFileRow],
) -> Result<u64, tokio_postgres::Error> {
    if rows.is_empty() {
        return Ok(0);
    }

    client
        .execute("DROP TABLE IF EXISTS _staging_datasetfile", &[])
        .await?;
    client
        .execute(
            "CREATE TEMP TABLE _staging_datasetfile (
                accession         VARCHAR(255),
                file_type         VARCHAR(20),
                source            VARCHAR(20),
                remote_url        TEXT,
                storage_key       TEXT,
                size_bytes        BIGINT,
                checksum_md5      VARCHAR(128),
                download_status   VARCHAR(20),
                bytes_downloaded  BIGINT,
                error_message     TEXT,
                dataset_id        BIGINT,
                sample_id         BIGINT,
                created_at        TIMESTAMPTZ,
                updated_at        TIMESTAMPTZ
            )",
            &[],
        )
        .await?;

    let csv = build_dataset_file_csv(rows);
    bulk_insert_csv(
        client,
        "COPY _staging_datasetfile (
            accession, file_type, source, remote_url, storage_key,
            size_bytes, checksum_md5, download_status, bytes_downloaded,
            error_message, dataset_id, sample_id, created_at, updated_at
        ) FROM STDIN WITH (FORMAT csv, NULL 'NULL')",
        &csv,
    )
    .await?;

    let affected = client
        .execute(
            "INSERT INTO core_datasetfile (
                accession, file_type, source, remote_url, storage_key,
                checksum_md5, checksum_algo, size_bytes, download_status,
                bytes_downloaded, error_message, dataset_id, sample_id,
                created_at, updated_at
            )
            SELECT
                accession, file_type, source, remote_url, storage_key,
                checksum_md5, 'md5', size_bytes, download_status,
                bytes_downloaded, error_message, dataset_id, sample_id,
                created_at, updated_at
            FROM _staging_datasetfile
            ON CONFLICT (accession) DO UPDATE SET
                storage_key      = EXCLUDED.storage_key,
                size_bytes       = COALESCE(EXCLUDED.size_bytes, core_datasetfile.size_bytes),
                checksum_md5     = COALESCE(EXCLUDED.checksum_md5, core_datasetfile.checksum_md5),
                download_status  = EXCLUDED.download_status,
                bytes_downloaded = EXCLUDED.bytes_downloaded,
                error_message    = EXCLUDED.error_message,
                updated_at       = NOW()",
            &[],
        )
        .await?;

    client
        .execute("DROP TABLE IF EXISTS _staging_datasetfile", &[])
        .await?;

    Ok(affected)
}

fn build_dataset_file_csv(rows: &[DatasetFileRow]) -> String {
    let now = Utc::now().format("%Y-%m-%d %H:%M:%S%.6f+00").to_string();
    let mut csv = String::new();
    for r in rows {
        csv.push_str(&format!(
            "{},{},{},{},{},{},{},{},{},{},{},{},{},{}\n",
            escape_csv_field(&sanitize_str(&r.accession, 255)),
            escape_csv_field(&sanitize_str(&r.file_type, 20)),
            escape_csv_field(&sanitize_str(&r.source, 20)),
            escape_csv_field(&r.remote_url),
            // storage_key: never NULL — empty string when not yet uploaded
            escape_csv_field(&r.storage_key),
            r.size_bytes.map_or("NULL".to_string(), |v| v.to_string()),
            r.checksum_md5
                .as_deref()
                .map_or("NULL".to_string(), |v| escape_csv_field(&sanitize_str(v, 128))),
            escape_csv_field(&sanitize_str(&r.download_status, 20)),
            r.bytes_downloaded,
            escape_csv_field(&r.error_message),
            r.dataset_id.map_or("NULL".to_string(), |v| v.to_string()),
            r.sample_id.map_or("NULL".to_string(), |v| v.to_string()),
            &now, // created_at
            &now, // updated_at
        ));
    }
    csv
}

// ─── OmicSample bulk writer ───────────────────────────────────────────────────

/// Bulk-upsert omics samples into `core_omicsample`.
///
/// Uses a temp staging table + COPY FROM STDIN + INSERT … ON CONFLICT DO UPDATE.
///
/// Conflict key: `accession` (globally unique natural key for GSM/SRS).
/// On conflict: update mutable fields (title, source_name, organism, tax_id,
/// platform, characteristics, extra_metadata, updated_at).
/// `ingested_at` is intentionally excluded from the UPDATE clause — it records
/// the first time this sample was seen and must not be overwritten.
///
/// Returns the number of rows inserted or updated.
pub async fn copy_omic_samples(
    client: &Client,
    samples: &[OmicSampleData],
) -> Result<u64, tokio_postgres::Error> {
    if samples.is_empty() {
        return Ok(0);
    }

    // --- Dedup within batch by accession ---
    // In practice each accession should appear once, but guard just in case.
    let mut seen: HashSet<&str> = HashSet::with_capacity(samples.len());
    let deduped: Vec<&OmicSampleData> = samples
        .iter()
        .filter(|s| seen.insert(s.accession.as_str()))
        .collect();

    // Create temp staging table
    client
        .execute("DROP TABLE IF EXISTS _staging_omicsample", &[])
        .await?;
    client
        .execute(
            "CREATE TEMP TABLE _staging_omicsample (
                dataset_id      BIGINT,
                accession       VARCHAR(100),
                title           TEXT,
                source_name     TEXT,
                organism        VARCHAR(200),
                tax_id          INTEGER,
                platform        VARCHAR(200),
                characteristics JSONB,
                extra_metadata  JSONB,
                ingested_at     TIMESTAMPTZ,
                updated_at      TIMESTAMPTZ
            )",
            &[],
        )
        .await?;

    // Build CSV and COPY into staging
    let csv = build_sample_csv(&deduped);
    bulk_insert_csv(
        client,
        "COPY _staging_omicsample (
            dataset_id, accession, title, source_name, organism, tax_id,
            platform, characteristics, extra_metadata, ingested_at, updated_at
        ) FROM STDIN WITH (FORMAT csv, NULL 'NULL')",
        &csv,
    )
    .await?;

    // Upsert from staging into core_omicsample
    let affected = client
        .execute(
            "INSERT INTO core_omicsample (
                dataset_id, accession, title, source_name, organism, tax_id,
                platform, characteristics, extra_metadata, ingested_at, updated_at
            )
            SELECT
                dataset_id, accession, title, source_name, organism, tax_id,
                platform, characteristics, extra_metadata, ingested_at, updated_at
            FROM _staging_omicsample
            ON CONFLICT (accession) DO UPDATE SET
                title           = CASE
                    WHEN length(EXCLUDED.title) > length(core_omicsample.title)
                    THEN EXCLUDED.title
                    ELSE core_omicsample.title END,
                source_name     = COALESCE(NULLIF(EXCLUDED.source_name, ''), core_omicsample.source_name),
                organism        = COALESCE(NULLIF(EXCLUDED.organism, ''), core_omicsample.organism),
                tax_id          = COALESCE(EXCLUDED.tax_id, core_omicsample.tax_id),
                platform        = COALESCE(NULLIF(EXCLUDED.platform, ''), core_omicsample.platform),
                characteristics = core_omicsample.characteristics || EXCLUDED.characteristics,
                extra_metadata  = core_omicsample.extra_metadata  || EXCLUDED.extra_metadata,
                updated_at      = NOW()",
            &[],
        )
        .await?;

    client
        .execute("DROP TABLE IF EXISTS _staging_omicsample", &[])
        .await?;

    Ok(affected)
}

/// Emit a CSV row for each `OmicSampleData`.
fn build_sample_csv(samples: &[&OmicSampleData]) -> String {
    let now = Utc::now().format("%Y-%m-%d %H:%M:%S%.6f+00").to_string();
    let mut csv = String::new();
    for s in samples {
        let char_str =
            serde_json::to_string(&s.characteristics).unwrap_or_else(|_| "{}".to_string());
        let extra_str =
            serde_json::to_string(&s.extra_metadata).unwrap_or_else(|_| "{}".to_string());
        let row = format!(
            "{},{},{},{},{},{},{},{},{},{},{}\n",
            s.dataset_id,
            escape_csv_field(&sanitize_str(&s.accession, 100)),
            escape_csv_field(&s.title),
            escape_csv_field(&s.source_name),
            escape_csv_field(&sanitize_str(&s.organism, 200)),
            s.tax_id.map_or("NULL".to_string(), |v| v.to_string()),
            escape_csv_field(&sanitize_str(&s.platform, 200)),
            escape_csv_field(&char_str),
            escape_csv_field(&extra_str),
            &now,
            &now,
        );
        csv.push_str(&row);
    }
    csv
}
