// Tipos derivados do schema OpenAPI gerado pelo backend.
// NÃO edite manualmente — altere o serializer Django e rode `npm run gen:types`.

import type { components } from './api-schema';

// Aliases diretos dos schemas gerados
export type ProjectDrugList = components['schemas']['ProjectDrugList'];
export type ProjectDrugDetail = components['schemas']['ProjectDrugDetail'];
export type DrugReference = components['schemas']['DrugReference'];
export type DrugSnippet = components['schemas']['DrugSnippet'];
export type PaginatedProjectDrugListList = components['schemas']['PaginatedProjectDrugListList'];
export type ContextStatus = components['schemas']['ContextStatusEnum'];

// Filtros de listagem (parâmetros de query — não gerados pelo OpenAPI)
export interface DrugFilters {
  q?: string;
  ordering?: 'unique_citations_included' | '-unique_citations_included'
           | 'unique_citations_total' | '-unique_citations_total'
           | 'mention_count_total' | '-mention_count_total'
           | 'drug_name' | '-drug_name';
  page?: number;
  page_size?: number;
  included_only?: boolean;
}
