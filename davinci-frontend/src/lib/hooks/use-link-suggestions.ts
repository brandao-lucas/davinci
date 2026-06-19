import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { linksApi } from '@/lib/api/links';
import { papersApi } from '@/lib/api/papers';
import { datasetsApi } from '@/lib/api/datasets';
import { extractApiErrorMessage } from '@/lib/utils/api-error';
import type { LinkSuggestionFilters, OrphanLinkSuggestion } from '@/lib/types/links';

/**
 * Hook para buscar sugestões de órfãos (Nível 2 — read-only).
 *
 * Cada sugestão indica um vínculo global (DatasetPaperLink) onde apenas
 * uma das pontas já está curada no projeto:
 * - 'dataset_missing': paper já no projeto, dataset ainda não.
 * - 'paper_missing': dataset já no projeto, paper ainda não.
 */
export function useLinkSuggestions(projectId: string, filters?: LinkSuggestionFilters) {
  return useQuery({
    queryKey: ['link-suggestions', projectId, filters],
    queryFn: () => linksApi.suggestions(projectId, filters).then(r => r.data),
    enabled: !!projectId,
  });
}

/**
 * Hook de mutation para adicionar a ponta ausente de uma sugestão de órfão ao projeto.
 *
 * - `dataset_missing`: chama POST .../datasets/add_from_suggestion/ com dataset_id.
 * - `paper_missing`: chama POST .../papers/add_from_suggestion/ com pmid.
 *
 * Em ambos os casos o backend materializa o ProjectPaperDataset com confidence='auto'
 * de forma síncrona, portanto após o sucesso invalidamos:
 *   1. link-suggestions (o item sai da lista de sugestões)
 *   2. links (o vínculo aparece como confirmado/auto)
 *   3. papers ou datasets do projeto (ganhou um item novo)
 */
export function useAddFromSuggestion(projectId: string) {
  const queryClient = useQueryClient();

  return useMutation<unknown, Error, OrphanLinkSuggestion>({
    mutationFn: (suggestion: OrphanLinkSuggestion): Promise<unknown> => {
      if (suggestion.suggestion_type === 'dataset_missing') {
        return datasetsApi.addFromSuggestion(projectId, suggestion.dataset_id);
      }
      return papersApi.addFromSuggestion(projectId, suggestion.paper_pmid);
    },

    onSuccess: (_data, suggestion) => {
      const isDataset = suggestion.suggestion_type === 'dataset_missing';
      toast.success(
        isDataset
          ? 'Dataset adicionado ao projeto'
          : 'Paper adicionado ao projeto',
      );

      // Invalida sugestões (o item deve sair da lista)
      queryClient.invalidateQueries({ queryKey: ['link-suggestions', projectId] });
      // Invalida vínculos confirmados (o novo vínculo auto aparece)
      queryClient.invalidateQueries({ queryKey: ['links', projectId] });
      // Invalida a lista da entidade adicionada (papers ou datasets do projeto)
      if (isDataset) {
        queryClient.invalidateQueries({ queryKey: ['datasets', projectId] });
      } else {
        queryClient.invalidateQueries({ queryKey: ['papers', projectId] });
      }
    },

    onError: (err) => {
      toast.error(extractApiErrorMessage(err, 'Falha ao adicionar ao projeto'));
    },
  });
}
