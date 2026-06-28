'use client';

import { use, useState, Suspense } from 'react';
import { PageHeader } from '@/components/layout/page-header';
import { DiscoveryFilters } from '@/components/discovery/discovery-filters';
import { DiscoveryTable } from '@/components/discovery/discovery-table';
import { QueryErrorState } from '@/components/ui/query-error-state';
import { Button } from '@/components/ui/button';
import { useDiscovery, useDownloadDiscovery } from '@/lib/hooks/use-discovery';
import { useFilterStore } from '@/lib/stores/filter-store';
import { useDebounce } from '@/lib/hooks/use-debounce';
import { Loader2, Download, FileJson } from 'lucide-react';
import type { DiscoveryFilters as DiscoveryFiltersType } from '@/lib/types/discovery';

const EMPTY_FILTERS: DiscoveryFiltersType = {};

// Inner component com acesso ao store (client-only, sem Suspense para useSearchParams aqui,
// pois não lemos URL — sem necessidade de Suspense boundary adicional).
function DiscoveryPageContent({ projectId }: { projectId: string }) {
  const [page, setPage] = useState(1);

  const { discoveryFilters, setDiscoveryFilters, clearDiscoveryFilters } = useFilterStore();
  const filters = discoveryFilters[projectId] ?? EMPTY_FILTERS;

  // Debounce aplicado ao campo `search` internamente ao filtrar
  const debouncedSearch = useDebounce(filters.search, 300);
  const activeFilters: DiscoveryFiltersType = {
    ...filters,
    search: debouncedSearch || undefined,
  };

  function handleFiltersChange(next: DiscoveryFiltersType) {
    setDiscoveryFilters(projectId, next);
    setPage(1); // reset paginação ao mudar filtros
  }

  function handleClear() {
    clearDiscoveryFilters(projectId);
    setPage(1);
  }

  const { data, isLoading, isError, error, refetch } = useDiscovery(projectId, activeFilters, page);

  const download = useDownloadDiscovery(projectId);

  const items = data?.results ?? [];
  const totalCount = data?.count ?? 0;
  const hasNext = !!data?.next;
  const hasPrev = !!data?.previous;

  // Snapshot/provenance: DatasetExportItem tem snapshot_version por item.
  // Exibimos a versão do primeiro item como representativa da página (todos devem ser iguais).
  const snapshotVersion = items[0]?.snapshot_version ?? null;

  return (
    <div className="space-y-4">
      <PageHeader
        title="Descoberta"
        description={
          isLoading
            ? 'Carregando catálogo…'
            : `${totalCount} dataset${totalCount !== 1 ? 's' : ''} no catálogo`
        }
        actions={
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={download.isPending || isLoading}
              onClick={() =>
                download.mutate({ format: 'csv', filters: activeFilters })
              }
            >
              {download.isPending ? (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              ) : (
                <Download className="h-4 w-4 mr-2" />
              )}
              {download.isPending ? 'Baixando…' : 'CSV'}
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={download.isPending || isLoading}
              onClick={() =>
                download.mutate({ format: 'json', filters: activeFilters })
              }
            >
              {download.isPending ? (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              ) : (
                <FileJson className="h-4 w-4 mr-2" />
              )}
              {download.isPending ? 'Baixando…' : 'JSON'}
            </Button>
          </div>
        }
      />

      <div className="flex gap-4">
        {/* Sidebar de filtros */}
        <div className="w-56 shrink-0">
          <DiscoveryFilters
            filters={filters}
            onChange={handleFiltersChange}
            onClear={handleClear}
          />
        </div>

        {/* Área principal */}
        <div className="flex-1 space-y-3 min-w-0">
          {isLoading ? (
            <div className="h-64 bg-muted rounded-lg animate-pulse" />
          ) : isError ? (
            <QueryErrorState error={error} onRetry={() => refetch()} />
          ) : (
            <DiscoveryTable items={items} />
          )}

          {/* Controles de paginação */}
          {!isLoading && !isError && (hasPrev || hasNext) && (
            <div className="flex items-center justify-between pt-2">
              <Button
                variant="outline"
                size="sm"
                disabled={!hasPrev}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
              >
                Anterior
              </Button>
              <span className="text-sm text-muted-foreground">
                Página {page}
              </span>
              <Button
                variant="outline"
                size="sm"
                disabled={!hasNext}
                onClick={() => setPage((p) => p + 1)}
              >
                Próxima
              </Button>
            </div>
          )}

          {/* Rodapé de proveniência */}
          {snapshotVersion && (
            <p className="text-xs text-muted-foreground text-right pt-1">
              Snapshot: <span className="font-mono">{snapshotVersion}</span>
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

export default function DiscoveryPage({
  params,
}: {
  params: Promise<{ projectId: string }>;
}) {
  // Next.js 16: params é uma Promise — desempacotar com use()
  const { projectId } = use(params);

  return (
    <Suspense fallback={<div className="h-64 bg-muted rounded-lg animate-pulse" />}>
      <DiscoveryPageContent projectId={projectId} />
    </Suspense>
  );
}
