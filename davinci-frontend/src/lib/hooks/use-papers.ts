import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { papersApi } from '@/lib/api/papers';
import type { PaperFilters } from '@/lib/types/paper';

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

export function useCuratePaper(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ paperId, data }: {
      paperId: number;
      data: { curation_status: string; exclusion_reason?: string; notes?: string };
    }) => papersApi.curate(projectId, paperId, data).then(r => r.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['papers', projectId] });
      queryClient.invalidateQueries({ queryKey: ['projects', projectId] });
    },
  });
}

export function useBulkCurate(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: { paper_ids: number[]; curation_status: string; exclusion_reason?: string }) =>
      papersApi.bulkCurate(projectId, data).then(r => r.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['papers', projectId] });
      queryClient.invalidateQueries({ queryKey: ['projects', projectId] });
    },
  });
}
