// Tipos derivados do schema OpenAPI gerado pelo backend.
// NÃO edite manualmente — altere o serializer Django e rode `npm run gen:types`.

import type { components, operations } from './api-schema';

// Item individual do catálogo de descoberta (DatasetExportItem)
export type DiscoveryItem = components['schemas']['DatasetExportItem'];

// Envelope paginado do catálogo de descoberta
export type DiscoveryPage = components['schemas']['PaginatedDatasetExportItemList'];

// Todos os parâmetros de query do endpoint projects_datasets_export_list
// (fonte única de verdade — derivados do schema OpenAPI)
export type DiscoveryQueryParams = NonNullable<
  operations['projects_datasets_export_list']['parameters']['query']
>;

// Subconjunto de filtros sem paginação e sem export_format
// Usado pelo store/UI — exclui campos de paginação/controle de formato
export type DiscoveryFilters = Omit<DiscoveryQueryParams, 'export_format' | 'page' | 'ordering'>;
