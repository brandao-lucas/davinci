import apiClient from './client';
import type { MeshSuggestion, MagnitudePreview, SearchPreviewPayload } from '@/lib/types/advanced-search';

export const advancedSearchApi = {
  /**
   * POST /api/v1/projects/{id}/mesh/suggest/
   * Sugere descritores MeSH a partir de um termo livre ou dos termos do projeto.
   */
  meshSuggest: (projectId: string, term?: string) =>
    apiClient.post<MeshSuggestion[]>(`/projects/${projectId}/mesh/suggest/`, term ? { term } : {}),

  /**
   * POST /api/v1/projects/{id}/search/preview/
   * Retorna contagens comparativas (free-text vs MeSH vs combinado).
   * Operação read-only — não cria IngestionJob.
   */
  searchPreview: (projectId: string, payload: SearchPreviewPayload) =>
    apiClient.post<MagnitudePreview>(`/projects/${projectId}/search/preview/`, payload),
};
