'use client';

import { useState } from 'react';
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
import { Checkbox } from '@/components/ui/checkbox';
import { truncate, formatDate } from '@/lib/utils/format';
import type { ProjectSample } from '@/lib/types/sample';

const statusColors: Record<string, string> = {
  pending: 'bg-amber-100 text-amber-800',
  included: 'bg-green-100 text-green-800',
  excluded: 'bg-red-100 text-red-800',
  maybe: 'bg-violet-100 text-violet-800',
};

interface SamplesTableProps {
  samples: ProjectSample[];
  onSelect?: (sample: ProjectSample) => void;
  onSelectionChange?: (sampleIds: number[]) => void;
}

export function SamplesTable({ samples, onSelect, onSelectionChange }: SamplesTableProps) {
  const [rowSelection, setRowSelection] = useState<Record<string, boolean>>({});

  const columns: ColumnDef<ProjectSample>[] = [
    {
      id: 'select',
      header: ({ table }) => (
        <Checkbox
          checked={table.getIsAllPageRowsSelected()}
          onCheckedChange={(value) => table.toggleAllPageRowsSelected(!!value)}
        />
      ),
      cell: ({ row }) => (
        <Checkbox
          checked={row.getIsSelected()}
          onCheckedChange={(value) => row.toggleSelected(!!value)}
          onClick={(e) => e.stopPropagation()}
        />
      ),
      size: 40,
    },
    {
      accessorKey: 'accession',
      header: 'Accession',
      size: 110,
      cell: ({ getValue }) => (
        <span className="font-mono text-xs">{getValue<string>()}</span>
      ),
    },
    {
      accessorKey: 'title',
      header: 'Title',
      cell: ({ getValue }) => (
        <span title={getValue<string>()}>{truncate(getValue<string>(), 70)}</span>
      ),
    },
    {
      accessorKey: 'organism',
      header: 'Organism',
      size: 130,
      cell: ({ getValue }) => (
        <span className="italic text-sm">{getValue<string>()}</span>
      ),
    },
    {
      accessorKey: 'platform',
      header: 'Platform',
      size: 120,
      cell: ({ getValue }) => {
        const v = getValue<string | null>();
        return v ? <span className="text-xs">{truncate(v, 20)}</span> : <span className="text-muted-foreground">—</span>;
      },
    },
    {
      accessorKey: 'dataset_accession',
      header: 'Dataset',
      size: 100,
      cell: ({ getValue }) => (
        <span className="font-mono text-xs text-muted-foreground">{getValue<string>()}</span>
      ),
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
    {
      accessorKey: 'curated_at',
      header: 'Curated at',
      size: 110,
      cell: ({ getValue }) => {
        const v = getValue<string | null>();
        return v ? (
          <span className="text-xs text-muted-foreground">{formatDate(v)}</span>
        ) : (
          <span className="text-muted-foreground">—</span>
        );
      },
    },
  ];

  const table = useReactTable({
    data: samples,
    columns,
    state: { rowSelection },
    onRowSelectionChange: (updater) => {
      const next = typeof updater === 'function' ? updater(rowSelection) : updater;
      setRowSelection(next);
      onSelectionChange?.(Object.keys(next).filter((k) => next[k]).map(Number));
    },
    getCoreRowModel: getCoreRowModel(),
    getRowId: (row) => String(row.id),
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
                No samples found.
              </TableCell>
            </TableRow>
          ) : (
            table.getRowModel().rows.map((row) => (
              <TableRow
                key={row.id}
                className="cursor-pointer hover:bg-muted/50"
                data-state={row.getIsSelected() ? 'selected' : undefined}
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
