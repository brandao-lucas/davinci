use serde::{Deserialize, Serialize};
use serde_json::Value as JsonValue;

/// Mirrors the core_omicsample columns written via COPY.
///
/// `dataset_id` is the PK integer of the parent OmicDataset row (FK).
/// `accession` is the natural key: GSM*, SRX*/SRS*, etc.
/// `ingested_at` and `updated_at` are set by the COPY writer; `ingested_at`
/// is never overwritten on conflict (only `updated_at` changes).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OmicSampleData {
    pub dataset_id: i64,           // FK → core_omicdataset.id (PK integer)
    pub accession: String,         // GSM*, SRX*/SRS*/SRR* — natural key, globally unique
    pub title: String,
    pub source_name: String,       // tissue / cell line / condition
    pub organism: String,
    pub tax_id: Option<i32>,
    pub platform: String,          // GPL* for GEO, instrument model for SRA
    pub characteristics: JsonValue, // key→value JSONB (e.g. {"age": "25", "tissue": "liver"})
    pub extra_metadata: JsonValue,  // raw fields not mapped above
}

/// Mirrors the core_omicdataset columns written via COPY.
/// `id`, `search_vector`, `ingested_at`, `updated_at` are omitted —
/// Postgres fills them via DEFAULT and the FTS trigger.
///
/// Campos do contrato OmnisPathway (Fase 1):
/// - `omics_layers`: camadas ômicas normalizadas (ex: vec!["proteomic"]).
///   Serializado no CSV como literal Postgres array: `{proteomic}`.
/// - `omics_count`: número de camadas distintas (1 para conectores mono-ômicos).
/// - `data_format`: 'raw' | 'processed' | 'unknown'.
/// - `access_type`: 'public' | 'controlled' | 'unknown'.
///
/// Conectores existentes (geo, sra, bioproject, gwas) usam os defaults:
/// `omics_layers = vec![]`, `omics_count = None`, `data_format = "unknown"`,
/// `access_type = "unknown"` — que o COALESCE/NULLIF no ON CONFLICT preserva.
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
    // --- Contrato OmnisPathway (adicionados na Fase 1) ---
    /// Camadas ômicas (ex: vec!["proteomic"]). Vazio = não avaliado por este conector.
    pub omics_layers: Vec<String>,
    /// Nº de camadas. None = não avaliado por este conector.
    pub omics_count: Option<i32>,
    /// 'raw' | 'processed' | 'unknown'
    pub data_format: String,
    /// 'public' | 'controlled' | 'unknown'
    pub access_type: String,
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
