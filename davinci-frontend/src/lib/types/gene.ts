// Tipos derivados do schema OpenAPI gerado pelo backend.
// NÃO edite manualmente — altere o serializer Django e rode `npm run gen:types`.

import type { components } from './api-schema';

// Aliases diretos dos schemas gerados
export type ProjectGeneList = components['schemas']['ProjectGeneList'];
export type ProjectGeneDetail = components['schemas']['ProjectGeneDetail'];
export type GeneReference = components['schemas']['GeneReference'];
export type GeneSnippet = components['schemas']['GeneSnippet'];
export type PaginatedProjectGeneListList = components['schemas']['PaginatedProjectGeneListList'];
export type ContextStatus = components['schemas']['ContextStatusEnum'];

// Filtros de listagem (parâmetros de query — não gerados pelo OpenAPI)
export interface GeneFilters {
  q?: string;
  ordering?: 'unique_citations_included' | '-unique_citations_included'
           | 'unique_citations_total' | '-unique_citations_total'
           | 'mention_count_total' | '-mention_count_total'
           | 'gene_symbol' | '-gene_symbol';
  page?: number;
  page_size?: number;
  included_only?: boolean;
}
