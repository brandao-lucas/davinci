export interface IngestionJob {
  id: string;
  project: string;
  job_type: 'pubmed_search' | 'pubmed_fetch' | 'geo_search' | 'sra_search' | 'variant_annotation' | 'gene_ner';
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  parameters: Record<string, unknown>;
  records_processed: number;
  records_inserted: number;
  records_updated: number;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
}
