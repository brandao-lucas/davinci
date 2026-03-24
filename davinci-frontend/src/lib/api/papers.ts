import apiClient from './client';
import type { Paper, PaperFilters } from '@/lib/types/paper';
import type { PaginatedResponse } from '@/lib/types/api';

export const papersApi = {
  list: (projectId: string, filters?: PaperFilters) =>
    apiClient.get<PaginatedResponse<Paper>>(`/projects/${projectId}/papers/`, { params: filters }),

  get: (projectId: string, paperId: number) =>
    apiClient.get<Paper>(`/projects/${projectId}/papers/${paperId}/`),

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
};
