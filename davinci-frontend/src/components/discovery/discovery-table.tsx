'use client';

import {
  useReactTable,
  getCoreRowModel,
  flexRender,
  type ColumnDef,
} from '@tanstack/react-table';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Badge } from '@/components/ui/badge';
import { Lock, ExternalLink } from 'lucide-react';
import type { DiscoveryItem } from '@/lib/types/discovery';

// Cores por camada ômica
const layerColors: Record<string, string> = {
  genomic: 'bg-blue-100 text-blue-800',
  transcriptomic: 'bg-green-100 text-green-800',
  proteomic: 'bg-purple-100 text-purple-800',
  metabolomic: 'bg-orange-100 text-orange-800',
  epigenomic: 'bg-pink-100 text-pink-800',
  metagenomic: 'bg-teal-100 text-teal-800',
  microbiome: 'bg-lime-100 text-lime-800',
};

function layerBadgeClass(layer: string): string {
  return layerColors[layer] ?? 'bg-gray-100 text-gray-800';
}

// Renderiza o valor de confiança de um eixo do contract_confidence.
// disease_axis pode ser sub-objeto; os demais são float 0–1 ou undefined.
function renderConfidence(value: unknown): string {
  if (value === undefined || value === null) return '—';
  if (typeof value === 'number') return value.toFixed(2);
  // sub-objeto (ex: disease_axis com score/method)
  if (typeof value === 'object') {
    const v = value as { score?: number };
    if (typeof v.score === 'number') return v.score.toFixed(2);
    return '~';
  }
  return String(value);
}

// Renderiza matrix_pointer como link (apenas http/https e sem acesso_controlado),
// ou como texto puro nos demais casos.
// Regras (M8 007):
//   1. access_controlled=true → sempre texto puro, nunca link clicável.
//   2. Somente http/https são links clicáveis; ftp/ftps e outros são texto puro.
// NUNCA renderizar o conteúdo da matriz — é ponteiro de acesso (Objetivo 2).
function MatrixPointer({
  pointer,
  controlled,
}: {
  pointer?: string | null;
  controlled: boolean;
}) {
  if (!pointer) return <span className="text-muted-foreground text-xs">—</span>;

  const isHttps = pointer.startsWith('http://') || pointer.startsWith('https://');
  const truncated = pointer.length > 40 ? pointer.slice(0, 40) + '…' : pointer;

  // Link clicável apenas para http(s) e sem acesso controlado
  const content = isHttps && !controlled ? (
    <a
      href={pointer}
      target="_blank"
      rel="noopener noreferrer"
      className="flex items-center gap-1 text-xs text-blue-600 hover:underline font-mono"
      title={pointer}
    >
      {truncated}
      <ExternalLink className="h-3 w-3 shrink-0" />
    </a>
  ) : (
    // ftp/ftps, outros esquemas, ou acesso controlado → texto puro
    <span
      className="font-mono text-xs text-muted-foreground"
      title={controlled ? 'Acesso controlado — requer credenciais (Objetivo 2)' : pointer}
    >
      {truncated}
    </span>
  );

  return (
    <div className="flex items-center gap-1">
      {content}
    </div>
  );
}

interface DiscoveryTableProps {
  items: DiscoveryItem[];
}

export function DiscoveryTable({ items }: DiscoveryTableProps) {
  const columns: ColumnDef<DiscoveryItem>[] = [
    {
      accessorKey: 'accession',
      header: 'Accession',
      size: 120,
      cell: ({ getValue, row }) => (
        <div className="flex items-center gap-1.5">
          <span className="font-mono text-xs">{getValue<string>()}</span>
          {row.original.access_controlled && (
            <Lock className="h-3 w-3 text-amber-600 shrink-0" aria-label="Acesso controlado" />
          )}
        </div>
      ),
    },
    {
      accessorKey: 'source_db',
      header: 'DB',
      size: 80,
      cell: ({ getValue }) => (
        <span className="text-xs uppercase font-semibold text-muted-foreground">
          {getValue<string>()}
        </span>
      ),
    },
    {
      accessorKey: 'omics_layers',
      header: 'Camadas',
      cell: ({ getValue }) => {
        const layers = getValue<string[]>();
        return (
          <div className="flex flex-wrap gap-1">
            {layers.map((l) => (
              <span
                key={l}
                className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-semibold ${layerBadgeClass(l)}`}
              >
                {l}
              </span>
            ))}
          </div>
        );
      },
    },
    {
      accessorKey: 'omics_count',
      header: 'N.o',
      size: 50,
      cell: ({ getValue }) => {
        const v = getValue<number | null>();
        return <span className="text-xs">{v ?? '—'}</span>;
      },
    },
    {
      accessorKey: 'disease_axis',
      header: 'Eixo',
      size: 110,
      cell: ({ getValue, row }) => {
        const axis = getValue<string>();
        const conf = row.original.contract_confidence;
        const score = conf ? renderConfidence((conf as Record<string, unknown>)['disease_axis']) : '—';
        return (
          <div className="space-y-0.5">
            <span className="text-xs">{axis}</span>
            <div className="text-xs text-muted-foreground">conf: {score}</div>
          </div>
        );
      },
    },
    {
      accessorKey: 'has_control_group',
      header: 'Controle',
      size: 90,
      cell: ({ getValue, row }) => {
        const v = getValue<string>();
        const conf = row.original.contract_confidence;
        const score = conf ? renderConfidence((conf as Record<string, unknown>)['has_control_group']) : '—';
        return (
          <div className="space-y-0.5">
            <span className="text-xs">{v}</span>
            <div className="text-xs text-muted-foreground">conf: {score}</div>
          </div>
        );
      },
    },
    {
      accessorKey: 'is_single_cell',
      header: 'Resolução',
      size: 90,
      cell: ({ getValue, row }) => {
        const v = getValue<string>();
        const conf = row.original.contract_confidence;
        const score = conf ? renderConfidence((conf as Record<string, unknown>)['is_single_cell']) : '—';
        return (
          <div className="space-y-0.5">
            <span className="text-xs">{v}</span>
            <div className="text-xs text-muted-foreground">conf: {score}</div>
          </div>
        );
      },
    },
    {
      accessorKey: 'data_format',
      header: 'Formato',
      size: 80,
      cell: ({ getValue }) => <span className="text-xs">{getValue<string>()}</span>,
    },
    {
      accessorKey: 'access_type',
      header: 'Acesso',
      size: 100,
      cell: ({ getValue, row }) => {
        const v = getValue<string>();
        const controlled = row.original.access_controlled;
        return (
          <div className="flex items-center gap-1">
            {controlled ? (
              <Badge
                variant="destructive"
                className="text-xs flex items-center gap-1"
              >
                <Lock className="h-2.5 w-2.5" />
                Controlado
              </Badge>
            ) : (
              <span className="text-xs">{v}</span>
            )}
          </div>
        );
      },
    },
    {
      id: 'monogenic_gene',
      header: 'Gene mono.',
      size: 110,
      cell: ({ row }) => {
        // monogenic_gene_hit é OBJETO (não bool): { genes, confidence, gene_details }
        const hit = row.original.contract?.monogenic_gene_hit;
        if (!hit || !hit.genes || hit.genes.length === 0) {
          return <span className="text-xs text-muted-foreground">—</span>;
        }
        return (
          <div className="flex flex-wrap gap-1">
            {hit.genes.map((g) => (
              <Badge key={g} variant="secondary" className="text-xs font-mono">
                {g}
              </Badge>
            ))}
          </div>
        );
      },
    },
    {
      id: 'sample_join_key',
      header: 'Chaves pareamento',
      cell: ({ row }) => {
        const keys = row.original.sample_join_key;
        const conf = row.original.contract_confidence;
        const score = conf ? renderConfidence((conf as Record<string, unknown>)['sample_join_key']) : null;
        if (!keys || keys.length === 0) {
          return <span className="text-xs text-muted-foreground">—</span>;
        }
        return (
          <div className="space-y-0.5">
            <div className="flex flex-wrap gap-1">
              {keys.map((k) => (
                <Badge key={k} variant="outline" className="text-xs font-mono">
                  {k}
                </Badge>
              ))}
            </div>
            {score && (
              <div className="text-xs text-muted-foreground">conf: {score}</div>
            )}
          </div>
        );
      },
    },
    {
      id: 'matrix_pointer',
      header: 'Ponteiro de matriz',
      cell: ({ row }) => (
        <MatrixPointer
          pointer={row.original.contract?.matrix_pointer}
          controlled={row.original.access_controlled}
        />
      ),
    },
  ];

  const table = useReactTable({
    data: items,
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  if (items.length === 0) {
    return (
      <div className="flex items-center justify-center h-32 text-muted-foreground text-sm">
        Nenhum dataset encontrado com os filtros atuais.
      </div>
    );
  }

  return (
    <div className="rounded-md border overflow-x-auto">
      <Table>
        <TableHeader>
          {table.getHeaderGroups().map((headerGroup) => (
            <TableRow key={headerGroup.id}>
              {headerGroup.headers.map((header) => (
                <TableHead key={header.id} style={{ width: header.getSize() }}>
                  {flexRender(header.column.columnDef.header, header.getContext())}
                </TableHead>
              ))}
            </TableRow>
          ))}
        </TableHeader>
        <TableBody>
          {table.getRowModel().rows.map((row) => (
            <TableRow
              key={row.id}
              className={row.original.access_controlled ? 'bg-amber-50/50' : undefined}
            >
              {row.getVisibleCells().map((cell) => (
                <TableCell key={cell.id} className="py-2 align-top">
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
