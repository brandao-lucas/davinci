import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { datasetsApi } from '@/lib/api/datasets';
import { extractApiErrorMessage } from '@/lib/utils/api-error';
import type { DatasetFilters, ProjectDatasetDetail } from '@/lib/types/dataset';

export function useDatasets(projectId: string, filters?: DatasetFilters) {
  return useQuery({
    queryKey: ['datasets', projectId, filters],
    queryFn: () => datasetsApi.list(projectId, filters).then(r => r.data),
    enabled: !!projectId,
  });
}

export function useDataset(projectId: string, datasetId: number) {
  return useQuery<ProjectDatasetDetail>({
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
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['datasets', projectId] });
      queryClient.invalidateQueries({ queryKey: ['projects', projectId] });
      toast.success(`${data.updated} datasets atualizados`);
    },
    onError: (err) => {
      toast.error(extractApiErrorMessage(err, 'Falha na curadoria em lote'));
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
      toast.success('Curadoria atualizada');
    },
    onError: (err) => {
      toast.error(extractApiErrorMessage(err, 'Falha ao atualizar curadoria'));
    },
  });
}
