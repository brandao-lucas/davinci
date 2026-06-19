'use client';

import { use, useState, useCallback } from 'react';
import Link from 'next/link';
import { PageHeader } from '@/components/layout/page-header';
import { GenesTable } from '@/components/genes/genes-table';
import { GeneContextPanel } from '@/components/genes/gene-context-panel';
import { Button } from '@/components/ui/button';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { useGenes } from '@/lib/hooks/use-genes';
import { useDebounce } from '@/lib/hooks/use-debounce';
import type { ProjectGeneList, GeneFilters } from '@/lib/types/gene';

const DEFAULT_ORDERING: GeneFilters['ordering'] = '-unique_citations_included';
const PAGE_SIZE = 20;

export default function GenesPage({
  params,
}: {
  params: Promise<{ projectId: string }>;
}) {
  const { projectId } = use(params);

  const [ordering, setOrdering] = useState<GeneFilters['ordering']>(DEFAULT_ORDERING);
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(1);
  const [includedOnly, setIncludedOnly] = useState(false);
  const [selectedGene, setSelectedGene] = useState<string | null>(null);

  // Seletores estáveis: evita o bug de seletor instável (ver commit 41496ae)
  const handleOrderingChange = useCallback(
    (next: GeneFilters['ordering']) => {
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

  const handleSelectGene = useCallback((gene: ProjectGeneList) => {
    setSelectedGene((prev) =>
      prev === gene.gene_symbol ? null : gene.gene_symbol,
    );
  }, []);

  const handleClosePanel = useCallback(() => setSelectedGene(null), []);

  const debouncedSearch = useDebounce(search, 300);

  const filters: GeneFilters = {
    ordering,
    page,
    page_size: PAGE_SIZE,
    ...(debouncedSearch ? { q: debouncedSearch } : {}),
    ...(includedOnly ? { included_only: true } : {}),
  };

  const { data, isLoading } = useGenes(projectId, filters);

  const genes = data?.results ?? [];
  const totalCount = data?.count ?? 0;
  const totalPages = Math.ceil(totalCount / PAGE_SIZE);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Genes do Projeto"
        description="Todos os genes extraidos dos papers deste projeto, agregados por simbolo."
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
          <GenesTable
            genes={genes}
            ordering={ordering}
            search={search}
            includedOnly={includedOnly}
            onOrderingChange={handleOrderingChange}
            onSearchChange={handleSearchChange}
            onIncludedOnlyChange={handleIncludedOnlyChange}
            onSelect={handleSelectGene}
            selectedSymbol={selectedGene}
          />

          {/* Paginacao */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between text-sm text-muted-foreground">
              <span>
                {totalCount} gene{totalCount !== 1 ? 's' : ''} encontrado
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

      <GeneContextPanel
        projectId={projectId}
        geneSymbol={selectedGene}
        onClose={handleClosePanel}
      />
    </div>
  );
}
