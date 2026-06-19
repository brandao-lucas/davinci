import apiClient from './client';
import type { ProjectMeSHDetail, MeSHFilters, PaginatedProjectMeSHListList } from '@/lib/types/mesh';

export const meshApi = {
  list: (projectId: string, filters?: MeSHFilters) => {
    // Converte `included_only: false` para ausente (backend ignora false; só envia quando true)
    const params = filters ? { ...filters } : undefined;
    if (params && !params.included_only) delete params.included_only;
    return apiClient.get<PaginatedProjectMeSHListList>(`/projects/${projectId}/mesh/`, { params });
  },

  get: (projectId: string, descriptor: string) =>
    apiClient.get<ProjectMeSHDetail>(
      `/projects/${projectId}/mesh/${encodeURIComponent(descriptor)}/`,
    ),
};
