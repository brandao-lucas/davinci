import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { toast } from 'sonner';
import { jobsApi } from '@/lib/api/jobs';
import { extractApiErrorMessage } from '@/lib/utils/api-error';
import type { IngestionJob } from '@/lib/types/job';
import type { PaginatedResponse } from '@/lib/types/api';

const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled']);

export function useJobs(projectId: string) {
  return useQuery({
    queryKey: ['jobs', projectId],
    queryFn: () => jobsApi.list(projectId).then(r => r.data),
    enabled: !!projectId,
  });
}

export function useJobPolling(projectId: string, jobId: string) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ['jobs', projectId, jobId],
    queryFn: () => jobsApi.get(projectId, jobId).then(r => r.data),
    enabled: !!jobId,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (status === 'pending' || status === 'running') return 2000;
      return false;
    },
  });

  // When this job reaches a terminal state, refresh the jobs list once so that
  // useJobs reflects the updated status (and any auto-chained jobs created by
  // the backend, e.g. geo_search after pubmed_search completes).
  // This runs in an effect (not inside refetchInterval) and targets the list
  // query exactly, so it never re-invalidates this polling query — avoiding the
  // invalidate -> refetch -> invalidate loop that would freeze the UI.
  const status = query.data?.status;
  const currentJob = query.data;
  useEffect(() => {
    if (status && TERMINAL_STATUSES.has(status)) {
      // Patch imediato: reflete o status final do job atual na lista sem aguardar
      // o refetch assíncrono provocado pela invalidação abaixo. O patch substitui
      // apenas a entrada do job cujo id bate; outros jobs (incluindo encadeados
      // criados no backend) só aparecem após o refetch completo da lista — por
      // isso a invalidação exact=true é MANTIDA (não removida).
      if (currentJob) {
        queryClient.setQueryData<PaginatedResponse<IngestionJob>>(
          ['jobs', projectId],
          (cached) => {
            if (!cached) return cached;
            const alreadyUpdated = cached.results.some(
              (j) => j.id === currentJob.id && j.status === currentJob.status,
            );
            // Evita re-renderização desnecessária se o cache já está atualizado.
            if (alreadyUpdated) return cached;
            return {
              ...cached,
              results: cached.results.map((j) =>
                j.id === currentJob.id ? { ...j, ...currentJob } : j,
              ),
            };
          },
        );
      }

      // Invalida a query de lista exata para capturar jobs encadeados (geo_search
      // etc.) criados pelo backend após a conclusão deste job.
      // exact: true garante que esta query de polling (jobId) não seja
      // re-invalidada, prevenindo o loop invalidate→refetch.
      queryClient.invalidateQueries({ queryKey: ['jobs', projectId], exact: true });
    }
  }, [status, projectId, queryClient, currentJob]);

  return query;
}

export function useCancelJob(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (jobId: string) => jobsApi.cancel(projectId, jobId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['jobs', projectId] });
      toast.success('Job cancelado');
    },
    onError: (err) => {
      toast.error(extractApiErrorMessage(err, 'Falha ao cancelar job'));
    },
  });
}
