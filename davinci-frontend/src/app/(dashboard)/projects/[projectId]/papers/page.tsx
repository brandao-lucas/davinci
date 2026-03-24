'use client';

import { use, useState } from 'react';
import { PageHeader } from '@/components/layout/page-header';
import { PapersTable } from '@/components/papers/papers-table';
import { PaperDetailPanel } from '@/components/papers/paper-detail-panel';
import { PaperFiltersPanel } from '@/components/papers/paper-filters';
import { BulkCurationBar } from '@/components/papers/bulk-curation-bar';
import { Input } from '@/components/ui/input';
import { usePapers } from '@/lib/hooks/use-papers';
import { useDebounce } from '@/lib/hooks/use-debounce';
import { useFilterStore } from '@/lib/stores/filter-store';
import type { Paper } from '@/lib/types/paper';
import { Search } from 'lucide-react';

export default function PapersPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = use(params);
  const [selectedPaper, setSelectedPaper] = useState<Paper | null>(null);
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [searchQuery, setSearchQuery] = useState('');
  const debouncedQuery = useDebounce(searchQuery, 300);

  const { paperFilters, setPaperFilters } = useFilterStore();
  const filters = paperFilters[projectId] ?? {};

  const activeFilters = {
    ...filters,
    search: debouncedQuery || undefined,
  };

  const { data, isLoading } = usePapers(projectId, activeFilters);
  const papers = data?.results ?? [];

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
          ) : (
            <PapersTable
              papers={papers}
              onSelect={setSelectedPaper}
              onSelectionChange={setSelectedIds}
            />
          )}
        </div>
      </div>

      <PaperDetailPanel paper={selectedPaper} onClose={() => setSelectedPaper(null)} />

      <BulkCurationBar
        projectId={projectId}
        selectedIds={selectedIds}
        onClear={() => setSelectedIds([])}
      />
    </div>
  );
}
