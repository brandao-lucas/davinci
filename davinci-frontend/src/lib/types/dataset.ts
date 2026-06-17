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
