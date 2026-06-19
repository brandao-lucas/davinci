import apiClient from './client';
import type { ProjectDrugDetail, DrugFilters, PaginatedProjectDrugListList } from '@/lib/types/drug';

export const drugsApi = {
  list: (projectId: string, filters?: DrugFilters) => {
    // Converte `included_only: false` para ausente (backend ignora false; só envia quando true)
    const params = filters ? { ...filters } : undefined;
    if (params && !params.included_only) delete params.included_only;
    return apiClient.get<PaginatedProjectDrugListList>(`/projects/${projectId}/drugs/`, { params });
  },

  get: (projectId: string, drugNameLower: string) =>
    apiClient.get<ProjectDrugDetail>(
      `/projects/${projectId}/drugs/${encodeURIComponent(drugNameLower)}/`,
    ),
};
