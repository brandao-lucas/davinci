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
  SraResolutionResponse,
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

// Job types de download suportados (GEO supplementary + FASTQ + resolucao SRA).
const DOWNLOAD_JOB_TYPES = new Set(['geo_supplementary_download', 'fastq_download']);
const SRA_RESOLUTION_JOB_TYPE = 'sra_resolution';

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

/**
 * Busca o job de resolucao SRA (sra_resolution) mais recente do projeto.
 * Polling a cada 2s enquanto houver job ativo (pending | running).
 * Usado para monitorar o progresso de POST .../resolve-sra/.
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
 * Payload de disparo de download — estende o DownloadDispatchRequest gerado com
 * os novos campos de escopo (MVP-A) mantendo retrocompatibilidade.
 *
 * scope:
 *   - 'all' (padrao): todas as runs do dataset.
 *   - 'included': so amostras com curation_status='included'.
 *   - 'manual': exatamente os sample_ids fornecidos.
 * sample_ids: obrigatorio quando scope='manual' (ids de ProjectSample/OmicSample).
 * destination: so 'server' no MVP; 'client' (Inc-1) retorna 400 no backend.
 */
export type TriggerDownloadPayload = Partial<DownloadDispatchRequest>;

/**
 * Dispara o download para o dataset (GEO supplementary ou FASTQ/SRA).
 *
 * Fluxo GEO (F1): body vazio → 202 direto.
 * Fluxo SRA/FASTQ (F2) e GEO com sra_runs resolvidos:
 *   - scope='all'|'included'|'manual' + sample_ids (quando manual).
 *   - Primeira chamada sem confirm → backend retorna 400 com DownloadQuotaPreview.
 *     O hook rejeita com DownloadQuotaError (httpStatus=400, confirm_required=true).
 *   - Componente exibe dialogo; ao confirmar, re-chama com { confirm: true }.
 *   - Se quota esgotada: 409 → DownloadQuotaError (httpStatus=409, confirm_required=false).
 *
 * O hook NAO exibe toast para erros de quota (400/409); deixa o componente decidir a UI.
 * Para erros inesperados (500, rede), exibe toast de erro generico.
 *
 * Apos sucesso (202): invalida queries de jobs, dataset-files e dataset-download-job.
 */
export function useTriggerDatasetDownload(projectId: string, datasetId: number) {
  const queryClient = useQueryClient();

  return useMutation<DownloadDispatchResponse, DownloadQuotaError | Error, TriggerDownloadPayload | undefined>({
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

/**
 * Dispara a resolucao GEO→SRA para um dataset GEO.
 * POST .../resolve-sra/ → 202 com IngestionJob (job_type='sra_resolution').
 *
 * O Rust le extra_metadata['relation'] de cada GSM, extrai SRX, resolve SRX→SRR
 * via ENA filereport e grava sra_runs nos GSMs.
 *
 * Apos sucesso (202): invalida queries de jobs e sra-resolution-job para iniciar polling.
 * Quando o job terminar (completed), os GSMs terao sra_runs — entao o componente
 * pode exibir o botao "Baixar dados (FASTQ)".
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
      // Invalida jobs gerais para refletir o novo job de resolucao.
      queryClient.invalidateQueries({ queryKey: ['jobs', projectId] });
      // Invalida sra-resolution-job para iniciar o polling.
      queryClient.invalidateQueries({
        queryKey: ['sra-resolution-job', projectId, datasetId],
      });
      // Invalida a query do dataset (lista + detalhe, por prefixo) para que sra_resolved
      // seja recarregado quando o job completar — a fonte de verdade de sra_resolved vem
      // do backend via esta query, nao do polling do job.
      queryClient.invalidateQueries({ queryKey: ['datasets', projectId] });
    },
    onError: (err) => {
      toast.error(extractApiErrorMessage(err, 'Falha ao iniciar resolucao SRA'));
    },
  });
}
