'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { advancedSearchApi } from '@/lib/api/advanced-search';
import { useDebounce } from '@/lib/hooks/use-debounce';
import type { SearchPreviewPayload, MeshDefaultMode, SelectedMeshItem, PanelFlags } from '@/lib/types/advanced-search';

/**
 * Busca sugestões MeSH para um projeto.
 * O termo é debounced (400ms) para não disparar a cada keystroke.
 * Quando term === undefined, usa os termos do projeto (backend default).
 */
export function useMeshSuggest(projectId: string, term?: string) {
  const debouncedTerm = useDebounce(term, 400);

  return useQuery({
    queryKey: ['mesh-suggest', projectId, debouncedTerm],
    queryFn: () =>
      advancedSearchApi.meshSuggest(projectId, debouncedTerm).then(r => r.data),
    enabled: !!projectId,
    staleTime: 5 * 60 * 1000, // sugestões ficam válidas 5min
  });
}

/**
 * Preview de magnitude — core counts (free_text, mesh, combined, overlap).
 * - Os 4 counts "core" recalculam ao vivo a cada mudança nos selectedMesh / mode,
 *   debounced 400ms para não martelar o backend a cada clique.
 * - O painel pesado (by_year, by_pub_type, open_access) só é ativado
 *   quando o caller passa panelFlags com ao menos uma flag true.
 */
export function useSearchPreview(
  projectId: string,
  selectedMesh: SelectedMeshItem[],
  meshDefaultMode: MeshDefaultMode,
  panelFlags: PanelFlags,
  enabled = true,
) {
  const payload: SearchPreviewPayload = {
    selected_mesh: selectedMesh,
    mesh_default_mode: meshDefaultMode,
    panel_flags: panelFlags,
  };

  // Debounce a key serializada para agrupar cliques rápidos
  const debouncedKey = useDebounce(
    JSON.stringify({ selectedMesh, meshDefaultMode, panelFlags }),
    400,
  );

  return useQuery({
    queryKey: ['search-preview', projectId, debouncedKey],
    queryFn: () => advancedSearchApi.searchPreview(projectId, payload).then(r => r.data),
    enabled: enabled && !!projectId,
    staleTime: 120 * 1000, // espelha o cache de 120s do servidor
    retry: false,           // 503 (Rust falhou) não deve ser retentado
  });
}

/**
 * Mutation para buscar preview sob demanda (painel pesado via botão).
 * Atualiza o cache da query principal ao retornar.
 */
export function useSearchPreviewMutation(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: SearchPreviewPayload) =>
      advancedSearchApi.searchPreview(projectId, payload).then(r => r.data),
    onSuccess: (data, variables) => {
      // Injeta no cache para o hook useSearchPreview reutilizar sem nova requisição
      queryClient.setQueryData(
        ['search-preview', projectId, JSON.stringify({
          selectedMesh: variables.selected_mesh,
          meshDefaultMode: variables.mesh_default_mode,
          panelFlags: variables.panel_flags,
        })],
        data,
      );
    },
  });
}
