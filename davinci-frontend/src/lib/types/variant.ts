// Tipos derivados do schema OpenAPI gerado pelo backend.
// NÃO edite manualmente — altere o serializer Django e rode `npm run gen:types`.

import type { components } from './api-schema';

// Aliases diretos dos schemas gerados
export type ProjectVariantList = components['schemas']['ProjectVariantList'];
export type ProjectVariantDetail = components['schemas']['ProjectVariantDetail'];
export type VariantReference = components['schemas']['VariantReference'];
export type VariantSnippet = components['schemas']['VariantSnippet'];
export type PaginatedProjectVariantListList = components['schemas']['PaginatedProjectVariantListList'];
export type ContextStatus = components['schemas']['ContextStatusEnum'];

// Filtros de listagem (parâmetros de query — não gerados pelo OpenAPI)
export interface VariantFilters {
  q?: string;
  ordering?: 'unique_citations_included' | '-unique_citations_included'
           | 'unique_citations_total' | '-unique_citations_total'
           | 'mention_count_total' | '-mention_count_total'
           | 'rs_number' | '-rs_number';
  page?: number;
  page_size?: number;
  included_only?: boolean;
}
