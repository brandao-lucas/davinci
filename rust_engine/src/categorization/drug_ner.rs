use regex::Regex;
use std::sync::OnceLock;

static DRUG_RE: OnceLock<Regex> = OnceLock::new();

pub fn extract_drugs(abstract_text: &str) -> Vec<String> {
    let re = DRUG_RE.get_or_init(|| Regex::new(r"(?i)\b(aspirin|ibuprofen|metformin|adalimumab|pembrolizumab|paclitaxel)\b").unwrap());
    re.find_iter(abstract_text).map(|m| m.as_str().to_string()).collect()
}
