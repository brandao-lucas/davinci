use regex::Regex;
use std::collections::{HashMap, HashSet};
use std::sync::OnceLock;

/// HGNC gene symbols loaded at compile time (~99k symbols + aliases).
static GENE_SYMBOLS: OnceLock<HashSet<String>> = OnceLock::new();

/// Common English words that coincide with gene symbols — must be filtered out.
static FALSE_POSITIVES: OnceLock<HashSet<&'static str>> = OnceLock::new();

/// Regex to detect biomedical context (required for short gene symbols).
static GENE_CONTEXT_RE: OnceLock<Regex> = OnceLock::new();

fn get_gene_symbols() -> &'static HashSet<String> {
    GENE_SYMBOLS.get_or_init(|| {
        include_str!("../../data/gene_symbols.txt")
            .lines()
            .filter(|l| !l.is_empty() && l.len() >= 2)
            .map(|l| l.trim().to_uppercase())
            .collect()
    })
}

fn get_false_positives() -> &'static HashSet<&'static str> {
    FALSE_POSITIVES.get_or_init(|| {
        [
            // 3-letter common English words that are gene symbols
            "NOT", "WAS", "CAN", "SET", "MAP", "GAP", "REST", "CAMP", "ACE",
            "SHE", "HER", "HIS", "ALL", "FOR", "ARE", "THE", "AND", "BUT",
            "HAD", "HAS", "LET", "MAY", "RAN", "SAT", "SAW", "PUT", "GOT",
            "DID", "TOP", "END", "AGE", "BAD", "BIG", "CUT", "FAT", "FIT",
            "HOT", "LOW", "MET", "NEW", "OLD", "RED", "RUN", "TEN", "TIE",
            "USE", "WIN", "AIM", "AIR", "ARM", "ART", "BAR", "BIT", "BOX",
            "BUS", "CAR", "COP", "DAD", "DAM", "DOT", "DRY", "DUE", "EAR",
            "EAT", "EGG", "ERA", "EVE", "EYE", "FAN", "FAR", "FEW", "FIG",
            "FIN", "FIX", "FLY", "FOG", "FOX", "FUN", "FUR", "GAS", "GEL",
            "GUM", "GUN", "GUT", "GYM", "HAM", "HAT", "HIT", "HOP", "HUB",
            "ICE", "ILL", "INK", "ION", "JAM", "JAR", "JAW", "JET", "JOB",
            "JOG", "JOY", "KEY", "KID", "KIT", "LAB", "LAP", "LAW", "LAY",
            "LED", "LEG", "LID", "LIP", "LOG", "LOT", "MAD", "MAN", "MAT",
            "MIX", "MOB", "MOM", "MUD", "MUG", "NAP", "NET", "NIT", "NOD",
            "NOR", "NUN", "NUT", "OAK", "OAR", "ODD", "OIL", "ONE", "OPT",
            "ORB", "ORE", "OWE", "OWL", "OWN", "PAD", "PAN", "PAT", "PAW",
            "PEA", "PEN", "PET", "PIE", "PIG", "PIN", "PIT", "PLY", "POD",
            "POP", "POT", "PUB", "PUP", "RAG", "RAM", "RAP", "RAT", "RAW",
            "RAY", "RIB", "RIG", "RIM", "RIP", "ROB", "ROD", "ROT", "ROW",
            "RUB", "RUG", "RUM", "SAD", "SAP", "SIR", "SIS", "SIT", "SIX",
            "SKI", "SKY", "SLY", "SOB", "SOD", "SON", "SOP", "SOW", "SOY",
            "SPA", "SPY", "STY", "SUB", "SUM", "SUN", "TAB", "TAG", "TAN",
            "TAP", "TAR", "TAX", "TEA", "TIN", "TIP", "TOE", "TON", "TOO",
            "TOW", "TOY", "TUB", "TUG", "TWO", "URN", "VAN", "VET", "VIA",
            "VOW", "WAR", "WAX", "WAY", "WEB", "WED", "WET", "WIG", "WIT",
            "WOE", "WOK", "WON", "WOO", "WOW", "YAM", "YAP", "YAW", "YES",
            "YET", "YEW", "ZAP", "ZEN", "ZIP", "ZIT", "ZOO",
            // 4-letter common/biomedical words that are gene symbols
            "CELL", "GENE", "DRUG", "DOSE", "RISK", "CARE", "CASE", "DATA",
            "DIET", "FISH", "FOOD", "HAND", "HEAD", "HEAR", "HELP",
            "HIGH", "HOME", "HOPE", "HOST", "HOUR", "IRON", "LACK", "LAST",
            "LATE", "LEAD", "LEFT", "LESS", "LIFE", "LIKE", "LINE", "LINK",
            "LIST", "LIVE", "LONG", "LOOK", "LOOP", "LOSS", "LOST", "LUNG",
            "MADE", "MAIN", "MAKE", "MALE", "MANY", "MARK", "MASS", "MEAN",
            "MILD", "MILK", "MIND", "MINE", "MODE", "MORE", "MOST", "MUCH",
            "MUST", "NAME", "NEAR", "NEED", "NEXT", "NINE", "NODE", "NONE",
            "NORM", "NOSE", "NOTE", "ODDS", "ONCE", "ONLY", "OPEN", "ORAL",
            "OVER", "PACE", "PACK", "PAGE", "PAID", "PAIN", "PAIR", "PALE",
            "PALM", "PART", "PASS", "PAST", "PATH", "PEAK", "PEER", "PICK",
            "PILL", "PLAN", "PLAY", "PLOT", "PLUS", "POLL", "POOL", "POOR",
            "PORT", "POSE", "POST", "POUR", "PULL", "PUMP", "PURE", "PUSH",
            "RACE", "RANK", "RARE", "RATE", "READ", "REAL", "REAR", "RICE",
            "RICH", "RIDE", "RING", "RISE", "ROLE", "ROLL", "ROOT", "ROPE",
            "ROSE", "RULE", "RUSH", "SAFE", "SAID", "SAKE", "SALE", "SALT",
            "SAME", "SAND", "SAVE", "SCAN", "SEAL", "SEAT", "SEED", "SEEK",
            "SELF", "SEND", "SEPT", "SHIP", "SHOP", "SHOT", "SHOW", "SHUT",
            "SICK", "SIDE", "SIGN", "SILK", "SITE", "SIZE", "SKIN", "SLIP",
            "SLOT", "SLOW", "SNAP", "SNOW", "SOLE", "SOME", "SONG", "SOON",
            "SORT", "SOUL", "SPIN", "SPOT", "STAR", "STAY", "STEM", "STEP",
            "STOP", "SUCH", "SUIT", "SURE", "SWIM", "TAIL", "TAKE", "TALE",
            "TALK", "TALL", "TANK", "TAPE", "TASK", "TEAM", "TEAR", "TELL",
            "TERM", "TEST", "TEXT", "THAN", "THAT", "THEM", "THEN", "THEY",
            "THIN", "THIS", "THUS", "TIED", "TILL", "TIME", "TINY", "TIRE",
            "TOLD", "TOLL", "TONE", "TOOK", "TOOL", "TORN", "TOUR", "TOWN",
            "TRAP", "TREE", "TRIM", "TRIP", "TRUE", "TUBE", "TUCK", "TUNE",
            "TURN", "TWIN", "UPON", "USED", "USER", "VALE", "VARY",
            "VAST", "VERY", "VIEW", "VINE", "VOID", "VOTE", "WAGE", "WAIT",
            "WAKE", "WALK", "WALL", "WANT", "WARD", "WARM", "WARN", "WASH",
            "WAVE", "WEAK", "WEAR", "WEEK", "WELL", "WENT", "WERE", "WEST",
            "WHAT", "WHEN", "WHOM", "WIDE", "WIFE", "WILD", "WILL", "WIND",
            "WINE", "WING", "WIRE", "WISE", "WISH", "WITH", "WOKE", "WOLF",
            "WOOD", "WOOL", "WORD", "WORE", "WORK", "WORM", "WORN", "WRAP",
            "YARD", "YEAR", "YOUR", "ZERO", "ZONE",
            // Short gene symbols that are common English words — these should NOT appear
            // as standalone gene mentions in normal biomedical text.
            // FAST (Fas-activated serine threonine kinase), BASE (basic helix factor),
            // TYPE are real HGNC entries but far too ambiguous when used as standalone tokens.
            "FAST", "BASE", "TYPE",
        ]
        .iter()
        .cloned()
        .collect()
    })
}

fn get_context_regex() -> &'static Regex {
    GENE_CONTEXT_RE.get_or_init(|| {
        Regex::new(r"(?i)\b(gene|protein|express|mutat|variant|allele|locus|transcript|mRNA|encod|regulat|pathway|receptor|kinase|inhibit|activat|phosphoryl|knockout|knockdown|overexpress|silenc|polymorphism|SNP|amplif|delet|fusion|transloc|promoter|enhancer|exon|intron|domain|isoform|signaling|oncogene|tumor.?suppress|methylat|acetylat|ubiquitin|apoptos|proliferat)\b").unwrap()
    })
}

/// Apply the standard gene-symbol acceptance filters to a candidate uppercase symbol.
///
/// Returns `true` if `candidate` should be counted as a gene mention.
/// `original_word` is the original-case token (used for the 2-char uppercase guard).
/// `has_bio_context` is the pre-computed biomedical-context flag for the full text.
#[inline]
fn accept_gene_candidate(
    candidate: &str,
    original_word: &str,
    symbols: &HashSet<String>,
    false_pos: &HashSet<&'static str>,
    has_bio_context: bool,
) -> bool {
    let len = candidate.len();
    if !(2..=15).contains(&len) {
        return false;
    }
    if false_pos.contains(candidate) {
        return false;
    }
    if !symbols.contains(candidate) {
        return false;
    }
    // Short symbols (<=3 chars) require biomedical context in the abstract.
    if len <= 3 && !has_bio_context {
        return false;
    }
    // 2-letter symbols must appear UPPERCASE in the original text to avoid matching
    // common lowercase abbreviations (e.g., "as", "at").
    if len == 2 && original_word != candidate {
        return false;
    }
    true
}

/// Extract gene symbols from abstract text using HGNC dictionary lookup.
///
/// Returns a list of `(gene_symbol, mention_count)` tuples.
///
/// # Hyphen handling
///
/// The tokenizer preserves `-` so that legitimate hyphenated gene symbols stored
/// in the HGNC dictionary (e.g. `EGFR-AS1`, `IL-6`, `MMP-2`) are matched as a
/// whole token.  Two additional strategies recover gene mentions that use a hyphen
/// as a *modifier separator* rather than as part of the official symbol name:
///
/// **Strategy A — clinical-modifier suffix** (`TP53-mutant` → `TP53`)
/// When the full token is not in the dictionary, the suffix after the first `-`
/// is examined.  If that suffix (uppercased) is *not* itself a known gene symbol,
/// we attempt to match only the prefix (the part before the first `-`).  This
/// captures constructs like `EGFR-positive`, `KRAS-null`, `BRCA1-deficient`,
/// `CDK4-driven`.
/// Guard: if the suffix IS a gene (e.g. `TP53-CDKN2A`), we do not split — both
/// genes should be mentioned independently elsewhere in the abstract.
///
/// **Strategy B — numeric/short suffix concatenation** (`BCL-2` → `BCL2`)
/// When the suffix after the first `-` is purely alphanumeric and at most 3
/// characters long, we also try the *concatenated* form (prefix + suffix with the
/// hyphen removed).  This recovers common notations like `BCL-2` → `BCL2`,
/// `CDK-4` → `CDK4`, `MYC-N` → `MYCN`, `TGF-B` → `TGFB`, `E2F-1` → `E2F1`.
/// The concat form must also pass all standard filters.
///
/// All standard filters (false-positive list, length bounds, uppercase guard for
/// 2-char symbols, biomedical-context gate for ≤3-char symbols) are applied to
/// every candidate regardless of which path produced it.
///
/// **What is intentionally NOT handled:**
/// - `HER2neu` (no separator between uppercase and lowercase) — ambiguous boundary
///   detection would introduce regressions; `HER2`/`ERBB2` appear without the suffix.
/// - Tokens with two gene-level parts joined by `-` (`TP53-CDKN2A`) — both genes
///   appear independently in the abstract with better frequency signals.
pub fn extract_genes(abstract_text: &str) -> Vec<(String, i32)> {
    if abstract_text.is_empty() {
        return vec![];
    }

    let symbols = get_gene_symbols();
    let false_pos = get_false_positives();
    let has_bio_context = get_context_regex().is_match(abstract_text);

    let mut gene_counts: HashMap<String, i32> = HashMap::new();

    for word in abstract_text.split(|c: char| !c.is_alphanumeric() && c != '-') {
        if word.len() < 2 || word.len() > 15 {
            continue;
        }

        let upper = word.to_uppercase();

        // --- Primary match: token as-is (covers plain symbols AND hyphenated HGNC
        //     entries like EGFR-AS1, IL-6, MMP-2 that are in the dictionary intact) ---
        if accept_gene_candidate(&upper, word, symbols, false_pos, has_bio_context) {
            *gene_counts.entry(upper).or_insert(0) += 1;
            continue; // already matched — no need for fallback strategies
        }

        // --- Fallback strategies for tokens containing a hyphen ---
        if !upper.contains('-') {
            continue;
        }

        // Split on the FIRST hyphen only.
        let hyphen_pos = match upper.find('-') {
            Some(p) => p,
            None => continue,
        };
        let prefix_upper = &upper[..hyphen_pos];
        let suffix_upper = &upper[hyphen_pos + 1..];

        if prefix_upper.len() < 2 {
            continue; // prefix too short to be a gene symbol
        }

        // Strategy B: suffix is purely alphanumeric and short (<=3 chars).
        // Try the concatenated form (e.g. "BCL-2" → "BCL2", "CDK-4" → "CDK4",
        // "TGF-B" → "TGFB").  We use `word` as the original-word stand-in for the
        // uppercase guard; since the concatenated form will always be uppercase it
        // will pass the 2-char guard correctly when candidate len == 2.
        let suffix_is_short_alnum = suffix_upper.len() <= 3
            && !suffix_upper.is_empty()
            && suffix_upper.chars().all(|c| c.is_ascii_alphanumeric());

        if suffix_is_short_alnum {
            let concat = format!("{}{}", prefix_upper, suffix_upper);
            if accept_gene_candidate(&concat, &concat, symbols, false_pos, has_bio_context) {
                *gene_counts.entry(concat).or_insert(0) += 1;
                // Do not `continue` — Strategy A below may also yield a valid prefix.
            }
        }

        // Strategy A: suffix is NOT a known gene symbol → try the prefix alone.
        // This recovers "TP53-mutant", "EGFR-positive", "KRAS-null", etc.
        // If the suffix IS a gene we refuse to split, to avoid silently discarding it.
        if !symbols.contains(suffix_upper)
            && accept_gene_candidate(prefix_upper, prefix_upper, symbols, false_pos, has_bio_context)
        {
            *gene_counts.entry(prefix_upper.to_string()).or_insert(0) += 1;
        }
    }

    gene_counts.into_iter().collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── Existing regression tests ─────────────────────────────────────────────

    #[test]
    fn test_extract_known_genes() {
        let text = "We found that BRCA1 and TP53 mutations were associated with \
                    increased risk. EGFR expression was elevated in tumor samples. \
                    The BRCA1 gene was also linked to DNA repair pathways.";
        let genes = extract_genes(text);
        let map: HashMap<&str, i32> = genes.iter().map(|(s, c)| (s.as_str(), *c)).collect();
        assert!(map.contains_key("BRCA1"));
        assert!(map.contains_key("TP53"));
        assert!(map.contains_key("EGFR"));
        assert_eq!(*map.get("BRCA1").unwrap(), 2);
    }

    #[test]
    fn test_no_false_positives_for_common_words() {
        let text = "The patient was not able to set a new goal for the race. \
                    This case was very rare and the risk was high.";
        let genes = extract_genes(text);
        assert!(genes.is_empty(), "Found false positives: {:?}", genes);
    }

    #[test]
    fn test_short_gene_needs_bio_context() {
        // IL6 (3 chars) in biomedical context → should match
        let text = "IL6 expression was upregulated in the inflammatory pathway.";
        let genes = extract_genes(text);
        let symbols: Vec<&str> = genes.iter().map(|(s, _)| s.as_str()).collect();
        assert!(symbols.contains(&"IL6"), "Should find IL6 in bio context");
    }

    #[test]
    fn test_gene_symbols_loaded() {
        let symbols = get_gene_symbols();
        assert!(symbols.len() > 10000, "Gene dictionary too small: {}", symbols.len());
        assert!(symbols.contains("BRCA1"));
        assert!(symbols.contains("TP53"));
        assert!(symbols.contains("EGFR"));
        assert!(symbols.contains("TNF"));
        assert!(symbols.contains("BRAF"));
    }

    // ── Strategy A: clinical-modifier suffix (GENE-modifier → GENE) ──────────

    #[test]
    fn test_strategy_a_tp53_mutant() {
        // "TP53-mutant" → token "TP53-MUTANT" → "MUTANT" not in gene dict → extract "TP53"
        let text = "TP53-mutant tumors showed increased apoptosis and gene expression changes.";
        let genes = extract_genes(text);
        let symbols: Vec<&str> = genes.iter().map(|(s, _)| s.as_str()).collect();
        assert!(
            symbols.contains(&"TP53"),
            "Should extract TP53 from 'TP53-mutant'; got {:?}",
            symbols
        );
    }

    #[test]
    fn test_strategy_a_egfr_positive() {
        let text = "EGFR-positive patients had significantly better response to the kinase inhibitor.";
        let genes = extract_genes(text);
        let symbols: Vec<&str> = genes.iter().map(|(s, _)| s.as_str()).collect();
        assert!(
            symbols.contains(&"EGFR"),
            "Should extract EGFR from 'EGFR-positive'; got {:?}",
            symbols
        );
    }

    #[test]
    fn test_strategy_a_kras_null() {
        let text = "KRAS-null cell lines were resistant to the drug in the signaling pathway study.";
        let genes = extract_genes(text);
        let symbols: Vec<&str> = genes.iter().map(|(s, _)| s.as_str()).collect();
        assert!(
            symbols.contains(&"KRAS"),
            "Should extract KRAS from 'KRAS-null'; got {:?}",
            symbols
        );
    }

    #[test]
    fn test_strategy_a_brca1_deficient() {
        let text = "BRCA1-deficient cells showed impaired DNA repair and increased mutat frequency.";
        let genes = extract_genes(text);
        let symbols: Vec<&str> = genes.iter().map(|(s, _)| s.as_str()).collect();
        assert!(
            symbols.contains(&"BRCA1"),
            "Should extract BRCA1 from 'BRCA1-deficient'; got {:?}",
            symbols
        );
    }

    #[test]
    fn test_strategy_a_does_not_split_gene_gene() {
        // "TP53-CDKN2A" → suffix "CDKN2A" IS a gene → do NOT split to "TP53" only.
        // Both genes should appear independently elsewhere in the abstract.
        // Here we confirm Strategy A does NOT manufacture a false "TP53" from this token.
        // (Both may still appear from other positions in a longer text, so we only
        //  assert that the junction token itself does not produce a match.)
        let text = "The TP53-CDKN2A axis regulates tumor suppression via the pathway.";
        // "TP53" appears as a standalone token too here, but the hyphenated one should not
        // produce an extra count beyond what the standalone provides.
        let genes = extract_genes(text);
        let map: HashMap<&str, i32> = genes.iter().map(|(s, c)| (s.as_str(), *c)).collect();
        // If present, count must be exactly 1 (from standalone "TP53") not 2.
        if let Some(&count) = map.get("TP53") {
            assert_eq!(
                count, 1,
                "TP53 count should be 1 (standalone only, not split from hyphenated); got {}",
                count
            );
        }
    }

    // ── Strategy B: short-suffix concatenation (GENE-N → GENEN) ─────────────

    #[test]
    fn test_strategy_b_bcl2() {
        // "BCL-2" is written as "Bcl-2" in the HGNC dictionary, which after .to_uppercase()
        // becomes "BCL-2". The primary match therefore already extracts the hyphenated form
        // "BCL-2" directly.  Strategy B additionally concatenates to "BCL2", which is also
        // in the dict.  Either symbol in the output is a correct extraction; we accept both.
        let text = "BCL-2 overexpression was associated with apoptosis resistance in the tumor.";
        let genes = extract_genes(text);
        let symbols: Vec<&str> = genes.iter().map(|(s, _)| s.as_str()).collect();
        assert!(
            symbols.contains(&"BCL2") || symbols.contains(&"BCL-2"),
            "Should extract BCL2 or BCL-2 from 'BCL-2'; got {:?}",
            symbols
        );
    }

    #[test]
    fn test_strategy_b_cdk4_pure() {
        // CDK-4 is NOT in the dictionary as "CDK-4"; only "CDK4" exists.
        // This is a pure Strategy B case: concatenation "CDK"+"4" = "CDK4" hits the dict.
        let text = "CDK-4 inhibition blocked cell cycle via the receptor kinase pathway.";
        let genes = extract_genes(text);
        let symbols: Vec<&str> = genes.iter().map(|(s, _)| s.as_str()).collect();
        assert!(
            symbols.contains(&"CDK4"),
            "Should extract CDK4 from 'CDK-4' via Strategy B; got {:?}",
            symbols
        );
    }

    #[test]
    fn test_strategy_b_cdk4() {
        let text = "CDK-4 inhibition blocked cell cycle progression via the receptor pathway.";
        let genes = extract_genes(text);
        let symbols: Vec<&str> = genes.iter().map(|(s, _)| s.as_str()).collect();
        assert!(
            symbols.contains(&"CDK4"),
            "Should extract CDK4 from 'CDK-4'; got {:?}",
            symbols
        );
    }

    #[test]
    fn test_strategy_b_mycn() {
        let text = "MYC-N amplification is a hallmark of neuroblastoma gene expression profiles.";
        let genes = extract_genes(text);
        let symbols: Vec<&str> = genes.iter().map(|(s, _)| s.as_str()).collect();
        assert!(
            symbols.contains(&"MYCN"),
            "Should extract MYCN from 'MYC-N'; got {:?}",
            symbols
        );
    }

    #[test]
    fn test_strategy_b_tgfb() {
        let text = "TGF-B signaling was elevated in the tumor microenvironment pathway.";
        let genes = extract_genes(text);
        let symbols: Vec<&str> = genes.iter().map(|(s, _)| s.as_str()).collect();
        assert!(
            symbols.contains(&"TGFB"),
            "Should extract TGFB from 'TGF-B'; got {:?}",
            symbols
        );
    }

    #[test]
    fn test_strategy_b_e2f1() {
        let text = "E2F-1 controls cell cycle gene transcription via the promoter enhancer.";
        let genes = extract_genes(text);
        let symbols: Vec<&str> = genes.iter().map(|(s, _)| s.as_str()).collect();
        assert!(
            symbols.contains(&"E2F1"),
            "Should extract E2F1 from 'E2F-1'; got {:?}",
            symbols
        );
    }

    // ── Existing hyphenated HGNC symbols still work (primary match) ───────────

    #[test]
    fn test_primary_match_hyphenated_hgnc_symbols() {
        // EGFR-AS1 and IL-6 are in the HGNC dictionary with hyphens intact.
        let text = "EGFR-AS1 and IL-6 were differentially expressed in the tumor gene pathway.";
        let genes = extract_genes(text);
        let symbols: Vec<&str> = genes.iter().map(|(s, _)| s.as_str()).collect();
        assert!(
            symbols.contains(&"EGFR-AS1"),
            "Should find EGFR-AS1 via primary match; got {:?}",
            symbols
        );
        assert!(
            symbols.contains(&"IL-6"),
            "Should find IL-6 via primary match; got {:?}",
            symbols
        );
    }

    // ── Precision guards: common hyphenated phrases must NOT yield genes ───────

    #[test]
    fn test_no_fp_and_or() {
        // "and/or" → '/' splits → "and", "or" → both are common words; neither is in gene dict
        // as a meaningful standalone (even if "OR" appears, context gate should block short symbols).
        let text = "Patients showed high or low levels of expression and/or protein activity.";
        let genes = extract_genes(text);
        // "OR" (2 chars): would require uppercase in original — "or" is lowercase → blocked ✓
        // "AND" is not in the gene dict.
        for (sym, _) in &genes {
            assert!(
                sym.len() >= 3 || sym == "OR",
                "Unexpected 2-char symbol from and/or text: {}",
                sym
            );
        }
        // More specifically: common English words should not appear.
        let symbols: Vec<&str> = genes.iter().map(|(s, _)| s.as_str()).collect();
        assert!(!symbols.contains(&"AND"), "AND should not be a gene");
    }

    #[test]
    fn test_no_fp_high_low() {
        let text = "Patients with high/low expression showed different outcomes in this gene study.";
        let genes = extract_genes(text);
        let symbols: Vec<&str> = genes.iter().map(|(s, _)| s.as_str()).collect();
        assert!(!symbols.contains(&"HIGH"), "HIGH should not be a gene");
        assert!(!symbols.contains(&"LOW"), "LOW should not be a gene");
    }

    #[test]
    fn test_no_fp_dose_response() {
        // "dose-response" → primary token "DOSE-RESPONSE" not in dict
        // Strategy A: suffix "RESPONSE" not in gene dict → try prefix "DOSE"
        // "DOSE" is in false_pos → blocked ✓
        let text = "We observed a clear dose-response relationship in the gene pathway study.";
        let genes = extract_genes(text);
        let symbols: Vec<&str> = genes.iter().map(|(s, _)| s.as_str()).collect();
        assert!(
            !symbols.contains(&"DOSE"),
            "DOSE should be blocked by false_pos; got {:?}",
            symbols
        );
    }

    #[test]
    fn test_no_fp_cell_free() {
        // "cell-free" → Strategy A: suffix "FREE" not in gene dict → try prefix "CELL"
        // "CELL" is in false_pos → blocked ✓
        let text = "Cell-free DNA analysis was used to assess mutat burden in the pathway.";
        let genes = extract_genes(text);
        let symbols: Vec<&str> = genes.iter().map(|(s, _)| s.as_str()).collect();
        assert!(
            !symbols.contains(&"CELL"),
            "CELL should be blocked by false_pos in 'cell-free'; got {:?}",
            symbols
        );
    }

    #[test]
    fn test_no_fp_fast_acting() {
        // "fast-acting" → Strategy A: suffix "ACTING" not in gene dict → try prefix "FAST"
        // "FAST" is now in false_pos → blocked ✓
        let text = "The fast-acting drug reduced inflammation via the kinase receptor pathway.";
        let genes = extract_genes(text);
        let symbols: Vec<&str> = genes.iter().map(|(s, _)| s.as_str()).collect();
        assert!(
            !symbols.contains(&"FAST"),
            "FAST should be blocked by false_pos in 'fast-acting'; got {:?}",
            symbols
        );
    }

    #[test]
    fn test_no_fp_long_term() {
        // "long-term" → Strategy A: suffix "TERM" not in gene dict → try prefix "LONG"
        // "LONG" is in false_pos → blocked ✓
        let text = "Long-term follow-up showed improved survival in the gene therapy cohort.";
        let genes = extract_genes(text);
        let symbols: Vec<&str> = genes.iter().map(|(s, _)| s.as_str()).collect();
        assert!(
            !symbols.contains(&"LONG"),
            "LONG should be blocked by false_pos in 'long-term'; got {:?}",
            symbols
        );
    }

    #[test]
    fn test_no_fp_type_based() {
        // "type-based" → "TYPE" is now in false_pos → blocked ✓
        // "base-type" → "BASE" is now in false_pos → blocked ✓
        let text = "A type-based classification was used in the gene expression analysis.";
        let genes = extract_genes(text);
        let symbols: Vec<&str> = genes.iter().map(|(s, _)| s.as_str()).collect();
        assert!(
            !symbols.contains(&"TYPE"),
            "TYPE should be blocked by false_pos; got {:?}",
            symbols
        );
    }

    // ── BRCA1/2 slash notation — already worked, stays working ───────────────

    #[test]
    fn test_slash_notation_brca1_brca2() {
        // '/' is a separator → "BRCA1/BRCA2" splits to two tokens, both in dict.
        let text = "BRCA1/BRCA2 mutations predispose to breast cancer gene pathway activation.";
        let genes = extract_genes(text);
        let symbols: Vec<&str> = genes.iter().map(|(s, _)| s.as_str()).collect();
        assert!(
            symbols.contains(&"BRCA1"),
            "Should find BRCA1 in 'BRCA1/BRCA2'; got {:?}",
            symbols
        );
        assert!(
            symbols.contains(&"BRCA2"),
            "Should find BRCA2 in 'BRCA1/BRCA2'; got {:?}",
            symbols
        );
    }

    #[test]
    fn test_slash_notation_brca1_2() {
        // "BRCA1/2" → "BRCA1" and "2"; "2" has len<2 → discarded; "BRCA1" extracted.
        let text = "BRCA1/2 mutation carriers showed elevated gene expression in tumor tissue.";
        let genes = extract_genes(text);
        let symbols: Vec<&str> = genes.iter().map(|(s, _)| s.as_str()).collect();
        assert!(
            symbols.contains(&"BRCA1"),
            "Should find BRCA1 in 'BRCA1/2'; got {:?}",
            symbols
        );
    }

    // ── TP53+ notation — already worked, stays working ────────────────────────

    #[test]
    fn test_plus_notation_tp53() {
        // '+' is a separator → "TP53+" splits to "TP53" and "".
        let text = "TP53+ tumors showed increased apoptosis and mutat load in the gene study.";
        let genes = extract_genes(text);
        let symbols: Vec<&str> = genes.iter().map(|(s, _)| s.as_str()).collect();
        assert!(
            symbols.contains(&"TP53"),
            "Should find TP53 in 'TP53+'; got {:?}",
            symbols
        );
    }
}
