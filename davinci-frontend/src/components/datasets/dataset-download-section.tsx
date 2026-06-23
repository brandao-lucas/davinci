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
  Monitor,
  Server,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { Progress } from '@/components/ui/progress';
import { Checkbox } from '@/components/ui/checkbox';
import { Label } from '@/components/ui/label';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
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
  useClientFastqDownloader,
  isDownloadQuotaError,
} from '@/lib/hooks/use-dataset-files';
import { useSamplesByDataset } from '@/lib/hooks/use-samples';
import type {
  DatasetFile,
  DatasetFileDownloadStatus,
  DownloadQuotaPreview,
  ScopeEnum,
  SampleFilter,
  FastqUrlItem,
  FastqUrlListResponse,
} from '@/lib/types/dataset';

interface DatasetDownloadSectionProps {
  projectId: string;
  datasetId: number;
  sourceDb: string;
  /** Para datasets GEO: true = runs SRA ja resolvidas (FASTQ disponivel); false = precisa resolver primeiro. */
  sraResolved?: boolean;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '—';
  if (bytes === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  const clamped = Math.min(i, units.length - 1);
  const value = bytes / Math.pow(1024, clamped);
  return `${value.toFixed(clamped === 0 ? 0 : 1)} ${units[clamped]}`;
}

function truncateChecksum(checksum: string | null): string {
  if (!checksum) return '—';
  return checksum.length > 12 ? `${checksum.slice(0, 12)}…` : checksum;
}

// ---------------------------------------------------------------------------
// Badges de status de arquivo (download servidor)
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Linha de arquivo salvo no servidor
// ---------------------------------------------------------------------------

function FileRow({ file }: { file: DatasetFile }) {
  const canDownload = file.download_status === 'downloaded' && !!file.download_url;
  const showProgress =
    file.download_status === 'downloading' && file.bytes_downloaded > 0 && !!file.size_bytes;
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
        {showProgress && <Progress value={progressPercent} className="h-1 mt-1" />}
      </div>
      <div className="shrink-0">
        {canDownload ? (
          <a href={file.download_url!} target="_blank" rel="noreferrer" download>
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

// ---------------------------------------------------------------------------
// Inc-1: lista de URLs (destination='client')
// ---------------------------------------------------------------------------

function FastqUrlRow({
  item,
  onDownload,
}: {
  item: FastqUrlItem;
  onDownload: (item: FastqUrlItem) => void;
}) {
  const urls = item.fastq_url ? item.fastq_url.split(';').map((u) => u.trim()).filter(Boolean) : [];
  const fileCount = urls.length;

  return (
    <div className="flex items-start justify-between gap-2 py-2 text-xs border-b last:border-b-0">
      <div className="min-w-0 flex-1 space-y-0.5">
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="font-mono truncate max-w-[180px]" title={item.run_accession}>
            {item.run_accession}
          </span>
          {fileCount > 1 && (
            <Badge variant="secondary" className="text-xs shrink-0">
              {fileCount} arquivos
            </Badge>
          )}
          {!item.has_public_fastq && (
            <Badge variant="outline" className="text-xs shrink-0 bg-amber-50 text-amber-700">
              BAM-only
            </Badge>
          )}
        </div>
        <div className="flex gap-3 text-muted-foreground">
          <span>{formatBytes(item.size_bytes)}</span>
          {item.checksum_md5 && (
            <span className="font-mono">MD5: {truncateChecksum(item.checksum_md5)}</span>
          )}
        </div>
      </div>
      <div className="shrink-0">
        {item.has_public_fastq ? (
          <Button
            size="sm"
            variant="outline"
            className="h-7 px-2 text-xs gap-1"
            onClick={() => onDownload(item)}
          >
            <FileDown className="h-3.5 w-3.5" />
            Baixar
          </Button>
        ) : (
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <span>
                  <Button size="sm" variant="outline" className="h-7 px-2 text-xs" disabled>
                    <FileDown className="h-3.5 w-3.5" />
                  </Button>
                </span>
              </TooltipTrigger>
              <TooltipContent>
                <p className="text-xs">
                  Run BAM-only — sem FASTQ publicado na ENA. Use destino Servidor.
                </p>
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        )}
      </div>
    </div>
  );
}

function FastqUrlList({
  urlResponse,
  onDownloadAll,
  onDownloadSingle,
}: {
  urlResponse: FastqUrlListResponse;
  onDownloadAll: () => void;
  onDownloadSingle: (item: FastqUrlItem) => void;
}) {
  const publicCount = urlResponse.runs.filter((r) => r.has_public_fastq).length;

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>
          {urlResponse.total_runs} run(s) · {formatBytes(urlResponse.bytes_total)}
          {urlResponse.bam_only_count > 0 && (
            <span className="ml-1.5 text-amber-600">
              ({urlResponse.bam_only_count} BAM-only)
            </span>
          )}
        </span>
        {publicCount > 0 && (
          <Button
            size="sm"
            variant="outline"
            className="h-7 px-2 text-xs gap-1"
            onClick={onDownloadAll}
          >
            <Download className="h-3.5 w-3.5" />
            Baixar todos ({publicCount})
          </Button>
        )}
      </div>
      <div>
        {urlResponse.runs.map((item) => (
          <FastqUrlRow key={item.run_accession} item={item} onDownload={onDownloadSingle} />
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Dialogo de confirmacao de quota
// ---------------------------------------------------------------------------

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
          <AlertDialogAction onClick={onConfirm} disabled={isPending} className="gap-1.5">
            {isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            Confirmar download
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

// ---------------------------------------------------------------------------
// Seletor de destino: Servidor vs Meu computador (Inc-1)
// Exibido apenas para FASTQ (SRA ou GEO resolvido).
// ---------------------------------------------------------------------------

type DestinationMode = 'server' | 'client';

function DestinationSelector({
  value,
  onChange,
}: {
  value: DestinationMode;
  onChange: (v: DestinationMode) => void;
}) {
  return (
    <div className="flex gap-1 rounded-md border p-0.5 w-fit bg-muted/30">
      {([
        { key: 'server', icon: Server, label: 'Servidor' },
        { key: 'client', icon: Monitor, label: 'Meu computador' },
      ] as const).map(({ key, icon: Icon, label }) => (
        <button
          key={key}
          type="button"
          onClick={() => onChange(key)}
          className={`flex items-center gap-1.5 px-2 py-1 text-xs rounded transition-colors ${
            value === key
              ? 'bg-background shadow-sm font-medium'
              : 'text-muted-foreground hover:text-foreground'
          }`}
        >
          <Icon className="h-3.5 w-3.5" />
          {label}
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Seletor de escopo (MVP-A + Inc-2)
// ---------------------------------------------------------------------------

interface ScopeOption {
  value: ScopeEnum;
  label: string;
  description: string;
}

const SCOPE_OPTIONS: ScopeOption[] = [
  { value: 'all', label: 'Todas as amostras', description: 'Baixa todas as runs do dataset.' },
  { value: 'included', label: 'Apenas incluidas', description: 'So amostras com status "incluida".' },
  { value: 'manual', label: 'Selecionar manualmente', description: 'Escolha quais amostras baixar.' },
  { value: 'filter', label: 'Filtrar', description: 'Filtra por status, organismo ou plataforma.' },
];

const CURATION_STATUSES = ['pending', 'included', 'excluded', 'maybe'];

interface ScopeSelectorProps {
  projectId: string;
  datasetId: number;
  scope: ScopeEnum;
  selectedSampleIds: number[];
  sampleFilter: SampleFilter;
  onScopeChange: (scope: ScopeEnum) => void;
  onSampleToggle: (id: number, checked: boolean) => void;
  onSelectAll: (ids: number[]) => void;
  onClearAll: () => void;
  onFilterChange: (f: SampleFilter) => void;
}

function ScopeSelector({
  projectId,
  datasetId,
  scope,
  selectedSampleIds,
  sampleFilter,
  onScopeChange,
  onSampleToggle,
  onSelectAll,
  onClearAll,
  onFilterChange,
}: ScopeSelectorProps) {
  const samplesQuery = useSamplesByDataset(projectId, datasetId);
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
          <label key={opt.value} className="flex items-start gap-2 cursor-pointer">
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

      {/* Modo manual: lista de amostras com checkboxes */}
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
                  className="text-xs text-primary hover:underline disabled:opacity-50"
                  disabled={isAllSelected}
                >
                  Todas
                </button>
                <button
                  type="button"
                  onClick={onClearAll}
                  className="text-xs text-muted-foreground hover:underline disabled:opacity-50"
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
            <p className="text-xs text-muted-foreground">Nenhuma amostra encontrada.</p>
          )}
          {samples.length > 0 && (
            <div className="max-h-40 overflow-y-auto space-y-1 pr-1">
              {samples.map((sample) => (
                <div key={sample.id} className="flex items-start gap-2">
                  <Checkbox
                    id={`sample-${sample.id}`}
                    checked={selectedSampleIds.includes(sample.id)}
                    onCheckedChange={(checked) => onSampleToggle(sample.id, checked === true)}
                    className="mt-0.5 shrink-0"
                  />
                  <Label
                    htmlFor={`sample-${sample.id}`}
                    className="text-xs font-normal leading-snug cursor-pointer"
                  >
                    <span className="font-mono">{sample.accession}</span>
                    {sample.title && (
                      <span className="text-muted-foreground ml-1.5">{sample.title}</span>
                    )}
                    {sample.curation_status && (
                      <Badge variant="outline" className="ml-1 text-xs py-0 h-4">
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

      {/* Modo filter (Inc-2): inputs para SampleFilter */}
      {scope === 'filter' && (
        <div className="mt-2 space-y-2">
          <div className="space-y-1">
            <p className="text-xs text-muted-foreground font-medium">Status de curadoria</p>
            <div className="flex flex-wrap gap-2">
              {CURATION_STATUSES.map((status) => {
                const checked = sampleFilter.curation_status?.includes(status) ?? false;
                return (
                  <label key={status} className="flex items-center gap-1.5 cursor-pointer text-xs">
                    <Checkbox
                      checked={checked}
                      onCheckedChange={(v) => {
                        const current = sampleFilter.curation_status ?? [];
                        const next = v
                          ? [...current, status]
                          : current.filter((s) => s !== status);
                        onFilterChange({
                          ...sampleFilter,
                          curation_status: next.length > 0 ? next : null,
                        });
                      }}
                    />
                    {status}
                  </label>
                );
              })}
            </div>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div className="space-y-1">
              <Label className="text-xs">Organismo</Label>
              <Input
                className="h-7 text-xs"
                placeholder="Homo sapiens…"
                value={sampleFilter.organism ?? ''}
                onChange={(e) =>
                  onFilterChange({ ...sampleFilter, organism: e.target.value || null })
                }
              />
            </div>
            <div className="space-y-1">
              <Label className="text-xs">Plataforma</Label>
              <Input
                className="h-7 text-xs"
                placeholder="GPL570…"
                value={sampleFilter.platform ?? ''}
                onChange={(e) =>
                  onFilterChange({ ...sampleFilter, platform: e.target.value || null })
                }
              />
            </div>
          </div>
          <p className="text-xs text-muted-foreground">
            Ao menos um campo deve ser preenchido. Campos combinados com AND.
          </p>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Componente principal
// ---------------------------------------------------------------------------

export function DatasetDownloadSection({
  projectId,
  datasetId,
  sourceDb,
  sraResolved = false,
}: DatasetDownloadSectionProps) {
  const isGeo = sourceDb.toLowerCase() === 'geo';
  const isSra = sourceDb.toLowerCase() === 'sra';

  // --- Escopo de download (MVP-A + Inc-2) ---
  const [scope, setScope] = useState<ScopeEnum>('all');
  const [selectedSampleIds, setSelectedSampleIds] = useState<number[]>([]);
  const [sampleFilter, setSampleFilter] = useState<SampleFilter>({});

  // --- Destino (Inc-1) ---
  const [destination, setDestination] = useState<'server' | 'client'>('server');

  // --- Resultado client mode (Inc-1) ---
  const [clientUrls, setClientUrls] = useState<FastqUrlListResponse | null>(null);

  // --- Dialogo de confirmacao de quota (apenas para server) ---
  const [quotaPreview, setQuotaPreview] = useState<DownloadQuotaPreview | null>(null);
  const [quotaBlockError, setQuotaBlockError] = useState<string | null>(null);

  const filesQuery = useDatasetFiles(projectId, isGeo || isSra ? datasetId : null);
  const jobQuery = useDatasetDownloadJob(projectId, isGeo || isSra ? datasetId : null);
  const triggerMutation = useTriggerDatasetDownload(projectId, datasetId);
  const triggerBrowserDownloads = useClientFastqDownloader();

  // --- Resolucao SRA para datasets GEO (MVP-B) ---
  const sraResolutionJobQuery = useSraResolutionJob(projectId, isGeo ? datasetId : null);
  const resolveSraMutation = useResolveSraRuns(projectId, datasetId);

  const job = jobQuery.data;
  const isJobActive = job?.status === 'pending' || job?.status === 'running';

  const sraResolutionJob = sraResolutionJobQuery.data;
  const isSraResolutionActive =
    sraResolutionJob?.status === 'pending' || sraResolutionJob?.status === 'running';

  const files = filesQuery.data?.results ?? [];
  const hasFiles = files.length > 0;
  const downloadedCount = files.filter((f) => f.download_status === 'downloaded').length;
  const progressPercent = hasFiles ? Math.round((downloadedCount / files.length) * 100) : 0;

  // Determina se o filtro de amostra tem ao menos um campo preenchido.
  const filterHasValue =
    (sampleFilter.curation_status ?? []).length > 0 ||
    !!(sampleFilter.organism?.trim()) ||
    !!(sampleFilter.platform?.trim());

  // Monta o payload de escopo para o trigger.
  function buildScopePayload() {
    if (scope === 'manual') return { scope, sample_ids: selectedSampleIds };
    if (scope === 'filter') return { scope, filters: sampleFilter };
    return { scope };
  }

  // GEO (F1): disparo direto sem dialogo de quota.
  function handleGeoDownload() {
    setClientUrls(null);
    triggerMutation.mutate({ ...buildScopePayload() });
  }

  // SRA/FASTQ: primeira chamada sem confirm; 400 abre dialogo; 409 mostra bloqueio.
  // Para destination='client': nenhum confirm necessario — retorna 200 direto.
  async function handleSraDownload() {
    setQuotaBlockError(null);
    setClientUrls(null);
    try {
      const result = await triggerMutation.mutateAsync({
        confirm: destination === 'server' ? false : undefined,
        destination,
        ...buildScopePayload(),
      });
      if (result.mode === 'client') {
        setClientUrls(result.urls);
      }
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

  async function handleConfirmDownload() {
    try {
      const result = await triggerMutation.mutateAsync({
        confirm: true,
        destination,
        ...buildScopePayload(),
      });
      setQuotaPreview(null);
      if (result.mode === 'client') {
        setClientUrls(result.urls);
      }
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

  function handleSampleToggle(id: number, checked: boolean) {
    setSelectedSampleIds((prev) =>
      checked ? [...prev, id] : prev.filter((x) => x !== id),
    );
  }

  function handleScopeChange(newScope: ScopeEnum) {
    setScope(newScope);
    if (newScope !== 'manual') setSelectedSampleIds([]);
    if (newScope !== 'filter') setSampleFilter({});
    setClientUrls(null);
  }

  function handleDestinationChange(newDest: 'server' | 'client') {
    setDestination(newDest);
    setClientUrls(null);
    setQuotaPreview(null);
    setQuotaBlockError(null);
  }

  // Dataset nao suportado.
  if (!isGeo && !isSra) {
    return (
      <div className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">
        Download de arquivos disponivel apenas para datasets GEO (F1) e SRA/FASTQ (F2).
      </div>
    );
  }

  // Logica de estado GEO (MVP-B).
  const geoCanDownloadFastq = isGeo && sraResolved;
  const showGeoResolveSra = isGeo && !sraResolved;

  // Rotulos dinamicos.
  let sectionLabel = isGeo ? 'Dados suplementares' : 'Dados FASTQ (SRA)';
  let buttonLabel = isGeo ? 'Baixar dados' : 'Baixar dados (FASTQ)';
  if (geoCanDownloadFastq) {
    sectionLabel = 'Dados FASTQ (GEO → SRA)';
    buttonLabel = 'Baixar dados (FASTQ)';
  }

  // Seletor de escopo: apenas para FASTQ (SRA ou GEO resolvido).
  const showScopeSelector = isSra || geoCanDownloadFastq;

  // Seletor de destino: apenas para FASTQ (sem GEO suplementar).
  const showDestinationSelector = showScopeSelector;

  // Validacao do trigger.
  const isTriggerDisabled =
    isJobActive ||
    triggerMutation.isPending ||
    (scope === 'manual' && selectedSampleIds.length === 0) ||
    (scope === 'filter' && !filterHasValue);

  return (
    <div className="space-y-3">
      {/* Dialogo de confirmacao de quota (server) */}
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

        {/* Botao de resolucao SRA (MVP-B) */}
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

        {/* Botao de download (server ou client) */}
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
              {scope === 'filter' && !filterHasValue && !isJobActive && (
                <TooltipContent>
                  <p className="text-xs">Preencha ao menos um filtro para continuar.</p>
                </TooltipContent>
              )}
            </Tooltip>
          </TooltipProvider>
        )}
      </div>

      {/* Seletor de destino (Inc-1) */}
      {showDestinationSelector && (
        <DestinationSelector value={destination} onChange={handleDestinationChange} />
      )}

      {/* Status da resolucao SRA (MVP-B) */}
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

      {/* Seletor de escopo (MVP-A + Inc-2) */}
      {showScopeSelector && (
        <ScopeSelector
          projectId={projectId}
          datasetId={datasetId}
          scope={scope}
          selectedSampleIds={selectedSampleIds}
          sampleFilter={sampleFilter}
          onScopeChange={handleScopeChange}
          onSampleToggle={handleSampleToggle}
          onSelectAll={(ids) => setSelectedSampleIds(ids)}
          onClearAll={() => setSelectedSampleIds([])}
          onFilterChange={setSampleFilter}
        />
      )}

      {/* Erro bloqueante de quota (409) */}
      {quotaBlockError && (
        <div className="flex items-start gap-2 rounded-md border border-red-200 bg-red-50 p-2 text-xs text-red-700">
          <AlertCircle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
          <span>{quotaBlockError}</span>
        </div>
      )}

      {/* Status do job de download (server) */}
      {job && destination === 'server' && (
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

      {/* Lista de arquivos salvos no servidor */}
      {hasFiles && destination === 'server' && (
        <div>
          {files.map((file) => (
            <FileRow key={file.id} file={file} />
          ))}
        </div>
      )}

      {!hasFiles && destination === 'server' && !isJobActive && !jobQuery.isLoading && !showGeoResolveSra && (
        <p className="text-xs text-muted-foreground">
          Nenhum arquivo baixado. Clique em &quot;{buttonLabel}&quot; para iniciar.
        </p>
      )}

      {filesQuery.isLoading && destination === 'server' && (
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          Carregando arquivos…
        </div>
      )}

      {/* Lista de URLs (client mode, Inc-1) */}
      {clientUrls && destination === 'client' && (
        <FastqUrlList
          urlResponse={clientUrls}
          onDownloadAll={() => triggerBrowserDownloads(clientUrls.runs)}
          onDownloadSingle={(item) => triggerBrowserDownloads([item])}
        />
      )}
    </div>
  );
}
