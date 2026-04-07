import apiClient from './client';
import type { OmicDataset, DatasetFilters } from '@/lib/types/dataset';
import type { PaginatedResponse } from '@/lib/types/api';

export const datasetsApi = {
  list: (projectId: string, filters?: DatasetFilters) =>
    apiClient.get<PaginatedResponse<OmicDataset>>(`/projects/${projectId}/datasets/`, { params: filters }),

  get: (projectId: string, datasetId: number) =>
    apiClient.get<OmicDataset>(`/projects/${projectId}/datasets/${datasetId}/`),

  curate: (projectId: string, datasetId: number, data: {
    curation_status: string;
    exclusion_reason?: string;
    notes?: string;
  }) =>
    apiClient.patch<OmicDataset>(`/projects/${projectId}/datasets/${datasetId}/`, data),

  bulkCurate: (projectId: string, data: {
    dataset_ids: number[];
    curation_status: string;
    exclusion_reason?: string;
  }) =>
    apiClient.post(`/projects/${projectId}/datasets/bulk_curate/`, data),

  search: (projectId: string, query: string) =>
    apiClient.get<PaginatedResponse<OmicDataset>>(`/projects/${projectId}/datasets/search/`, {
      params: { q: query },
    }),
};
