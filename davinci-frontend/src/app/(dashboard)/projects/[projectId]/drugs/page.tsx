'use client';

import { use, useState, useCallback } from 'react';
import Link from 'next/link';
import { PageHeader } from '@/components/layout/page-header';
import { DrugsTable } from '@/components/drugs/drugs-table';
import { DrugContextPanel } from '@/components/drugs/drug-context-panel';
import { Button } from '@/components/ui/button';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { useDrugs } from '@/lib/hooks/use-drugs';
import { useDebounce } from '@/lib/hooks/use-debounce';
import type { ProjectDrugList, DrugFilters } from '@/lib/types/drug';

const DEFAULT_ORDERING: DrugFilters['ordering'] = '-unique_citations_included';
const PAGE_SIZE = 20;

export default function DrugsPage({
  params,
}: {
  params: Promise<{ projectId: string }>;
}) {
  const { projectId } = use(params);

  const [ordering, setOrdering] = useState<DrugFilters['ordering']>(DEFAULT_ORDERING);
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(1);
  const [includedOnly, setIncludedOnly] = useState(false);
  const [selectedDrug, setSelectedDrug] = useState<string | null>(null);

  // Seletores estáveis: evita o bug de seletor instável (ver commit 41496ae)
  const handleOrderingChange = useCallback(
    (next: DrugFilters['ordering']) => {
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

  const handleSelectDrug = useCallback((drug: ProjectDrugList) => {
    // Usa drug_name como chave de toggle de seleção; o panel converte para lower ao buscar.
    setSelectedDrug((prev) =>
      prev === drug.drug_name ? null : drug.drug_name,
    );
  }, []);

  const handleClosePanel = useCallback(() => setSelectedDrug(null), []);

  const debouncedSearch = useDebounce(search, 300);

  const filters: DrugFilters = {
    ordering,
    page,
    page_size: PAGE_SIZE,
    ...(debouncedSearch ? { q: debouncedSearch } : {}),
    ...(includedOnly ? { included_only: true } : {}),
  };

  const { data, isLoading } = useDrugs(projectId, filters);

  const drugs = data?.results ?? [];
  const totalCount = data?.count ?? 0;
  const totalPages = Math.ceil(totalCount / PAGE_SIZE);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Medicamentos do Projeto"
        description="Todos os medicamentos extraidos dos papers deste projeto, agregados por nome canonico."
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
          <DrugsTable
            drugs={drugs}
            ordering={ordering}
            search={search}
            includedOnly={includedOnly}
            onOrderingChange={handleOrderingChange}
            onSearchChange={handleSearchChange}
            onIncludedOnlyChange={handleIncludedOnlyChange}
            onSelect={handleSelectDrug}
            selectedDrug={selectedDrug}
          />

          {/* Paginacao */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between text-sm text-muted-foreground">
              <span>
                {totalCount} medicamento{totalCount !== 1 ? 's' : ''} encontrado
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

      <DrugContextPanel
        projectId={projectId}
        drugNameLower={selectedDrug ? selectedDrug.toLowerCase() : null}
        onClose={handleClosePanel}
      />
    </div>
  );
}
