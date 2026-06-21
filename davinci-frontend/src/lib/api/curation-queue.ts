import apiClient from './client';
import type {
  CurationQueueItem,
  CurationQueueResolveInput,
  CurationQueueResolveResponse,
} from '@/lib/types/curation-queue';

export const curationQueueApi = {
  /**
   * GET /projects/{projectId}/curation-queue/
   * Lista datasets com has_control_group classificado-indeterminado (score < 0.5).
   */
  list: (projectId: string) =>
    apiClient.get<CurationQueueItem[]>(`/projects/${projectId}/curation-queue/`),

  /**
   * POST /projects/{projectId}/curation-queue/{id}/resolve/
   * Curador seta has_control_group manualmente ('yes' | 'no').
   * Preserva auditoria: curated_at, notes, marca manual em contract_confidence.
   */
  resolve: (
    projectId: string,
    projectDatasetId: number,
    data: CurationQueueResolveInput,
  ) =>
    apiClient.post<CurationQueueResolveResponse>(
      `/projects/${projectId}/curation-queue/${projectDatasetId}/resolve/`,
      data,
    ),
};
