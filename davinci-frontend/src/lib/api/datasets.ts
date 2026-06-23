import apiClient from './client';
import type {
  OmicDataset,
  ProjectDatasetDetail,
  DatasetFilters,
  DownloadDispatchRequest,
  DownloadDispatchResponse,
  FastqUrlListResponse,
  PaginatedDatasetFileList,
  BulkCurateDatasetByFilterInput,
  BulkCurateResponse,
  SraResolutionResponse,
  BatchDownloadRequest,
  BatchDownloadResponse,
  BatchFastqUrlListResponse,
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
  //
  // destination='server' (padrão): enfileira job Celery → HTTP 202 (DownloadDispatchResponse)
  //   ou HTTP 400/409 (DownloadQuotaPreview) quando falta confirm ou quota esgotada.
  // destination='client' (Inc-1): resolve URLs públicas ENA e retorna lista síncrona → HTTP 200
  //   (FastqUrlListResponse). Sem job, sem quota, sem DatasetFile.
  //
  // scope: 'all' | 'included' | 'manual' | 'filter' (Inc-2)
  //   - 'manual': obriga sample_ids (ProjectSample.id).
  //   - 'filter': obriga filters (SampleFilter com curation_status/organism/platform).
  //
  // O tipo de retorno usa union porque o status HTTP discrimina a resposta.
  // O hook (useTriggerDatasetDownload) trata a discriminação pelo httpStatus da resposta.
  triggerDownload: (projectId: string, datasetId: number, body?: Partial<DownloadDispatchRequest>) =>
    apiClient.post<DownloadDispatchResponse | FastqUrlListResponse>(
      `/projects/${projectId}/datasets/${datasetId}/download/`,
      body ?? {},
    ),

  // POST /projects/{project_pk}/datasets/{id}/resolve-sra/
  // Dispara resolução GEO→SRA: para cada GSM do dataset, lê extra_metadata['relation'],
  // extrai SRX e resolve SRX→SRR via ENA. Grava sra_runs nos GSMs. Retorna 202 com IngestionJob.
  resolveSra: (projectId: string, datasetId: number) =>
    apiClient.post<SraResolutionResponse>(
      `/projects/${projectId}/datasets/${datasetId}/resolve-sra/`,
    ),

  // GET /projects/{project_pk}/datasets/{id}/files/
  listFiles: (projectId: string, datasetId: number) =>
    apiClient.get<PaginatedDatasetFileList>(
      `/projects/${projectId}/datasets/${datasetId}/files/`,
    ),

  // POST /projects/{project_pk}/datasets/download-batch/ (Inc-5)
  // Lote de downloads FASTQ para múltiplos datasets do projeto.
  //
  // destination='server': HTTP 202 (BatchDownloadResponse) ou 400/409 (BatchDownloadQuotaPreview).
  // destination='client': HTTP 200 (BatchFastqUrlListResponse).
  //
  // dataset_ids: lista de OmicDataset.id; omitido = todos os datasets com amostras no scope.
  // scope: 'included' (padrão) | 'all' — sem 'manual'/'filter' no batch.
  downloadBatch: (projectId: string, body: Partial<BatchDownloadRequest>) =>
    apiClient.post<BatchDownloadResponse | BatchFastqUrlListResponse>(
      `/projects/${projectId}/datasets/download-batch/`,
      body,
    ),
};
