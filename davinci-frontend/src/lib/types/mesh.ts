// Tipos derivados do schema OpenAPI gerado pelo backend.
// NÃO edite manualmente — altere o serializer Django e rode `npm run gen:types`.

import type { components } from './api-schema';

// Aliases diretos dos schemas gerados
export type ProjectMeSHList = components['schemas']['ProjectMeSHList'];
export type ProjectMeSHDetail = components['schemas']['ProjectMeSHDetail'];
export type MeSHReference = components['schemas']['MeSHReference'];
export type MeSHSnippet = components['schemas']['MeSHSnippet'];
export type PaginatedProjectMeSHListList = components['schemas']['PaginatedProjectMeSHListList'];
export type ContextStatus = components['schemas']['ContextStatusEnum'];

// Filtros de listagem (parâmetros de query — não gerados pelo OpenAPI)
export interface MeSHFilters {
  q?: string;
  ordering?: 'major_topic_count' | '-major_topic_count'
           | 'unique_citations_included' | '-unique_citations_included'
           | 'unique_citations_total' | '-unique_citations_total'
           | 'descriptor' | '-descriptor';
  page?: number;
  page_size?: number;
  included_only?: boolean;
}
