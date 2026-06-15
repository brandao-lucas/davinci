import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { samplesApi } from '@/lib/api/samples';
import { extractApiErrorMessage } from '@/lib/utils/api-error';
import type { SampleFilters } from '@/lib/types/sample';

// ── List by dataset ──────────────────────────────────────────────────────────

export function useSamplesByDataset(
  projectId: string,
  datasetId: number | string,
  filters?: SampleFilters
) {
  return useQuery({
    queryKey: ['samples', 'dataset', projectId, datasetId, filters],
    queryFn: () => samplesApi.listByDataset(projectId, datasetId, filters).then((r) => r.data),
    enabled: !!projectId && !!datasetId,
  });
}

// ── List by project ──────────────────────────────────────────────────────────

export function useSamplesByProject(projectId: string, filters?: SampleFilters) {
  return useQuery({
    queryKey: ['samples', 'project', projectId, filters],
    queryFn: () => samplesApi.listByProject(projectId, filters).then((r) => r.data),
    enabled: !!projectId,
  });
}

// ── Detail ───────────────────────────────────────────────────────────────────

export function useSample(projectId: string, sampleId: number) {
  return useQuery({
    queryKey: ['samples', projectId, sampleId],
    queryFn: () => samplesApi.get(projectId, sampleId).then((r) => r.data),
    enabled: !!projectId && !!sampleId,
  });
}

// ── Individual curation ──────────────────────────────────────────────────────

export function useCurateSample(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      sampleId,
      data,
    }: {
      sampleId: number;
      data: {
        curation_status: string;
        exclusion_reason?: string;
        notes?: string;
        relevance_score?: number | null;
      };
    }) => samplesApi.curate(projectId, sampleId, data).then((r) => r.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['samples', 'dataset', projectId] });
      queryClient.invalidateQueries({ queryKey: ['samples', 'project', projectId] });
      queryClient.invalidateQueries({ queryKey: ['projects', projectId] });
      toast.success('Curadoria atualizada');
    },
    onError: (err) => {
      toast.error(extractApiErrorMessage(err, 'Falha ao atualizar curadoria'));
    },
  });
}

// ── Bulk curation ────────────────────────────────────────────────────────────

export function useBulkCurateSample(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: {
      sample_ids: number[];
      curation_status: string;
      exclusion_reason?: string;
    }) => samplesApi.bulkCurate(projectId, data).then((r) => r.data),
    onSuccess: (data: { updated?: number }) => {
      queryClient.invalidateQueries({ queryKey: ['samples', 'dataset', projectId] });
      queryClient.invalidateQueries({ queryKey: ['samples', 'project', projectId] });
      queryClient.invalidateQueries({ queryKey: ['projects', projectId] });
      toast.success(`${data.updated ?? 'Samples'} atualizados`);
    },
    onError: (err) => {
      toast.error(extractApiErrorMessage(err, 'Falha na curadoria em lote'));
    },
  });
}
