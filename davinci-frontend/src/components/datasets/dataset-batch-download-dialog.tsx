'use client';

import { useState } from 'react';
import { Download, HardDrive, Loader2, Monitor, Server, AlertCircle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  useTriggerBatchDownload,
  useClientFastqDownloader,
  isBatchDownloadQuotaError,
  type TriggerBatchResult,
} from '@/lib/hooks/use-dataset-files';
import type {
  BatchDownloadQuotaPreview,
  BatchDownloadScopeEnum,
  BatchFastqUrlListResponse,
} from '@/lib/types/dataset';

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

// ---------------------------------------------------------------------------
// Seletor de destino inline
// ---------------------------------------------------------------------------

type DestinationMode = 'server' | 'client';

function BatchDestinationSelector({
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
          className={`flex items-center gap-1.5 px-3 py-1.5 text-sm rounded transition-colors ${
            value === key
              ? 'bg-background shadow-sm font-medium'
              : 'text-muted-foreground hover:text-foreground'
          }`}
        >
          <Icon className="h-4 w-4" />
          {label}
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Preview de quota para o lote
// ---------------------------------------------------------------------------

function BatchQuotaPreviewPanel({
  preview,
  isPending,
  onConfirm,
  onCancel,
}: {
  preview: BatchDownloadQuotaPreview;
  isPending: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const usedPct = preview.quota_bytes > 0
    ? Math.min(100, Math.round((preview.used_bytes / preview.quota_bytes) * 100))
    : 0;

  const datasetsPreview = preview.datasets_preview as Array<{
    dataset_accession?: string;
    sample_count?: number;
  }>;

  return (
    <div className="space-y-3">
      <div className="rounded-md border p-3 space-y-2 bg-muted/40">
        <div className="flex items-center justify-between text-sm">
          <span className="text-muted-foreground">Quota utilizada</span>
          <span className="font-medium">
            {formatBytes(preview.used_bytes)} / {formatBytes(preview.quota_bytes)}
          </span>
        </div>
        <Progress value={usedPct} className="h-1.5" />
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span>{usedPct}% utilizado</span>
          <span>Estimado: {formatBytes(preview.estimated_bytes)}</span>
        </div>
      </div>

      {datasetsPreview.length > 0 && (
        <div className="space-y-1">
          <p className="text-xs font-medium text-muted-foreground">Datasets no lote</p>
          <div className="max-h-32 overflow-y-auto space-y-0.5">
            {datasetsPreview.map((d, i) => (
              <div key={i} className="flex items-center justify-between text-xs text-muted-foreground">
                <span className="font-mono">{d.dataset_accession ?? '—'}</span>
                <span>{d.sample_count != null ? `${d.sample_count} amostra(s)` : ''}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {preview.detail && (
        <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded p-2">
          {preview.detail}
        </p>
      )}

      <div className="flex justify-end gap-2 pt-1">
        <Button variant="outline" size="sm" onClick={onCancel} disabled={isPending}>
          Cancelar
        </Button>
        <Button size="sm" onClick={onConfirm} disabled={isPending} className="gap-1.5">
          {isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
          Confirmar download em lote
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Resultado client-mode no dialog
// ---------------------------------------------------------------------------

function BatchClientResult({
  result,
  onDownloadAll,
}: {
  result: BatchFastqUrlListResponse;
  onDownloadAll: () => void;
}) {
  const publicRuns = result.datasets.flatMap((d) =>
    d.runs.filter((r) => r.has_public_fastq),
  );
  const bamOnlyTotal = result.datasets.reduce((acc, d) => acc + d.bam_only_count, 0);

  return (
    <div className="space-y-3">
      <div className="rounded-md border p-3 text-sm space-y-1 bg-muted/40">
        <div className="flex justify-between">
          <span className="text-muted-foreground">Datasets</span>
          <span className="font-medium">{result.total_datasets}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-muted-foreground">Runs com FASTQ publico</span>
          <span className="font-medium">{publicRuns.length}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-muted-foreground">Tamanho total estimado</span>
          <span className="font-medium">{formatBytes(result.bytes_total)}</span>
        </div>
        {bamOnlyTotal > 0 && (
          <div className="flex justify-between text-amber-600">
            <span>Runs BAM-only (ignoradas)</span>
            <span>{bamOnlyTotal}</span>
          </div>
        )}
      </div>

      {result.skipped_datasets.length > 0 && (
        <div className="text-xs text-muted-foreground">
          <span className="font-medium">Datasets ignorados: </span>
          {result.skipped_datasets.join(', ')}
        </div>
      )}

      {publicRuns.length > 0 ? (
        <Button onClick={onDownloadAll} className="w-full gap-2">
          <Download className="h-4 w-4" />
          Baixar {publicRuns.length} arquivo(s) no navegador
        </Button>
      ) : (
        <p className="text-sm text-muted-foreground">
          Nenhuma run com FASTQ publico encontrada no lote.
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Dialog principal
// ---------------------------------------------------------------------------

interface DatasetBatchDownloadDialogProps {
  projectId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** IDs de OmicDataset selecionados na tabela — quando vazio, usa todos do projeto no scope. */
  datasetIds?: number[];
}

export function DatasetBatchDownloadDialog({
  projectId,
  open,
  onOpenChange,
  datasetIds,
}: DatasetBatchDownloadDialogProps) {
  const [destination, setDestination] = useState<DestinationMode>('server');
  const [scope] = useState<BatchDownloadScopeEnum>('included');

  // Estados do fluxo
  const [quotaPreview, setQuotaPreview] = useState<BatchDownloadQuotaPreview | null>(null);
  const [blockError, setBlockError] = useState<string | null>(null);
  const [clientResult, setClientResult] = useState<BatchFastqUrlListResponse | null>(null);
  const [serverResult, setServerResult] = useState<TriggerBatchResult & { mode: 'server' } | null>(null);

  const batchMutation = useTriggerBatchDownload(projectId);
  const triggerBrowserDownloads = useClientFastqDownloader();

  function resetState() {
    setQuotaPreview(null);
    setBlockError(null);
    setClientResult(null);
    setServerResult(null);
  }

  function handleOpenChange(nextOpen: boolean) {
    if (!nextOpen) resetState();
    onOpenChange(nextOpen);
  }

  function handleDestinationChange(newDest: DestinationMode) {
    setDestination(newDest);
    resetState();
  }

  async function handleDispatch(confirm: boolean) {
    setBlockError(null);
    try {
      const result = await batchMutation.mutateAsync({
        dataset_ids: datasetIds && datasetIds.length > 0 ? datasetIds : undefined,
        scope,
        confirm,
        destination,
      });
      if (result.mode === 'client') {
        setClientResult(result.urls);
      } else {
        setServerResult(result as TriggerBatchResult & { mode: 'server' });
      }
    } catch (err) {
      if (isBatchDownloadQuotaError(err)) {
        if (err.httpStatus === 400 && err.preview.confirm_required) {
          setQuotaPreview(err.preview);
        } else if (err.httpStatus === 409 && !err.preview.confirm_required) {
          setBlockError(
            err.preview.detail ??
              `Quota esgotada: ${formatBytes(err.preview.used_bytes)} usados de ${formatBytes(err.preview.quota_bytes)}.`,
          );
        }
      }
    }
  }

  function handleDownloadAllClient() {
    if (!clientResult) return;
    const allRuns = clientResult.datasets.flatMap((d) => d.runs);
    triggerBrowserDownloads(allRuns);
  }

  const hasDatasetsSelected = datasetIds && datasetIds.length > 0;
  const scopeLabel = hasDatasetsSelected
    ? `${datasetIds.length} dataset(s) selecionado(s) — amostras ${scope === 'included' ? 'incluidas' : 'todas'}`
    : `Todos os datasets do projeto — amostras ${scope === 'included' ? 'incluidas' : 'todas'}`;

  // Conteudo do dialog varia conforme o estado do fluxo.
  const isInitialState = !quotaPreview && !clientResult && !serverResult && !blockError;

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <HardDrive className="h-4 w-4 text-amber-600" />
            Download FASTQ em lote
          </DialogTitle>
          <DialogDescription>
            {scopeLabel}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-1">
          {/* Estado inicial: escolha de destino + botao de disparo */}
          {isInitialState && (
            <>
              <div className="space-y-2">
                <p className="text-sm font-medium">Destino</p>
                <BatchDestinationSelector value={destination} onChange={handleDestinationChange} />
                {destination === 'client' && (
                  <p className="text-xs text-muted-foreground">
                    Sem consumo de quota — as URLs FASTQ publicas da ENA sao resolvidas e
                    o navegador inicia os downloads diretamente.
                  </p>
                )}
                {destination === 'server' && (
                  <p className="text-xs text-muted-foreground">
                    Os arquivos serao baixados para o servidor e ficam disponiveis no
                    storage do projeto. Consome quota de armazenamento.
                  </p>
                )}
              </div>

              <DialogFooter>
                <Button variant="outline" onClick={() => handleOpenChange(false)}>
                  Cancelar
                </Button>
                <Button
                  onClick={() => handleDispatch(false)}
                  disabled={batchMutation.isPending}
                  className="gap-1.5"
                >
                  {batchMutation.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                  <Download className="h-4 w-4" />
                  Iniciar download
                </Button>
              </DialogFooter>
            </>
          )}

          {/* Preview de quota (400): aguarda confirmacao */}
          {quotaPreview && (
            <BatchQuotaPreviewPanel
              preview={quotaPreview}
              isPending={batchMutation.isPending}
              onConfirm={() => handleDispatch(true)}
              onCancel={() => setQuotaPreview(null)}
            />
          )}

          {/* Erro de quota esgotada (409) */}
          {blockError && (
            <div className="space-y-3">
              <div className="flex items-start gap-2 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700">
                <AlertCircle className="h-4 w-4 shrink-0 mt-0.5" />
                <span>{blockError}</span>
              </div>
              <DialogFooter>
                <Button variant="outline" onClick={() => handleOpenChange(false)}>
                  Fechar
                </Button>
              </DialogFooter>
            </div>
          )}

          {/* Resultado server (202): jobs criados */}
          {serverResult && (
            <div className="space-y-3">
              <div className="rounded-md border p-3 text-sm space-y-1 bg-muted/40">
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Jobs criados</span>
                  <span className="font-medium text-teal-700">{serverResult.response.total_jobs}</span>
                </div>
                {serverResult.response.skipped_datasets.length > 0 && (
                  <div className="text-xs text-muted-foreground pt-1">
                    <span className="font-medium">Ignorados: </span>
                    {serverResult.response.skipped_datasets.join(', ')}
                  </div>
                )}
              </div>
              <p className="text-sm text-muted-foreground">
                Acompanhe o progresso na pagina de Jobs.
              </p>
              <DialogFooter>
                <Button onClick={() => handleOpenChange(false)}>Fechar</Button>
              </DialogFooter>
            </div>
          )}

          {/* Resultado client (200): lista de URLs + download no browser */}
          {clientResult && (
            <BatchClientResult
              result={clientResult}
              onDownloadAll={handleDownloadAllClient}
            />
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
