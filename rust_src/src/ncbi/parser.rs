use quick_xml::events::Event;
use quick_xml::Reader;
use regex::Regex;
use sha2::{Digest, Sha256};
use std::sync::OnceLock;

use crate::ncbi::models::{AuthorData, MeSHTerm, PaperData};

// ── Country regex ─────────────────────────────────────────────────────────────

static COUNTRY_RE: OnceLock<Regex> = OnceLock::new();

fn extract_country(affiliation: &str) -> String {
    let re = COUNTRY_RE.get_or_init(|| {
        Regex::new(concat!(
            r"\b(USA|United States|U\.S\.A\.|UK|United Kingdom|Brazil|Brasil|China|Germany|",
            r"France|Italy|Spain|Japan|Canada|Australia|Netherlands|Sweden|Switzerland|India|",
            r"South Korea|Korea|Denmark|Norway|Finland|Belgium|Austria|Portugal|Poland|Israel|",
            r"Turkey|Mexico|Argentina|Chile|Singapore|Taiwan)\b"
        ))
        .unwrap()
    });
    re.find(affiliation)
        .map(|m| m.as_str().to_string())
        .unwrap_or_default()
}

fn parse_month(s: &str) -> Option<u16> {
    match s.trim() {
        "Jan" | "January" => Some(1),
        "Feb" | "February" => Some(2),
        "Mar" | "March" => Some(3),
        "Apr" | "April" => Some(4),
        "May" => Some(5),
        "Jun" | "June" => Some(6),
        "Jul" | "July" => Some(7),
        "Aug" | "August" => Some(8),
        "Sep" | "September" => Some(9),
        "Oct" | "October" => Some(10),
        "Nov" | "November" => Some(11),
        "Dec" | "December" => Some(12),
        other => other.parse::<u16>().ok(),
    }
}

// ── Text capture target ───────────────────────────────────────────────────────

/// Priority order for selecting the primary publication type.
/// Lower index = higher priority.
const PUB_TYPE_PRIORITY: &[&str] = &[
    "Systematic Review",
    "Meta-Analysis",
    "Review",
    "Randomized Controlled Trial",
    "Clinical Trial",
    "Observational Study",
    "Case Reports",
    "Editorial",
    "Letter",
    "Comment",
    "Journal Article",
];

fn pick_pub_type(types: &[String]) -> String {
    for priority in PUB_TYPE_PRIORITY {
        if types.iter().any(|t| t == priority) {
            return priority.to_string();
        }
    }
    types.first().cloned().unwrap_or_default()
}

#[derive(PartialEq, Clone)]
enum Cap {
    None,
    Pmid,
    ArticleTitle,
    AbstractText,
    JournalTitle,
    PubYear,
    PubMonth,
    LastName,
    Initials,
    Affiliation,
    Keyword,
    Descriptor,
    Qualifier,
    ELocDoi,
    ArticleIdDoi,
    ArticleIdPmc,
    PublicationType,
}

// ── Parser ────────────────────────────────────────────────────────────────────

/// Parse a PubMed XML string (as returned by efetch) into a list of PaperData.
///
/// Handles:
/// - Multiple `<AbstractText>` sections (concatenated with space)
/// - Month names and numeric months
/// - MeSH major-topic flags on both descriptor and qualifier
/// - DOI from `<ELocationID>` (fallback) and `<ArticleId IdType="doi">` (preferred)
/// - PMC ID from `<ArticleId IdType="pmc">`
/// - Country extraction from affiliation text via regex
pub fn parse_pubmed_xml(xml: &str) -> Result<Vec<PaperData>, String> {
    let mut reader = Reader::from_str(xml);
    reader.config_mut().trim_text(true);

    let mut papers: Vec<PaperData> = Vec::new();
    let mut buf = Vec::new();

    // ── Per-article accumulators ──────────────────────────────────────────────
    let mut pmid: i64 = 0;
    let mut pmc_id = String::new();
    let mut doi = String::new();
    let mut title = String::new();
    let mut abstract_parts: Vec<String> = Vec::new();
    let mut journal = String::new();
    let mut pub_year: Option<u16> = None;
    let mut pub_month: Option<u16> = None;
    let mut pub_types: Vec<String> = Vec::new();
    let mut authors: Vec<AuthorData> = Vec::new();
    let mut keywords: Vec<String> = Vec::new();
    let mut mesh_terms: Vec<MeSHTerm> = Vec::new();

    // ── Per-author / per-mesh accumulators ────────────────────────────────────
    let mut cur_last_name = String::new();
    let mut cur_initials = String::new();
    let mut cur_affiliation = String::new();
    let mut cur_descriptor = String::new();
    let mut cur_descriptor_major = false;
    let mut cur_qualifier = String::new();
    let mut cur_qualifier_major = false;
    let mut mesh_heading_has_qualifier = false;

    // ── Nesting flags ─────────────────────────────────────────────────────────
    let mut in_pubmed_article = false;
    let mut in_medline = false;
    let mut in_pub_type_list = false;
    let mut in_journal = false;
    let mut in_journal_issue = false;
    let mut in_pubdate = false;
    let mut in_abstract = false;
    let mut in_author_list = false;
    let mut in_author = false;
    let mut in_affiliation_info = false;
    let mut in_keyword_list = false;
    let mut in_mesh_list = false;
    let mut in_mesh_heading = false;
    let mut in_pubmed_data = false;
    let mut in_article_id_list = false;

    let mut cap = Cap::None;

    macro_rules! reset_article {
        () => {
            pmid = 0;
            pmc_id.clear();
            doi.clear();
            title.clear();
            abstract_parts.clear();
            journal.clear();
            pub_year = None;
            pub_month = None;
            pub_types.clear();
            authors.clear();
            keywords.clear();
            mesh_terms.clear();
        };
    }

    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Eof) => break,

            Ok(Event::Start(ref e)) => {
                let qname = e.name();
                let name = qname.as_ref();
                match name {
                    b"PubmedArticle" => {
                        in_pubmed_article = true;
                        reset_article!();
                    }
                    b"MedlineCitation" if in_pubmed_article => in_medline = true,
                    b"PMID" if in_medline && !in_author => cap = Cap::Pmid,
                    b"Journal" if in_medline && !in_pubmed_data => in_journal = true,
                    b"JournalIssue" if in_journal => in_journal_issue = true,
                    b"PubDate" if in_journal_issue => in_pubdate = true,
                    b"Year" if in_pubdate => cap = Cap::PubYear,
                    b"Month" if in_pubdate => cap = Cap::PubMonth,
                    b"Title" if in_journal && !in_journal_issue => cap = Cap::JournalTitle,
                    b"ArticleTitle" if in_medline => cap = Cap::ArticleTitle,
                    b"Abstract" if in_medline => in_abstract = true,
                    b"AbstractText" if in_abstract => cap = Cap::AbstractText,
                    b"AuthorList" if in_medline => in_author_list = true,
                    b"Author" if in_author_list => {
                        in_author = true;
                        cur_last_name.clear();
                        cur_initials.clear();
                        cur_affiliation.clear();
                    }
                    b"LastName" if in_author => cap = Cap::LastName,
                    b"Initials" if in_author => cap = Cap::Initials,
                    b"AffiliationInfo" if in_author => in_affiliation_info = true,
                    b"Affiliation" if in_affiliation_info => cap = Cap::Affiliation,
                    b"ELocationID" if in_medline && doi.is_empty() => {
                        for attr in e.attributes().flatten() {
                            if attr.key.as_ref() == b"EIdType"
                                && attr.value.as_ref() == b"doi"
                            {
                                cap = Cap::ELocDoi;
                            }
                        }
                    }
                    b"PublicationTypeList" if in_medline => in_pub_type_list = true,
                    b"PublicationType" if in_pub_type_list => cap = Cap::PublicationType,
                    b"KeywordList" if in_medline => in_keyword_list = true,
                    b"Keyword" if in_keyword_list => cap = Cap::Keyword,
                    b"MeshHeadingList" if in_medline => in_mesh_list = true,
                    b"MeshHeading" if in_mesh_list => {
                        in_mesh_heading = true;
                        cur_descriptor.clear();
                        cur_descriptor_major = false;
                        cur_qualifier.clear();
                        cur_qualifier_major = false;
                        mesh_heading_has_qualifier = false;
                    }
                    b"DescriptorName" if in_mesh_heading => {
                        cap = Cap::Descriptor;
                        for attr in e.attributes().flatten() {
                            if attr.key.as_ref() == b"MajorTopicYN" {
                                cur_descriptor_major = attr.value.as_ref() == b"Y";
                            }
                        }
                    }
                    b"QualifierName" if in_mesh_heading => {
                        cap = Cap::Qualifier;
                        for attr in e.attributes().flatten() {
                            if attr.key.as_ref() == b"MajorTopicYN" {
                                cur_qualifier_major = attr.value.as_ref() == b"Y";
                            }
                        }
                    }
                    b"PubmedData" if in_pubmed_article => in_pubmed_data = true,
                    b"ArticleIdList" if in_pubmed_data => in_article_id_list = true,
                    b"ArticleId" if in_article_id_list => {
                        for attr in e.attributes().flatten() {
                            if attr.key.as_ref() == b"IdType" {
                                match attr.value.as_ref() {
                                    b"doi" => cap = Cap::ArticleIdDoi,
                                    b"pmc" => cap = Cap::ArticleIdPmc,
                                    _ => {}
                                }
                            }
                        }
                    }
                    _ => {}
                }
            }

            Ok(Event::Text(ref e)) => {
                if cap == Cap::None {
                    buf.clear();
                    continue;
                }
                let text = match e.unescape() {
                    Ok(t) => t.into_owned(),
                    Err(_) => {
                        buf.clear();
                        continue;
                    }
                };
                match &cap {
                    Cap::Pmid => {
                        if let Ok(v) = text.trim().parse::<i64>() {
                            pmid = v;
                        }
                    }
                    Cap::ArticleTitle => title = text,
                    Cap::AbstractText => abstract_parts.push(text),
                    Cap::JournalTitle => {
                        if journal.is_empty() {
                            journal = text;
                        }
                    }
                    Cap::PubYear => {
                        if let Ok(v) = text.trim().parse::<u16>() {
                            pub_year = Some(v);
                        }
                    }
                    Cap::PubMonth => pub_month = parse_month(&text),
                    Cap::LastName => cur_last_name = text,
                    Cap::Initials => cur_initials = text,
                    Cap::Affiliation => cur_affiliation = text,
                    Cap::Keyword => keywords.push(text),
                    Cap::Descriptor => cur_descriptor = text,
                    Cap::Qualifier => cur_qualifier = text,
                    Cap::ELocDoi => {
                        if doi.is_empty() {
                            doi = text;
                        }
                    }
                    Cap::ArticleIdDoi => doi = text,
                    Cap::ArticleIdPmc => pmc_id = text,
                    Cap::PublicationType => pub_types.push(text),
                    Cap::None => {}
                }
                cap = Cap::None;
            }

            Ok(Event::End(ref e)) => {
                cap = Cap::None;
                match e.name().as_ref() {
                    b"PubmedArticle" => {
                        if pmid > 0 {
                            let abstract_text = abstract_parts.join(" ");
                            let mut hasher = Sha256::new();
                            hasher.update(pmid.to_string().as_bytes());
                            hasher.update(abstract_text.as_bytes());
                            let raw_xml_hash = format!("{:x}", hasher.finalize());

                            papers.push(PaperData {
                                pmid,
                                pmc_id: if pmc_id.is_empty() { None } else { Some(pmc_id.clone()) },
                                doi: if doi.is_empty() { None } else { Some(doi.clone()) },
                                title: title.clone(),
                                abstract_text,
                                journal: journal.clone(),
                                pub_year,
                                pub_month,
                                pub_type: pick_pub_type(&pub_types),
                                authors: authors.clone(),
                                keywords: keywords.clone(),
                                mesh_terms: mesh_terms.clone(),
                                raw_xml_hash,
                            });
                        }
                        in_pubmed_article = false;
                        in_medline = false;
                        in_pubmed_data = false;
                    }
                    b"MedlineCitation" => in_medline = false,
                    b"Journal" => {
                        in_journal = false;
                        in_journal_issue = false;
                        in_pubdate = false;
                    }
                    b"JournalIssue" => {
                        in_journal_issue = false;
                        in_pubdate = false;
                    }
                    b"PubDate" => in_pubdate = false,
                    b"Abstract" => in_abstract = false,
                    b"AuthorList" => in_author_list = false,
                    b"Author" => {
                        if in_author && !cur_last_name.is_empty() {
                            authors.push(AuthorData {
                                last_name: cur_last_name.clone(),
                                initials: cur_initials.clone(),
                                affiliation: cur_affiliation.clone(),
                                country: extract_country(&cur_affiliation),
                            });
                        }
                        in_author = false;
                        in_affiliation_info = false;
                    }
                    b"AffiliationInfo" => in_affiliation_info = false,
                    b"PublicationTypeList" => in_pub_type_list = false,
                    b"KeywordList" => in_keyword_list = false,
                    b"MeshHeadingList" => in_mesh_list = false,
                    b"QualifierName" if in_mesh_heading => {
                        if !cur_descriptor.is_empty() && !cur_qualifier.is_empty() {
                            mesh_terms.push(MeSHTerm {
                                descriptor: cur_descriptor.clone(),
                                qualifier: cur_qualifier.clone(),
                                is_major: cur_descriptor_major || cur_qualifier_major,
                            });
                            mesh_heading_has_qualifier = true;
                            cur_qualifier.clear();
                            cur_qualifier_major = false;
                        }
                    }
                    b"MeshHeading" => {
                        // If no qualifier was emitted, add one with empty qualifier
                        if !cur_descriptor.is_empty() && !mesh_heading_has_qualifier {
                            mesh_terms.push(MeSHTerm {
                                descriptor: cur_descriptor.clone(),
                                qualifier: String::new(),
                                is_major: cur_descriptor_major,
                            });
                        }
                        in_mesh_heading = false;
                    }
                    b"PubmedData" => {
                        in_pubmed_data = false;
                        in_article_id_list = false;
                    }
                    b"ArticleIdList" => in_article_id_list = false,
                    _ => {}
                }
            }

            Err(e) => {
                return Err(format!(
                    "XML parse error at offset {}: {:?}",
                    reader.buffer_position(),
                    e
                ))
            }
            _ => {}
        }
        buf.clear();
    }

    Ok(papers)
}
