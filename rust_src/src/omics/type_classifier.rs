use regex::Regex;
use std::sync::OnceLock;

/// Classification result with omic_type and omic_subcategory strings
/// matching OmicDataset::OmicType choices.
pub struct OmicClassification {
    pub omic_type: &'static str,
    pub omic_subcategory: &'static str,
}

struct CompiledRule {
    omic_type: &'static str,
    omic_subcategory: &'static str,
    regex: Regex,
}

/// Rules in priority order — most specific first to avoid 'genomic'
/// swallowing 'epigenomic', etc. Compiled once on first call via OnceLock.
static COMPILED_RULES: OnceLock<Vec<CompiledRule>> = OnceLock::new();

fn get_rules() -> &'static [CompiledRule] {
    COMPILED_RULES.get_or_init(|| {
        vec![
            CompiledRule {
                omic_type: "epigenomic",
                omic_subcategory: "ChIP-Seq/ATAC",
                regex: Regex::new(r"(?i)chip.?seq|histone|atac.?seq|bisulfite|methylat|dnase.?seq|faire.?seq").unwrap(),
            },
            CompiledRule {
                omic_type: "microbiome",
                omic_subcategory: "16S rRNA",
                regex: Regex::new(r"(?i)\b16s\b|microbiom|amplicon.?seq|gut.?flora|oral.?microb|skin.?microb").unwrap(),
            },
            CompiledRule {
                omic_type: "metagenomic",
                omic_subcategory: "Metagenomic",
                regex: Regex::new(r"(?i)metagenom|shotgun.*microb|whole.?metagenom").unwrap(),
            },
            CompiledRule {
                omic_type: "metabolomic",
                omic_subcategory: "Metabolomics",
                regex: Regex::new(r"(?i)metabolom|metabolite|nmr.?spectro|lc.?ms.*metabol|gc.?ms.*metabol").unwrap(),
            },
            CompiledRule {
                omic_type: "proteomic",
                omic_subcategory: "Proteomics",
                regex: Regex::new(r"(?i)proteom|mass.?spec.*protein|2d.?gel|proteogenom|phosphoproteom").unwrap(),
            },
            CompiledRule {
                omic_type: "transcriptomic",
                omic_subcategory: "RNA-Seq",
                regex: Regex::new(r"(?i)rna.?seq|transcriptom|mrna.?express|gene.?express|microarray|affymetrix|illumina.*rna|scrna|single.?cell.*rna").unwrap(),
            },
            CompiledRule {
                omic_type: "genomic",
                omic_subcategory: "WGS/SNP",
                regex: Regex::new(r"(?i)whole.?genome|wgs|snp.?array|gwas|variant.?call|exome|wes|\bsnp\b|genome.?wide").unwrap(),
            },
            CompiledRule {
                omic_type: "multi_omic",
                omic_subcategory: "Multi-omic",
                regex: Regex::new(r"(?i)multi.?om|integrat.*omic|multi.?modal.*omic").unwrap(),
            },
        ]
    })
}

/// Classify an omics dataset by scanning its title and summary.
///
/// Returns all matching rules' (omic_type, omic_subcategory).
/// Falls back to [("other", "")] if no rule matches.
///
/// # Arguments
/// * `title` - Dataset title
/// * `summary` - Dataset summary/description (may include library_strategy for SRA)
pub fn classify_omic_type(title: &str, summary: &str) -> Vec<OmicClassification> {
    let combined = format!("{} {}", title, summary);
    let mut matches = Vec::new();

    for rule in get_rules() {
        if rule.regex.is_match(&combined) {
            matches.push(OmicClassification {
                omic_type: rule.omic_type,
                omic_subcategory: rule.omic_subcategory,
            });
        }
    }

    if matches.is_empty() {
        matches.push(OmicClassification {
            omic_type: "other",
            omic_subcategory: "",
        });
    }

    matches
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_rna_seq_classified_as_transcriptomic() {
        let matches = classify_omic_type("RNA-Seq of human heart tissue", "gene expression profiling");
        assert_eq!(matches[0].omic_type, "transcriptomic");
    }

    #[test]
    fn test_chip_seq_classified_as_epigenomic() {
        let matches = classify_omic_type("ChIP-seq of H3K27ac marks", "histone modification study");
        assert_eq!(matches[0].omic_type, "epigenomic");
    }

    #[test]
    fn test_microbiome_classified_correctly() {
        let matches = classify_omic_type("16S rRNA amplicon sequencing", "gut microbiome diversity");
        assert_eq!(matches[0].omic_type, "microbiome");
    }

    #[test]
    fn test_gwas_classified_as_genomic() {
        let matches = classify_omic_type("Genome-wide association study of BMI", "GWAS SNP array");
        assert_eq!(matches[0].omic_type, "genomic");
    }

    #[test]
    fn test_fallback_to_other() {
        let matches = classify_omic_type("Unknown dataset", "no keywords here");
        assert_eq!(matches[0].omic_type, "other");
    }

    #[test]
    fn test_epigenomic_wins_over_genomic() {
        // 'methylation' is epigenomic; 'genome' should NOT override to 'genomic'
        let matches = classify_omic_type("Whole genome bisulfite sequencing", "methylation patterns");
        assert_eq!(matches[0].omic_type, "epigenomic");
    }

    #[test]
    fn test_multiple_omics_detected() {
        let matches = classify_omic_type("RNA-Seq and ChIP-Seq study", "gene expression and methylation");
        let types: Vec<&str> = matches.iter().map(|m| m.omic_type).collect();
        assert!(types.contains(&"transcriptomic"));
        assert!(types.contains(&"epigenomic"));
    }
}
