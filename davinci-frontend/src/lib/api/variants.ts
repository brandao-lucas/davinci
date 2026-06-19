import apiClient from './client';
import type { ProjectVariantDetail, VariantFilters, PaginatedProjectVariantListList } from '@/lib/types/variant';

export const variantsApi = {
  list: (projectId: string, filters?: VariantFilters) => {
    // Converte `included_only: false` para ausente (backend ignora false; só envia quando true)
    const params = filters ? { ...filters } : undefined;
    if (params && !params.included_only) delete params.included_only;
    return apiClient.get<PaginatedProjectVariantListList>(`/projects/${projectId}/variants/`, { params });
  },

  get: (projectId: string, rsNumber: string) =>
    apiClient.get<ProjectVariantDetail>(`/projects/${projectId}/variants/${encodeURIComponent(rsNumber)}/`),
};
