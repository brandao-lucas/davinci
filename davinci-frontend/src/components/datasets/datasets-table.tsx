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
import { Badge } from '@/components/ui/badge';
import { truncate, formatNumber } from '@/lib/utils/format';
import type { OmicDataset } from '@/lib/types/dataset';

const statusColors: Record<string, string> = {
  pending: 'bg-amber-100 text-amber-800',
  included: 'bg-green-100 text-green-800',
  excluded: 'bg-red-100 text-red-800',
  queued: 'bg-blue-100 text-blue-800',
  downloaded: 'bg-teal-100 text-teal-800',
};

interface DatasetsTableProps {
  datasets: OmicDataset[];
  onSelect?: (dataset: OmicDataset) => void;
}

export function DatasetsTable({ datasets, onSelect }: DatasetsTableProps) {
  const columns: ColumnDef<OmicDataset>[] = [
    {
      accessorKey: 'accession',
      header: 'Accession',
      size: 110,
      cell: ({ getValue }) => <span className="font-mono text-xs">{getValue<string>()}</span>,
    },
    {
      accessorKey: 'title',
      header: 'Title',
      cell: ({ getValue }) => (
        <span title={getValue<string>()}>{truncate(getValue<string>(), 70)}</span>
      ),
    },
    {
      accessorKey: 'source_db',
      header: 'DB',
      size: 90,
    },
    {
      accessorKey: 'omic_type',
      header: 'Omic',
      size: 120,
    },
    {
      accessorKey: 'organism',
      header: 'Organism',
      size: 130,
      cell: ({ getValue }) => <span className="italic text-sm">{getValue<string>()}</span>,
    },
    {
      accessorKey: 'n_samples',
      header: 'Samples',
      size: 80,
      cell: ({ getValue }) => {
        const v = getValue<number | null>();
        return v !== null ? formatNumber(v) : '—';
      },
    },
    {
      accessorKey: 'curation_status',
      header: 'Status',
      size: 100,
      cell: ({ getValue }) => (
        <Badge className={statusColors[getValue<string>()] ?? ''} variant="outline">
          {getValue<string>()}
        </Badge>
      ),
    },
  ];

  const table = useReactTable({
    data: datasets,
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  return (
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
              <TableCell colSpan={columns.length} className="h-24 text-center text-muted-foreground">
                No datasets found.
              </TableCell>
            </TableRow>
          ) : (
            table.getRowModel().rows.map((row) => (
              <TableRow
                key={row.id}
                className="cursor-pointer hover:bg-muted/50"
                onClick={() => onSelect?.(row.original)}
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
  );
}
