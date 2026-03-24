import apiClient from './client';
import type { DaVinciProject, CreateProjectInput, ProjectStats } from '@/lib/types/project';
import type { PaginatedResponse } from '@/lib/types/api';

export const projectsApi = {
  list: () =>
    apiClient.get<PaginatedResponse<DaVinciProject>>('/projects/'),

  get: (id: string) =>
    apiClient.get<DaVinciProject>(`/projects/${id}/`),

  create: (data: CreateProjectInput) =>
    apiClient.post<DaVinciProject>('/projects/', data),

  update: (id: string, data: Partial<CreateProjectInput>) =>
    apiClient.patch<DaVinciProject>(`/projects/${id}/`, data),

  delete: (id: string) =>
    apiClient.delete(`/projects/${id}/`),

  search: (id: string) =>
    apiClient.post<{ job_id: string; status: string }>(`/projects/${id}/search/`),

  getStats: (id: string) =>
    apiClient.get<ProjectStats>(`/projects/${id}/stats/`),

  exportData: (id: string, exportFormat: 'json' | 'csv') =>
    apiClient.get(`/projects/${id}/export/`, { params: { export_format: exportFormat } }),
};
