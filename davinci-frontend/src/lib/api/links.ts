import apiClient from './client';
import type { PaginatedResponse } from '@/lib/types/api';

export interface PaperDatasetLink {
  id: number;
  paper: number;
  paper_title: string;
  paper_pmid: string;
  dataset: number;
  dataset_accession: string;
  dataset_title: string;
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
};
