'use client';

import { use, useState } from 'react';
import { PageHeader } from '@/components/layout/page-header';
import { DatasetsTable } from '@/components/datasets/datasets-table';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { useDatasets } from '@/lib/hooks/use-datasets';
import { useDebounce } from '@/lib/hooks/use-debounce';
import { useFilterStore } from '@/lib/stores/filter-store';
import type { OmicDataset } from '@/lib/types/dataset';
import { Search } from 'lucide-react';

const OMIC_TYPES = ['genomic', 'transcriptomic', 'proteomic', 'metabolomic', 'epigenomic', 'metagenomic', 'multi_omic'];
const SOURCE_DBS = ['GEO', 'SRA', 'BioProject', 'ArrayExpress', 'TCGA'];

export default function DatasetsPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = use(params);
  const [selectedDataset, setSelectedDataset] = useState<OmicDataset | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const debouncedQuery = useDebounce(searchQuery, 300);

  const { datasetFilters, setDatasetFilters } = useFilterStore();
  const filters = datasetFilters[projectId] ?? {};

  const activeFilters = { ...filters, search: debouncedQuery || undefined };
  const { data, isLoading } = useDatasets(projectId, activeFilters);
  const datasets = data?.results ?? [];

  return (
    <div className="space-y-4">
      <PageHeader title="Datasets" description={`${data?.count ?? '…'} datasets`} />

      <div className="flex gap-4">
        <div className="w-56 shrink-0 space-y-4">
          <div className="space-y-1.5">
            <Label>Omic Type</Label>
            <Select
              value={filters.omic_type ?? ''}
              onValueChange={(v) => setDatasetFilters(projectId, { ...filters, omic_type: v || undefined })}
            >
              <SelectTrigger><SelectValue placeholder="All types" /></SelectTrigger>
              <SelectContent>
                <SelectItem value="">All</SelectItem>
                {OMIC_TYPES.map((t) => <SelectItem key={t} value={t}>{t}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label>Source DB</Label>
            <Select
              value={filters.source_db ?? ''}
              onValueChange={(v) => setDatasetFilters(projectId, { ...filters, source_db: v || undefined })}
            >
              <SelectTrigger><SelectValue placeholder="All DBs" /></SelectTrigger>
              <SelectContent>
                <SelectItem value="">All</SelectItem>
                {SOURCE_DBS.map((db) => <SelectItem key={db} value={db}>{db}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
        </div>

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
          ) : (
            <DatasetsTable datasets={datasets} onSelect={setSelectedDataset} />
          )}
        </div>
      </div>
    </div>
  );
}
