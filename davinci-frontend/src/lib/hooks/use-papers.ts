import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { papersApi } from '@/lib/api/papers';
import { extractApiErrorMessage } from '@/lib/utils/api-error';
import type { PaperFilters, Paper } from '@/lib/types/paper';
import type { PaginatedResponse } from '@/lib/types/api';

export function usePapers(projectId: string, filters?: PaperFilters) {
  return useQuery({
    queryKey: ['papers', projectId, filters],
    queryFn: () => papersApi.list(projectId, filters).then(r => r.data),
    enabled: !!projectId,
  });
}

export function usePaper(projectId: string, paperId: number) {
  return useQuery({
    queryKey: ['papers', projectId, paperId],
    queryFn: () => papersApi.get(projectId, paperId).then(r => r.data),
    enabled: !!projectId && !!paperId,
  });
}

// Predicate que cobre todas as variações de queryKey ['papers', projectId, ...]
// (inclui filtros variáveis na posição 2 e queries de paper individual)
function papersPredicate(projectId: string) {
  return (query: { queryKey: readonly unknown[] }) =>
    Array.isArray(query.queryKey) &&
    query.queryKey[0] === 'papers' &&
    query.queryKey[1] === projectId;
}

// Aplica patch de curadoria em uma entrada de lista paginada, preservando
// todos os campos de auditoria existentes (Regra #2).
function applyPatchToPaper(
  paper: Paper,
  patch: { curation_status: string; exclusion_reason?: string; notes?: string },
): Paper {
  return {
    ...paper,
    curation_status: patch.curation_status as Paper['curation_status'],
    // Preserva curated_at existente; o backend vai sobrescrever no next refetch
    curated_at: paper.curated_at,
    // Atualiza exclusion_reason e notes apenas quando explicitamente enviados;
    // caso contrário mantém o valor existente para não apagar trilha de auditoria.
    exclusion_reason:
      patch.exclusion_reason !== undefined
        ? patch.exclusion_reason
        : paper.exclusion_reason,
    notes: patch.notes !== undefined ? patch.notes : paper.notes,
  };
}

export function useCuratePaper(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ paperId, data }: {
      paperId: number;
      data: { curation_status: string; exclusion_reason?: string; notes?: string };
    }) => papersApi.curate(projectId, paperId, data).then(r => r.data),

    onMutate: async ({ paperId, data }) => {
      // Cancela refetches em voo para não sobrescrever o patch otimista.
      await queryClient.cancelQueries({ predicate: papersPredicate(projectId) });

      // Snapshot de todas as queries de papers do projeto (predicate por prefixo).
      const snapshot = queryClient.getQueriesData<PaginatedResponse<Paper> | Paper>({
        predicate: papersPredicate(projectId),
      });

      // Aplica patch otimista em todas as queries de lista paginada e
      // na query de detalhe individual (se presente no cache).
      queryClient.setQueriesData<PaginatedResponse<Paper> | Paper>(
        { predicate: papersPredicate(projectId) },
        (cached) => {
          if (!cached) return cached;

          // Query de lista paginada
          if ('results' in cached) {
            return {
              ...cached,
              results: cached.results.map((p) =>
                p.id === paperId ? applyPatchToPaper(p, data) : p,
              ),
            };
          }

          // Query de paper individual
          if ('id' in cached && cached.id === paperId) {
            return applyPatchToPaper(cached as Paper, data);
          }

          return cached;
        },
      );

      return { snapshot };
    },

    onError: (err, _vars, context) => {
      // Rollback: restaura o snapshot completo de todas as queries afetadas.
      if (context?.snapshot) {
        for (const [queryKey, data] of context.snapshot) {
          queryClient.setQueryData(queryKey, data);
        }
      }
      toast.error(extractApiErrorMessage(err, 'Falha ao atualizar curadoria'));
    },

    onSuccess: () => {
      toast.success('Curadoria atualizada');
    },

    onSettled: () => {
      // Contadores agregados do projeto (leve — não bloqueia UI).
      queryClient.invalidateQueries({ queryKey: ['projects', projectId] });
    },
  });
}

export function useBulkCurate(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: { paper_ids: number[]; curation_status: string; exclusion_reason?: string }) =>
      papersApi.bulkCurate(projectId, data).then(r => r.data),

    onMutate: async (data) => {
      const idSet = new Set(data.paper_ids);

      await queryClient.cancelQueries({ predicate: papersPredicate(projectId) });

      const snapshot = queryClient.getQueriesData<PaginatedResponse<Paper> | Paper>({
        predicate: papersPredicate(projectId),
      });

      queryClient.setQueriesData<PaginatedResponse<Paper> | Paper>(
        { predicate: papersPredicate(projectId) },
        (cached) => {
          if (!cached) return cached;

          if ('results' in cached) {
            return {
              ...cached,
              results: cached.results.map((p) =>
                idSet.has(p.id) ? applyPatchToPaper(p, data) : p,
              ),
            };
          }

          // Paper individual no cache: atualiza se estiver no conjunto
          if ('id' in cached && idSet.has((cached as Paper).id)) {
            return applyPatchToPaper(cached as Paper, data);
          }

          return cached;
        },
      );

      return { snapshot };
    },

    onError: (err, _vars, context) => {
      if (context?.snapshot) {
        for (const [queryKey, data] of context.snapshot) {
          queryClient.setQueryData(queryKey, data);
        }
      }
      toast.error(extractApiErrorMessage(err, 'Falha na curadoria em lote'));
    },

    onSuccess: (responseData) => {
      toast.success(`${responseData.updated} papers atualizados`);
    },

    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['projects', projectId] });
    },
  });
}
