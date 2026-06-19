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
import type { ProjectMeSHList, MeSHFilters } from '@/lib/types/mesh';

type OrderingField =
  | 'major_topic_count'
  | 'unique_citations_included'
  | 'unique_citations_total'
  | 'descriptor';

function nextOrdering(
  current: MeSHFilters['ordering'],
  field: OrderingField,
): MeSHFilters['ordering'] {
  if (current === field) return `-${field}` as MeSHFilters['ordering'];
  if (current === `-${field}`) return field as MeSHFilters['ordering'];
  return `-${field}` as MeSHFilters['ordering'];
}

function SortIcon({
  field,
  current,
}: {
  field: OrderingField;
  current: MeSHFilters['ordering'];
}) {
  if (current === field) return <ArrowUp className="ml-1 h-3 w-3" />;
  if (current === `-${field}`) return <ArrowDown className="ml-1 h-3 w-3" />;
  return <ArrowUpDown className="ml-1 h-3 w-3 opacity-40" />;
}

interface MeSHTableProps {
  terms: ProjectMeSHList[];
  ordering: MeSHFilters['ordering'];
  search: string;
  includedOnly: boolean;
  onOrderingChange: (ordering: MeSHFilters['ordering']) => void;
  onSearchChange: (value: string) => void;
  onIncludedOnlyChange: (checked: boolean) => void;
  onSelect: (term: ProjectMeSHList) => void;
  selectedDescriptor?: string | null;
}

export function MeSHTable({
  terms,
  ordering,
  search,
  includedOnly,
  onOrderingChange,
  onSearchChange,
  onIncludedOnlyChange,
  onSelect,
  selectedDescriptor,
}: MeSHTableProps) {
  const columns: ColumnDef<ProjectMeSHList>[] = [
    {
      accessorKey: 'descriptor',
      header: () => (
        <Button
          variant="ghost"
          size="sm"
          className="-ml-3 h-8 font-semibold"
          onClick={() => onOrderingChange(nextOrdering(ordering, 'descriptor'))}
        >
          Descriptor
          <SortIcon field="descriptor" current={ordering} />
        </Button>
      ),
      cell: ({ getValue }) => (
        <span className="font-medium text-sm">{getValue<string>()}</span>
      ),
    },
    {
      accessorKey: 'major_topic_count',
      header: () => (
        <div className="flex items-center">
          <Button
            variant="ghost"
            size="sm"
            className="-ml-3 h-8 font-semibold"
            onClick={() => onOrderingChange(nextOrdering(ordering, 'major_topic_count'))}
          >
            Major Topic
            <SortIcon field="major_topic_count" current={ordering} />
          </Button>
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <span
                  role="img"
                  aria-label="Informações sobre a coluna Major Topic"
                  className="ml-1 cursor-help text-muted-foreground hover:text-foreground inline-flex items-center"
                >
                  <Info className="h-3.5 w-3.5" />
                </span>
              </TooltipTrigger>
              <TooltipContent side="top" className="max-w-xs text-xs">
                <p className="font-semibold mb-1">Major Topic</p>
                <p>
                  Quantidade de papers com status &quot;incluído&quot; onde este descriptor é
                  tópico principal (MajorTopicYN=&quot;Y&quot;). Cada paper conta apenas
                  uma vez — é a métrica primária de relevância do termo MeSH.
                </p>
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </div>
      ),
      cell: ({ getValue }) => (
        <span className="font-mono text-sm tabular-nums font-semibold text-purple-700">
          {getValue<number>()}
        </span>
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
                  papers com status &quot;incluído&quot; que citam este descriptor MeSH —
                  cada paper conta apenas uma vez.
                </p>
                <p>
                  <span className="font-semibold">Total:</span>{' '}
                  papers de qualquer status de curadoria que citam o descriptor —
                  também uma citação única por paper distinto.
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
      id: 'mesh_link',
      header: 'MeSH',
      cell: ({ row }) => {
        const { descriptor, ncbi_mesh_url } = row.original;
        const url = ncbi_mesh_url ?? `https://www.ncbi.nlm.nih.gov/mesh/?term=${encodeURIComponent(descriptor)}`;

        return (
          <div onClick={(e) => e.stopPropagation()}>
            <a
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-xs text-blue-600 hover:underline"
              title={`Buscar "${descriptor}" no NCBI MeSH`}
            >
              NCBI
              <ExternalLink className="h-3 w-3" />
            </a>
          </div>
        );
      },
    },
  ];

  const table = useReactTable({
    data: terms,
    columns,
    getCoreRowModel: getCoreRowModel(),
    getRowId: (row) => row.descriptor,
  });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-4">
        <Input
          placeholder="Filtrar por descriptor MeSH..."
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
                  Nenhum descriptor MeSH encontrado.
                </TableCell>
              </TableRow>
            ) : (
              table.getRowModel().rows.map((row) => (
                <TableRow
                  key={row.id}
                  className="cursor-pointer hover:bg-muted/50"
                  data-state={
                    row.original.descriptor === selectedDescriptor ? 'selected' : undefined
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
