// Shared omics sample (one per biological sample, independent of project)
export interface OmicSample {
  accession: string;
  title: string;
  source_name: string | null;
  organism: string;
  tax_id: number | null;
  platform: string | null;
  characteristics: Record<string, string> | null;
  extra_metadata: Record<string, unknown> | null;
  ingested_at: string;
  updated_at: string;
}

// Project-level sample with curation fields (list view)
export interface ProjectSample {
  id: number;
  accession: string;
  title: string;
  source_name: string | null;
  organism: string;
  tax_id: number | null;
  platform: string | null;
  dataset_id: number;
  dataset_accession: string;
  curation_status: 'pending' | 'included' | 'excluded' | 'maybe';
  exclusion_reason: string | null;
  notes: string;
  relevance_score: number | null;
  added_at: string;
  curated_at: string | null;
}

// Project-level sample with full detail (detail view)
export interface ProjectSampleDetail {
  id: number;
  sample: OmicSample;
  curation_status: 'pending' | 'included' | 'excluded' | 'maybe';
  exclusion_reason: string | null;
  notes: string;
  relevance_score: number | null;
  added_at: string;
  curated_at: string | null;
}

export interface SampleFilters {
  curation_status?: string;
  dataset?: number | string;
  organism?: string;
  search?: string;
  ordering?: string;
  page?: number;
}
