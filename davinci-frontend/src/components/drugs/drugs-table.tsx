'use client';

import {
  useReactTable,
  getCoreRowModel,
  flexRender,
  type ColumnDef,
} from '@tanstack/react-table';
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { ArrowUpDown, ArrowUp, ArrowDown, ExternalLink, Info } from 'lucide-react';
import { Checkbox } from '@/components/ui/checkbox';
import type { ProjectDrugList, DrugFilters } from '@/lib/types/drug';

type OrderingField =
  | 'unique_citations_included'
  | 'unique_citations_total'
  | 'mention_count_total'
  | 'drug_name';

function nextOrdering(
  current: DrugFilters['ordering'],
  field: OrderingField,
): DrugFilters['ordering'] {
  if (current === field) return `-${field}` as DrugFilters['ordering'];
  if (current === `-${field}`) return field as DrugFilters['ordering'];
  return `-${field}` as DrugFilters['ordering'];
}

function SortIcon({
  field,
  current,
}: {
  field: OrderingField;
  current: DrugFilters['ordering'];
}) {
  if (current === field) return <ArrowUp className="ml-1 h-3 w-3" />;
  if (current === `-${field}`) return <ArrowDown className="ml-1 h-3 w-3" />;
  return <ArrowUpDown className="ml-1 h-3 w-3 opacity-40" />;
}

interface DrugsTableProps {
  drugs: ProjectDrugList[];
  ordering: DrugFilters['ordering'];
  search: string;
  includedOnly: boolean;
  onOrderingChange: (ordering: DrugFilters['ordering']) => void;
  onSearchChange: (value: string) => void;
  onIncludedOnlyChange: (checked: boolean) => void;
  onSelect: (drug: ProjectDrugList) => void;
  selectedDrug?: string | null;
}

export function DrugsTable({
  drugs,
  ordering,
  search,
  includedOnly,
  onOrderingChange,
  onSearchChange,
  onIncludedOnlyChange,
  onSelect,
  selectedDrug,
}: DrugsTableProps) {
  const columns: ColumnDef<ProjectDrugList>[] = [
    {
      accessorKey: 'drug_name',
      header: () => (
        <Button
          variant="ghost"
          size="sm"
          className="-ml-3 h-8 font-semibold"
          onClick={() => onOrderingChange(nextOrdering(ordering, 'drug_name'))}
        >
          Medicamento
          <SortIcon field="drug_name" current={ordering} />
        </Button>
      ),
      cell: ({ getValue }) => (
        <span className="font-medium text-sm">{getValue<string>()}</span>
      ),
    },
    {
      id: 'citations',
      header: () => (
        <div className="flex items-center">
          <Button
            variant="ghost"
            size="sm"
            className="-ml-3 h-8 font-semibold"
            onClick={() => onOrderingChange(nextOrdering(ordering, 'unique_citations_included'))}
          >
            Citações
            <SortIcon field="unique_citations_included" current={ordering} />
          </Button>
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <span
                  role="img"
                  aria-label="Informações sobre a coluna Citações"
                  className="ml-1 cursor-help text-muted-foreground hover:text-foreground inline-flex items-center"
                >
                  <Info className="h-3.5 w-3.5" />
                </span>
              </TooltipTrigger>
              <TooltipContent side="top" className="max-w-xs text-xs">
                <p className="font-semibold mb-1">Citações únicas — incluídos | total</p>
                <p className="mb-1">
                  O formato exibido é{' '}
                  <span className="text-green-600 font-mono font-semibold">incluídos</span>
                  {' '}|{' '}
                  <span className="font-mono font-semibold">total</span>.
                </p>
                <p className="mb-1">
                  <span className="text-green-600 font-semibold">Número verde (incluídos):</span>{' '}
                  quantidade de papers com status &quot;incluído&quot; que citam este medicamento — cada paper conta apenas uma vez, independentemente de quantas vezes menciona o medicamento.
                </p>
                <p>
                  <span className="font-semibold">Total:</span>{' '}
                  quantidade de papers de qualquer status de curadoria que citam o medicamento — também uma citação única por paper distinto.
                </p>
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </div>
      ),
      cell: ({ row }) => {
        const inc = row.original.unique_citations_included;
        const tot = row.original.unique_citations_total;
        return (
          <span className="font-mono text-sm tabular-nums">
            <span className="text-green-700 font-semibold">{inc}</span>
            <span className="text-muted-foreground mx-1">|</span>
            <span>{tot}</span>
          </span>
        );
      },
    },
    {
      accessorKey: 'mention_count_total',
      header: () => (
        <div className="flex items-center">
          <Button
            variant="ghost"
            size="sm"
            className="-ml-3 h-8 font-semibold"
            onClick={() => onOrderingChange(nextOrdering(ordering, 'mention_count_total'))}
          >
            Menções totais
            <SortIcon field="mention_count_total" current={ordering} />
          </Button>
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <span
                  role="img"
                  aria-label="Informações sobre a coluna Menções totais"
                  className="ml-1 cursor-help text-muted-foreground hover:text-foreground inline-flex items-center"
                >
                  <Info className="h-3.5 w-3.5" />
                </span>
              </TooltipTrigger>
              <TooltipContent side="top" className="max-w-xs text-xs">
                <p className="font-semibold mb-1">Menções totais</p>
                <p className="mb-1">
                  Soma de <span className="font-semibold">todas</span> as ocorrências do medicamento em todos os papers do projeto, incluindo múltiplas menções dentro de um mesmo paper.
                </p>
                <p>
                  Diferente de &quot;Citações&quot;, que conta <span className="font-semibold">um paper distinto por linha</span>, Menções soma cada ocorrência individualmente — portanto pode ser maior que o número de papers que citam o medicamento.
                </p>
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </div>
      ),
      cell: ({ getValue }) => (
        <span className="font-mono text-sm tabular-nums">{getValue<number>()}</span>
      ),
    },
    {
      id: 'links',
      header: 'Links',
      cell: ({ row }) => {
        const { drug_name, drugbank_url, pubchem_search_url } = row.original;

        return (
          <div className="flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
            {drugbank_url && (
              <a
                href={drugbank_url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-xs text-blue-600 hover:underline"
                title={`DrugBank: ${drug_name}`}
              >
                DrugBank
                <ExternalLink className="h-3 w-3" />
              </a>
            )}
            <a
              href={pubchem_search_url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-xs text-blue-600 hover:underline"
              title={`Buscar "${drug_name}" no PubChem`}
            >
              PubChem
              <ExternalLink className="h-3 w-3" />
            </a>
          </div>
        );
      },
    },
  ];

  const table = useReactTable({
    data: drugs,
    columns,
    getCoreRowModel: getCoreRowModel(),
    getRowId: (row) => row.drug_name,
  });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-4">
        <Input
          placeholder="Filtrar por nome do medicamento..."
          value={search}
          onChange={(e) => onSearchChange(e.target.value)}
          className="max-w-sm"
        />
        <label className="flex items-center gap-2 cursor-pointer text-sm select-none">
          <Checkbox
            id="included-only"
            checked={includedOnly}
            onCheckedChange={(v) => onIncludedOnlyChange(!!v)}
          />
          <span>So estudos incluidos</span>
        </label>
      </div>
      <div className="rounded-md border overflow-auto">
        <Table>
          <TableHeader>
            {table.getHeaderGroups().map((hg) => (
              <TableRow key={hg.id}>
                {hg.headers.map((h) => (
                  <TableHead key={h.id} style={{ width: h.getSize() }}>
                    {flexRender(h.column.columnDef.header, h.getContext())}
                  </TableHead>
                ))}
              </TableRow>
            ))}
          </TableHeader>
          <TableBody>
            {table.getRowModel().rows.length === 0 ? (
              <TableRow>
                <TableCell
                  colSpan={columns.length}
                  className="h-24 text-center text-muted-foreground"
                >
                  Nenhum medicamento encontrado.
                </TableCell>
              </TableRow>
            ) : (
              table.getRowModel().rows.map((row) => (
                <TableRow
                  key={row.id}
                  className="cursor-pointer hover:bg-muted/50"
                  data-state={
                    row.original.drug_name === selectedDrug ? 'selected' : undefined
                  }
                  onClick={() => onSelect(row.original)}
                >
                  {row.getVisibleCells().map((cell) => (
                    <TableCell key={cell.id}>
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </TableCell>
                  ))}
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
