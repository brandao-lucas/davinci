import apiClient from './client';
import type { PaginatedResponse } from '@/lib/types/api';
import type { OrphanLinkSuggestion, LinkSuggestionFilters } from '@/lib/types/links';

export interface PaperDatasetLink {
  id: number;
  /** PMID do paper (número inteiro). */
  paper_pmid: number;
  paper_title: string;
  dataset_accession: string;
  dataset_title: string;
  omic_type: string;
  confidence: 'auto' | 'confirmed' | 'rejected';
  created_at: string;
}

export const linksApi = {
  list: (projectId: string) =>
    apiClient.get<PaginatedResponse<PaperDatasetLink>>(`/projects/${projectId}/links/`),

  confirm: (projectId: string, linkId: number) =>
    apiClient.post(`/projects/${projectId}/links/${linkId}/confirm/`),

  reject: (projectId: string, linkId: number) =>
    apiClient.post(`/projects/${projectId}/links/${linkId}/reject/`),

  suggestions: (projectId: string, filters?: LinkSuggestionFilters) =>
    apiClient.get<PaginatedResponse<OrphanLinkSuggestion>>(
      `/projects/${projectId}/links/suggestions/`,
      { params: filters },
    ),
};
