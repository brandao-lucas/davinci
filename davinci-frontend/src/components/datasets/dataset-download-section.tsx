'use client';

import { useState } from 'react';
import { Download, FileDown, Loader2, AlertCircle, CheckCircle2, Clock, HardDrive } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { Progress } from '@/components/ui/progress';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import {
  useDatasetFiles,
  useDatasetDownloadJob,
  useTriggerDatasetDownload,
  isDownloadQuotaError,
} from '@/lib/hooks/use-dataset-files';
import type { DatasetFile, DatasetFileDownloadStatus, DownloadQuotaPreview } from '@/lib/types/dataset';

interface DatasetDownloadSectionProps {
  projectId: string;
  datasetId: number;
  sourceDb: string;
}

// Formata bytes em unidade legivel (B, KB, MB, GB, TB).
function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '—';
  if (bytes === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  const clamped = Math.min(i, units.length - 1);
  const value = bytes / Math.pow(1024, clamped);
  return `${value.toFixed(clamped === 0 ? 0 : 1)} ${units[clamped]}`;
}

// Trunca checksum para exibicao compacta.
function truncateChecksum(checksum: string | null): string {
  if (!checksum) return '—';
  return checksum.length > 12 ? `${checksum.slice(0, 12)}…` : checksum;
}

// Configuracoes de badge por status de download.
const downloadStatusConfig: Record<
  DatasetFileDownloadStatus,
  { label: string; className: string }
> = {
  pending: { label: 'Pendente', className: 'bg-amber-100 text-amber-800' },
  queued: { label: 'Na fila', className: 'bg-blue-100 text-blue-800' },
  downloading: { label: 'Baixando', className: 'bg-indigo-100 text-indigo-800' },
  downloaded: { label: 'Baixado', className: 'bg-teal-100 text-teal-800' },
  failed: { label: 'Falhou', className: 'bg-red-100 text-red-800' },
};

function DownloadStatusBadge({ status }: { status: DatasetFileDownloadStatus }) {
  const config = downloadStatusConfig[status] ?? { label: status, className: '' };
  return (
    <Badge variant="outline" className={`text-xs ${config.className}`}>
      {config.label}
    </Badge>
  );
}

function FileRow({ file }: { file: DatasetFile }) {
  const canDownload =
    file.download_status === 'downloaded' && !!file.download_url;

  const showProgress =
    file.download_status === 'downloading' &&
    file.bytes_downloaded > 0 &&
    !!file.size_bytes;

  const progressPercent =
    showProgress && file.size_bytes
      ? Math.min(100, Math.round((file.bytes_downloaded / file.size_bytes) * 100))
      : 0;

  return (
    <div className="flex items-start justify-between gap-2 py-2 text-xs border-b last:border-b-0">
      <div className="min-w-0 flex-1 space-y-0.5">
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="font-mono truncate max-w-[180px]" title={file.accession}>
            {file.accession}
          </span>
          <Badge variant="secondary" className="text-xs shrink-0">
            {file.file_type}
          </Badge>
          <DownloadStatusBadge status={file.download_status} />
        </div>
        <div className="flex gap-3 text-muted-foreground">
          <span>{formatBytes(file.size_bytes)}</span>
          {file.checksum_md5 && (
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <span className="font-mono cursor-default">
                    MD5: {truncateChecksum(file.checksum_md5)}
                  </span>
                </TooltipTrigger>
                <TooltipContent>
                  <p className="font-mono text-xs">{file.checksum_md5}</p>
                </TooltipContent>
              </Tooltip>
            </TooltipProvider>
          )}
          {showProgress && (
            <span>
              {formatBytes(file.bytes_downloaded)} / {formatBytes(file.size_bytes)}
            </span>
          )}
        </div>
        {showProgress && (
          <Progress value={progressPercent} className="h-1 mt-1" />
        )}
      </div>
      <div className="shrink-0">
        {canDownload ? (
          <a
            href={file.download_url!}
            target="_blank"
            rel="noreferrer"
            download
          >
            <Button size="sm" variant="outline" className="h-7 px-2 text-xs gap-1">
              <FileDown className="h-3.5 w-3.5" />
              Baixar
            </Button>
          </a>
        ) : (
          <Button size="sm" variant="outline" className="h-7 px-2 text-xs" disabled>
            <FileDown className="h-3.5 w-3.5" />
          </Button>
        )}
      </div>
    </div>
  );
}

// Dialogo de confirmacao de quota para FASTQ/SRA (HTTP 400 com confirm_required=true).
interface FastqConfirmDialogProps {
  open: boolean;
  preview: DownloadQuotaPreview;
  isPending: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

function FastqConfirmDialog({
  open,
  preview,
  isPending,
  onConfirm,
  onCancel,
}: FastqConfirmDialogProps) {
  const usedPct = preview.quota_bytes > 0
    ? Math.min(100, Math.round((preview.used_bytes / preview.quota_bytes) * 100))
    : 0;

  return (
    <AlertDialog open={open}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle className="flex items-center gap-2">
            <HardDrive className="h-4 w-4 text-amber-600" />
            Confirmar download FASTQ
          </AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="space-y-3 text-sm text-foreground">
              <p className="text-muted-foreground">
                Arquivos FASTQ podem ter dezenas de GB. Confirme que deseja
                iniciar o download e consumir quota de armazenamento.
              </p>
              <div className="rounded-md border p-3 space-y-2 bg-muted/40">
                <div className="flex items-center justify-between text-xs">
                  <span className="text-muted-foreground">Quota utilizada</span>
                  <span className="font-medium">
                    {formatBytes(preview.used_bytes)} / {formatBytes(preview.quota_bytes)}
                  </span>
                </div>
                <Progress value={usedPct} className="h-1.5" />
                <p className="text-xs text-muted-foreground text-right">
                  {usedPct}% utilizado
                </p>
              </div>
              {preview.detail && (
                <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded p-2">
                  {preview.detail}
                </p>
              )}
            </div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel onClick={onCancel} disabled={isPending}>
            Cancelar
          </AlertDialogCancel>
          <AlertDialogAction
            onClick={onConfirm}
            disabled={isPending}
            className="gap-1.5"
          >
            {isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            Confirmar download
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

export function DatasetDownloadSection({
  projectId,
  datasetId,
  sourceDb,
}: DatasetDownloadSectionProps) {
  const isGeo = sourceDb.toLowerCase() === 'geo';
  const isSra = sourceDb.toLowerCase() === 'sra';

  // Estado do dialogo de confirmacao de quota (apenas para SRA/FASTQ).
  const [quotaPreview, setQuotaPreview] = useState<DownloadQuotaPreview | null>(null);
  // Mensagem de erro bloqueante (409 — quota esgotada).
  const [quotaBlockError, setQuotaBlockError] = useState<string | null>(null);

  const filesQuery = useDatasetFiles(projectId, isGeo || isSra ? datasetId : null);
  const jobQuery = useDatasetDownloadJob(projectId, isGeo || isSra ? datasetId : null);
  const triggerMutation = useTriggerDatasetDownload(projectId, datasetId);

  const job = jobQuery.data;
  const isJobActive =
    job?.status === 'pending' || job?.status === 'running';

  const files = filesQuery.data?.results ?? [];
  const hasFiles = files.length > 0;

  // Calcula progresso agregado: % de arquivos no status downloaded.
  const downloadedCount = files.filter((f) => f.download_status === 'downloaded').length;
  const progressPercent = hasFiles ? Math.round((downloadedCount / files.length) * 100) : 0;

  // GEO (F1): disparo direto, sem dialogo.
  function handleGeoDownload() {
    triggerMutation.mutate(undefined);
  }

  // SRA/FASTQ (F2): primeira chamada sem confirm; em caso de 400 confirm_required abre dialogo.
  async function handleSraDownload() {
    setQuotaBlockError(null);
    try {
      await triggerMutation.mutateAsync({ confirm: false });
      // 202 direto (nao esperado para SRA sem confirm, mas trata se backend mudar).
    } catch (err) {
      if (isDownloadQuotaError(err)) {
        if (err.httpStatus === 400 && err.preview.confirm_required) {
          // Abre dialogo de confirmacao de quota.
          setQuotaPreview(err.preview);
        } else if (err.httpStatus === 409 && !err.preview.confirm_required) {
          // Quota esgotada — bloqueio sem opcao de confirmar.
          setQuotaBlockError(
            err.preview.detail ??
              `Quota esgotada: ${formatBytes(err.preview.used_bytes)} usados de ${formatBytes(err.preview.quota_bytes)}.`,
          );
        }
      }
      // Erros inesperados ja foram tratados pelo onError do hook (toast).
    }
  }

  // Confirmacao no dialogo: re-dispara com confirm=true.
  async function handleConfirmDownload() {
    try {
      await triggerMutation.mutateAsync({ confirm: true });
      setQuotaPreview(null);
    } catch (err) {
      if (isDownloadQuotaError(err) && err.httpStatus === 409) {
        setQuotaPreview(null);
        setQuotaBlockError(
          err.preview.detail ??
            `Quota esgotada: ${formatBytes(err.preview.used_bytes)} usados de ${formatBytes(err.preview.quota_bytes)}.`,
        );
      }
      // Outros erros: toast ja exibido pelo onError do hook.
    }
  }

  function handleCancelDialog() {
    setQuotaPreview(null);
  }

  // Dataset nao suportado por download direto neste fluxo.
  if (!isGeo && !isSra) {
    return (
      <div className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">
        Download de arquivos disponivel apenas para datasets GEO (F1) e SRA/FASTQ (F2).
      </div>
    );
  }

  const sectionLabel = isGeo ? 'Dados suplementares' : 'Dados FASTQ (SRA)';
  const buttonLabel = isGeo ? 'Baixar dados' : 'Baixar dados (FASTQ)';

  return (
    <div className="space-y-3">
      {/* Dialogo de confirmacao de quota — apenas para SRA */}
      {quotaPreview && (
        <FastqConfirmDialog
          open={!!quotaPreview}
          preview={quotaPreview}
          isPending={triggerMutation.isPending}
          onConfirm={handleConfirmDownload}
          onCancel={handleCancelDialog}
        />
      )}

      {/* Cabecalho da secao */}
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm font-medium">{sectionLabel}</p>
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <span>
                <Button
                  size="sm"
                  variant="outline"
                  className="h-7 px-2 text-xs gap-1"
                  onClick={isGeo ? handleGeoDownload : handleSraDownload}
                  disabled={isJobActive || triggerMutation.isPending}
                >
                  {isJobActive || triggerMutation.isPending ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Download className="h-3.5 w-3.5" />
                  )}
                  {buttonLabel}
                </Button>
              </span>
            </TooltipTrigger>
            {isJobActive && (
              <TooltipContent>
                <p className="text-xs">Download em andamento. Aguarde a conclusao.</p>
              </TooltipContent>
            )}
          </Tooltip>
        </TooltipProvider>
      </div>

      {/* Erro bloqueante de quota esgotada (409) */}
      {quotaBlockError && (
        <div className="flex items-start gap-2 rounded-md border border-red-200 bg-red-50 p-2 text-xs text-red-700">
          <AlertCircle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
          <span>{quotaBlockError}</span>
        </div>
      )}

      {/* Status do job ativo */}
      {job && (
        <div className="rounded-md border p-2 text-xs space-y-1.5">
          <div className="flex items-center gap-1.5">
            {isJobActive ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin text-blue-600 shrink-0" />
            ) : job.status === 'completed' ? (
              <CheckCircle2 className="h-3.5 w-3.5 text-teal-600 shrink-0" />
            ) : job.status === 'failed' ? (
              <AlertCircle className="h-3.5 w-3.5 text-red-600 shrink-0" />
            ) : (
              <Clock className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
            )}
            <span className="text-muted-foreground">
              {isJobActive
                ? 'Download em andamento…'
                : job.status === 'completed'
                ? `Download concluido — ${job.records_inserted} arquivo(s) importado(s)`
                : job.status === 'failed'
                ? 'Download falhou'
                : `Job ${job.status}`}
            </span>
          </div>
          {job.status === 'failed' && job.error_message && (
            <p className="text-red-700 ml-5">{job.error_message}</p>
          )}
          {hasFiles && isJobActive && (
            <div className="ml-5 space-y-0.5">
              <Progress value={progressPercent} className="h-1.5" />
              <p className="text-muted-foreground">
                {downloadedCount} / {files.length} arquivo(s)
              </p>
            </div>
          )}
        </div>
      )}

      {/* Lista de arquivos */}
      {hasFiles && (
        <div>
          {files.map((file) => (
            <FileRow key={file.id} file={file} />
          ))}
        </div>
      )}

      {!hasFiles && !isJobActive && !jobQuery.isLoading && (
        <p className="text-xs text-muted-foreground">
          Nenhum arquivo baixado. Clique em &quot;{buttonLabel}&quot; para iniciar.
        </p>
      )}

      {filesQuery.isLoading && (
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          Carregando arquivos…
        </div>
      )}
    </div>
  );
}
