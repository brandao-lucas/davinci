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
import { truncate } from '@/lib/utils/format';
import type { Paper } from '@/lib/types/paper';

const statusColors: Record<string, string> = {
  pending: 'bg-amber-100 text-amber-800',
  included: 'bg-green-100 text-green-800',
  excluded: 'bg-red-100 text-red-800',
  maybe: 'bg-violet-100 text-violet-800',
};

interface PapersTableProps {
  papers: Paper[];
  onSelect?: (paper: Paper) => void;
  onSelectionChange?: (paperIds: number[]) => void;
}

export function PapersTable({ papers, onSelect, onSelectionChange }: PapersTableProps) {
  const [rowSelection, setRowSelection] = useState<Record<string, boolean>>({});

  const columns: ColumnDef<Paper>[] = [
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
      accessorKey: 'pmid',
      header: 'PMID',
      size: 90,
    },
    {
      accessorKey: 'title',
      header: 'Title',
      cell: ({ getValue }) => (
        <span title={getValue<string>()}>{truncate(getValue<string>(), 80)}</span>
      ),
    },
    {
      accessorKey: 'journal',
      header: 'Journal',
      size: 160,
      cell: ({ getValue }) => <span className="text-sm">{truncate(getValue<string>(), 30)}</span>,
    },
    {
      accessorKey: 'pub_year',
      header: 'Year',
      size: 70,
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
      accessorKey: 'relevance_score',
      header: 'Score',
      size: 70,
      cell: ({ getValue }) => {
        const v = getValue<number | null>();
        return v !== null ? v.toFixed(2) : '—';
      },
    },
  ];

  const table = useReactTable({
    data: papers,
    columns,
    state: { rowSelection },
    onRowSelectionChange: (updater) => {
      const next = typeof updater === 'function' ? updater(rowSelection) : updater;
      setRowSelection(next);
      onSelectionChange?.(
        Object.keys(next).filter((k) => next[k]).map((k) => papers[Number(k)]?.id).filter(Boolean)
      );
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
                No papers found.
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
