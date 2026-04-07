use serde::{Deserialize, Serialize};
use serde_json::Value as JsonValue;

/// Mirrors the core_omicdataset columns written via COPY.
/// `id`, `search_vector`, `ingested_at`, `updated_at` are omitted —
/// Postgres fills them via DEFAULT and the FTS trigger.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OmicDatasetData {
    pub accession: String,         // GSE…, SRP…, PRJNA…, GCST…
    pub source_db: String,         // 'geo' | 'sra' | 'bioproject' | 'gwas_catalog'
    pub bioproject_id: String,     // '' when absent
    pub title: String,
    pub summary: String,
    pub omic_type: String,         // OmicDataset::OmicType value string
    pub omic_subcategory: String,  // 'RNA-Seq', 'WGS', 'ChIP-Seq', '16S rRNA', etc.
    pub organism: String,
    pub tax_id: Option<i32>,
    pub n_samples: Option<i32>,
    pub platform: String,
    pub extra_metadata: JsonValue, // serialized to JSONB
    pub is_active: bool,           // always true on ingest
}

/// Mirrors core_datasetpaperlink.
/// `dataset_accession` and `paper_pmid` are resolved to FK IDs in the COPY phase
/// via SQL lookups, so no internal IDs are needed here.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DatasetPaperLinkData {
    pub dataset_accession: String, // resolved to dataset_id after OmicDataset COPY
    pub paper_pmid: i64,           // resolved to paper_id via SELECT FROM core_paper
    pub link_source: String,       // 'elink' | 'geo_xml' | 'gwas_catalog'
}
