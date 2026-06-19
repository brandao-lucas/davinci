import apiClient from './client';
import type { ProjectGeneDetail, GeneFilters, PaginatedProjectGeneListList } from '@/lib/types/gene';

export const genesApi = {
  list: (projectId: string, filters?: GeneFilters) => {
    // Converte `included_only: false` para ausente (backend ignora false; só envia quando true)
    const params = filters ? { ...filters } : undefined;
    if (params && !params.included_only) delete params.included_only;
    return apiClient.get<PaginatedProjectGeneListList>(`/projects/${projectId}/genes/`, { params });
  },

  get: (projectId: string, geneSymbol: string) =>
    apiClient.get<ProjectGeneDetail>(`/projects/${projectId}/genes/${encodeURIComponent(geneSymbol)}/`),
};
