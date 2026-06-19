use regex::Regex;
use std::collections::HashMap;
use std::sync::OnceLock;

/// Compiled regex for rs-number extraction.
///
/// Pattern: word-boundary + case-insensitive "rs" + one-or-more digits + word-boundary.
/// The `(?i)` flag makes "RS123" and "Rs123" match in addition to "rs123".
/// We enforce max length (≤ 20 chars) after extraction to fit `CharField(max_length=20)`.
static RS_REGEX: OnceLock<Regex> = OnceLock::new();

fn get_rs_regex() -> &'static Regex {
    RS_REGEX.get_or_init(|| {
        // \b ensures we do not match "hrs123" or "rs123abc".
        // The digit part uses \d+ (one or more digits).
        // ReDoS-safe: no backtracking, linear in input length.
        Regex::new(r"(?i)\brs\d+\b").expect("variant_ner: failed to compile rs-number regex")
    })
}

/// Extract rs-numbers (dbSNP variant identifiers) from text.
///
/// Scans `text` for tokens matching `rs\d+` (case-insensitive, word-bounded).
/// Normalises each match to lowercase (`rs####`).
/// Discards any match whose normalised form exceeds 20 characters (the
/// `max_length` of `PaperVariant.rs_number`) to prevent truncation errors.
///
/// Returns a list of `(rs_number, mention_count)` tuples, where `rs_number`
/// is lowercase (e.g. `"rs334"`) and `mention_count` is the number of times
/// it appears in `text`.
pub fn extract_variants(text: &str) -> Vec<(String, i32)> {
    if text.is_empty() {
        return vec![];
    }

    let re = get_rs_regex();
    let mut counts: HashMap<String, i32> = HashMap::new();

    for mat in re.find_iter(text) {
        let rs = mat.as_str().to_lowercase();

        // Guard: `PaperVariant.rs_number` is VARCHAR(20); discard longer matches.
        // An rs-number with > 18 digits (rs + 18 d = 20 chars) would be malformed.
        if rs.len() > 20 {
            continue;
        }

        *counts.entry(rs).or_insert(0) += 1;
    }

    counts.into_iter().collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_basic_extraction() {
        let text = "The variant rs334 was associated with sickle cell disease. \
                    rs334 is also known as HbS. rs1805007 affects pigmentation.";
        let variants = extract_variants(text);
        let map: HashMap<&str, i32> = variants.iter().map(|(s, c)| (s.as_str(), *c)).collect();
        assert_eq!(*map.get("rs334").unwrap(), 2, "rs334 should appear twice");
        assert_eq!(*map.get("rs1805007").unwrap(), 1, "rs1805007 should appear once");
    }

    #[test]
    fn test_case_insensitive_normalisation() {
        let text = "RS334 and Rs334 and rs334 are the same variant.";
        let variants = extract_variants(text);
        let map: HashMap<&str, i32> = variants.iter().map(|(s, c)| (s.as_str(), *c)).collect();
        // All three forms must collapse into lowercase "rs334" with count 3.
        assert_eq!(*map.get("rs334").unwrap(), 3, "case-insensitive collapse to rs334");
        assert!(!map.contains_key("RS334"), "uppercase form must not appear");
    }

    #[test]
    fn test_word_boundary_no_false_matches() {
        // "hrs123" and "rs123abc" must NOT match.
        let text = "The compound hrs123 and the token rs123abc are not rs-numbers.";
        let variants = extract_variants(text);
        assert!(
            variants.is_empty(),
            "Expected no matches but got: {:?}",
            variants
        );
    }

    #[test]
    fn test_max_length_guard() {
        // rs + 19 digits = 21 chars → must be discarded.
        let text = "The SNP rs1234567890123456789 was reported.";
        let variants = extract_variants(text);
        assert!(
            variants.is_empty(),
            "rs-number > 20 chars should be discarded: {:?}",
            variants
        );
    }

    #[test]
    fn test_empty_text() {
        assert!(extract_variants("").is_empty());
    }

    #[test]
    fn test_mixed_text() {
        let text = "BRCA1 mutation rs80357906 and TP53 variant rs28934578 both increase risk. \
                    rs80357906 was confirmed in 200 patients.";
        let variants = extract_variants(text);
        let map: HashMap<&str, i32> = variants.iter().map(|(s, c)| (s.as_str(), *c)).collect();
        assert_eq!(*map.get("rs80357906").unwrap(), 2);
        assert_eq!(*map.get("rs28934578").unwrap(), 1);
    }
}
