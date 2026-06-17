// Tipos derivados do schema OpenAPI gerado pelo backend.
// NÃO edite manualmente — altere o serializer Django e rode `npm run gen:types`.

import type { components } from './api-schema';

// OmicSample: entidade compartilhada (sem curadoria)
export type OmicSample = components['schemas']['OmicSample'];

// ProjectSampleList: view compacta de lista (campos achatados + curadoria)
export type ProjectSample = components['schemas']['ProjectSampleList'];

// ProjectSampleDetail: sample aninhado + curadoria (endpoint de detalhe)
export type ProjectSampleDetail = components['schemas']['ProjectSampleDetail'];

// Filtros de listagem (parâmetros de query — não gerados pelo OpenAPI)
export interface SampleFilters {
  curation_status?: string;
  dataset?: number | string;
  organism?: string;
  search?: string;
  ordering?: string;
  page?: number;
}
