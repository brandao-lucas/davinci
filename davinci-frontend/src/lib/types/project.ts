export interface DaVinciProject {
  id: string;
  slug: string;
  title: string;
  description: string;
  query_term: string;
  query_synonyms: string[];
  date_from: number | null;
  date_to: number | null;
  target_organisms: string[];
  target_tissues: string[];
  status: 'draft' | 'searching' | 'curating' | 'analyzing' | 'complete';
  created_at: string;
  updated_at: string;
  stats?: ProjectStats;
}

export interface ProjectStats {
  total_papers: number;
  included_papers: number;
  excluded_papers: number;
  pending_papers: number;
  total_datasets: number;
  included_datasets: number;
  total_samples: number;
  papers_by_year: Record<string, number>;
  papers_by_journal: Record<string, number>;
  datasets_by_omic_type: Record<string, number>;
  datasets_by_organism: Record<string, number>;
  top_genes: Array<{ gene: string; count: number }>;
  top_mesh_terms: Array<{ term: string; count: number }>;
  top_variants: Array<{ rs: string; count: number }>;
}

export interface CreateProjectInput {
  title: string;
  description?: string;
  query_term: string;
  query_synonyms?: string[];
  date_from?: number;
  date_to?: number;
  target_organisms?: string[];
  target_tissues?: string[];
}
