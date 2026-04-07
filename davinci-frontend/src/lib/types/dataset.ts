export interface OmicDataset {
  id: number;
  accession: string;
  source_db: 'geo' | 'sra' | 'bioproject' | 'gwas_catalog' | 'arrayexpress' | 'tcga';
  bioproject_id: string | null;
  title: string;
  summary: string;
  omic_type: string;
  omic_subcategory: string | null;
  organism: string;
  n_samples: number | null;
  platform: string | null;
  curation_status: 'pending' | 'included' | 'excluded' | 'queued' | 'downloaded';
  exclusion_reason: string | null;
  notes: string;
  relevance_score: number | null;
}

export interface DatasetFilters {
  curation_status?: string;
  omic_type?: string;
  organism?: string;
  source_db?: string;
  has_summary?: boolean;
  search?: string;
  ordering?: string;
  page?: number;
}
