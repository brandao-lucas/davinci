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
import type { ProjectGeneList, GeneFilters } from '@/lib/types/gene';

type OrderingField =
  | 'unique_citations_included'
  | 'unique_citations_total'
  | 'mention_count_total'
  | 'gene_symbol';

function nextOrdering(
  current: GeneFilters['ordering'],
  field: OrderingField,
): GeneFilters['ordering'] {
  if (current === field) return `-${field}` as GeneFilters['ordering'];
  if (current === `-${field}`) return field as GeneFilters['ordering'];
  return `-${field}` as GeneFilters['ordering'];
}

function SortIcon({
  field,
  current,
}: {
  field: OrderingField;
  current: GeneFilters['ordering'];
}) {
  if (current === field) return <ArrowUp className="ml-1 h-3 w-3" />;
  if (current === `-${field}`) return <ArrowDown className="ml-1 h-3 w-3" />;
  return <ArrowUpDown className="ml-1 h-3 w-3 opacity-40" />;
}

interface GenesTableProps {
  genes: ProjectGeneList[];
  ordering: GeneFilters['ordering'];
  search: string;
  includedOnly: boolean;
  onOrderingChange: (ordering: GeneFilters['ordering']) => void;
  onSearchChange: (value: string) => void;
  onIncludedOnlyChange: (checked: boolean) => void;
  onSelect: (gene: ProjectGeneList) => void;
  selectedSymbol?: string | null;
}

export function GenesTable({
  genes,
  ordering,
  search,
  includedOnly,
  onOrderingChange,
  onSearchChange,
  onIncludedOnlyChange,
  onSelect,
  selectedSymbol,
}: GenesTableProps) {
  const columns: ColumnDef<ProjectGeneList>[] = [
    {
      accessorKey: 'gene_symbol',
      header: () => (
        <Button
          variant="ghost"
          size="sm"
          className="-ml-3 h-8 font-semibold"
          onClick={() => onOrderingChange(nextOrdering(ordering, 'gene_symbol'))}
        >
          Gene
          <SortIcon field="gene_symbol" current={ordering} />
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
                  quantidade de papers com status &quot;incluído&quot; que citam este gene — cada paper conta apenas uma vez, independentemente de quantas vezes menciona o gene.
                </p>
                <p>
                  <span className="font-semibold">Total:</span>{' '}
                  quantidade de papers de qualquer status de curadoria que citam o gene — também uma citação única por paper distinto.
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
            Menções
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
                  Soma de <span className="font-semibold">todas</span> as ocorrências do gene em todos os papers do projeto, incluindo múltiplas menções dentro de um mesmo paper.
                </p>
                <p>
                  Diferente de &quot;Citações&quot;, que conta <span className="font-semibold">um paper distinto por linha</span>, Menções soma cada ocorrência individualmente — portanto pode ser maior que o número de papers que citam o gene.
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
      id: 'ncbi_links',
      header: 'NCBI',
      cell: ({ row }) => {
        const { gene_symbol, entrez_id } = row.original;
        const searchUrl = `https://www.ncbi.nlm.nih.gov/gene/?term=${encodeURIComponent(gene_symbol)}`;
        const geneUrl = entrez_id
          ? `https://www.ncbi.nlm.nih.gov/gene/${entrez_id}`
          : null;

        return (
          <div className="flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
            {geneUrl && (
              <a
                href={geneUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-xs text-blue-600 hover:underline"
                title={`NCBI Gene: ${entrez_id}`}
              >
                ID:{entrez_id}
                <ExternalLink className="h-3 w-3" />
              </a>
            )}
            <a
              href={searchUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-xs text-blue-600 hover:underline"
              title={`Buscar "${gene_symbol}" no NCBI Gene`}
            >
              Buscar
              <ExternalLink className="h-3 w-3" />
            </a>
          </div>
        );
      },
    },
  ];

  const table = useReactTable({
    data: genes,
    columns,
    getCoreRowModel: getCoreRowModel(),
    getRowId: (row) => row.gene_symbol,
  });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-4">
        <Input
          placeholder="Filtrar por simbolo do gene..."
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
                  Nenhum gene encontrado.
                </TableCell>
              </TableRow>
            ) : (
              table.getRowModel().rows.map((row) => (
                <TableRow
                  key={row.id}
                  className="cursor-pointer hover:bg-muted/50"
                  data-state={
                    row.original.gene_symbol === selectedSymbol ? 'selected' : undefined
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
