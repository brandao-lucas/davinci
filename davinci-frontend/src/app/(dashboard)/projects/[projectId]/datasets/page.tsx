'use client';

import { use, useState, useEffect, useRef, Suspense } from 'react';
import { useSearchParams } from 'next/navigation';
import { PageHeader } from '@/components/layout/page-header';
import { DatasetsTable } from '@/components/datasets/datasets-table';
import { DatasetDetailPanel } from '@/components/datasets/dataset-detail-panel';
import { DatasetBulkCurationBar } from '@/components/datasets/dataset-bulk-curation-bar';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { QueryErrorState } from '@/components/ui/query-error-state';
import { Checkbox } from '@/components/ui/checkbox';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { useDatasets } from '@/lib/hooks/use-datasets';
import { useJobs, useJobPolling } from '@/lib/hooks/use-jobs';
import { useDispatchOmicsSearch, useProject } from '@/lib/hooks/use-projects';
import { useDebounce } from '@/lib/hooks/use-debounce';
import { descriptorsChangedSinceLastSearch } from '@/lib/utils/descriptor-diff';
import { useFilterStore } from '@/lib/stores/filter-store';
import type { OmicDataset } from '@/lib/types/dataset';
import { Loader2, Search, Database } from 'lucide-react';

const OMIC_TYPES = ['genomic', 'transcriptomic', 'proteomic', 'metabolomic', 'epigenomic', 'metagenomic', 'multi_omic'];
const SOURCE_DBS = [
  { label: 'GEO', value: 'geo' },
  { label: 'SRA', value: 'sra' },
  { label: 'BioProject', value: 'bioproject' },
  { label: 'GWAS Catalog', value: 'gwas_catalog' },
];
const STATUSES = ['pending', 'included', 'excluded', 'queued', 'downloaded'];

// Inner component isolado para uso do useSearchParams (exige Suspense boundary).
function DatasetsPageContent({ projectId }: { projectId: string }) {
  const searchParams = useSearchParams();
  const [selectedDataset, setSelectedDataset] = useState<OmicDataset | null>(null);
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [searchQuery, setSearchQuery] = useState('');
  const debouncedQuery = useDebounce(searchQuery, 300);

  const { datasetFilters, setDatasetFilters } = useFilterStore();
  const filters = datasetFilters[projectId] ?? {};

  // Seed do filtro a partir da URL — roda uma única vez ao montar.
  const seededRef = useRef(false);
  useEffect(() => {
    if (seededRef.current) return;
    seededRef.current = true;
    const urlStatus = searchParams.get('curation_status');
    if (urlStatus) {
      setDatasetFilters(projectId, { curation_status: urlStatus });
    }
    // Intencionalmente sem deps de `filters`: lemos o store apenas no mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Dados do projeto (para comparar descritores com o último job)
  const { data: project } = useProject(projectId);

  // Omics search dispatch + job polling
  const dispatchOmics = useDispatchOmicsSearch(projectId);
  const { data: jobs } = useJobs(projectId);
  const latestOmicsJob = jobs?.results?.find(j => j.job_type === 'geo_search');
  const latestOmicsJobId = latestOmicsJob?.id ?? '';
  useJobPolling(projectId, latestOmicsJobId);
  const jobIsActive = latestOmicsJob?.status === 'pending' || latestOmicsJob?.status === 'running';
  const isSearching = dispatchOmics.isPending || jobIsActive;

  // Desabilita o botão quando os descritores não mudaram desde a última busca omics concluída
  const descriptorsChanged = project
    ? descriptorsChangedSinceLastSearch(project, jobs?.results, 'geo_search')
    : true;
  const alreadySearched = !descriptorsChanged;
  const isSearchDisabled = isSearching || alreadySearched;

  let searchButtonTitle: string | undefined;
  if (dispatchOmics.isError) {
    searchButtonTitle = 'Failed to start search — check if Celery worker is running';
  } else if (alreadySearched && !isSearching) {
    searchButtonTitle = 'Já buscado com os descritores atuais. Altere termo/sinônimos/datas para rebuscar.';
  }

  const activeFilters = { ...filters, search: debouncedQuery || undefined };
  const { data, isLoading, isError, error, refetch } = useDatasets(projectId, activeFilters);
  const datasets = data?.results ?? [];

  return (
    <div className="space-y-4">
      <PageHeader
        title="Datasets"
        description={`${data?.count ?? '…'} omics datasets`}
        actions={
          <Button
            onClick={() => dispatchOmics.mutate({})}
            disabled={isSearchDisabled}
            variant={dispatchOmics.isError ? 'destructive' : 'default'}
            title={searchButtonTitle}
          >
            {isSearching
              ? <><Loader2 className="h-4 w-4 mr-2 animate-spin" />Searching datasets…</>
              : dispatchOmics.isError
                ? 'Failed — Retry'
                : <><Database className="h-4 w-4 mr-2" />Search Datasets</>
            }
          </Button>
        }
      />

      <div className="flex gap-4">
        {/* Sidebar filters */}
        <div className="w-56 shrink-0 space-y-4">
          <div className="space-y-1.5">
            <Label>Status</Label>
            <Select
              value={filters.curation_status ?? 'all'}
              onValueChange={(v) => setDatasetFilters(projectId, { ...filters, curation_status: v === 'all' ? undefined : v })}
            >
              <SelectTrigger><SelectValue placeholder="All statuses" /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All</SelectItem>
                {STATUSES.map((s) => <SelectItem key={s} value={s}>{s}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label>Omic Type</Label>
            <Select
              value={filters.omic_type ?? 'all'}
              onValueChange={(v) => setDatasetFilters(projectId, { ...filters, omic_type: v === 'all' ? undefined : v })}
            >
              <SelectTrigger><SelectValue placeholder="All types" /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All</SelectItem>
                {OMIC_TYPES.map((t) => <SelectItem key={t} value={t}>{t}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label>Source DB</Label>
            <Select
              value={filters.source_db ?? 'all'}
              onValueChange={(v) => setDatasetFilters(projectId, { ...filters, source_db: v === 'all' ? undefined : v })}
            >
              <SelectTrigger><SelectValue placeholder="All DBs" /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All</SelectItem>
                {SOURCE_DBS.map((db) => <SelectItem key={db.value} value={db.value}>{db.label}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label>Organism</Label>
            <Input
              placeholder="Homo sapiens…"
              value={filters.organism ?? ''}
              onChange={(e) => setDatasetFilters(projectId, { ...filters, organism: e.target.value || undefined })}
            />
          </div>

          <div className="space-y-2 pt-1">
            <Label className="text-xs uppercase tracking-wide text-muted-foreground">Content</Label>
            <label className="flex items-center gap-2 cursor-pointer text-sm">
              <Checkbox
                checked={!!filters.has_summary}
                onCheckedChange={(v) => setDatasetFilters(projectId, { ...filters, has_summary: v ? true : undefined })}
              />
              With summary
            </label>
          </div>
        </div>

        {/* Table area */}
        <div className="flex-1 space-y-3 min-w-0">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              className="pl-9"
              placeholder="Search datasets…"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
            />
          </div>

          {isLoading ? (
            <div className="h-64 bg-muted rounded-lg animate-pulse" />
          ) : isError ? (
            <QueryErrorState error={error} onRetry={() => refetch()} />
          ) : (
            <DatasetsTable
              datasets={datasets}
              onSelect={setSelectedDataset}
              onSelectionChange={setSelectedIds}
            />
          )}
        </div>
      </div>

      <DatasetDetailPanel
        dataset={selectedDataset}
        projectId={projectId}
        onClose={() => setSelectedDataset(null)}
      />

      <DatasetBulkCurationBar
        projectId={projectId}
        selectedIds={selectedIds}
        onClear={() => setSelectedIds([])}
        activeFilters={activeFilters}
        filteredTotal={data?.count}
      />
    </div>
  );
}

export default function DatasetsPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = use(params);

  return (
    <Suspense fallback={<div className="h-64 bg-muted rounded-lg animate-pulse" />}>
      <DatasetsPageContent projectId={projectId} />
    </Suspense>
  );
}
