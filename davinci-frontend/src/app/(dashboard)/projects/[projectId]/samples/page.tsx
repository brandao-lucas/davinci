'use client';

import { use, useState, useEffect, useRef, Suspense } from 'react';
import { useSearchParams } from 'next/navigation';
import { PageHeader } from '@/components/layout/page-header';
import { SamplesTable } from '@/components/samples/samples-table';
import { SampleDetailPanel } from '@/components/samples/sample-detail-panel';
import { SampleBulkCurationBar } from '@/components/samples/sample-bulk-curation-bar';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { QueryErrorState } from '@/components/ui/query-error-state';
import { useSamplesByProject } from '@/lib/hooks/use-samples';
import { useDebounce } from '@/lib/hooks/use-debounce';
import { useFilterStore } from '@/lib/stores/filter-store';
import type { ProjectSample } from '@/lib/types/sample';
import { Search, FlaskConical } from 'lucide-react';

const STATUSES = ['pending', 'included', 'excluded', 'maybe'];

// Inner component isolado para uso do useSearchParams (exige Suspense boundary).
function SamplesPageContent({ projectId }: { projectId: string }) {
  const searchParams = useSearchParams();
  const [selectedSample, setSelectedSample] = useState<ProjectSample | null>(null);
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [searchQuery, setSearchQuery] = useState('');
  const debouncedQuery = useDebounce(searchQuery, 300);
  const [page, setPage] = useState(1);

  // Usa projectId como chave do store — consistente com paperFilters/datasetFilters.
  const { sampleFilters, setSampleFilters } = useFilterStore();
  const filters = sampleFilters[projectId] ?? {};

  // Seed do filtro a partir da URL — roda uma única vez ao montar.
  // Sem parâmetro na URL, não toca no store (não sobrepõe estado de visita anterior).
  const seededRef = useRef(false);
  useEffect(() => {
    if (seededRef.current) return;
    seededRef.current = true;
    const urlStatus = searchParams.get('curation_status');
    if (urlStatus) {
      setSampleFilters(projectId, { curation_status: urlStatus });
    }
    // Intencionalmente sem deps de `filters`: lemos o store apenas no mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const activeFilters = {
    ...filters,
    search: debouncedQuery || undefined,
    page,
  };

  const { data, isLoading, isError, error, refetch } = useSamplesByProject(projectId, activeFilters);
  const samples = data?.results ?? [];
  const totalCount = data?.count ?? 0;
  const hasNext = !!data?.next;
  const hasPrev = !!data?.previous;

  return (
    <div className="space-y-4">
      <PageHeader
        title="Samples"
        description={totalCount > 0 ? `${totalCount} samples` : 'Samples do projeto'}
      />

      <div className="flex gap-4">
        {/* Sidebar filters */}
        <div className="w-56 shrink-0 space-y-4">
          <div className="space-y-1.5">
            <Label>Status</Label>
            <Select
              value={filters.curation_status ?? 'all'}
              onValueChange={(v) => {
                setSampleFilters(projectId, { ...filters, curation_status: v === 'all' ? undefined : v });
                setPage(1);
              }}
            >
              <SelectTrigger><SelectValue placeholder="All statuses" /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All</SelectItem>
                {STATUSES.map((s) => <SelectItem key={s} value={s}>{s}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label>Organism</Label>
            <Input
              placeholder="Homo sapiens…"
              value={filters.organism ?? ''}
              onChange={(e) => {
                setSampleFilters(projectId, { ...filters, organism: e.target.value || undefined });
                setPage(1);
              }}
            />
          </div>
        </div>

        {/* Table area */}
        <div className="flex-1 space-y-3 min-w-0">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              className="pl-9"
              placeholder="Search samples…"
              value={searchQuery}
              onChange={(e) => {
                setSearchQuery(e.target.value);
                setPage(1);
              }}
            />
          </div>

          {isLoading ? (
            <div className="h-64 bg-muted rounded-lg animate-pulse" />
          ) : isError ? (
            <QueryErrorState error={error} onRetry={() => refetch()} />
          ) : samples.length === 0 ? (
            // Samples são ingeridos sob demanda — apenas após datasets serem curados como "included".
            // Estado vazio é esperado em projetos novos ou sem datasets incluídos ainda.
            <div className="flex flex-col items-center justify-center h-48 gap-3 text-muted-foreground border rounded-lg">
              <FlaskConical className="h-10 w-10 opacity-30" />
              <div className="text-center space-y-1">
                <p className="font-medium">No samples found</p>
                <p className="text-xs max-w-xs">
                  Samples are fetched on demand after a dataset is curated as
                  &quot;included&quot;. If you just included a dataset, the
                  background job may still be running — check the Jobs page.
                </p>
              </div>
            </div>
          ) : (
            <SamplesTable
              samples={samples}
              onSelect={setSelectedSample}
              onSelectionChange={setSelectedIds}
            />
          )}

          {/* Pagination */}
          {(hasNext || hasPrev) && (
            <div className="flex items-center justify-between pt-2">
              <Button
                size="sm"
                variant="outline"
                disabled={!hasPrev}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
              >
                Previous
              </Button>
              <span className="text-xs text-muted-foreground">Page {page}</span>
              <Button
                size="sm"
                variant="outline"
                disabled={!hasNext}
                onClick={() => setPage((p) => p + 1)}
              >
                Next
              </Button>
            </div>
          )}
        </div>
      </div>

      <SampleDetailPanel
        sample={selectedSample}
        projectId={projectId}
        onClose={() => setSelectedSample(null)}
      />

      <SampleBulkCurationBar
        projectId={projectId}
        selectedIds={selectedIds}
        onClear={() => setSelectedIds([])}
      />
    </div>
  );
}

export default function ProjectSamplesPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = use(params);

  return (
    <Suspense fallback={<div className="h-64 bg-muted rounded-lg animate-pulse" />}>
      <SamplesPageContent projectId={projectId} />
    </Suspense>
  );
}
