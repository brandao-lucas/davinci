/// Sample ingestion for GEO (GSE) and SRA datasets.
///
/// # Fetch strategy
///
/// ## GEO
/// Uses the GEO Accession Display endpoint (NOT efetch, which has no `db=gse`):
/// `https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSExxxxx&targ=gsm&form=text&view=brief`
///
/// - `targ=gsm` returns all GSM sample records for the series.
/// - `form=text` returns SOFT (line-oriented text format).
/// - `view=brief` limits each record to core metadata fields.
///
/// GEO SOFT is a line-oriented text format. Each sample block starts with
/// `^SAMPLE = GSMxxxxxxx` and ends at the next `^` directive. Fields are
/// `!Sample_<key> = <value>`. Characteristics are `!Sample_characteristics_ch1`.
/// This gives us GSM accession, title, source_name, organism, geo_accession,
/// characteristics and platform in one download per dataset.
///
/// SOFT was chosen over MINiML (which is XML) because:
/// 1. It is much smaller (no namespace cruft, no binary blobs).
/// 2. It is line-delimited, so parsing is a single streaming pass.
/// 3. The acc.cgi endpoint returns it reliably for any size GSE.
///
/// ## SRA
/// Three-step via E-utilities:
/// 1. `esearch.fcgi?db=sra&term=SRP...&retmax=500&retmode=json` → all UIDs for the study
/// 2. UIDs are batched into groups of up to 200, joined by comma.
/// 3. `efetch.fcgi?db=sra&id=<uid1,uid2,...>&retmode=xml` → EXPERIMENT_PACKAGE_SET XML
///    (one efetch call per batch, respecting the NcbiClient rate limit between calls)
///
/// The `efetch` endpoint requires numeric UIDs via the `id=` parameter;
/// the `acc=` parameter is not valid for efetch and returns 400.
///
/// The SRA efetch XML for a study returns a `<EXPERIMENT_PACKAGE_SET>` where
/// each `<EXPERIMENT_PACKAGE>` has a `<SAMPLE>` element with accession,
/// title, scientific name, taxon id, and `<SAMPLE_ATTRIBUTE>` key/value pairs.
/// One pass through the XML extracts all sample records.
///
/// `parse_sra_samples_xml` deduplicates by SRS accession, so samples referenced
/// across multiple experiment packages are never inserted twice.
///
/// SRA fetch-by-study-accession is preferred over fetch-by-run because it
/// gives sample-level (SRS) records, which map directly to `OmicSample`.
///
/// ## Other sources
/// BioProject and GWAS have no per-sample concept → return empty vec, no error.

use quick_xml::events::Event;
use quick_xml::Reader;
use serde::Deserialize;
use serde_json::{json, Map, Value as JsonValue};

use crate::ncbi::client::NcbiClient;
use crate::omics::models::OmicSampleData;

const GEO_ACC_CGI_URL: &str = "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi";
const ESEARCH_URL: &str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi";
const EFETCH_URL: &str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi";

/// Maximum UIDs to retrieve from esearch in a single SRA query.
/// NCBI supports up to 10 000 for esearch; 500 covers all practical studies
/// without risking oversized efetch payloads.
const SRA_ESEARCH_RETMAX: &str = "500";

/// Maximum UIDs per efetch call when fetching SRA experiment packages.
/// NCBI recommends keeping comma-delimited id lists short; 200 is safe and
/// keeps individual payloads manageable while minimising round-trips.
const SRA_EFETCH_BATCH_SIZE: usize = 200;

// ─── SRA esearch response structs ─────────────────────────────────────────────

#[derive(Deserialize)]
struct SraEsearchResponse {
    esearchresult: SraEsearchResult,
}

#[derive(Deserialize)]
struct SraEsearchResult {
    idlist: Vec<String>,
}

// ─── Public API ───────────────────────────────────────────────────────────────

/// Fetch samples for a single dataset.
///
/// `dataset_id` is the integer PK of the OmicDataset row in the DB.
/// `dataset_accession` is the external accession (e.g. "GSE12345" or "SRP123456").
/// `source_db` is one of "geo", "sra", "bioproject", "gwas_catalog".
///
/// Returns a flat `Vec<OmicSampleData>` ready for COPY. For sources without
/// sample concepts (bioproject, gwas_catalog) returns an empty vec without error.
pub async fn fetch_samples_for_dataset(
    client: &NcbiClient,
    dataset_id: i64,
    dataset_accession: &str,
    source_db: &str,
) -> Result<Vec<OmicSampleData>, String> {
    match source_db {
        "geo" => fetch_geo_samples(client, dataset_id, dataset_accession).await,
        "sra" => fetch_sra_samples(client, dataset_id, dataset_accession).await,
        // BioProject and GWAS do not expose individual sample records through
        // the NCBI E-utilities that we use — return empty without error.
        _ => Ok(vec![]),
    }
}

// ─── GEO SOFT parser ──────────────────────────────────────────────────────────

async fn fetch_geo_samples(
    client: &NcbiClient,
    dataset_id: i64,
    gse_accession: &str,
) -> Result<Vec<OmicSampleData>, String> {
    // GEO SOFT via the GEO Accession Display endpoint.
    //
    // efetch has no `db=gse` — using it with that db returns 400.
    // The canonical way to get SOFT for all samples of a series is:
    //   https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi
    //     ?acc=GSExxxxx&targ=gsm&form=text&view=brief
    //
    // `targ=gsm`  → include GSM sample records
    // `form=text` → SOFT text format (compatible with parse_geo_soft)
    // `view=brief`→ omits bulky data tables, keeps metadata fields
    //
    // The NcbiClient may append `api_key` as an extra query param;
    // acc.cgi silently ignores unrecognised parameters.
    let params = [
        ("acc", gse_accession),
        ("targ", "gsm"),
        ("form", "text"),
        ("view", "brief"),
    ];

    let body = client.fetch_with_retry(GEO_ACC_CGI_URL, &params).await?;
    parse_geo_soft(&body, dataset_id)
}

/// Parse a GEO SOFT text payload. Extracts one `OmicSampleData` per `^SAMPLE` block.
///
/// SOFT line structure:
/// ```
/// ^SAMPLE = GSMxxxxxxx
/// !Sample_title = ...
/// !Sample_source_name_ch1 = ...
/// !Sample_organism_ch1 = ...
/// !Sample_taxid_ch1 = ...
/// !Sample_platform_id = ...
/// !Sample_characteristics_ch1 = key: value
/// ```
///
/// Single pass: iterate lines, accumulate into current sample, emit when the
/// next `^` record-separator is encountered.
fn parse_geo_soft(text: &str, dataset_id: i64) -> Result<Vec<OmicSampleData>, String> {
    let mut samples: Vec<OmicSampleData> = Vec::new();

    // Mutable state for the current sample being built
    let mut accession = String::new();
    let mut title = String::new();
    let mut source_name = String::new();
    let mut organism = String::new();
    let mut tax_id: Option<i32> = None;
    let mut platform = String::new();
    let mut characteristics: Map<String, JsonValue> = Map::new();
    let mut extra: Map<String, JsonValue> = Map::new();
    let mut in_sample = false;

    let flush = |accession: &str,
                 title: &str,
                 source_name: &str,
                 organism: &str,
                 tax_id: Option<i32>,
                 platform: &str,
                 characteristics: Map<String, JsonValue>,
                 extra: Map<String, JsonValue>,
                 samples: &mut Vec<OmicSampleData>| {
        if accession.is_empty() {
            return;
        }
        samples.push(OmicSampleData {
            dataset_id,
            accession: accession.to_string(),
            title: title.to_string(),
            source_name: source_name.to_string(),
            organism: organism.to_string(),
            tax_id,
            platform: platform.to_string(),
            characteristics: JsonValue::Object(characteristics),
            extra_metadata: JsonValue::Object(extra),
        });
    };

    for raw_line in text.lines() {
        let line = raw_line.trim();

        // Record separator — a line starting with '^' marks a new entity
        if line.starts_with('^') {
            // Flush current sample if we were inside one
            if in_sample {
                flush(
                    &accession,
                    &title,
                    &source_name,
                    &organism,
                    tax_id,
                    &platform,
                    std::mem::take(&mut characteristics),
                    std::mem::take(&mut extra),
                    &mut samples,
                );
                // Reset state
                accession.clear();
                title.clear();
                source_name.clear();
                organism.clear();
                tax_id = None;
                platform.clear();
                in_sample = false;
            }

            // Detect start of a SAMPLE block
            if line.starts_with("^SAMPLE") {
                if let Some(acc) = line.splitn(2, '=').nth(1) {
                    accession = acc.trim().to_string();
                    in_sample = true;
                }
            }
            continue;
        }

        if !in_sample {
            continue;
        }

        // All sample fields start with '!'
        if !line.starts_with('!') {
            continue;
        }

        // Split on first '=' to get key = value
        let (key, value) = match line.splitn(2, '=').collect::<Vec<_>>()[..] {
            [k, v] => (k.trim(), v.trim()),
            _ => continue,
        };

        match key {
            "!Sample_title" => {
                if title.is_empty() {
                    title = value.to_string();
                }
            }
            "!Sample_source_name_ch1" => {
                if source_name.is_empty() {
                    source_name = value.to_string();
                }
            }
            "!Sample_organism_ch1" => {
                if organism.is_empty() {
                    organism = value.to_string();
                }
            }
            "!Sample_taxid_ch1" => {
                if tax_id.is_none() {
                    tax_id = value.parse::<i32>().ok();
                }
            }
            "!Sample_platform_id" => {
                if platform.is_empty() {
                    platform = value.to_string();
                }
            }
            "!Sample_geo_accession" => {
                // Prefer the explicit geo_accession field over the ^ header
                // (they should agree, but the field is canonical)
                if !value.is_empty() && accession.is_empty() {
                    accession = value.to_string();
                }
            }
            k if k.starts_with("!Sample_characteristics_ch") => {
                // Format: "key: value" or just "value"
                if let Some((ck, cv)) = value.split_once(':') {
                    characteristics.insert(
                        ck.trim().to_string(),
                        JsonValue::String(cv.trim().to_string()),
                    );
                } else if !value.is_empty() {
                    // Unnamed characteristic — store with sequential key
                    let idx = characteristics.len();
                    characteristics.insert(
                        format!("characteristic_{}", idx),
                        JsonValue::String(value.to_string()),
                    );
                }
            }
            k if k.starts_with("!Sample_") => {
                // Capture other sample fields into extra_metadata
                // Strip the "!Sample_" prefix for cleaner key names
                let short_key = k.trim_start_matches("!Sample_");
                extra.insert(short_key.to_string(), JsonValue::String(value.to_string()));
            }
            _ => {}
        }
    }

    // Flush the last sample
    if in_sample {
        flush(
            &accession,
            &title,
            &source_name,
            &organism,
            tax_id,
            &platform,
            characteristics,
            extra,
            &mut samples,
        );
    }

    Ok(samples)
}

// ─── SRA XML parser ───────────────────────────────────────────────────────────

async fn fetch_sra_samples(
    client: &NcbiClient,
    dataset_id: i64,
    srp_accession: &str,
) -> Result<Vec<OmicSampleData>, String> {
    // efetch requires numeric UIDs via `id=`; the `acc=` parameter is not
    // valid for efetch and produces a 400 response.
    //
    // Step 1: esearch to resolve the SRP accession to ALL numeric UIDs.
    //   esearch.fcgi?db=sra&term=SRP...&retmax=500&retmode=json
    //
    //   retmax=500 captures all experiments for any realistic study.
    //   The SRP study accession returns one UID per SRX (experiment), and each
    //   experiment maps to one or more SRS (sample) records. Using retmax=1
    //   would cap the fetch at 1 experiment → 1 sample regardless of study size.
    //
    // Step 2: batch the UID list into groups of SRA_EFETCH_BATCH_SIZE and issue
    //   one efetch call per batch:
    //   efetch.fcgi?db=sra&id=<uid1,uid2,...>&retmode=xml
    //   Each call returns an EXPERIMENT_PACKAGE_SET; the XML parser deduplicates
    //   SRS accessions across batches so no sample appears twice.

    let esearch_params = [
        ("db", "sra"),
        ("term", srp_accession),
        ("retmax", SRA_ESEARCH_RETMAX),
        ("retmode", "json"),
    ];
    let esearch_body = client.fetch_with_retry(ESEARCH_URL, &esearch_params).await?;

    let esearch_resp: SraEsearchResponse = serde_json::from_str(&esearch_body)
        .map_err(|e| format!("SRA esearch JSON parse error: {e}"))?;

    let uids = esearch_resp.esearchresult.idlist;
    if uids.is_empty() {
        return Err(format!(
            "SRA esearch returned no UIDs for accession: {srp_accession}"
        ));
    }

    eprintln!(
        "[sra] {srp_accession}: esearch returned {} UIDs, fetching in batches of {}",
        uids.len(),
        SRA_EFETCH_BATCH_SIZE,
    );

    // Shared dedup set across all batches so the same SRS accession from
    // different experiment packages is never inserted twice.
    let mut seen_accessions: std::collections::HashSet<String> =
        std::collections::HashSet::new();
    let mut all_samples: Vec<OmicSampleData> = Vec::new();

    for (batch_idx, batch) in uids.chunks(SRA_EFETCH_BATCH_SIZE).enumerate() {
        let id_list = batch.join(",");
        let efetch_params = [
            ("db", "sra"),
            ("id", id_list.as_str()),
            ("retmode", "xml"),
        ];
        let body = client.fetch_with_retry(EFETCH_URL, &efetch_params).await?;

        let batch_samples = parse_sra_samples_xml_with_dedup(&body, dataset_id, &mut seen_accessions)?;
        eprintln!(
            "[sra] {srp_accession}: batch {} → {} new samples (total so far: {})",
            batch_idx,
            batch_samples.len(),
            all_samples.len() + batch_samples.len(),
        );
        all_samples.extend(batch_samples);
    }

    eprintln!(
        "[sra] {srp_accession}: finished — {} distinct samples total",
        all_samples.len(),
    );
    Ok(all_samples)
}

/// Parse SRA efetch XML.  Expected structure (simplified):
/// ```xml
/// <EXPERIMENT_PACKAGE_SET>
///   <EXPERIMENT_PACKAGE>
///     <SAMPLE accession="SRS123456" alias="...">
///       <TITLE>...</TITLE>
///       <SAMPLE_NAME>
///         <SCIENTIFIC_NAME>Homo sapiens</SCIENTIFIC_NAME>
///         <TAXON_ID>9606</TAXON_ID>
///       </SAMPLE_NAME>
///       <SAMPLE_ATTRIBUTES>
///         <SAMPLE_ATTRIBUTE>
///           <TAG>age</TAG>
///           <VALUE>25</VALUE>
///         </SAMPLE_ATTRIBUTE>
///       </SAMPLE_ATTRIBUTES>
///     </SAMPLE>
///   </EXPERIMENT_PACKAGE>
/// </EXPERIMENT_PACKAGE_SET>
/// ```
///
/// One pass extracts all `<SAMPLE>` blocks. Deduplicates by `accession`.
///
/// This is a thin wrapper around `parse_sra_samples_xml_with_dedup` that
/// creates a fresh dedup set, used by unit tests.
#[cfg(test)]
fn parse_sra_samples_xml(xml: &str, dataset_id: i64) -> Result<Vec<OmicSampleData>, String> {
    let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
    parse_sra_samples_xml_with_dedup(xml, dataset_id, &mut seen)
}

/// Inner parser that accepts an external dedup set so that samples are
/// deduplicated across multiple efetch batches (cross-batch dedup).
fn parse_sra_samples_xml_with_dedup(
    xml: &str,
    dataset_id: i64,
    mut seen_accessions: &mut std::collections::HashSet<String>,
) -> Result<Vec<OmicSampleData>, String> {
    let mut reader = Reader::from_str(xml);
    reader.config_mut().trim_text(true);

    let mut samples: Vec<OmicSampleData> = Vec::new();
    // seen_accessions is provided by the caller for cross-batch dedup.

    // Per-sample state
    let mut in_sample = false;
    let mut sample_accession = String::new();
    let mut sample_title = String::new();
    let mut organism = String::new();
    let mut tax_id: Option<i32> = None;
    let mut characteristics: Map<String, JsonValue> = Map::new();
    let mut extra: Map<String, JsonValue> = Map::new();

    // Per-attribute state
    let mut in_title = false;
    let mut in_scientific_name = false;
    let mut in_taxon_id = false;
    let mut in_tag = false;
    let mut in_value = false;
    let mut current_tag = String::new();
    let mut current_value = String::new();

    // Nesting depth for SAMPLE (to detect nested elements properly)
    let mut sample_depth: usize = 0;

    let mut buf = Vec::new();
    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Start(ref e)) => {
                let name = String::from_utf8_lossy(e.name().as_ref()).to_string();
                match name.as_str() {
                    "SAMPLE" => {
                        if in_sample {
                            sample_depth += 1;
                        } else {
                            in_sample = true;
                            sample_depth = 0;
                            // Extract accession from attribute
                            for attr in e.attributes().flatten() {
                                let k = String::from_utf8_lossy(attr.key.as_ref()).to_string();
                                if k == "accession" {
                                    sample_accession =
                                        String::from_utf8_lossy(&attr.value).to_string();
                                }
                            }
                        }
                    }
                    "TITLE" if in_sample && sample_depth == 0 => {
                        in_title = true;
                    }
                    "SCIENTIFIC_NAME" if in_sample => {
                        in_scientific_name = true;
                    }
                    "TAXON_ID" if in_sample => {
                        in_taxon_id = true;
                    }
                    "TAG" if in_sample => {
                        in_tag = true;
                        current_tag.clear();
                    }
                    "VALUE" if in_sample => {
                        in_value = true;
                        current_value.clear();
                    }
                    _ => {}
                }
            }
            Ok(Event::Empty(ref e)) => {
                // Self-closing elements — e.g. <SAMPLE accession="..." alias="..."/>
                let name = String::from_utf8_lossy(e.name().as_ref()).to_string();
                if name == "SAMPLE" && !in_sample {
                    // Rare case: self-closing SAMPLE element (minimal payload).
                    // Collect accession then flush immediately (no children to parse).
                    let mut acc_tmp = String::new();
                    for attr in e.attributes().flatten() {
                        let k = String::from_utf8_lossy(attr.key.as_ref()).to_string();
                        if k == "accession" {
                            acc_tmp = String::from_utf8_lossy(&attr.value).to_string();
                        }
                    }
                    sample_accession = acc_tmp;
                    flush_sra_sample(
                        &mut samples,
                        &mut seen_accessions,
                        dataset_id,
                        &mut sample_accession,
                        &mut sample_title,
                        &mut organism,
                        &mut tax_id,
                        std::mem::take(&mut characteristics),
                        std::mem::take(&mut extra),
                    );
                    // in_sample stays false — this element was fully self-contained
                }
            }
            Ok(Event::Text(ref e)) => {
                let text = e.unescape().unwrap_or_default().to_string();
                if in_title {
                    sample_title.push_str(&text);
                } else if in_scientific_name {
                    organism.push_str(&text);
                } else if in_taxon_id {
                    if tax_id.is_none() {
                        tax_id = text.trim().parse::<i32>().ok();
                    }
                } else if in_tag {
                    current_tag.push_str(&text);
                } else if in_value {
                    current_value.push_str(&text);
                }
            }
            Ok(Event::End(ref e)) => {
                let name = String::from_utf8_lossy(e.name().as_ref()).to_string();
                match name.as_str() {
                    "SAMPLE" => {
                        if sample_depth > 0 {
                            sample_depth -= 1;
                        } else if in_sample {
                            flush_sra_sample(
                                &mut samples,
                                &mut seen_accessions,
                                dataset_id,
                                &mut sample_accession,
                                &mut sample_title,
                                &mut organism,
                                &mut tax_id,
                                std::mem::take(&mut characteristics),
                                std::mem::take(&mut extra),
                            );
                            in_sample = false;
                            sample_depth = 0;
                        }
                    }
                    "TITLE" => {
                        in_title = false;
                    }
                    "SCIENTIFIC_NAME" => {
                        in_scientific_name = false;
                    }
                    "TAXON_ID" => {
                        in_taxon_id = false;
                    }
                    "TAG" => {
                        in_tag = false;
                    }
                    "VALUE" => {
                        in_value = false;
                        // Commit attribute when VALUE closes
                        if !current_tag.is_empty() {
                            characteristics.insert(
                                current_tag.trim().to_lowercase().replace(' ', "_"),
                                JsonValue::String(current_value.trim().to_string()),
                            );
                            current_tag.clear();
                            current_value.clear();
                        }
                    }
                    _ => {}
                }
            }
            Ok(Event::Eof) => break,
            Err(e) => return Err(format!("SRA XML parse error: {e}")),
            _ => {}
        }
        buf.clear();
    }

    Ok(samples)
}

#[allow(clippy::too_many_arguments)]
fn flush_sra_sample(
    samples: &mut Vec<OmicSampleData>,
    seen: &mut std::collections::HashSet<String>,
    dataset_id: i64,
    accession: &mut String,
    title: &mut String,
    organism: &mut String,
    tax_id: &mut Option<i32>,
    characteristics: Map<String, JsonValue>,
    extra: Map<String, JsonValue>,
) {
    let acc = accession.trim().to_string();
    if acc.is_empty() || seen.contains(&acc) {
        accession.clear();
        title.clear();
        organism.clear();
        *tax_id = None;
        return;
    }
    seen.insert(acc.clone());

    samples.push(OmicSampleData {
        dataset_id,
        accession: acc,
        title: std::mem::take(title),
        source_name: String::new(), // SRA XML does not have a direct source_name field
        organism: std::mem::take(organism),
        tax_id: tax_id.take(),
        platform: String::new(), // platform is at experiment level, not sample level
        characteristics: JsonValue::Object(characteristics),
        extra_metadata: if extra.is_empty() {
            json!({})
        } else {
            JsonValue::Object(extra)
        },
    });
    accession.clear();
}

// ─── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    // ── GEO SOFT tests ────────────────────────────────────────────────────────

    #[test]
    fn test_parse_geo_soft_single_sample() {
        let soft = r#"^SERIES = GSE12345
!Series_title = Test series

^SAMPLE = GSM111111
!Sample_title = Sample 1 - liver
!Sample_geo_accession = GSM111111
!Sample_source_name_ch1 = liver biopsy
!Sample_organism_ch1 = Homo sapiens
!Sample_taxid_ch1 = 9606
!Sample_platform_id = GPL570
!Sample_characteristics_ch1 = tissue: liver
!Sample_characteristics_ch1 = age: 30
!Sample_description = Control sample
"#;
        let samples = parse_geo_soft(soft, 42).unwrap();
        assert_eq!(samples.len(), 1);
        let s = &samples[0];
        assert_eq!(s.dataset_id, 42);
        assert_eq!(s.accession, "GSM111111");
        assert_eq!(s.title, "Sample 1 - liver");
        assert_eq!(s.source_name, "liver biopsy");
        assert_eq!(s.organism, "Homo sapiens");
        assert_eq!(s.tax_id, Some(9606));
        assert_eq!(s.platform, "GPL570");
        assert_eq!(s.characteristics["tissue"], "liver");
        assert_eq!(s.characteristics["age"], "30");
    }

    #[test]
    fn test_parse_geo_soft_multiple_samples() {
        let soft = r#"^SERIES = GSE99
^SAMPLE = GSM001
!Sample_title = Sample A
!Sample_organism_ch1 = Mus musculus
!Sample_taxid_ch1 = 10090
!Sample_platform_id = GPL11180
!Sample_characteristics_ch1 = genotype: wild type

^SAMPLE = GSM002
!Sample_title = Sample B
!Sample_organism_ch1 = Mus musculus
!Sample_taxid_ch1 = 10090
!Sample_platform_id = GPL11180
!Sample_characteristics_ch1 = genotype: knockout
"#;
        let samples = parse_geo_soft(soft, 7).unwrap();
        assert_eq!(samples.len(), 2);
        assert_eq!(samples[0].accession, "GSM001");
        assert_eq!(samples[1].accession, "GSM002");
        assert_eq!(samples[0].characteristics["genotype"], "wild type");
        assert_eq!(samples[1].characteristics["genotype"], "knockout");
    }

    #[test]
    fn test_parse_geo_soft_no_samples() {
        let soft = "^SERIES = GSE0\n!Series_title = Empty\n";
        let samples = parse_geo_soft(soft, 1).unwrap();
        assert!(samples.is_empty());
    }

    #[test]
    fn test_parse_geo_soft_unnamed_characteristic() {
        let soft = r#"^SAMPLE = GSM999
!Sample_title = X
!Sample_characteristics_ch1 = just a value
"#;
        let samples = parse_geo_soft(soft, 1).unwrap();
        assert_eq!(samples.len(), 1);
        // Unnamed characteristic stored with sequential key
        assert!(samples[0].characteristics.get("characteristic_0").is_some());
    }

    // ── SRA XML tests ─────────────────────────────────────────────────────────

    #[test]
    fn test_parse_sra_samples_xml_basic() {
        let xml = r#"<?xml version="1.0" encoding="UTF-8"?>
<EXPERIMENT_PACKAGE_SET>
  <EXPERIMENT_PACKAGE>
    <SAMPLE accession="SRS111111" alias="sample_a">
      <TITLE>Heart tissue RNA-seq sample</TITLE>
      <SAMPLE_NAME>
        <TAXON_ID>9606</TAXON_ID>
        <SCIENTIFIC_NAME>Homo sapiens</SCIENTIFIC_NAME>
      </SAMPLE_NAME>
      <SAMPLE_ATTRIBUTES>
        <SAMPLE_ATTRIBUTE>
          <TAG>tissue</TAG>
          <VALUE>heart</VALUE>
        </SAMPLE_ATTRIBUTE>
        <SAMPLE_ATTRIBUTE>
          <TAG>age</TAG>
          <VALUE>52</VALUE>
        </SAMPLE_ATTRIBUTE>
      </SAMPLE_ATTRIBUTES>
    </SAMPLE>
  </EXPERIMENT_PACKAGE>
</EXPERIMENT_PACKAGE_SET>"#;

        let samples = parse_sra_samples_xml(xml, 99).unwrap();
        assert_eq!(samples.len(), 1);
        let s = &samples[0];
        assert_eq!(s.dataset_id, 99);
        assert_eq!(s.accession, "SRS111111");
        assert_eq!(s.title, "Heart tissue RNA-seq sample");
        assert_eq!(s.organism, "Homo sapiens");
        assert_eq!(s.tax_id, Some(9606));
        assert_eq!(s.characteristics["tissue"], "heart");
        assert_eq!(s.characteristics["age"], "52");
    }

    #[test]
    fn test_parse_sra_samples_xml_dedup() {
        // Same SRS accession appears in two experiment packages
        let xml = r#"<EXPERIMENT_PACKAGE_SET>
  <EXPERIMENT_PACKAGE>
    <SAMPLE accession="SRS000001" alias="s1">
      <TITLE>Sample 1</TITLE>
      <SAMPLE_NAME><TAXON_ID>9606</TAXON_ID><SCIENTIFIC_NAME>Homo sapiens</SCIENTIFIC_NAME></SAMPLE_NAME>
    </SAMPLE>
  </EXPERIMENT_PACKAGE>
  <EXPERIMENT_PACKAGE>
    <SAMPLE accession="SRS000001" alias="s1">
      <TITLE>Sample 1</TITLE>
      <SAMPLE_NAME><TAXON_ID>9606</TAXON_ID><SCIENTIFIC_NAME>Homo sapiens</SCIENTIFIC_NAME></SAMPLE_NAME>
    </SAMPLE>
  </EXPERIMENT_PACKAGE>
</EXPERIMENT_PACKAGE_SET>"#;

        let samples = parse_sra_samples_xml(xml, 1).unwrap();
        assert_eq!(samples.len(), 1, "duplicate SRS should be deduped");
    }

    #[test]
    fn test_parse_sra_samples_xml_multiple() {
        let xml = r#"<EXPERIMENT_PACKAGE_SET>
  <EXPERIMENT_PACKAGE>
    <SAMPLE accession="SRS000002" alias="s2">
      <TITLE>Kidney sample</TITLE>
      <SAMPLE_NAME><TAXON_ID>9606</TAXON_ID><SCIENTIFIC_NAME>Homo sapiens</SCIENTIFIC_NAME></SAMPLE_NAME>
      <SAMPLE_ATTRIBUTES>
        <SAMPLE_ATTRIBUTE><TAG>tissue</TAG><VALUE>kidney</VALUE></SAMPLE_ATTRIBUTE>
      </SAMPLE_ATTRIBUTES>
    </SAMPLE>
  </EXPERIMENT_PACKAGE>
  <EXPERIMENT_PACKAGE>
    <SAMPLE accession="SRS000003" alias="s3">
      <TITLE>Lung sample</TITLE>
      <SAMPLE_NAME><TAXON_ID>10090</TAXON_ID><SCIENTIFIC_NAME>Mus musculus</SCIENTIFIC_NAME></SAMPLE_NAME>
    </SAMPLE>
  </EXPERIMENT_PACKAGE>
</EXPERIMENT_PACKAGE_SET>"#;

        let samples = parse_sra_samples_xml(xml, 5).unwrap();
        assert_eq!(samples.len(), 2);
        assert_eq!(samples[0].accession, "SRS000002");
        assert_eq!(samples[1].accession, "SRS000003");
        assert_eq!(samples[1].organism, "Mus musculus");
        assert_eq!(samples[1].tax_id, Some(10090));
    }

    #[test]
    fn test_parse_sra_samples_xml_empty() {
        let xml = "<EXPERIMENT_PACKAGE_SET></EXPERIMENT_PACKAGE_SET>";
        let samples = parse_sra_samples_xml(xml, 1).unwrap();
        assert!(samples.is_empty());
    }

    // ── Integration tests (require network, run with --ignored) ──────────────
    //
    // How to run:
    //   cd rust_src && cargo test -- --ignored --nocapture
    //
    // These tests hit real NCBI endpoints. They validate that the corrected
    // fetch URLs (acc.cgi for GEO, esearch+efetch for SRA) work end-to-end.
    // They are marked #[ignore] so CI does not depend on network availability.

    #[tokio::test]
    #[ignore = "requires network access to real NCBI endpoints"]
    async fn integration_geo_fetch_gse10072() {
        // GSE10072: lung cancer study with ~107 samples — small enough for a quick test.
        let client = NcbiClient::new(None);
        let result = fetch_geo_samples(&client, 1, "GSE10072").await;
        assert!(result.is_ok(), "fetch_geo_samples failed: {:?}", result.err());
        let samples = result.unwrap();
        assert!(
            samples.len() > 0,
            "Expected samples > 0 for GSE10072, got 0. \
             Check that acc.cgi URL and params are correct."
        );
        // Spot-check: all samples should have a GSM accession and a dataset_id
        for s in &samples {
            assert!(
                s.accession.starts_with("GSM"),
                "Expected GSM accession, got: {}",
                s.accession
            );
            assert_eq!(s.dataset_id, 1);
        }
        eprintln!("GSE10072: {} samples fetched", samples.len());
    }

    #[tokio::test]
    #[ignore = "requires network access to real NCBI endpoints"]
    async fn integration_sra_fetch_srp009539() {
        // SRP009539: small RNA-seq study — modest size for a quick validation.
        let client = NcbiClient::new(None);
        let result = fetch_sra_samples(&client, 2, "SRP009539").await;
        assert!(result.is_ok(), "fetch_sra_samples failed: {:?}", result.err());
        let samples = result.unwrap();
        assert!(
            samples.len() > 0,
            "Expected samples > 0 for SRP009539, got 0. \
             Check that esearch+efetch pipeline is correct."
        );
        for s in &samples {
            assert!(
                s.accession.starts_with("SRS"),
                "Expected SRS accession, got: {}",
                s.accession
            );
            assert_eq!(s.dataset_id, 2);
        }
        eprintln!("SRP009539: {} samples fetched", samples.len());
    }

    #[tokio::test]
    #[ignore = "requires network access to real NCBI endpoints"]
    async fn integration_sra_fetch_srp612352_multi_sample() {
        // SRP612352: the study that exposed the retmax=1 bug.
        // Ground truth from the database: n_samples=13, only 1 OmicSample was
        // created before the fix.  After the fix, esearch must return >1 UID
        // and the full efetch+parse pipeline must return >1 distinct SRS sample.
        let client = NcbiClient::new(None);
        let result = fetch_sra_samples(&client, 3, "SRP612352").await;
        assert!(result.is_ok(), "fetch_sra_samples failed: {:?}", result.err());
        let samples = result.unwrap();
        assert!(
            samples.len() > 1,
            "Expected >1 sample for SRP612352 (ground truth: 13), got {}. \
             Verify that esearch retmax is not 1.",
            samples.len()
        );
        // Spot-check: all accessions must be SRS*, dataset_id must be correct
        for s in &samples {
            assert!(
                s.accession.starts_with("SRS"),
                "Expected SRS accession, got: {}",
                s.accession
            );
            assert_eq!(s.dataset_id, 3);
        }
        // No duplicate accessions
        let unique: std::collections::HashSet<&str> =
            samples.iter().map(|s| s.accession.as_str()).collect();
        assert_eq!(
            unique.len(),
            samples.len(),
            "Duplicate SRS accessions detected in result"
        );
        eprintln!("SRP612352: {} distinct samples fetched", samples.len());
    }
}
