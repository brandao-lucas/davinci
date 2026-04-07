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
            "DIET", "FAST", "FISH", "FOOD", "HAND", "HEAD", "HEAR", "HELP",
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
            "TURN", "TWIN", "TYPE", "UPON", "USED", "USER", "VALE", "VARY",
            "VAST", "VERY", "VIEW", "VINE", "VOID", "VOTE", "WAGE", "WAIT",
            "WAKE", "WALK", "WALL", "WANT", "WARD", "WARM", "WARN", "WASH",
            "WAVE", "WEAK", "WEAR", "WEEK", "WELL", "WENT", "WERE", "WEST",
            "WHAT", "WHEN", "WHOM", "WIDE", "WIFE", "WILD", "WILL", "WIND",
            "WINE", "WING", "WIRE", "WISE", "WISH", "WITH", "WOKE", "WOLF",
            "WOOD", "WOOL", "WORD", "WORE", "WORK", "WORM", "WORN", "WRAP",
            "YARD", "YEAR", "YOUR", "ZERO", "ZONE",
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

/// Extract gene symbols from abstract text using HGNC dictionary lookup.
///
/// Returns a list of `(gene_symbol, mention_count)` tuples.
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

        // Filter false positives
        if false_pos.contains(upper.as_str()) {
            continue;
        }

        // Check if it's a known gene symbol
        if symbols.contains(&upper) {
            // For symbols <= 3 chars, require biomedical context
            if upper.len() <= 3 && !has_bio_context {
                continue;
            }

            // For 2-letter symbols, require UPPERCASE in original text
            if upper.len() == 2 && word != upper {
                continue;
            }

            *gene_counts.entry(upper).or_insert(0) += 1;
        }
    }

    gene_counts.into_iter().collect()
}

#[cfg(test)]
mod tests {
    use super::*;

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
}
