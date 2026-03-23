use regex::Regex;
use std::sync::OnceLock;

static DIAGNOSIS_RE: OnceLock<Regex> = OnceLock::new();
static TREATMENT_RE: OnceLock<Regex> = OnceLock::new();
static EPIDEMIO_RE: OnceLock<Regex> = OnceLock::new();
static MECHANISM_RE: OnceLock<Regex> = OnceLock::new();
static SYMPTOMS_RE: OnceLock<Regex> = OnceLock::new();

pub struct ClinicalCategorizer {}

impl ClinicalCategorizer {
    pub fn new() -> Self { Self {} }
    
    pub fn categorize(&self, abstract_text: &str) -> Vec<(String, f64)> {
        let mut results = Vec::new();
        let text = abstract_text.to_lowercase();
        
        let diag = DIAGNOSIS_RE.get_or_init(|| Regex::new(r"(?i)\b(diagnos|biomarker|detect)\b").unwrap());
        if diag.is_match(&text) { results.push(("diagnosis".to_string(), 0.8)); }
        
        let treat = TREATMENT_RE.get_or_init(|| Regex::new(r"(?i)\b(therap|treat|drug)\b").unwrap());
        if treat.is_match(&text) { results.push(("treatment".to_string(), 0.8)); }
        
        let epi = EPIDEMIO_RE.get_or_init(|| Regex::new(r"(?i)\b(prevalence|incidence|epidemiolog)\b").unwrap());
        if epi.is_match(&text) { results.push(("epidemiology".to_string(), 0.9)); }
        
        let mech = MECHANISM_RE.get_or_init(|| Regex::new(r"(?i)\b(pathophysiolog|mechanism|pathway)\b").unwrap());
        if mech.is_match(&text) { results.push(("mechanism".to_string(), 0.8)); }
        
        let symp = SYMPTOMS_RE.get_or_init(|| Regex::new(r"(?i)\b(symptom|sign|manifestation)\b").unwrap());
        if symp.is_match(&text) { results.push(("signs_symptoms".to_string(), 0.7)); }
        
        results
    }
}
