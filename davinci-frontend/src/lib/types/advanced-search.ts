// Tipos derivados do schema OpenAPI gerado pelo backend.
// NÃO edite manualmente — altere o serializer Django e rode `npm run gen:types`.

import type { components } from './api-schema';

export type MeshSuggestion = components['schemas']['MeshSuggestion'];
export type MagnitudePreview = components['schemas']['MagnitudePreview'];
export type PanelFlags = components['schemas']['PanelFlags'];
export type MeshDefaultMode = components['schemas']['MeshDefaultModeEnum'];

/** Item de MeSH selecionado pelo pesquisador — salvo em DaVinciProject.selected_mesh */
export interface SelectedMeshItem {
  descriptor: string;
  ui: string;
  qualifiers: string[];
  mode: MeshDefaultMode;
  major_only: boolean;
}

/** Payload para POST /projects/{id}/search/preview/ */
export interface SearchPreviewPayload {
  selected_mesh: SelectedMeshItem[];
  mesh_default_mode: MeshDefaultMode;
  panel_flags: PanelFlags;
}
