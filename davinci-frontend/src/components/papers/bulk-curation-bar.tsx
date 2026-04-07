'use client';

import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';
import { useBulkCurate } from '@/lib/hooks/use-papers';

interface BulkCurationBarProps {
  projectId: string;
  selectedIds: number[];
  onClear: () => void;
}

export function BulkCurationBar({ projectId, selectedIds, onClear }: BulkCurationBarProps) {
  const [excludeDialogOpen, setExcludeDialogOpen] = useState(false);
  const [reason, setReason] = useState('');
  const bulkCurate = useBulkCurate(projectId);

  if (selectedIds.length === 0) return null;

  const handleCurate = async (status: string, exclusionReason?: string) => {
    await bulkCurate.mutateAsync({
      paper_ids: selectedIds,
      curation_status: status,
      exclusion_reason: exclusionReason,
    });
    onClear();
  };

  return (
    <>
      <div className="fixed bottom-0 left-0 right-0 bg-background border-t shadow-lg py-3 px-6 flex items-center gap-4 z-50">
        <span className="text-sm font-medium">{selectedIds.length} selected</span>
        <div className="flex gap-2">
          <Button size="sm" variant="outline" className="text-green-700 border-green-300"
            onClick={() => handleCurate('included')}>
            Include
          </Button>
          <Button size="sm" variant="outline" className="text-red-700 border-red-300"
            onClick={() => setExcludeDialogOpen(true)}>
            Exclude
          </Button>
          <Button size="sm" variant="outline" className="text-violet-700 border-violet-300"
            onClick={() => handleCurate('maybe')}>
            Maybe
          </Button>
        </div>
        <Button size="sm" variant="ghost" onClick={onClear} className="ml-auto">
          Clear selection
        </Button>
      </div>

      <Dialog open={excludeDialogOpen} onOpenChange={setExcludeDialogOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Exclude {selectedIds.length} papers</DialogTitle>
            <DialogDescription>Provide a reason for exclusion (required for audit trail).</DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1.5">
              <Label>Exclusion reason (required)</Label>
              <Input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="Not relevant, wrong population…"
              />
            </div>
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setExcludeDialogOpen(false)}>Cancel</Button>
              <Button
                variant="destructive"
                disabled={!reason.trim() || bulkCurate.isPending}
                onClick={() => {
                  handleCurate('excluded', reason);
                  setExcludeDialogOpen(false);
                  setReason('');
                }}
              >
                Exclude
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
