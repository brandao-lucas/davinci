'use client';

import { use, useState, useCallback } from 'react';
import Link from 'next/link';
import { PageHeader } from '@/components/layout/page-header';
import { MeSHTable } from '@/components/mesh/mesh-table';
import { MeSHContextPanel } from '@/components/mesh/mesh-context-panel';
import { Button } from '@/components/ui/button';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { useMesh } from '@/lib/hooks/use-mesh';
import { useDebounce } from '@/lib/hooks/use-debounce';
import type { ProjectMeSHList, MeSHFilters } from '@/lib/types/mesh';

const DEFAULT_ORDERING: MeSHFilters['ordering'] = '-major_topic_count';
const PAGE_SIZE = 20;

export default function MeSHPage({
  params,
}: {
  params: Promise<{ projectId: string }>;
}) {
  const { projectId } = use(params);

  const [ordering, setOrdering] = useState<MeSHFilters['ordering']>(DEFAULT_ORDERING);
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(1);
  const [includedOnly, setIncludedOnly] = useState(false);
  const [selectedDescriptor, setSelectedDescriptor] = useState<string | null>(null);

  // Seletores estáveis: evita o bug de seletor instável (ver commit 41496ae)
  const handleOrderingChange = useCallback(
    (next: MeSHFilters['ordering']) => {
      setOrdering(next);
      setPage(1);
    },
    [],
  );

  const handleSearchChange = useCallback((value: string) => {
    setSearch(value);
    setPage(1);
  }, []);

  const handleIncludedOnlyChange = useCallback((checked: boolean) => {
    setIncludedOnly(checked);
    setPage(1);
  }, []);

  const handleSelectTerm = useCallback((term: ProjectMeSHList) => {
    setSelectedDescriptor((prev) =>
      prev === term.descriptor ? null : term.descriptor,
    );
  }, []);

  const handleClosePanel = useCallback(() => setSelectedDescriptor(null), []);

  const debouncedSearch = useDebounce(search, 300);

  const filters: MeSHFilters = {
    ordering,
    page,
    page_size: PAGE_SIZE,
    ...(debouncedSearch ? { q: debouncedSearch } : {}),
    ...(includedOnly ? { included_only: true } : {}),
  };

  const { data, isLoading } = useMesh(projectId, filters);

  const terms = data?.results ?? [];
  const totalCount = data?.count ?? 0;
  const totalPages = Math.ceil(totalCount / PAGE_SIZE);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Termos MeSH do Projeto"
        description="Todos os descritores MeSH extraidos dos papers deste projeto, agregados por descriptor."
        actions={
          <Link href={`/projects/${projectId}/analysis`}>
            <Button variant="ghost" size="sm">
              <ChevronLeft className="h-4 w-4 mr-1" />
              Voltar para Analysis
            </Button>
          </Link>
        }
      />

      {isLoading ? (
        <div className="h-64 bg-muted rounded-lg animate-pulse" />
      ) : (
        <>
          <MeSHTable
            terms={terms}
            ordering={ordering}
            search={search}
            includedOnly={includedOnly}
            onOrderingChange={handleOrderingChange}
            onSearchChange={handleSearchChange}
            onIncludedOnlyChange={handleIncludedOnlyChange}
            onSelect={handleSelectTerm}
            selectedDescriptor={selectedDescriptor}
          />

          {/* Paginacao */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between text-sm text-muted-foreground">
              <span>
                {totalCount} descriptor{totalCount !== 1 ? 'es' : ''} encontrado
                {totalCount !== 1 ? 's' : ''}
              </span>
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page <= 1}
                  onClick={() => setPage((p) => p - 1)}
                >
                  <ChevronLeft className="h-4 w-4" />
                </Button>
                <span>
                  {page} / {totalPages}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page >= totalPages}
                  onClick={() => setPage((p) => p + 1)}
                >
                  <ChevronRight className="h-4 w-4" />
                </Button>
              </div>
            </div>
          )}
        </>
      )}

      <MeSHContextPanel
        projectId={projectId}
        descriptor={selectedDescriptor}
        onClose={handleClosePanel}
      />
    </div>
  );
}
