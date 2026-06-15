use quick_xml::events::Event;
use quick_xml::Reader;
use serde::Deserialize;
use serde_json::{json, Value as JsonValue};
use std::collections::HashMap;

use crate::ncbi::client::NcbiClient;
use crate::omics::models::{DatasetPaperLinkData, OmicDatasetData};
use crate::omics::type_classifier::classify_omic_type;

const ESEARCH_URL: &str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi";
const ESUMMARY_URL: &str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi";

// ─── JSON deserialization structs ────────────────────────────────────────────

#[derive(Deserialize)]
struct EsearchResponse {
    esearchresult: EsearchResult,
}

#[derive(Deserialize)]
struct EsearchResult {
    idlist: Vec<String>,
}

/// Parsed data from the SRA expxml fragment.
struct ParsedSraExpxml {
    study_acc: String,
    study_name: String,
    title: String,
    organism: String,
    tax_id: Option<i32>,
    platform: String,
    instrument_model: String,
    library_strategy: String,
    bioproject: String,
    total_runs: Option<i32>,
}

// ─── Public API ──────────────────────────────────────────────────────────────

/// Fetch SRA study metadata for the given query.
///
/// The SRA esummary response embeds metadata as XML strings in the `expxml`
/// and `runs` fields. This parser extracts data from those XML fragments.
///
/// Returns `(datasets, links)`. Most SRA entries do not embed PMIDs directly,
/// so the returned `links` vec is typically empty. Use elink for PMID discovery.
pub async fn fetch_sra_datasets(
    client: &NcbiClient,
    query: &str,
    max_results: usize,
) -> Result<(Vec<OmicDatasetData>, Vec<DatasetPaperLinkData>), String> {
    // Step 1 — esearch
    let max_str = max_results.to_string();
    let search_params = [
        ("db", "sra"),
        ("term", query),
        ("retmax", &max_str),
        ("retmode", "json"),
    ];
    let search_body = client.fetch_with_retry(ESEARCH_URL, &search_params).await?;
    let search_resp: EsearchResponse =
        serde_json::from_str(&search_body).map_err(|e| format!("SRA esearch parse error: {e}"))?;

    let uids = search_resp.esearchresult.idlist;
    if uids.is_empty() {
        return Ok((vec![], vec![]));
    }

    // Step 2 — esummary in batches of 25 (NCBI GET URL length limit ~4KB)
    // Deduplicate by Study accession (SRP) — multiple UIDs can belong to the same study
    let mut study_map: HashMap<String, OmicDatasetData> = HashMap::new();
    let mut uid_to_acc: HashMap<String, String> = HashMap::new();
    let mut all_links: Vec<DatasetPaperLinkData> = Vec::new();

    for chunk in uids.chunks(25) {
        let ids_csv = chunk.join(",");
        let summary_params = [
            ("db", "sra"),
            ("id", ids_csv.as_str()),
            ("retmode", "json"),
        ];
        let summary_body = client.fetch_with_retry(ESUMMARY_URL, &summary_params).await?;

        let raw: serde_json::Value = serde_json::from_str(&summary_body)
            .map_err(|e| format!("SRA esummary parse error: {e}"))?;

        let result_obj = match raw.get("result").and_then(|v| v.as_object()) {
            Some(obj) => obj,
            None => continue,
        };

        for uid in chunk {
            let entry_val = match result_obj.get(uid) {
                Some(v) if v.is_object() => v,
                _ => continue,
            };

            // Extract expxml (XML string embedded in JSON)
            let expxml = match entry_val.get("expxml").and_then(|v| v.as_str()) {
                Some(xml) if !xml.trim().is_empty() => xml,
                _ => {
                    eprintln!("[sra_parser] UID {} has no expxml, skipping", uid);
                    continue;
                }
            };

            // Parse the XML fragment
            let wrapped = format!("<root>{}</root>", expxml);
            let parsed = match parse_sra_expxml(&wrapped) {
                Ok(p) => p,
                Err(e) => {
                    eprintln!("[sra_parser] Failed to parse expxml for UID {}: {}", uid, e);
                    continue;
                }
            };

            if parsed.study_acc.is_empty() {
                eprintln!("[sra_parser] UID {} has no Study accession in expxml, skipping", uid);
                continue;
            }

            let accession = parsed.study_acc.clone();
            uid_to_acc.insert(uid.clone(), accession.clone());

            // Extract createdate from top-level JSON
            let createdate = entry_val
                .get("createdate")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_string();

            // Classify omic type using title + library_strategy
            let classify_text = format!("{} {} {}", parsed.title, parsed.study_name, parsed.library_strategy);
            let classifications = classify_omic_type(&classify_text, "");
            let omic_type = classifications
                .first()
                .map(|c| c.omic_type.to_string())
                .unwrap_or_else(|| "other".to_string());
            let omic_subcategory = if !parsed.library_strategy.is_empty() {
                parsed.library_strategy.clone()
            } else {
                classifications
                    .first()
                    .map(|c| c.omic_subcategory.to_string())
                    .unwrap_or_default()
            };

            let extra_metadata = json!({
                "sra_uid": uid,
                "library_strategy": parsed.library_strategy,
                "createdate": createdate,
            });

            // Deduplicate by study accession — keep the entry with more info
            study_map
                .entry(accession.clone())
                .and_modify(|existing| {
                    // Merge: keep longest title, accumulate runs
                    if parsed.title.len() > existing.title.len() && !parsed.title.is_empty() {
                        existing.title = parsed.title.clone();
                    }
                    if let (Some(new_runs), Some(old_runs)) = (parsed.total_runs, existing.n_samples) {
                        existing.n_samples = Some(old_runs.max(new_runs));
                    }
                })
                .or_insert_with(|| OmicDatasetData {
                    accession: accession.clone(),
                    source_db: "sra".to_string(),
                    bioproject_id: parsed.bioproject.clone(),
                    title: if !parsed.title.is_empty() {
                        parsed.title.clone()
                    } else {
                        parsed.study_name.clone()
                    },
                    summary: String::new(), // SRA esummary has no abstract
                    omic_type,
                    omic_subcategory,
                    organism: parsed.organism.clone(),
                    tax_id: parsed.tax_id,
                    n_samples: parsed.total_runs,
                    platform: if !parsed.instrument_model.is_empty() {
                        parsed.instrument_model.clone()
                    } else {
                        parsed.platform.clone()
                    },
                    extra_metadata,
                    is_active: true,
                });
        }
    }

    let all_datasets: Vec<OmicDatasetData> = study_map.into_values().collect();

    Ok((all_datasets, all_links))
}

// ─── XML parser ──────────────────────────────────────────────────────────────

fn parse_sra_expxml(xml: &str) -> Result<ParsedSraExpxml, String> {
    let mut reader = Reader::from_str(xml);
    reader.config_mut().trim_text(true);

    let mut study_acc = String::new();
    let mut study_name = String::new();
    let mut title = String::new();
    let mut organism = String::new();
    let mut tax_id: Option<i32> = None;
    let mut platform = String::new();
    let mut instrument_model = String::new();
    let mut library_strategy = String::new();
    let mut bioproject = String::new();
    let mut total_runs: Option<i32> = None;

    let mut in_title = false;
    let mut in_bioproject = false;
    let mut in_library_strategy = false;

    let mut buf = Vec::new();
    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Start(ref e)) | Ok(Event::Empty(ref e)) => {
                let name = String::from_utf8_lossy(e.name().as_ref()).to_string();
                match name.as_str() {
                    "Study" => {
                        for attr in e.attributes().flatten() {
                            let key = String::from_utf8_lossy(attr.key.as_ref()).to_string();
                            let val = String::from_utf8_lossy(&attr.value).to_string();
                            if key == "acc" {
                                study_acc = val.clone();
                            }
                            if key == "name" && study_name.is_empty() {
                                study_name = val;
                            }
                        }
                    }
                    "Organism" => {
                        for attr in e.attributes().flatten() {
                            let key = String::from_utf8_lossy(attr.key.as_ref()).to_string();
                            let val = String::from_utf8_lossy(&attr.value).to_string();
                            if key == "ScientificName" {
                                organism = val.clone();
                            }
                            if key == "taxid" {
                                tax_id = val.parse::<i32>().ok();
                            }
                        }
                    }
                    "Platform" => {
                        for attr in e.attributes().flatten() {
                            let key = String::from_utf8_lossy(attr.key.as_ref()).to_string();
                            let val = String::from_utf8_lossy(&attr.value).to_string();
                            if key == "instrument_model" {
                                instrument_model = val;
                            }
                        }
                        // Platform text content is the platform name (e.g., "ILLUMINA")
                    }
                    "Instrument" => {
                        // <Instrument ILLUMINA="Illumina NovaSeq 6000"/>
                        for attr in e.attributes().flatten() {
                            let val = String::from_utf8_lossy(&attr.value).to_string();
                            if instrument_model.is_empty() {
                                instrument_model = val;
                            }
                        }
                    }
                    "Statistics" => {
                        for attr in e.attributes().flatten() {
                            let key = String::from_utf8_lossy(attr.key.as_ref()).to_string();
                            let val = String::from_utf8_lossy(&attr.value).to_string();
                            if key == "total_runs" {
                                total_runs = val.parse::<i32>().ok();
                            }
                        }
                    }
                    "Title" => {
                        in_title = true;
                    }
                    "Bioproject" => {
                        in_bioproject = true;
                    }
                    "LIBRARY_STRATEGY" => {
                        in_library_strategy = true;
                    }
                    _ => {}
                }
            }
            Ok(Event::Text(ref e)) => {
                let text = e.unescape().unwrap_or_default().to_string();
                if in_title && title.is_empty() {
                    title = text.clone();
                    in_title = false;
                }
                if in_bioproject {
                    bioproject = text.clone();
                    in_bioproject = false;
                }
                if in_library_strategy {
                    library_strategy = text;
                    in_library_strategy = false;
                }
            }
            Ok(Event::End(ref e)) => {
                let name = String::from_utf8_lossy(e.name().as_ref()).to_string();
                match name.as_str() {
                    "Title" => in_title = false,
                    "Bioproject" => in_bioproject = false,
                    "LIBRARY_STRATEGY" => in_library_strategy = false,
                    "Platform" => {}
                    _ => {}
                }
            }
            Ok(Event::Eof) => break,
            Err(e) => return Err(format!("XML parse error: {e}")),
            _ => {}
        }
        buf.clear();
    }

    if study_acc.is_empty() {
        return Err("No Study accession found in expxml".into());
    }

    Ok(ParsedSraExpxml {
        study_acc,
        study_name,
        title,
        organism,
        tax_id,
        platform,
        instrument_model,
        library_strategy,
        bioproject,
        total_runs,
    })
}

/// Returns `(uid, accession)` pairs for datasets that had no embedded PMIDs,
/// to be used for elink discovery.
pub fn datasets_without_pmids<'a>(
    datasets: &'a [OmicDatasetData],
    links: &[DatasetPaperLinkData],
) -> Vec<(String, String)> {
    let linked: std::collections::HashSet<&str> =
        links.iter().map(|l| l.dataset_accession.as_str()).collect();

    datasets
        .iter()
        .filter(|d| !linked.contains(d.accession.as_str()))
        .filter_map(|d| {
            d.extra_metadata
                .get("sra_uid")
                .and_then(|v| v.as_str())
                .map(|uid| (uid.to_string(), d.accession.clone()))
        })
        .collect()
}

// ─── Tests ───────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_expxml_basic() {
        let xml = r#"<root>
            <Summary>
                <Title>RNA-Seq analysis of heart tissue</Title>
                <Platform instrument_model="Illumina HiSeq 2500">ILLUMINA</Platform>
                <Statistics total_runs="24" total_spots="500000000" total_bases="75000000000"/>
            </Summary>
            <Study acc="SRP123456" name="Cardiovascular RNA-Seq Study"/>
            <Organism taxid="9606" ScientificName="Homo sapiens"/>
            <Instrument ILLUMINA="Illumina HiSeq 2500"/>
            <Library_descriptor>
                <LIBRARY_STRATEGY>RNA-Seq</LIBRARY_STRATEGY>
            </Library_descriptor>
            <Bioproject>PRJNA123456</Bioproject>
        </root>"#;

        let result = parse_sra_expxml(xml).unwrap();
        assert_eq!(result.study_acc, "SRP123456");
        assert_eq!(result.title, "RNA-Seq analysis of heart tissue");
        assert_eq!(result.organism, "Homo sapiens");
        assert_eq!(result.tax_id, Some(9606));
        assert_eq!(result.library_strategy, "RNA-Seq");
        assert_eq!(result.total_runs, Some(24));
        assert_eq!(result.bioproject, "PRJNA123456");
        assert_eq!(result.instrument_model, "Illumina HiSeq 2500");
    }

    #[test]
    fn test_parse_expxml_empty_returns_error() {
        let xml = "<root></root>";
        assert!(parse_sra_expxml(xml).is_err());
    }

    #[test]
    fn test_parse_expxml_minimal() {
        let xml = r#"<root>
            <Study acc="SRP999999" name="WGS of cardiac patients"/>
        </root>"#;

        let result = parse_sra_expxml(xml).unwrap();
        assert_eq!(result.study_acc, "SRP999999");
        assert_eq!(result.study_name, "WGS of cardiac patients");
        assert!(result.organism.is_empty());
        assert!(result.library_strategy.is_empty());
    }

    #[test]
    fn test_parse_real_sra_expxml() {
        // Real structure from NCBI API (simplified)
        let xml = r#"<root>  <Summary><Title>16S rRNA sequencing of bacterial microbiota</Title><Platform instrument_model="Illumina NovaSeq 6000">ILLUMINA</Platform><Statistics total_runs="1" total_spots="152845" total_bases="62340974" total_size="26400747" load_done="true" cluster_name="public"/></Summary><Submitter acc="SRA2330084" center_name="Test Center" contact_name="Test" lab_name="Test"/><Experiment acc="SRX32094066" ver="1" status="public" name="Test experiment"/><Study acc="SRP675416" name="Gut microbiome and cardiovascular risk"/><Organism taxid="749906" ScientificName="gut metagenome"/><Sample acc="SRS28028373" name=""/><Instrument ILLUMINA="Illumina NovaSeq 6000"/><Library_descriptor><LIBRARY_NAME>BW193</LIBRARY_NAME><LIBRARY_STRATEGY>AMPLICON</LIBRARY_STRATEGY><LIBRARY_SOURCE>GENOMIC</LIBRARY_SOURCE><LIBRARY_SELECTION>PCR</LIBRARY_SELECTION><LIBRARY_LAYOUT><SINGLE/></LIBRARY_LAYOUT></Library_descriptor><Bioproject>PRJNA1405921</Bioproject><Biosample>SAMN55211049</Biosample>  </root>"#;

        let result = parse_sra_expxml(xml).unwrap();
        assert_eq!(result.study_acc, "SRP675416");
        assert_eq!(result.title, "16S rRNA sequencing of bacterial microbiota");
        assert_eq!(result.organism, "gut metagenome");
        assert_eq!(result.tax_id, Some(749906));
        assert_eq!(result.library_strategy, "AMPLICON");
        assert_eq!(result.total_runs, Some(1));
        assert_eq!(result.bioproject, "PRJNA1405921");
        assert_eq!(result.instrument_model, "Illumina NovaSeq 6000");
    }
}
