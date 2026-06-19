import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { AxiosError } from 'axios';
import { datasetsApi } from '@/lib/api/datasets';
import { jobsApi } from '@/lib/api/jobs';
import { extractApiErrorMessage } from '@/lib/utils/api-error';
import type {
  DatasetFile,
  DownloadDispatchRequest,
  DownloadDispatchResponse,
  DownloadQuotaPreview,
  PaginatedDatasetFileList,
} from '@/lib/types/dataset';
import type { IngestionJob } from '@/lib/types/job';
import type { PaginatedResponse } from '@/lib/types/api';

// Statuses de download de arquivo que indicam operacao ativa (polling deve continuar).
const ACTIVE_DOWNLOAD_STATUSES = new Set(['pending', 'queued', 'downloading']);

// Statuses de job que indicam job ativo (polling deve continuar).
const ACTIVE_JOB_STATUSES = new Set(['pending', 'running']);

/**
 * Lista os DatasetFile de um dataset com polling condicional.
 * Repoll a cada 3s enquanto houver pelo menos um arquivo em status ativo
 * (pending | queued | downloading).
 */
export function useDatasetFiles(projectId: string, datasetId: number | null) {
  return useQuery<PaginatedDatasetFileList>({
    queryKey: ['dataset-files', projectId, datasetId],
    queryFn: () =>
      datasetsApi.listFiles(projectId, datasetId!).then((r) => r.data),
    enabled: !!projectId && !!datasetId,
    refetchInterval: (query) => {
      const results = query.state.data?.results ?? [];
      const hasActive = results.some((f: DatasetFile) =>
        ACTIVE_DOWNLOAD_STATUSES.has(f.download_status),
      );
      return hasActive ? 3000 : false;
    },
  });
}

// Job types de download suportados (GEO supplementary + FASTQ).
const DOWNLOAD_JOB_TYPES = new Set(['geo_supplementary_download', 'fastq_download']);

/**
 * Busca o job de download (geo_supplementary_download ou fastq_download) mais recente do projeto.
 * Polling a cada 2s enquanto houver job ativo (pending | running).
 *
 * Nota: o endpoint de jobs nao tem filtro por dataset_id, por isso filtramos
 * por job_type no frontend e pegamos o job mais recente.
 */
export function useDatasetDownloadJob(projectId: string, datasetId: number | null) {
  return useQuery<IngestionJob | null>({
    queryKey: ['dataset-download-job', projectId, datasetId],
    queryFn: async () => {
      const response = await jobsApi.list(projectId);
      const all: IngestionJob[] = (response.data as PaginatedResponse<IngestionJob>).results;
      // Filtra pelos job_types de download e pega o mais recente (maior created_at).
      const downloadJobs = all
        .filter((j) => DOWNLOAD_JOB_TYPES.has(j.job_type))
        .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
      return downloadJobs[0] ?? null;
    },
    enabled: !!projectId && !!datasetId,
    refetchInterval: (query) => {
      const job = query.state.data;
      if (!job) return false;
      return ACTIVE_JOB_STATUSES.has(job.status) ? 2000 : false;
    },
  });
}

// Erro estruturado retornado pelo hook quando o backend exige confirmacao (HTTP 400)
// ou quota esgotada (HTTP 409). O componente usa isso para abrir o dialogo ou mostrar
// o bloqueio, sem precisar inspecionar o AxiosError diretamente.
export interface DownloadQuotaError extends Error {
  preview: DownloadQuotaPreview;
  httpStatus: 400 | 409;
}

function isDownloadQuotaError(err: unknown): err is DownloadQuotaError {
  return (
    err instanceof Error &&
    'preview' in err &&
    'httpStatus' in err
  );
}

// Re-exporta para uso nos componentes sem precisar importar AxiosError.
export { isDownloadQuotaError };

/**
 * Dispara o download para o dataset (GEO supplementary ou FASTQ/SRA).
 *
 * Fluxo GEO (F1): body vazio → 202 direto.
 * Fluxo SRA/FASTQ (F2):
 *   - Primeira chamada sem confirm → backend retorna 400 com DownloadQuotaPreview.
 *     O hook rejeita com DownloadQuotaError (httpStatus=400, confirm_required=true).
 *   - Componente exibe diálogo; ao confirmar, re-chama com { confirm: true }.
 *   - Se quota esgotada: 409 → DownloadQuotaError (httpStatus=409, confirm_required=false).
 *
 * O hook NAO exibe toast para erros de quota (400/409); deixa o componente decidir a UI.
 * Para erros inesperados (500, rede), exibe toast de erro genérico.
 *
 * Apos sucesso (202): invalida queries de jobs, dataset-files e dataset-download-job.
 */
export function useTriggerDatasetDownload(projectId: string, datasetId: number) {
  const queryClient = useQueryClient();

  return useMutation<DownloadDispatchResponse, DownloadQuotaError | Error, Partial<DownloadDispatchRequest> | undefined>({
    mutationFn: async (body) => {
      try {
        const response = await datasetsApi.triggerDownload(projectId, datasetId, body);
        return response.data;
      } catch (err) {
        if (err instanceof AxiosError) {
          const status = err.response?.status;
          if (status === 400 || status === 409) {
            const data = err.response?.data as DownloadQuotaPreview | undefined;
            if (data && typeof data.used_bytes === 'number') {
              const quotaErr = Object.assign(
                new Error(data.detail ?? 'Confirmacao necessaria'),
                { preview: data, httpStatus: status as 400 | 409 },
              ) as DownloadQuotaError;
              throw quotaErr;
            }
          }
        }
        throw err;
      }
    },
    onSuccess: () => {
      toast.success('Download enfileirado. Acompanhe o progresso abaixo.');
      // Invalida a lista geral de jobs para refletir o novo job.
      queryClient.invalidateQueries({ queryKey: ['jobs', projectId] });
      // Invalida dataset-files para refletir qualquer mudanca de status.
      queryClient.invalidateQueries({
        queryKey: ['dataset-files', projectId, datasetId],
      });
      // Invalida dataset-download-job para iniciar o polling do job recem-criado.
      queryClient.invalidateQueries({
        queryKey: ['dataset-download-job', projectId, datasetId],
      });
    },
    onError: (err) => {
      // Erros de quota (400/409) sao tratados pelo componente — nao exibir toast.
      if (isDownloadQuotaError(err)) return;
      toast.error(extractApiErrorMessage(err, 'Falha ao iniciar download'));
    },
  });
}
