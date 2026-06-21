use reqwest::Client;
use serde::Deserialize;
use serde_json::json;
use std::time::Duration;

use crate::omics::models::{DatasetPaperLinkData, OmicDatasetData};

const GWAS_BASE_URL: &str =
    "https://www.ebi.ac.uk/gwas/rest/api/studies/search/findByDiseaseTrait";

// ─── JSON deserialization structs (GWAS Catalog JSON-HAL) ────────────────────

#[derive(Deserialize)]
struct GwasResponse {
    #[serde(rename = "_embedded")]
    embedded: Option<GwasEmbedded>,
}

#[derive(Deserialize)]
struct GwasEmbedded {
    studies: Vec<GwasStudy>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct GwasStudy {
    accession_id: Option<String>,
    publication_info: Option<GwasPublication>,
    disease_trait: Option<GwasTrait>,
    association_count: Option<u64>,
    initial_sample_size: Option<String>,
    platforms: Option<Vec<GwasPlatform>>,
    genotyping_technologies: Option<Vec<GwasGenotypingTech>>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct GwasPublication {
    pubmed_id: Option<String>,
    title: Option<String>,
}

#[derive(Deserialize)]
struct GwasTrait {
    // `trait` is a Rust keyword — serde rename maps the JSON "trait" field.
    #[serde(rename = "trait")]
    name: Option<String>,
}

impl GwasTrait {
    fn name(&self) -> String {
        self.name.clone().unwrap_or_default()
    }
}

#[derive(Deserialize)]
struct GwasPlatform {
    manufacturer: Option<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct GwasGenotypingTech {
    genotyping_technology: Option<String>,
}

// ─── Public API ──────────────────────────────────────────────────────────────

/// Fetch GWAS Catalog studies for the given disease trait query.
///
/// Uses the EBI REST API directly (not NCBI eutils), so a plain `reqwest::Client`
/// is used. All GWAS studies are human (`Homo sapiens`, taxon 9606) and classified
/// as `genomic` / `GWAS Array` without running the type classifier.
pub async fn fetch_gwas_datasets(
    query: &str,
    max_results: usize,
) -> Result<(Vec<OmicDatasetData>, Vec<DatasetPaperLinkData>), String> {
    let client = Client::builder()
        .timeout(Duration::from_secs(60))
        .user_agent("DaVinci/1.0 (biohub.solutions; bioinformatics)")
        .build()
        .map_err(|e| format!("Failed to build GWAS HTTP client: {e}"))?;

    let size_str = max_results.to_string();
    let url = format!(
        "{}?diseaseTrait={}&page=0&size={}",
        GWAS_BASE_URL,
        urlencoded(query),
        size_str
    );

    let response = client
        .get(&url)
        .header("Accept", "application/json")
        .send()
        .await
        .map_err(|e| format!("GWAS Catalog request failed: {e}"))?;

    if !response.status().is_success() {
        return Err(format!(
            "GWAS Catalog returned HTTP {}: {}",
            response.status(),
            url
        ));
    }

    let body = response
        .text()
        .await
        .map_err(|e| format!("GWAS Catalog response read error: {e}"))?;

    let gwas_resp: GwasResponse =
        serde_json::from_str(&body).map_err(|e| format!("GWAS Catalog parse error: {e}"))?;

    let studies = match gwas_resp.embedded {
        Some(e) => e.studies,
        None => return Ok((vec![], vec![])),
    };

    let mut all_datasets: Vec<OmicDatasetData> = Vec::with_capacity(studies.len());
    let mut all_links: Vec<DatasetPaperLinkData> = Vec::new();

    for study in studies {
        let accession = match study.accession_id {
            Some(ref a) if !a.is_empty() => a.clone(),
            _ => continue,
        };

        let trait_name = study
            .disease_trait
            .as_ref()
            .map(|t| t.name())
            .unwrap_or_default();

        // Title comes from the publication when available, else the trait name
        let title = study
            .publication_info
            .as_ref()
            .and_then(|p| p.title.clone())
            .filter(|t| !t.is_empty())
            .unwrap_or_else(|| trait_name.clone());

        let platform = study
            .platforms
            .as_ref()
            .and_then(|v| v.first())
            .and_then(|p| p.manufacturer.clone())
            .unwrap_or_default();

        let genotyping_tech = study
            .genotyping_technologies
            .as_ref()
            .and_then(|v| v.first())
            .and_then(|g| g.genotyping_technology.clone())
            .unwrap_or_default();

        let extra_metadata = json!({
            "association_count": study.association_count,
            "sample_size": study.initial_sample_size,
            "genotyping_technology": genotyping_tech,
            "trait": trait_name,
        });

        // Build paper link from embedded PMID
        if let Some(pub_info) = &study.publication_info {
            if let Some(pmid_str) = &pub_info.pubmed_id {
                if let Ok(pmid) = pmid_str.parse::<i64>() {
                    all_links.push(DatasetPaperLinkData {
                        dataset_accession: accession.clone(),
                        paper_pmid: pmid,
                        link_source: "gwas_catalog".to_string(),
                    });
                }
            }
        }

        all_datasets.push(OmicDatasetData {
            accession,
            source_db: "gwas_catalog".to_string(),
            bioproject_id: String::new(),
            title,
            summary: trait_name,
            // All GWAS Catalog data is human genomic by definition
            omic_type: "genomic".to_string(),
            omic_subcategory: "GWAS Array".to_string(),
            organism: "Homo sapiens".to_string(),
            tax_id: Some(9606),
            n_samples: None,
            platform,
            extra_metadata,
            is_active: true,
            // Campos OmnisPathway: não avaliados por este conector (backfill Fase 0)
            omics_layers: vec![],
            omics_count: None,
            data_format: "unknown".to_string(),
            access_type: "unknown".to_string(),
        });
    }

    Ok((all_datasets, all_links))
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

/// Minimal percent-encoding for URL query values.
/// Only encodes characters that would break the URL structure.
fn urlencoded(s: &str) -> String {
    s.chars()
        .map(|c| match c {
            ' ' => "%20".to_string(),
            '&' => "%26".to_string(),
            '+' => "%2B".to_string(),
            '#' => "%23".to_string(),
            '%' => "%25".to_string(),
            _ => c.to_string(),
        })
        .collect()
}
