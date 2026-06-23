import { useCallback } from 'react';
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
  FastqUrlListResponse,
  FastqUrlItem,
  PaginatedDatasetFileList,
  SraResolutionResponse,
  BatchDownloadRequest,
  BatchDownloadResponse,
  BatchDownloadQuotaPreview,
  BatchFastqUrlListResponse,
} from '@/lib/types/dataset';
import type { IngestionJob } from '@/lib/types/job';
import type { PaginatedResponse } from '@/lib/types/api';

// Statuses de download de arquivo que indicam operacao ativa (polling deve continuar).
const ACTIVE_DOWNLOAD_STATUSES = new Set(['pending', 'queued', 'downloading']);

// Statuses de job que indicam job ativo (polling deve continuar).
const ACTIVE_JOB_STATUSES = new Set(['pending', 'running']);

/**
 * Lista os DatasetFile de um dataset com polling condicional.
 * Repoll a cada 3s enquanto houver pelo menos um arquivo em status ativo.
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

// Job types de download suportados.
const DOWNLOAD_JOB_TYPES = new Set(['geo_supplementary_download', 'fastq_download']);
const SRA_RESOLUTION_JOB_TYPE = 'sra_resolution';

/**
 * Busca o job de download (geo_supplementary_download ou fastq_download) mais recente.
 * Polling a cada 2s enquanto houver job ativo.
 */
export function useDatasetDownloadJob(projectId: string, datasetId: number | null) {
  return useQuery<IngestionJob | null>({
    queryKey: ['dataset-download-job', projectId, datasetId],
    queryFn: async () => {
      const response = await jobsApi.list(projectId);
      const all: IngestionJob[] = (response.data as PaginatedResponse<IngestionJob>).results;
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

/**
 * Busca o job de resolucao SRA (sra_resolution) mais recente.
 * Polling a cada 2s enquanto houver job ativo.
 */
export function useSraResolutionJob(projectId: string, datasetId: number | null) {
  return useQuery<IngestionJob | null>({
    queryKey: ['sra-resolution-job', projectId, datasetId],
    queryFn: async () => {
      const response = await jobsApi.list(projectId);
      const all: IngestionJob[] = (response.data as PaginatedResponse<IngestionJob>).results;
      const resolutionJobs = all
        .filter((j) => j.job_type === SRA_RESOLUTION_JOB_TYPE)
        .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
      return resolutionJobs[0] ?? null;
    },
    enabled: !!projectId && !!datasetId,
    refetchInterval: (query) => {
      const job = query.state.data;
      if (!job) return false;
      return ACTIVE_JOB_STATUSES.has(job.status) ? 2000 : false;
    },
  });
}

// ---------------------------------------------------------------------------
// Erros estruturados de quota — single dataset
// ---------------------------------------------------------------------------

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

export { isDownloadQuotaError };

// ---------------------------------------------------------------------------
// Erros estruturados de quota — batch (Inc-5)
// ---------------------------------------------------------------------------

export interface BatchDownloadQuotaError extends Error {
  preview: BatchDownloadQuotaPreview;
  httpStatus: 400 | 409;
}

export function isBatchDownloadQuotaError(err: unknown): err is BatchDownloadQuotaError {
  return (
    err instanceof Error &&
    'preview' in err &&
    'httpStatus' in err
  );
}

// ---------------------------------------------------------------------------
// Resultado do trigger single — discrimina server (202) vs client (200)
// ---------------------------------------------------------------------------

export type TriggerDownloadPayload = Partial<DownloadDispatchRequest>;

export type TriggerDownloadResult =
  | { mode: 'server'; job: DownloadDispatchResponse }
  | { mode: 'client'; urls: FastqUrlListResponse };

/**
 * Dispara o download para o dataset (GEO supplementary ou FASTQ/SRA).
 *
 * destination='server': HTTP 202 → retorna { mode: 'server', job }.
 *   Fluxo de quota: 400 (confirm ausente) ou 409 (esgotada) → rejeita com DownloadQuotaError.
 * destination='client' (Inc-1): HTTP 200 → retorna { mode: 'client', urls }.
 *   Sem job, sem polling; o componente recebe as URLs e dispara downloads no browser.
 *
 * O hook discrimina o modo pelo status HTTP da resposta (200 vs 202).
 * Apos sucesso server: invalida jobs, dataset-files e dataset-download-job.
 * Apos sucesso client: nenhuma invalidação (sem estado no servidor).
 */
export function useTriggerDatasetDownload(projectId: string, datasetId: number) {
  const queryClient = useQueryClient();

  return useMutation<
    TriggerDownloadResult,
    DownloadQuotaError | Error,
    TriggerDownloadPayload | undefined
  >({
    mutationFn: async (body) => {
      try {
        const response = await datasetsApi.triggerDownload(projectId, datasetId, body);
        // HTTP 200 = destination='client' (FastqUrlListResponse)
        // HTTP 202 = destination='server' (DownloadDispatchResponse)
        if (response.status === 200) {
          return { mode: 'client', urls: response.data as FastqUrlListResponse };
        }
        return { mode: 'server', job: response.data as DownloadDispatchResponse };
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
    onSuccess: (result) => {
      if (result.mode === 'server') {
        toast.success('Download enfileirado. Acompanhe o progresso abaixo.');
        queryClient.invalidateQueries({ queryKey: ['jobs', projectId] });
        queryClient.invalidateQueries({ queryKey: ['dataset-files', projectId, datasetId] });
        queryClient.invalidateQueries({ queryKey: ['dataset-download-job', projectId, datasetId] });
      }
      // mode='client': sem estado no servidor para invalidar; componente lida com as URLs.
    },
    onError: (err) => {
      if (isDownloadQuotaError(err)) return;
      toast.error(extractApiErrorMessage(err, 'Falha ao iniciar download'));
    },
  });
}

// ---------------------------------------------------------------------------
// Inc-1: disparar downloads no browser a partir de FastqUrlItem[]
// ---------------------------------------------------------------------------

/**
 * Dispara downloads diretos no browser para uma lista de FastqUrlItem.
 * Cria um elemento <a download> oculto, clica programaticamente e o remove.
 * Para runs com múltiplos arquivos (fastq_url com ';'), dispara um por segmento.
 * Runs com has_public_fastq=false são ignoradas silenciosamente.
 *
 * Uso: const triggerBrowserDownloads = useClientFastqDownloader();
 *      triggerBrowserDownloads(runs);
 */
export function useClientFastqDownloader() {
  return useCallback((runs: FastqUrlItem[]) => {
    const publicRuns = runs.filter((r) => r.has_public_fastq && r.fastq_url);
    publicRuns.forEach((run) => {
      const urls = run.fastq_url.split(';').map((u) => u.trim()).filter(Boolean);
      const fileNames = run.file_name.split(';').map((f) => f.trim());
      urls.forEach((url, idx) => {
        const a = document.createElement('a');
        a.href = url;
        a.download = fileNames[idx] ?? run.run_accession;
        a.rel = 'noreferrer';
        a.style.display = 'none';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
      });
    });
  }, []);
}

// ---------------------------------------------------------------------------
// MVP-B: resolucao GEO→SRA
// ---------------------------------------------------------------------------

/**
 * Dispara a resolucao GEO→SRA para um dataset GEO.
 * POST .../resolve-sra/ → 202 com IngestionJob (job_type='sra_resolution').
 */
export function useResolveSraRuns(projectId: string, datasetId: number) {
  const queryClient = useQueryClient();

  return useMutation<SraResolutionResponse, Error, void>({
    mutationFn: async () => {
      const response = await datasetsApi.resolveSra(projectId, datasetId);
      return response.data;
    },
    onSuccess: () => {
      toast.success('Resolucao SRA enfileirada. O botao de download FASTQ aparecera apos a conclusao.');
      queryClient.invalidateQueries({ queryKey: ['jobs', projectId] });
      queryClient.invalidateQueries({ queryKey: ['sra-resolution-job', projectId, datasetId] });
      // Invalida datasets para recarregar sra_resolved (fonte de verdade do campo).
      queryClient.invalidateQueries({ queryKey: ['datasets', projectId] });
    },
    onError: (err) => {
      toast.error(extractApiErrorMessage(err, 'Falha ao iniciar resolucao SRA'));
    },
  });
}

// ---------------------------------------------------------------------------
// Inc-5: download em lote
// ---------------------------------------------------------------------------

export type TriggerBatchResult =
  | { mode: 'server'; response: BatchDownloadResponse }
  | { mode: 'client'; urls: BatchFastqUrlListResponse };

/**
 * Dispara download em lote para múltiplos datasets do projeto.
 * POST .../download-batch/.
 *
 * destination='server': HTTP 202 → { mode: 'server', response }.
 *   Fluxo de quota análogo ao single: 400 → BatchDownloadQuotaError (confirm_required=true),
 *   409 → BatchDownloadQuotaError (confirm_required=false).
 * destination='client': HTTP 200 → { mode: 'client', urls }.
 *
 * Apos sucesso server: invalida jobs do projeto.
 * Apos sucesso client: nenhuma invalidação.
 */
export function useTriggerBatchDownload(projectId: string) {
  const queryClient = useQueryClient();

  return useMutation<
    TriggerBatchResult,
    BatchDownloadQuotaError | Error,
    Partial<BatchDownloadRequest>
  >({
    mutationFn: async (body) => {
      try {
        const response = await datasetsApi.downloadBatch(projectId, body);
        if (response.status === 200) {
          return { mode: 'client', urls: response.data as BatchFastqUrlListResponse };
        }
        return { mode: 'server', response: response.data as BatchDownloadResponse };
      } catch (err) {
        if (err instanceof AxiosError) {
          const status = err.response?.status;
          if (status === 400 || status === 409) {
            const data = err.response?.data as BatchDownloadQuotaPreview | undefined;
            if (data && typeof data.used_bytes === 'number') {
              const quotaErr = Object.assign(
                new Error(data.detail ?? 'Confirmacao necessaria'),
                { preview: data, httpStatus: status as 400 | 409 },
              ) as BatchDownloadQuotaError;
              throw quotaErr;
            }
          }
        }
        throw err;
      }
    },
    onSuccess: (result) => {
      if (result.mode === 'server') {
        const { total_jobs } = result.response;
        toast.success(`${total_jobs} job(s) de download enfileirado(s).`);
        queryClient.invalidateQueries({ queryKey: ['jobs', projectId] });
      }
    },
    onError: (err) => {
      if (isBatchDownloadQuotaError(err)) return;
      toast.error(extractApiErrorMessage(err, 'Falha ao iniciar download em lote'));
    },
  });
}
