import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { curationQueueApi } from '@/lib/api/curation-queue';
import { extractApiErrorMessage } from '@/lib/utils/api-error';
import type { CurationQueueResolveInput } from '@/lib/types/curation-queue';

/** Chave base para queries da fila de curadoria */
const queueKey = (projectId: string) => ['curation-queue', projectId] as const;

/**
 * Lista os items da fila de curadoria manual do projeto.
 * Retorna datasets com has_control_group classificado-indeterminado (score < 0.5).
 */
export function useCurationQueue(projectId: string) {
  return useQuery({
    queryKey: queueKey(projectId),
    queryFn: () => curationQueueApi.list(projectId).then((r) => r.data),
    enabled: !!projectId,
  });
}

/**
 * Resolve um item da fila de curadoria manualmente.
 * Após sucesso: invalida a query da fila e exibe toast de confirmação.
 */
export function useResolveCurationItem(projectId: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      projectDatasetId,
      data,
    }: {
      projectDatasetId: number;
      data: CurationQueueResolveInput;
    }) => curationQueueApi.resolve(projectId, projectDatasetId, data).then((r) => r.data),

    onSuccess: (_result, variables) => {
      const label = variables.data.has_control_group === 'yes' ? 'Com grupo controle' : 'Sem grupo controle';
      toast.success(`Curadoria registrada: ${label}`);
      // Invalida a fila para remover o item resolvido
      queryClient.invalidateQueries({ queryKey: queueKey(projectId) });
    },

    onError: (err) => {
      toast.error(extractApiErrorMessage(err, 'Falha ao registrar curadoria'));
    },
  });
}
