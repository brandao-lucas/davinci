'use client';

import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogFooter,
} from '@/components/ui/dialog';
import { useBulkCurateDataset, useBulkCurateDatasetByFilter } from '@/lib/hooks/use-datasets';
import type { DatasetFilters } from '@/lib/types/dataset';

interface DatasetBulkCurationBarProps {
  projectId: string;
  selectedIds: number[];
  onClear: () => void;
  /** Filtros ativos na listagem — permite "excluir todos os filtrados" */
  activeFilters?: DatasetFilters;
  /** Total de itens correspondentes ao filtro atual (mostrado no diálogo de confirmação) */
  filteredTotal?: number;
}

export function DatasetBulkCurationBar({ projectId, selectedIds, onClear, activeFilters, filteredTotal }: DatasetBulkCurationBarProps) {
  const [excludeDialogOpen, setExcludeDialogOpen] = useState(false);
  const [excludeAllDialogOpen, setExcludeAllDialogOpen] = useState(false);
  const [reason, setReason] = useState('');
  const [reasonAll, setReasonAll] = useState('');
  const bulkCurate = useBulkCurateDataset(projectId);
  const bulkCurateByFilter = useBulkCurateDatasetByFilter(projectId);

  const hasActiveFilters = activeFilters && Object.values(activeFilters).some(v => v !== undefined && v !== '');
  const showExcludeAll = hasActiveFilters;

  if (selectedIds.length === 0 && !showExcludeAll) return null;

  const handleCurate = async (status: string, exclusionReason?: string) => {
    await bulkCurate.mutateAsync({ dataset_ids: selectedIds, curation_status: status, exclusion_reason: exclusionReason });
    onClear();
  };

  const handleExcludeAll = async () => {
    if (!activeFilters) return;
    await bulkCurateByFilter.mutateAsync({
      filters: activeFilters,
      curation_status: 'excluded',
      exclusion_reason: reasonAll || undefined,
    });
    setExcludeAllDialogOpen(false);
    setReasonAll('');
    onClear();
  };

  return (
    <>
      <div className="fixed bottom-0 left-0 right-0 bg-background border-t shadow-lg py-3 px-6 flex items-center gap-4 z-50">
        {selectedIds.length > 0 && (
          <>
            <span className="text-sm font-medium">{selectedIds.length} selected</span>
            <div className="flex gap-2">
              <Button size="sm" variant="outline" className="text-green-700 border-green-300"
                onClick={() => handleCurate('included')}>
                Include
              </Button>
              <Button size="sm" variant="outline" className="text-blue-700 border-blue-300"
                onClick={() => handleCurate('queued')}>
                Queue
              </Button>
              <Button size="sm" variant="outline" className="text-teal-700 border-teal-300"
                onClick={() => handleCurate('downloaded')}>
                Downloaded
              </Button>
              <Button size="sm" variant="outline" className="text-red-700 border-red-300"
                onClick={() => setExcludeDialogOpen(true)}>
                Exclude
              </Button>
            </div>
          </>
        )}

        {showExcludeAll && (
          <Button
            size="sm"
            variant="outline"
            className="text-red-700 border-red-300 ml-auto"
            onClick={() => setExcludeAllDialogOpen(true)}
            disabled={bulkCurateByFilter.isPending}
          >
            Excluir todos os filtrados
            {filteredTotal !== undefined && ` (${filteredTotal})`}
          </Button>
        )}

        {selectedIds.length > 0 && (
          <Button size="sm" variant="ghost" onClick={onClear} className={showExcludeAll ? '' : 'ml-auto'}>
            Clear selection
          </Button>
        )}
      </div>

      {/* Diálogo: excluir selecionados */}
      <Dialog open={excludeDialogOpen} onOpenChange={setExcludeDialogOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Excluir {selectedIds.length} datasets</DialogTitle>
            <DialogDescription>Informe o motivo da exclusão.</DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1.5">
              <Label>Motivo da exclusão (obrigatório)</Label>
              <Input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="Organismo incorreto, tipo de dado errado…"
              />
            </div>
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setExcludeDialogOpen(false)}>Cancelar</Button>
              <Button
                variant="destructive"
                disabled={!reason.trim() || bulkCurate.isPending}
                onClick={() => {
                  handleCurate('excluded', reason);
                  setExcludeDialogOpen(false);
                  setReason('');
                }}
              >
                Excluir
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      {/* Diálogo: excluir todos os filtrados */}
      <Dialog open={excludeAllDialogOpen} onOpenChange={setExcludeAllDialogOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Excluir todos os datasets filtrados?</DialogTitle>
            <DialogDescription>
              Esta ação excluirá em massa{' '}
              {filteredTotal !== undefined ? (
                <strong>{filteredTotal} datasets</strong>
              ) : (
                'todos os datasets'
              )}{' '}
              que correspondem aos filtros ativos. Esta operação não pode ser desfeita individualmente — use com cuidado.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3 py-2">
            <div className="space-y-1.5">
              <Label>Motivo da exclusão (opcional)</Label>
              <Textarea
                value={reasonAll}
                onChange={(e) => setReasonAll(e.target.value)}
                placeholder="Não relevantes para o critério aplicado…"
                rows={2}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setExcludeAllDialogOpen(false)}>Cancelar</Button>
            <Button
              variant="destructive"
              disabled={bulkCurateByFilter.isPending}
              onClick={handleExcludeAll}
            >
              {bulkCurateByFilter.isPending ? 'Excluindo…' : 'Confirmar exclusão em massa'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
