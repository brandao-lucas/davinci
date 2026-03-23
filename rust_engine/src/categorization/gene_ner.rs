use regex::Regex;
use std::sync::OnceLock;

static GENE_RE: OnceLock<Regex> = OnceLock::new();

pub fn extract_genes(abstract_text: &str) -> Vec<String> {
    let re = GENE_RE.get_or_init(|| Regex::new(r"(?i)\b(BRCA1|TP53|EGFR|TNF|IL6|BRAF)\b").unwrap());
    re.find_iter(abstract_text).map(|m| m.as_str().to_string()).collect()
}
