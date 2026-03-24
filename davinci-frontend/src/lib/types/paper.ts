export interface Paper {
  id: number;
  pmid: string;
  pmc_id: string | null;
  doi: string | null;
  title: string;
  abstract: string;
  journal: string;
  pub_year: number;
  pub_month: number | null;
  authors: PaperAuthor[];
  keywords: string[];
  mesh_terms: MeSHTerm[];
  genes: PaperGene[];
  variants: string[];
  curation_status: 'pending' | 'included' | 'excluded' | 'maybe';
  exclusion_reason: string | null;
  notes: string;
  relevance_score: number | null;
}

export interface PaperAuthor {
  position: number;
  last_name: string;
  initials: string;
  affiliation: string;
  country: string | null;
}

export interface MeSHTerm {
  descriptor: string;
  qualifier: string | null;
  is_major_topic: boolean;
}

export interface PaperGene {
  gene_symbol: string;
  entrez_id: number | null;
  mention_count: number;
}

export interface PaperFilters {
  curation_status?: string;
  pub_year_min?: number;
  pub_year_max?: number;
  journal?: string;
  search?: string;
  ordering?: string;
  page?: number;
}
