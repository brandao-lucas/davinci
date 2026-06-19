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
import { Badge } from '@/components/ui/badge';
import { Checkbox } from '@/components/ui/checkbox';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { ArrowUpDown, ArrowUp, ArrowDown, Info } from 'lucide-react';
import type { ProjectVariantList, VariantFilters } from '@/lib/types/variant';

type OrderingField =
  | 'unique_citations_included'
  | 'unique_citations_total'
  | 'mention_count_total'
  | 'rs_number';

/**
 * Mapa de cores do Badge por significância clínica (ClinVar).
 * Cobre os valores canônicos; fallback para cinza neutro.
 */
const clinicalSignificanceColors: Record<string, string> = {
  pathogenic: 'bg-red-100 text-red-800 border-red-200',
  'likely pathogenic': 'bg-red-50 text-red-700 border-red-200',
  'uncertain significance': 'bg-amber-100 text-amber-800 border-amber-200',
  'likely benign': 'bg-green-50 text-green-700 border-green-200',
  benign: 'bg-green-100 text-green-800 border-green-200',
  'drug response': 'bg-blue-100 text-blue-800 border-blue-200',
  'risk factor': 'bg-orange-100 text-orange-800 border-orange-200',
  association: 'bg-violet-100 text-violet-800 border-violet-200',
  protective: 'bg-teal-100 text-teal-800 border-teal-200',
  'conflicting interpretations': 'bg-amber-100 text-amber-800 border-amber-200',
  other: 'bg-slate-100 text-slate-700 border-slate-200',
};

function clinicalBadgeClass(sig: string): string {
  const key = sig.toLowerCase();
  return clinicalSignificanceColors[key] ?? 'bg-slate-100 text-slate-700 border-slate-200';
}

function nextOrdering(
  current: VariantFilters['ordering'],
  field: OrderingField,
): VariantFilters['ordering'] {
  if (current === field) return `-${field}` as VariantFilters['ordering'];
  if (current === `-${field}`) return field as VariantFilters['ordering'];
  return `-${field}` as VariantFilters['ordering'];
}

function SortIcon({
  field,
  current,
}: {
  field: OrderingField;
  current: VariantFilters['ordering'];
}) {
  if (current === field) return <ArrowUp className="ml-1 h-3 w-3" />;
  if (current === `-${field}`) return <ArrowDown className="ml-1 h-3 w-3" />;
  return <ArrowUpDown className="ml-1 h-3 w-3 opacity-40" />;
}

interface VariantsTableProps {
  variants: ProjectVariantList[];
  ordering: VariantFilters['ordering'];
  search: string;
  includedOnly: boolean;
  onOrderingChange: (ordering: VariantFilters['ordering']) => void;
  onSearchChange: (value: string) => void;
  onIncludedOnlyChange: (checked: boolean) => void;
  onSelect: (variant: ProjectVariantList) => void;
  selectedRsNumber?: string | null;
}

export function VariantsTable({
  variants,
  ordering,
  search,
  includedOnly,
  onOrderingChange,
  onSearchChange,
  onIncludedOnlyChange,
  onSelect,
  selectedRsNumber,
}: VariantsTableProps) {
  const columns: ColumnDef<ProjectVariantList>[] = [
    {
      accessorKey: 'rs_number',
      header: () => (
        <Button
          variant="ghost"
          size="sm"
          className="-ml-3 h-8 font-semibold"
          onClick={() => onOrderingChange(nextOrdering(ordering, 'rs_number'))}
        >
          Variante
          <SortIcon field="rs_number" current={ordering} />
        </Button>
      ),
      cell: ({ getValue }) => (
        <span className="font-mono font-semibold text-sm">{getValue<string>()}</span>
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
            Citacoes
            <SortIcon field="unique_citations_included" current={ordering} />
          </Button>
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <span
                  role="img"
                  aria-label="Informacoes sobre a coluna Citacoes"
                  className="ml-1 cursor-help text-muted-foreground hover:text-foreground inline-flex items-center"
                >
                  <Info className="h-3.5 w-3.5" />
                </span>
              </TooltipTrigger>
              <TooltipContent side="top" className="max-w-xs text-xs">
                <p className="font-semibold mb-1">Citacoes unicas — incluidos | total</p>
                <p className="mb-1">
                  O formato exibido e{' '}
                  <span className="text-green-600 font-mono font-semibold">incluidos</span>
                  {' '}|{' '}
                  <span className="font-mono font-semibold">total</span>.
                </p>
                <p className="mb-1">
                  <span className="text-green-600 font-semibold">Numero verde (incluidos):</span>{' '}
                  quantidade de papers com status &quot;incluido&quot; que citam esta variante — cada paper conta apenas uma vez.
                </p>
                <p>
                  <span className="font-semibold">Total:</span>{' '}
                  quantidade de papers de qualquer status de curadoria que citam a variante — uma citacao unica por paper distinto.
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
            Mencoes
            <SortIcon field="mention_count_total" current={ordering} />
          </Button>
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <span
                  role="img"
                  aria-label="Informacoes sobre a coluna Mencoes totais"
                  className="ml-1 cursor-help text-muted-foreground hover:text-foreground inline-flex items-center"
                >
                  <Info className="h-3.5 w-3.5" />
                </span>
              </TooltipTrigger>
              <TooltipContent side="top" className="max-w-xs text-xs">
                <p className="font-semibold mb-1">Mencoes totais</p>
                <p>
                  Soma de <span className="font-semibold">todas</span> as ocorrencias da variante em todos os papers do projeto, incluindo multiplas mencoes dentro de um mesmo paper.
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
      id: 'gene',
      header: 'Gene',
      cell: ({ row }) => {
        const ann = row.original.annotation;
        return ann?.gene_symbol ? (
          <span className="font-mono text-sm">{ann.gene_symbol}</span>
        ) : (
          <span className="text-muted-foreground text-sm">—</span>
        );
      },
    },
    {
      id: 'clinical_significance',
      header: 'Significancia Clinica',
      cell: ({ row }) => {
        const ann = row.original.annotation;
        if (!ann?.clinical_significance) {
          return <span className="text-muted-foreground text-sm">—</span>;
        }
        return (
          <Badge
            variant="outline"
            className={`text-xs ${clinicalBadgeClass(ann.clinical_significance)}`}
          >
            {ann.clinical_significance}
          </Badge>
        );
      },
    },
    {
      id: 'chromosome',
      header: 'Cromossomo',
      cell: ({ row }) => {
        const ann = row.original.annotation;
        return ann?.chromosome ? (
          <span className="font-mono text-sm">chr{ann.chromosome}</span>
        ) : (
          <span className="text-muted-foreground text-sm">—</span>
        );
      },
    },
    {
      id: 'maf',
      header: 'MAF',
      cell: ({ row }) => {
        const ann = row.original.annotation;
        if (ann?.maf == null) {
          return <span className="text-muted-foreground text-sm">—</span>;
        }
        return (
          <span className="font-mono text-sm tabular-nums">
            {ann.maf.toFixed(4)}
          </span>
        );
      },
    },
  ];

  const table = useReactTable({
    data: variants,
    columns,
    getCoreRowModel: getCoreRowModel(),
    getRowId: (row) => row.rs_number,
  });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-4">
        <Input
          placeholder="Filtrar por rs number..."
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
                  Nenhuma variante encontrada.
                </TableCell>
              </TableRow>
            ) : (
              table.getRowModel().rows.map((row) => (
                <TableRow
                  key={row.id}
                  className="cursor-pointer hover:bg-muted/50"
                  data-state={
                    row.original.rs_number === selectedRsNumber ? 'selected' : undefined
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
