'use client';

import { useState } from 'react';
import { toast } from 'sonner';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
  DialogDescription,
} from '@/components/ui/dialog';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { Skeleton } from '@/components/ui/skeleton';
import { QueryErrorState } from '@/components/ui/query-error-state';
import { useCurationQueue, useResolveCurationItem } from '@/lib/hooks/use-curation-queue';
import type { CurationQueueItem } from '@/lib/types/curation-queue';
import { CheckCircle2, XCircle, AlertCircle, Loader2 } from 'lucide-react';

interface CurationQueueTableProps {
  projectId: string;
}

function ScoreBadge({ score }: { score: number | null }) {
  if (score === null) return <span className="text-muted-foreground text-xs">—</span>;
  const pct = Math.round(score * 100);
  const variant = pct < 30 ? 'destructive' : 'secondary';
  return (
    <Badge variant={variant} className="text-xs font-mono">
      {pct}%
    </Badge>
  );
}

interface ResolveDialogProps {
  item: CurationQueueItem | null;
  projectId: string;
  onClose: () => void;
}

function ResolveDialog({ item, projectId, onClose }: ResolveDialogProps) {
  const [notes, setNotes] = useState('');
  const [decision, setDecision] = useState<'yes' | 'no' | null>(null);
  const { mutate: resolve, isPending } = useResolveCurationItem(projectId);

  if (!item) return null;

  function handleResolve() {
    if (!decision) {
      toast.error('Selecione uma decisão antes de confirmar.');
      return;
    }
    resolve(
      { projectDatasetId: item!.id, data: { has_control_group: decision, notes } },
      {
        onSuccess: () => {
          setNotes('');
          setDecision(null);
          onClose();
        },
      },
    );
  }

  function handleOpenChange(open: boolean) {
    if (!open) {
      setNotes('');
      setDecision(null);
      onClose();
    }
  }

  return (
    <Dialog open={!!item} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Resolver curadoria — {item.accession}</DialogTitle>
          <DialogDescription>
            Revise o contexto abaixo e defina se o dataset possui grupo controle.
            Esta decisão é auditada e irreversível via fila (pode ser editada depois no detalhe do dataset).
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {/* Contexto do dataset */}
          <div className="rounded-md border p-4 space-y-2 bg-muted/40">
            <div className="flex items-center gap-2 flex-wrap">
              <Badge variant="outline" className="text-xs">{item.source_db.toUpperCase()}</Badge>
              <Badge variant="outline" className="text-xs">{item.omic_type}</Badge>
              {item.organism && (
                <span className="text-xs text-muted-foreground italic">{item.organism}</span>
              )}
              {item.n_samples !== null && (
                <span className="text-xs text-muted-foreground">{item.n_samples} amostras</span>
              )}
              <span className="text-xs text-muted-foreground ml-auto">
                Score do classificador: <ScoreBadge score={item.has_control_group_score} />
              </span>
            </div>
            {item.title && (
              <p className="text-sm font-medium">{item.title}</p>
            )}
            {item.summary && (
              <p className="text-sm text-muted-foreground leading-relaxed line-clamp-6">
                {item.summary}
              </p>
            )}
          </div>

          {/* Decisão */}
          <div className="space-y-2">
            <Label>Decisão do curador</Label>
            <div className="flex gap-3">
              <Button
                type="button"
                variant={decision === 'yes' ? 'default' : 'outline'}
                className="flex-1 gap-2"
                onClick={() => setDecision('yes')}
              >
                <CheckCircle2 className="h-4 w-4" />
                Com grupo controle
              </Button>
              <Button
                type="button"
                variant={decision === 'no' ? 'destructive' : 'outline'}
                className="flex-1 gap-2"
                onClick={() => setDecision('no')}
              >
                <XCircle className="h-4 w-4" />
                Sem grupo controle
              </Button>
            </div>
          </div>

          {/* Notas */}
          <div className="space-y-2">
            <Label htmlFor="curation-notes">
              Notas <span className="text-muted-foreground text-xs">(opcional, auditável)</span>
            </Label>
            <Textarea
              id="curation-notes"
              placeholder="Justificativa da decisão, evidências observadas..."
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              maxLength={2000}
              rows={3}
            />
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={isPending}>
            Cancelar
          </Button>
          <Button
            onClick={handleResolve}
            disabled={!decision || isPending}
          >
            {isPending && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
            Confirmar curadoria
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function CurationQueueTable({ projectId }: CurationQueueTableProps) {
  const { data: items, isLoading, isError, error, refetch } = useCurationQueue(projectId);
  const [selectedItem, setSelectedItem] = useState<CurationQueueItem | null>(null);

  if (isLoading) {
    return (
      <div className="space-y-2">
        {Array.from({ length: 5 }).map((_, i) => (
          <Skeleton key={i} className="h-12 w-full" />
        ))}
      </div>
    );
  }

  if (isError) {
    return <QueryErrorState error={error} onRetry={() => refetch()} />;
  }

  if (!items || items.length === 0) {
    return (
      <div className="flex flex-col items-center gap-2 py-16 text-center text-muted-foreground">
        <CheckCircle2 className="h-8 w-8 text-green-500" />
        <p className="text-sm">Nenhum dataset pendente de curadoria manual.</p>
        <p className="text-xs">
          Itens aparecem aqui quando o classificador automático retorna confiança &lt; 50%.
        </p>
      </div>
    );
  }

  return (
    <>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Accession</TableHead>
            <TableHead>Fonte</TableHead>
            <TableHead>Tipo ômico</TableHead>
            <TableHead>Título</TableHead>
            <TableHead className="text-right">Score</TableHead>
            <TableHead className="text-right">Ação</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {items.map((item) => (
            <TableRow key={item.id}>
              <TableCell className="font-mono text-xs">{item.accession}</TableCell>
              <TableCell>
                <Badge variant="outline" className="text-xs">{item.source_db.toUpperCase()}</Badge>
              </TableCell>
              <TableCell className="text-xs text-muted-foreground">{item.omic_type}</TableCell>
              <TableCell className="max-w-xs">
                <span className="line-clamp-2 text-sm">
                  {item.title ?? <span className="text-muted-foreground italic">Sem título</span>}
                </span>
              </TableCell>
              <TableCell className="text-right">
                <ScoreBadge score={item.has_control_group_score} />
              </TableCell>
              <TableCell className="text-right">
                <Button
                  size="sm"
                  variant="outline"
                  className="gap-1.5"
                  onClick={() => setSelectedItem(item)}
                >
                  <AlertCircle className="h-3.5 w-3.5 text-amber-500" />
                  Resolver
                </Button>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>

      <ResolveDialog
        item={selectedItem}
        projectId={projectId}
        onClose={() => setSelectedItem(null)}
      />
    </>
  );
}
