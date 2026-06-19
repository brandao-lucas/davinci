'use client';

import { use, useState, useEffect, useRef, Suspense } from 'react';
import { useSearchParams } from 'next/navigation';
import { PageHeader } from '@/components/layout/page-header';
import { PapersTable } from '@/components/papers/papers-table';
import { PaperDetailPanel } from '@/components/papers/paper-detail-panel';
import { PaperFiltersPanel } from '@/components/papers/paper-filters';
import { BulkCurationBar } from '@/components/papers/bulk-curation-bar';
import { Input } from '@/components/ui/input';
import { QueryErrorState } from '@/components/ui/query-error-state';
import { usePapers, usePaper } from '@/lib/hooks/use-papers';
import { useDebounce } from '@/lib/hooks/use-debounce';
import { useFilterStore } from '@/lib/stores/filter-store';
import type { Paper } from '@/lib/types/paper';
import { Search } from 'lucide-react';

// Inner component isolado para uso do useSearchParams (exige Suspense boundary).
// Semeia o store com curation_status da URL uma única vez ao montar.
function PapersPageContent({ projectId }: { projectId: string }) {
  const searchParams = useSearchParams();
  const [selectedPaperId, setSelectedPaperId] = useState<number | null>(null);
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [searchQuery, setSearchQuery] = useState('');
  const debouncedQuery = useDebounce(searchQuery, 300);

  const { paperFilters, setPaperFilters } = useFilterStore();
  const filters = paperFilters[projectId] ?? {};

  // Seed do filtro a partir da URL — roda uma única vez por montagem.
  // Guarda o projectId que já foi semeado para não reaplicar em navegações
  // dentro da mesma página nem brigar com mudanças manuais no dropdown.
  const seededRef = useRef(false);
  useEffect(() => {
    if (seededRef.current) return;
    seededRef.current = true;
    const urlStatus = searchParams.get('curation_status');
    if (urlStatus) {
      setPaperFilters(projectId, { ...filters, curation_status: urlStatus });
    }
    // Intencionalmente sem `filters` nas deps: queremos ler o store apenas
    // no momento do mount, sem reaplicar a cada render subsequente.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const activeFilters = {
    ...filters,
    search: debouncedQuery || undefined,
  };

  const { data, isLoading, isError, error, refetch } = usePapers(projectId, activeFilters);
  const papers = data?.results ?? [];

  // Busca o detalhe completo apenas quando um paper está selecionado.
  const { data: paperDetail, isLoading: detailLoading } = usePaper(
    projectId,
    selectedPaperId ?? 0,
  );

  const handleSelectPaper = (paper: Paper) => setSelectedPaperId(paper.id);
  const handleCloseDetail = () => setSelectedPaperId(null);

  return (
    <div className="space-y-4">
      <PageHeader title="Papers" description={`${data?.count ?? '…'} papers`} />

      <div className="flex gap-4">
        <div className="w-56 shrink-0 space-y-4">
          <PaperFiltersPanel
            filters={filters}
            onChange={(f) => setPaperFilters(projectId, f)}
          />
        </div>

        <div className="flex-1 space-y-3 min-w-0">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              className="pl-9"
              placeholder="Full-text search…"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
            />
          </div>

          {isLoading ? (
            <div className="h-64 bg-muted rounded-lg animate-pulse" />
          ) : isError ? (
            <QueryErrorState error={error} onRetry={() => refetch()} />
          ) : (
            <PapersTable
              papers={papers}
              onSelect={handleSelectPaper}
              onSelectionChange={setSelectedIds}
            />
          )}
        </div>
      </div>

      <PaperDetailPanel
        paperId={selectedPaperId}
        detail={paperDetail}
        isLoading={detailLoading}
        onClose={handleCloseDetail}
      />

      <BulkCurationBar
        projectId={projectId}
        selectedIds={selectedIds}
        onClear={() => setSelectedIds([])}
      />
    </div>
  );
}

export default function PapersPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = use(params);

  return (
    <Suspense fallback={<div className="h-64 bg-muted rounded-lg animate-pulse" />}>
      <PapersPageContent projectId={projectId} />
    </Suspense>
  );
}
