use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PaperData {
    pub pmid: i64,
    pub pmc_id: Option<String>,
    pub doi: Option<String>,
    pub title: String,
    pub abstract_text: String,
    pub journal: String,
    pub pub_year: Option<u16>,
    pub pub_month: Option<u16>,
    pub pub_type: String,
    pub authors: Vec<AuthorData>,
    pub keywords: Vec<String>,
    pub mesh_terms: Vec<MeSHTerm>,
    pub raw_xml_hash: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuthorData {
    pub last_name: String,
    pub initials: String,
    pub affiliation: String,
    pub country: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MeSHTerm {
    pub descriptor: String,
    pub qualifier: String,
    pub is_major: bool,
}
