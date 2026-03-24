import apiClient from './client';
import type { IngestionJob } from '@/lib/types/job';
import type { PaginatedResponse } from '@/lib/types/api';

export const jobsApi = {
  list: (projectId: string) =>
    apiClient.get<PaginatedResponse<IngestionJob>>(`/projects/${projectId}/jobs/`),

  get: (projectId: string, jobId: string) =>
    apiClient.get<IngestionJob>(`/projects/${projectId}/jobs/${jobId}/`),

  cancel: (projectId: string, jobId: string) =>
    apiClient.post(`/projects/${projectId}/jobs/${jobId}/cancel/`),
};
