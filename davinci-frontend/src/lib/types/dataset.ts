// Tipos derivados do schema OpenAPI gerado pelo backend.
// NÃO edite manualmente — altere o serializer Django e rode `npm run gen:types`.

import type { components } from './api-schema';

// ProjectDatasetList: view compacta de lista (campos achatados + curadoria)
// Usada pelo endpoint GET /projects/{project_pk}/datasets/
export type ProjectDatasetList = components['schemas']['ProjectDatasetList'];

// Alias de compatibilidade: código existente importa OmicDataset para a view de lista.
// O endpoint de lista retorna ProjectDatasetList (campos achatados), não OmicDataset
// (entidade compartilhada sem curadoria). Alias mantém imports funcionando sem quebrar
// componentes existentes — divergência real: ver relatório de migração.
export type OmicDataset = components['schemas']['ProjectDatasetList'];

// OmicDatasetRaw: entidade compartilhada sem curadoria (aninhada em ProjectDatasetDetail.dataset)
export type OmicDatasetRaw = components['schemas']['OmicDataset'];

// ProjectDatasetDetail: dataset aninhado + curadoria (endpoint de detalhe)
export type ProjectDatasetDetail = components['schemas']['ProjectDatasetDetail'];

// DatasetFile: arquivo suplementar/rawdata vinculado a um dataset
export type DatasetFile = components['schemas']['DatasetFile'];

// DatasetFileDownloadStatus: status do download de um DatasetFile
export type DatasetFileDownloadStatus = components['schemas']['DatasetFileDownloadStatusEnum'];

// DownloadDispatchRequest: body do POST .../download/
export type DownloadDispatchRequest = components['schemas']['DownloadDispatchRequest'];

// DownloadDispatchResponse: resposta do POST .../download/
export type DownloadDispatchResponse = components['schemas']['DownloadDispatchResponse'];

// DownloadQuotaPreview: payload de erro 400 (confirm ausente) ou 409 (quota esgotada)
export type DownloadQuotaPreview = components['schemas']['DownloadQuotaPreview'];

// PaginatedDatasetFileList: lista paginada de DatasetFile
export type PaginatedDatasetFileList = components['schemas']['PaginatedDatasetFileList'];

// Filtros de listagem (parâmetros de query — não gerados pelo OpenAPI)
export interface DatasetFilters {
  curation_status?: string;
  omic_type?: string;
  organism?: string;
  source_db?: string;
  has_summary?: boolean;
  search?: string;
  ordering?: string;
  page?: number;
}

// Payload de bulk_curate por filtro (novo contrato do backend)
// Envia `filters` em vez de `dataset_ids` para excluir todos os datasets filtrados de uma vez.
export interface BulkCurateDatasetByFilterInput {
  filters: DatasetFilters;
  curation_status: string;
  exclusion_reason?: string;
}

// Resposta comum de bulk_curate (por IDs ou por filtro)
export interface BulkCurateResponse {
  updated: number;
}
