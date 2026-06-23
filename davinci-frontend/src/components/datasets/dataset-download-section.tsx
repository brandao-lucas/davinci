'use client';

import { useState } from 'react';
import {
  Download,
  FileDown,
  Loader2,
  AlertCircle,
  CheckCircle2,
  Clock,
  HardDrive,
  RefreshCw,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { Progress } from '@/components/ui/progress';
import { Checkbox } from '@/components/ui/checkbox';
import { Label } from '@/components/ui/label';
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
  useSraResolutionJob,
  useResolveSraRuns,
  isDownloadQuotaError,
} from '@/lib/hooks/use-dataset-files';
import { useSamplesByDataset } from '@/lib/hooks/use-samples';
import type { DatasetFile, DatasetFileDownloadStatus, DownloadQuotaPreview } from '@/lib/types/dataset';
import type { ScopeEnum } from '@/lib/types/dataset';

interface DatasetDownloadSectionProps {
  projectId: string;
  datasetId: number;
  sourceDb: string;
  /** Para datasets GEO: true = runs SRA ja resolvidas (FASTQ disponivel); false = precisa resolver primeiro. */
  sraResolved?: boolean;
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

// Opcao do seletor de escopo de download.
interface ScopeOption {
  value: ScopeEnum;
  label: string;
  description: string;
}

const SCOPE_OPTIONS: ScopeOption[] = [
  {
    value: 'all',
    label: 'Todas as amostras',
    description: 'Baixa todas as runs do dataset.',
  },
  {
    value: 'included',
    label: 'Apenas incluidas',
    description: 'So amostras com status "incluida" no projeto.',
  },
  {
    value: 'manual',
    label: 'Selecionar manualmente',
    description: 'Escolha quais amostras baixar.',
  },
];

// Seletor de escopo: radio buttons estilizados + lista de checkboxes para modo manual.
interface ScopeSelectorProps {
  projectId: string;
  datasetId: number;
  scope: ScopeEnum;
  selectedSampleIds: number[];
  onScopeChange: (scope: ScopeEnum) => void;
  onSampleToggle: (id: number, checked: boolean) => void;
  onSelectAll: (ids: number[]) => void;
  onClearAll: () => void;
}

function ScopeSelector({
  projectId,
  datasetId,
  scope,
  selectedSampleIds,
  onScopeChange,
  onSampleToggle,
  onSelectAll,
  onClearAll,
}: ScopeSelectorProps) {
  const samplesQuery = useSamplesByDataset(
    projectId,
    datasetId,
    scope === 'manual' ? undefined : undefined,
  );
  const samples = samplesQuery.data?.results ?? [];
  const allIds = samples.map((s) => s.id);

  const isAllSelected = allIds.length > 0 && allIds.every((id) => selectedSampleIds.includes(id));

  return (
    <div className="space-y-2 rounded-md border p-3 bg-muted/30">
      <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
        Escopo do download
      </p>
      <div className="space-y-1.5">
        {SCOPE_OPTIONS.map((opt) => (
          <label
            key={opt.value}
            className="flex items-start gap-2 cursor-pointer group"
          >
            <input
              type="radio"
              name={`scope-${datasetId}`}
              value={opt.value}
              checked={scope === opt.value}
              onChange={() => onScopeChange(opt.value)}
              className="mt-0.5 accent-primary shrink-0"
            />
            <span className="text-xs leading-snug">
              <span className="font-medium">{opt.label}</span>
              <span className="text-muted-foreground"> — {opt.description}</span>
            </span>
          </label>
        ))}
      </div>

      {scope === 'manual' && (
        <div className="mt-2 space-y-1.5">
          <div className="flex items-center justify-between">
            <p className="text-xs text-muted-foreground">
              {samplesQuery.isLoading
                ? 'Carregando amostras…'
                : `${selectedSampleIds.length} de ${samples.length} selecionada(s)`}
            </p>
            {samples.length > 0 && (
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => onSelectAll(allIds)}
                  className="text-xs text-primary hover:underline"
                  disabled={isAllSelected}
                >
                  Todas
                </button>
                <button
                  type="button"
                  onClick={onClearAll}
                  className="text-xs text-muted-foreground hover:underline"
                  disabled={selectedSampleIds.length === 0}
                >
                  Limpar
                </button>
              </div>
            )}
          </div>

          {samplesQuery.isLoading && (
            <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Loader2 className="h-3 w-3 animate-spin" />
              Carregando…
            </div>
          )}

          {!samplesQuery.isLoading && samples.length === 0 && (
            <p className="text-xs text-muted-foreground">
              Nenhuma amostra encontrada para este dataset.
            </p>
          )}

          {samples.length > 0 && (
            <div className="max-h-40 overflow-y-auto space-y-1 pr-1">
              {samples.map((sample) => (
                <div key={sample.id} className="flex items-start gap-2">
                  <Checkbox
                    id={`sample-${sample.id}`}
                    checked={selectedSampleIds.includes(sample.id)}
                    onCheckedChange={(checked) =>
                      onSampleToggle(sample.id, checked === true)
                    }
                    className="mt-0.5 shrink-0"
                  />
                  <Label
                    htmlFor={`sample-${sample.id}`}
                    className="text-xs font-normal leading-snug cursor-pointer"
                  >
                    <span className="font-mono">{sample.accession}</span>
                    {sample.title && (
                      <span className="text-muted-foreground ml-1.5 truncate">
                        {sample.title}
                      </span>
                    )}
                    {sample.curation_status && (
                      <Badge
                        variant="outline"
                        className="ml-1 text-xs py-0 h-4"
                      >
                        {sample.curation_status}
                      </Badge>
                    )}
                  </Label>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function DatasetDownloadSection({
  projectId,
  datasetId,
  sourceDb,
  sraResolved = false,
}: DatasetDownloadSectionProps) {
  const isGeo = sourceDb.toLowerCase() === 'geo';
  const isSra = sourceDb.toLowerCase() === 'sra';

  // --- Escopo de download (MVP-A) ---
  const [scope, setScope] = useState<ScopeEnum>('all');
  const [selectedSampleIds, setSelectedSampleIds] = useState<number[]>([]);

  // --- Dialogo de confirmacao de quota (apenas para SRA/FASTQ) ---
  const [quotaPreview, setQuotaPreview] = useState<DownloadQuotaPreview | null>(null);
  // Mensagem de erro bloqueante (409 — quota esgotada).
  const [quotaBlockError, setQuotaBlockError] = useState<string | null>(null);

  const filesQuery = useDatasetFiles(projectId, isGeo || isSra ? datasetId : null);
  const jobQuery = useDatasetDownloadJob(projectId, isGeo || isSra ? datasetId : null);
  const triggerMutation = useTriggerDatasetDownload(projectId, datasetId);

  // --- Resolucao SRA para datasets GEO (MVP-B) ---
  const sraResolutionJobQuery = useSraResolutionJob(projectId, isGeo ? datasetId : null);
  const resolveSraMutation = useResolveSraRuns(projectId, datasetId);

  const job = jobQuery.data;
  const isJobActive =
    job?.status === 'pending' || job?.status === 'running';

  const sraResolutionJob = sraResolutionJobQuery.data;
  const isSraResolutionActive =
    sraResolutionJob?.status === 'pending' || sraResolutionJob?.status === 'running';

  const files = filesQuery.data?.results ?? [];
  const hasFiles = files.length > 0;

  // Calcula progresso agregado: % de arquivos no status downloaded.
  const downloadedCount = files.filter((f) => f.download_status === 'downloaded').length;
  const progressPercent = hasFiles ? Math.round((downloadedCount / files.length) * 100) : 0;

  // Monta o payload de scope para o trigger.
  function buildScopePayload(): { scope: ScopeEnum; sample_ids?: number[] } {
    if (scope === 'manual') {
      return { scope: 'manual', sample_ids: selectedSampleIds };
    }
    return { scope };
  }

  // GEO (F1): disparo direto sem dialogo de quota.
  function handleGeoDownload() {
    triggerMutation.mutate({ ...buildScopePayload() });
  }

  // SRA/FASTQ (F2): primeira chamada sem confirm; em caso de 400 confirm_required abre dialogo.
  async function handleSraDownload() {
    setQuotaBlockError(null);
    try {
      await triggerMutation.mutateAsync({ confirm: false, ...buildScopePayload() });
    } catch (err) {
      if (isDownloadQuotaError(err)) {
        if (err.httpStatus === 400 && err.preview.confirm_required) {
          setQuotaPreview(err.preview);
        } else if (err.httpStatus === 409 && !err.preview.confirm_required) {
          setQuotaBlockError(
            err.preview.detail ??
              `Quota esgotada: ${formatBytes(err.preview.used_bytes)} usados de ${formatBytes(err.preview.quota_bytes)}.`,
          );
        }
      }
    }
  }

  // Confirmacao no dialogo: re-dispara com confirm=true.
  async function handleConfirmDownload() {
    try {
      await triggerMutation.mutateAsync({ confirm: true, ...buildScopePayload() });
      setQuotaPreview(null);
    } catch (err) {
      if (isDownloadQuotaError(err) && err.httpStatus === 409) {
        setQuotaPreview(null);
        setQuotaBlockError(
          err.preview.detail ??
            `Quota esgotada: ${formatBytes(err.preview.used_bytes)} usados de ${formatBytes(err.preview.quota_bytes)}.`,
        );
      }
    }
  }

  function handleCancelDialog() {
    setQuotaPreview(null);
  }

  // Handlers de selecao de amostras (modo manual).
  function handleSampleToggle(id: number, checked: boolean) {
    setSelectedSampleIds((prev) =>
      checked ? [...prev, id] : prev.filter((x) => x !== id),
    );
  }

  function handleSelectAll(ids: number[]) {
    setSelectedSampleIds(ids);
  }

  function handleClearAll() {
    setSelectedSampleIds([]);
  }

  // Reseta selecao ao trocar escopo.
  function handleScopeChange(newScope: ScopeEnum) {
    setScope(newScope);
    if (newScope !== 'manual') {
      setSelectedSampleIds([]);
    }
  }

  // Dataset nao suportado por download direto neste fluxo.
  if (!isGeo && !isSra) {
    return (
      <div className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">
        Download de arquivos disponivel apenas para datasets GEO (F1) e SRA/FASTQ (F2).
      </div>
    );
  }

  // Para datasets GEO: fonte de verdade e o campo sra_resolved do backend.
  // true  = runs SRA ja resolvidas → exibir "Baixar dados (FASTQ)" com seletor de escopo.
  // false = precisa rodar resolve-sra primeiro → exibir "Resolver dados SRA".
  // Durante a resolucao (job ativo), o botao fica desabilitado com spinner.
  const geoCanDownloadFastq = isGeo && sraResolved;

  // Para datasets GEO sem sra_resolved: exibir botao de resolucao SRA em vez de FASTQ.
  const showGeoResolveSra = isGeo && !sraResolved;

  // Rotulos dinamicos.
  let sectionLabel = isGeo ? 'Dados suplementares' : 'Dados FASTQ (SRA)';
  let buttonLabel = isGeo ? 'Baixar dados' : 'Baixar dados (FASTQ)';

  // Para GEO com FASTQ resolvido: usar rotulo de FASTQ.
  if (geoCanDownloadFastq) {
    sectionLabel = 'Dados FASTQ (GEO → SRA)';
    buttonLabel = 'Baixar dados (FASTQ)';
  }

  // Para SRA ou GEO com FASTQ: mostrar seletor de escopo (MVP-A).
  const showScopeSelector = isSra || geoCanDownloadFastq;

  // Validacao: modo manual requer ao menos uma amostra selecionada.
  const isTriggerDisabled =
    isJobActive ||
    triggerMutation.isPending ||
    (scope === 'manual' && selectedSampleIds.length === 0);

  return (
    <div className="space-y-3">
      {/* Dialogo de confirmacao de quota — apenas para SRA/FASTQ */}
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

        {/* Botao de resolucao SRA (MVP-B) — exibido para datasets GEO sem sra_runs */}
        {showGeoResolveSra && (
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <span>
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-7 px-2 text-xs gap-1"
                    onClick={() => resolveSraMutation.mutate()}
                    disabled={isSraResolutionActive || resolveSraMutation.isPending}
                  >
                    {isSraResolutionActive || resolveSraMutation.isPending ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <RefreshCw className="h-3.5 w-3.5" />
                    )}
                    Resolver dados SRA
                  </Button>
                </span>
              </TooltipTrigger>
              <TooltipContent>
                <p className="text-xs">
                  Mapeia cada GSM para suas runs SRA (SRX → SRR) via ENA.
                  Necessario antes de baixar arquivos FASTQ.
                </p>
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        )}

        {/* Botao de download — exibido para SRA ou GEO com sra_runs resolvidos */}
        {!showGeoResolveSra && (
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <span>
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-7 px-2 text-xs gap-1"
                    onClick={isGeo ? handleGeoDownload : handleSraDownload}
                    disabled={isTriggerDisabled}
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
              {scope === 'manual' && selectedSampleIds.length === 0 && !isJobActive && (
                <TooltipContent>
                  <p className="text-xs">Selecione ao menos uma amostra para continuar.</p>
                </TooltipContent>
              )}
            </Tooltip>
          </TooltipProvider>
        )}
      </div>

      {/* Status da resolucao SRA (MVP-B) — exibido enquanto o job existir */}
      {isGeo && sraResolutionJob && (
        <div className="rounded-md border p-2 text-xs space-y-1.5">
          <div className="flex items-center gap-1.5">
            {isSraResolutionActive ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin text-blue-600 shrink-0" />
            ) : sraResolved ? (
              <CheckCircle2 className="h-3.5 w-3.5 text-teal-600 shrink-0" />
            ) : sraResolutionJob.status === 'failed' ? (
              <AlertCircle className="h-3.5 w-3.5 text-red-600 shrink-0" />
            ) : (
              <Clock className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
            )}
            <span className="text-muted-foreground">
              {isSraResolutionActive
                ? 'Resolvendo runs SRA…'
                : sraResolved
                ? `Resolucao SRA concluida — ${sraResolutionJob.records_inserted} run(s) mapeada(s)`
                : sraResolutionJob.status === 'failed'
                ? 'Resolucao SRA falhou'
                : `Resolucao SRA: ${sraResolutionJob.status}`}
            </span>
          </div>
          {sraResolutionJob.status === 'failed' && sraResolutionJob.error_message && (
            <p className="text-red-700 ml-5">{sraResolutionJob.error_message}</p>
          )}
        </div>
      )}

      {/* Seletor de escopo (MVP-A) — exibido para SRA e GEO com sra_runs resolvidos */}
      {showScopeSelector && (
        <ScopeSelector
          projectId={projectId}
          datasetId={datasetId}
          scope={scope}
          selectedSampleIds={selectedSampleIds}
          onScopeChange={handleScopeChange}
          onSampleToggle={handleSampleToggle}
          onSelectAll={handleSelectAll}
          onClearAll={handleClearAll}
        />
      )}

      {/* Erro bloqueante de quota esgotada (409) */}
      {quotaBlockError && (
        <div className="flex items-start gap-2 rounded-md border border-red-200 bg-red-50 p-2 text-xs text-red-700">
          <AlertCircle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
          <span>{quotaBlockError}</span>
        </div>
      )}

      {/* Status do job de download ativo */}
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

      {!hasFiles && !isJobActive && !jobQuery.isLoading && !showGeoResolveSra && (
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
