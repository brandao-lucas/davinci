// Tipos derivados do schema OpenAPI gerado pelo backend.
// NÃO edite manualmente — altere o serializer Django e rode `npm run gen:types`.

import type { components } from './api-schema';

// Aliases diretos dos schemas gerados
export type Paper = components['schemas']['ProjectPaperList'];
export type PaperDetail = components['schemas']['ProjectPaperDetail'];
export type PaperAuthor = components['schemas']['PaperAuthor'];
export type MeSHTerm = components['schemas']['PaperMeSHTerm'];
export type PaperGene = components['schemas']['PaperGene'];
export type PaperDrug = components['schemas']['PaperDrug'];
export type PaperVariant = components['schemas']['PaperVariant'];
export type PaperKeyword = components['schemas']['PaperKeyword'];

// Filtros de listagem (parâmetros de query — não gerados pelo OpenAPI)
export interface PaperFilters {
  curation_status?: string;
  pub_year_min?: number;
  pub_year_max?: number;
  journal?: string;
  pub_type?: string;
  has_abstract?: boolean;
  free_full_text?: boolean;
  search?: string;
  ordering?: string;
  page?: number;
}
