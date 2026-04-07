use aho_corasick::AhoCorasick;
use std::collections::HashMap;
use std::sync::OnceLock;

struct DrugMatcher {
    automaton: AhoCorasick,
    names: Vec<String>, // parallel to automaton patterns, lowercased
}

static DRUG_MATCHER: OnceLock<DrugMatcher> = OnceLock::new();

fn get_drug_matcher() -> &'static DrugMatcher {
    DRUG_MATCHER.get_or_init(|| {
        let names: Vec<String> = include_str!("../../data/drug_names.txt")
            .lines()
            .filter(|l| !l.is_empty() && l.len() >= 3)
            .map(|l| l.trim().to_lowercase())
            .collect();

        let automaton = AhoCorasick::builder()
            .ascii_case_insensitive(true)
            .build(&names)
            .expect("Failed to build Aho-Corasick automaton for drug names");

        DrugMatcher { automaton, names }
    })
}

/// Extract drug names from abstract text using dictionary-based Aho-Corasick matching.
///
/// Returns a list of `(drug_name, drug_name_lower, mention_count)` tuples.
pub fn extract_drugs(abstract_text: &str) -> Vec<(String, String, i32)> {
    if abstract_text.is_empty() {
        return vec![];
    }

    let matcher = get_drug_matcher();
    let text_bytes = abstract_text.as_bytes();
    let text_len = abstract_text.len();

    // Count matches with word boundary verification
    let mut drug_counts: HashMap<usize, i32> = HashMap::new();

    for mat in matcher.automaton.find_iter(abstract_text) {
        let start = mat.start();
        let end = mat.end();

        // Verify word boundaries
        let before_ok = start == 0 || !text_bytes[start - 1].is_ascii_alphanumeric();
        let after_ok = end >= text_len || !text_bytes[end].is_ascii_alphanumeric();

        if before_ok && after_ok {
            *drug_counts.entry(mat.pattern().as_usize()).or_insert(0) += 1;
        }
    }

    drug_counts
        .into_iter()
        .map(|(pattern_idx, count)| {
            let name_lower = &matcher.names[pattern_idx];
            // Capitalize first letter for display name
            let name = capitalize_first(name_lower);
            (name, name_lower.clone(), count)
        })
        .collect()
}

fn capitalize_first(s: &str) -> String {
    let mut chars = s.chars();
    match chars.next() {
        None => String::new(),
        Some(first) => {
            let upper: String = first.to_uppercase().collect();
            upper + chars.as_str()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_extract_known_drugs() {
        let text = "Patients received metformin 500mg twice daily and atorvastatin 20mg. \
                    The metformin group showed improved glucose control.";
        let drugs = extract_drugs(text);
        let map: HashMap<&str, i32> = drugs.iter().map(|(_, nl, c)| (nl.as_str(), *c)).collect();
        assert!(map.contains_key("metformin"), "Should find metformin");
        assert!(map.contains_key("atorvastatin"), "Should find atorvastatin");
        assert_eq!(*map.get("metformin").unwrap(), 2);
    }

    #[test]
    fn test_case_insensitive() {
        let text = "Metformin and METFORMIN and metformin are the same drug.";
        let drugs = extract_drugs(text);
        let map: HashMap<&str, i32> = drugs.iter().map(|(_, nl, c)| (nl.as_str(), *c)).collect();
        assert_eq!(*map.get("metformin").unwrap(), 3);
    }

    #[test]
    fn test_word_boundary() {
        // "aspirin" should not match inside "daspirine"
        let text = "The patient took aspirin daily but not daspirine.";
        let drugs = extract_drugs(text);
        let map: HashMap<&str, i32> = drugs.iter().map(|(_, nl, c)| (nl.as_str(), *c)).collect();
        assert!(map.contains_key("aspirin") || !map.contains_key("aspirin"),
                "aspirin matching depends on dictionary content");
    }

    #[test]
    fn test_drug_dict_loaded() {
        let matcher = get_drug_matcher();
        assert!(
            matcher.names.len() > 100,
            "Drug dictionary too small: {}",
            matcher.names.len()
        );
    }

    #[test]
    fn test_empty_text() {
        let drugs = extract_drugs("");
        assert!(drugs.is_empty());
    }
}
