'use client';

import { use, useState, useCallback } from 'react';
import Link from 'next/link';
import { PageHeader } from '@/components/layout/page-header';
import { VariantsTable } from '@/components/variants/variants-table';
import { VariantContextPanel } from '@/components/variants/variant-context-panel';
import { Button } from '@/components/ui/button';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { useVariants } from '@/lib/hooks/use-variants';
import { useDebounce } from '@/lib/hooks/use-debounce';
import type { ProjectVariantList, VariantFilters } from '@/lib/types/variant';

const DEFAULT_ORDERING: VariantFilters['ordering'] = '-unique_citations_included';
const PAGE_SIZE = 20;

export default function VariantsPage({
  params,
}: {
  params: Promise<{ projectId: string }>;
}) {
  const { projectId } = use(params);

  const [ordering, setOrdering] = useState<VariantFilters['ordering']>(DEFAULT_ORDERING);
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(1);
  const [includedOnly, setIncludedOnly] = useState(false);
  const [selectedVariant, setSelectedVariant] = useState<string | null>(null);

  // Seletores estaveis: evita o bug de seletor instavel (ver commit 41496ae)
  const handleOrderingChange = useCallback(
    (next: VariantFilters['ordering']) => {
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

  const handleSelectVariant = useCallback((variant: ProjectVariantList) => {
    setSelectedVariant((prev) =>
      prev === variant.rs_number ? null : variant.rs_number,
    );
  }, []);

  const handleClosePanel = useCallback(() => setSelectedVariant(null), []);

  const debouncedSearch = useDebounce(search, 300);

  const filters: VariantFilters = {
    ordering,
    page,
    page_size: PAGE_SIZE,
    ...(debouncedSearch ? { q: debouncedSearch } : {}),
    ...(includedOnly ? { included_only: true } : {}),
  };

  const { data, isLoading } = useVariants(projectId, filters);

  const variants = data?.results ?? [];
  const totalCount = data?.count ?? 0;
  const totalPages = Math.ceil(totalCount / PAGE_SIZE);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Variantes do Projeto"
        description="Todas as variantes geneticas extraidas dos papers deste projeto, agregadas por rs_number."
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
          <VariantsTable
            variants={variants}
            ordering={ordering}
            search={search}
            includedOnly={includedOnly}
            onOrderingChange={handleOrderingChange}
            onSearchChange={handleSearchChange}
            onIncludedOnlyChange={handleIncludedOnlyChange}
            onSelect={handleSelectVariant}
            selectedRsNumber={selectedVariant}
          />

          {/* Paginacao */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between text-sm text-muted-foreground">
              <span>
                {totalCount} variante{totalCount !== 1 ? 's' : ''} encontrada
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

      <VariantContextPanel
        projectId={projectId}
        rsNumber={selectedVariant}
        onClose={handleClosePanel}
      />
    </div>
  );
}
