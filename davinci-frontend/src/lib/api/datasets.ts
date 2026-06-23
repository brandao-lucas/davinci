import apiClient from './client';
import type {
  OmicDataset,
  ProjectDatasetDetail,
  DatasetFilters,
  DownloadDispatchRequest,
  DownloadDispatchResponse,
  PaginatedDatasetFileList,
  BulkCurateDatasetByFilterInput,
  BulkCurateResponse,
  SraResolutionResponse,
} from '@/lib/types/dataset';
import type { PaginatedResponse } from '@/lib/types/api';

export const datasetsApi = {
  list: (projectId: string, filters?: DatasetFilters) =>
    apiClient.get<PaginatedResponse<OmicDataset>>(`/projects/${projectId}/datasets/`, { params: filters }),

  // Endpoint de detalhe retorna ProjectDatasetDetail (dataset aninhado + curadoria).
  get: (projectId: string, datasetId: number) =>
    apiClient.get<ProjectDatasetDetail>(`/projects/${projectId}/datasets/${datasetId}/`),

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

  // Bulk-curate por filtro: envia `filters` no body em vez de `dataset_ids`.
  // O backend aplica os filtros e retorna { updated: n }.
  bulkCurateByFilter: (projectId: string, data: BulkCurateDatasetByFilterInput) =>
    apiClient.post<BulkCurateResponse>(`/projects/${projectId}/datasets/bulk_curate/`, data),

  search: (projectId: string, query: string) =>
    apiClient.get<PaginatedResponse<OmicDataset>>(`/projects/${projectId}/datasets/search/`, {
      params: { q: query },
    }),

  addFromSuggestion: (projectId: string, datasetId: number) =>
    apiClient.post<OmicDataset>(
      `/projects/${projectId}/datasets/add_from_suggestion/`,
      { dataset_id: datasetId },
    ),

  // POST /projects/{project_pk}/datasets/{id}/download/
  // Dispara o download dos arquivos do dataset. Retorna 202 com o IngestionJob criado.
  // Para SRA/GEO com sra_runs: aceita scope, sample_ids, confirm, file_kind.
  //   - scope='all' (padrão): todas as runs do dataset.
  //   - scope='included': só samples com curation_status='included'.
  //   - scope='manual': exatamente os sample_ids informados.
  //   - sem confirm (ou confirm=false): retorna 400 com DownloadQuotaPreview (prévia de quota).
  //   - com confirm=true: enfileira o job (202).
  //   - quota esgotada: 409 com DownloadQuotaPreview (confirm_required=false).
  // destination: 'server' (padrão, único implementado no MVP); 'client' retorna 400 (Inc-1).
  triggerDownload: (projectId: string, datasetId: number, body?: Partial<DownloadDispatchRequest>) =>
    apiClient.post<DownloadDispatchResponse>(
      `/projects/${projectId}/datasets/${datasetId}/download/`,
      body ?? {},
    ),

  // POST /projects/{project_pk}/datasets/{id}/resolve-sra/
  // Dispara resolução GEO→SRA: para cada GSM do dataset, lê extra_metadata['relation'],
  // extrai SRX e resolve SRX→SRR via ENA. Grava sra_runs nos GSMs. Retorna 202 com IngestionJob.
  // Usado em datasets GEO antes de permitir download FASTQ.
  resolveSra: (projectId: string, datasetId: number) =>
    apiClient.post<SraResolutionResponse>(
      `/projects/${projectId}/datasets/${datasetId}/resolve-sra/`,
    ),

  // GET /projects/{project_pk}/datasets/{id}/files/
  listFiles: (projectId: string, datasetId: number) =>
    apiClient.get<PaginatedDatasetFileList>(
      `/projects/${projectId}/datasets/${datasetId}/files/`,
    ),
};
