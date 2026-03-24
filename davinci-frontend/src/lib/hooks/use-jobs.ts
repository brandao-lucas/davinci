import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { jobsApi } from '@/lib/api/jobs';

export function useJobs(projectId: string) {
  return useQuery({
    queryKey: ['jobs', projectId],
    queryFn: () => jobsApi.list(projectId).then(r => r.data),
    enabled: !!projectId,
  });
}

export function useJobPolling(projectId: string, jobId: string) {
  return useQuery({
    queryKey: ['jobs', projectId, jobId],
    queryFn: () => jobsApi.get(projectId, jobId).then(r => r.data),
    enabled: !!jobId,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (status === 'pending' || status === 'running') return 2000;
      return false;
    },
  });
}

export function useCancelJob(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (jobId: string) => jobsApi.cancel(projectId, jobId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['jobs', projectId] });
    },
  });
}
