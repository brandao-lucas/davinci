import apiClient from './client';
import type { Paper, PaperDetail, PaperFilters } from '@/lib/types/paper';
import type { PaginatedResponse } from '@/lib/types/api';

export const papersApi = {
  list: (projectId: string, filters?: PaperFilters) =>
    apiClient.get<PaginatedResponse<Paper>>(`/projects/${projectId}/papers/`, { params: filters }),

  // Endpoint de detalhe retorna ProjectPaperDetail (paper aninhado + campos de curadoria)
  get: (projectId: string, paperId: number) =>
    apiClient.get<PaperDetail>(`/projects/${projectId}/papers/${paperId}/`),

  // PATCH retorna ProjectPaperCurate (só campos de curadoria), mas usamos Paper para
  // o patch otimista local — não precisamos do retorno tipado do curate.
  curate: (projectId: string, paperId: number, data: {
    curation_status: string;
    exclusion_reason?: string;
    notes?: string;
  }) =>
    apiClient.patch<Paper>(`/projects/${projectId}/papers/${paperId}/`, data),

  bulkCurate: (projectId: string, data: {
    paper_ids: number[];
    curation_status: string;
    exclusion_reason?: string;
  }) =>
    apiClient.post(`/projects/${projectId}/papers/bulk_curate/`, data),

  search: (projectId: string, query: string) =>
    apiClient.get<PaginatedResponse<Paper>>(`/projects/${projectId}/papers/search/`, {
      params: { q: query },
    }),

  addFromSuggestion: (projectId: string, pmid: number) =>
    apiClient.post<import('@/lib/types/paper').Paper>(
      `/projects/${projectId}/papers/add_from_suggestion/`,
      { pmid },
    ),
};
