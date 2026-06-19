// Tipos derivados do schema OpenAPI gerado pelo backend.
// NÃO edite manualmente — altere o serializer Django e rode `npm run gen:types`.

import type { components } from './api-schema';

export type DaVinciProject = components['schemas']['DaVinciProject'];
export type ProjectStats = components['schemas']['ProjectStats'];

// CreateProjectInput: campos editáveis para criação (subconjunto de DaVinciProject)
// Os campos readOnly (id, slug, status, created_at, updated_at, user) são excluídos.
export interface CreateProjectInput {
  title: string;
  description?: string;
  query_term: string;
  query_synonyms?: string[];
  date_from?: number;
  date_to?: number;
  target_organisms?: string[];
  target_tissues?: string[];
}

// UpdateProjectInput: superconjunto de CreateProjectInput que inclui campos da pesquisa avançada.
// Todos opcionais porque PATCH só envia o que mudou.
export type UpdateProjectInput = Partial<CreateProjectInput> & {
  selected_mesh?: unknown;
  mesh_default_mode?: string;
  magnitude_snapshot?: unknown;
  advanced_search_enabled?: boolean;
};
