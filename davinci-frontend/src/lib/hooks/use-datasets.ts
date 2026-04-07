import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { datasetsApi } from '@/lib/api/datasets';
import type { DatasetFilters } from '@/lib/types/dataset';

export function useDatasets(projectId: string, filters?: DatasetFilters) {
  return useQuery({
    queryKey: ['datasets', projectId, filters],
    queryFn: () => datasetsApi.list(projectId, filters).then(r => r.data),
    enabled: !!projectId,
  });
}

export function useDataset(projectId: string, datasetId: number) {
  return useQuery({
    queryKey: ['datasets', projectId, datasetId],
    queryFn: () => datasetsApi.get(projectId, datasetId).then(r => r.data),
    enabled: !!projectId && !!datasetId,
  });
}

export function useBulkCurateDataset(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: { dataset_ids: number[]; curation_status: string; exclusion_reason?: string }) =>
      datasetsApi.bulkCurate(projectId, data).then(r => r.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['datasets', projectId] });
      queryClient.invalidateQueries({ queryKey: ['projects', projectId] });
    },
  });
}

export function useCurateDataset(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ datasetId, data }: {
      datasetId: number;
      data: { curation_status: string; exclusion_reason?: string; notes?: string };
    }) => datasetsApi.curate(projectId, datasetId, data).then(r => r.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['datasets', projectId] });
      queryClient.invalidateQueries({ queryKey: ['projects', projectId] });
    },
  });
}
