import apiClient from './client';
import type { ProjectSample, ProjectSampleDetail, SampleFilters } from '@/lib/types/sample';
import type { PaginatedResponse } from '@/lib/types/api';

export const samplesApi = {
  // Samples for a specific dataset within a project
  listByDataset: (projectId: string, datasetId: number | string, filters?: SampleFilters) =>
    apiClient.get<PaginatedResponse<ProjectSample>>(
      `/projects/${projectId}/datasets/${datasetId}/samples/`,
      { params: filters }
    ),

  // All samples for a project (with optional filters like curation_status, dataset, organism)
  listByProject: (projectId: string, filters?: SampleFilters) =>
    apiClient.get<PaginatedResponse<ProjectSample>>(
      `/projects/${projectId}/samples/`,
      { params: filters }
    ),

  // Detail of a single sample
  get: (projectId: string, sampleId: number) =>
    apiClient.get<ProjectSampleDetail>(`/projects/${projectId}/samples/${sampleId}/`),

  // Individual curation (PATCH)
  curate: (
    projectId: string,
    sampleId: number,
    data: { curation_status: string; exclusion_reason?: string; notes?: string; relevance_score?: number | null }
  ) =>
    apiClient.patch<ProjectSample>(`/projects/${projectId}/samples/${sampleId}/`, data),

  // Bulk curation — mirrors datasets bulk_curate path
  bulkCurate: (
    projectId: string,
    data: { sample_ids: number[]; curation_status: string; exclusion_reason?: string }
  ) =>
    apiClient.post(`/projects/${projectId}/samples/bulk_curate/`, data),
};
