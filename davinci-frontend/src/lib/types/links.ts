// Tipos de vínculos paper↔dataset — derivados do schema OpenAPI gerado.
// NÃO edite manualmente — altere o serializer Django e rode `npm run gen:types`.

import type { components } from './api-schema';

/** Vínculo de dataset listado dentro do detalhe de um paper. */
export type LinkedDataset = components['schemas']['LinkedDatasetBrief'];

/** Vínculo de paper listado dentro do detalhe de um dataset. */
export type LinkedPaper = components['schemas']['LinkedPaperBrief'];

/** Sugestão de órfão (Nível 2 — read-only). */
export type OrphanLinkSuggestion = components['schemas']['OrphanLinkSuggestion'];

/** Tipo de sugestão de órfão. */
export type SuggestionType = components['schemas']['SuggestionTypeEnum'];

/** Filtros para o endpoint GET /links/suggestions/. */
export interface LinkSuggestionFilters {
  type?: SuggestionType;
  page?: number;
  page_size?: number;
}
