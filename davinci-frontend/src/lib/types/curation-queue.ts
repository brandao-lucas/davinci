// Tipos para a fila de curadoria manual (OmnisPathway Fase 2).
// Derivados dos serializers em apps/core/serializers/curation_queue.py.
// Quando o OpenAPI for regenerado (make gen-types), substituir por reexports de api-schema.d.ts.

/** Item da fila de curadoria: ProjectDataset com has_control_group indeterminado. */
export interface CurationQueueItem {
  /** PK de ProjectDataset — usado no endpoint de resolução */
  id: number;
  /** PK de OmicDataset */
  dataset_id: number;
  accession: string;
  source_db: string;
  title: string | null;
  summary: string | null;
  omic_type: string;
  organism: string | null;
  n_samples: number | null;
  /** Valor atual: sempre 'unknown' enquanto na fila */
  has_control_group: 'unknown';
  /** Score do classificador automático (< 0.5 → na fila) */
  has_control_group_score: number | null;
  curation_status: string;
  notes: string | null;
  added_at: string;
  curated_at: string | null;
}

/** Body do POST /curation-queue/{id}/resolve/ */
export interface CurationQueueResolveInput {
  /** Decisão do curador: apenas 'yes' ou 'no' */
  has_control_group: 'yes' | 'no';
  /** Notas do curador (opcional, auditável) */
  notes?: string;
}

/** Resposta após resolução bem-sucedida */
export interface CurationQueueResolveResponse {
  id: number;
  dataset_id: number;
  accession: string;
  has_control_group: 'yes' | 'no';
  has_control_group_score: number | null;
  notes: string | null;
  curated_at: string | null;
}
