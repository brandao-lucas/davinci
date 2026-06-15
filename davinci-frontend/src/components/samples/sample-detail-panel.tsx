'use client';

import { useState } from 'react';
import { Sheet, SheetContent, SheetHeader, SheetTitle } from '@/components/ui/sheet';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Separator } from '@/components/ui/separator';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select';
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { useCurateSample } from '@/lib/hooks/use-samples';
import { formatDateTime } from '@/lib/utils/format';
import type { ProjectSample } from '@/lib/types/sample';

interface SampleDetailPanelProps {
  sample: ProjectSample | null;
  projectId: string;
  onClose: () => void;
}

const STATUSES = ['pending', 'included', 'excluded', 'maybe'];

const statusColors: Record<string, string> = {
  pending: 'bg-amber-100 text-amber-800',
  included: 'bg-green-100 text-green-800',
  excluded: 'bg-red-100 text-red-800',
  maybe: 'bg-violet-100 text-violet-800',
};

export function SampleDetailPanel({ sample, projectId, onClose }: SampleDetailPanelProps) {
  const curate = useCurateSample(projectId);
  const [editStatus, setEditStatus] = useState('');
  const [notes, setNotes] = useState('');
  const [exclusionReason, setExclusionReason] = useState('');
  const [editing, setEditing] = useState(false);
  const [confirmExcludeOpen, setConfirmExcludeOpen] = useState(false);
  const [pendingSave, setPendingSave] = useState(false);

  if (!sample) return null;

  const openEdit = () => {
    setEditStatus(sample.curation_status);
    setNotes(sample.notes ?? '');
    setExclusionReason(sample.exclusion_reason ?? '');
    setEditing(true);
  };

  const doSave = async () => {
    await curate.mutateAsync({
      sampleId: sample.id,
      data: { curation_status: editStatus, notes, exclusion_reason: exclusionReason || undefined },
    });
    setEditing(false);
    setPendingSave(false);
  };

  const handleSave = () => {
    // curation-audit-trail: warn if excluding without exclusion_reason
    if (editStatus === 'excluded' && !exclusionReason.trim()) {
      setConfirmExcludeOpen(true);
      return;
    }
    doSave();
  };

  return (
    <>
      <Sheet open={!!sample} onOpenChange={(o) => !o && onClose()}>
        <SheetContent className="w-full sm:max-w-xl overflow-y-auto">
          <SheetHeader>
            <SheetTitle className="text-base leading-tight pr-6">{sample.title}</SheetTitle>
          </SheetHeader>

          <div className="mt-4 space-y-4 text-sm">
            {/* Meta row */}
            <div className="flex flex-wrap items-center gap-2 text-muted-foreground">
              <Badge variant="outline" className="font-mono text-xs">{sample.accession}</Badge>
              {sample.platform && (
                <>
                  <span>·</span>
                  <span>{sample.platform}</span>
                </>
              )}
              <span>·</span>
              <span className="italic">{sample.organism}</span>
            </div>

            {/* Dataset reference */}
            <div className="text-xs text-muted-foreground">
              Dataset: <span className="font-mono">{sample.dataset_accession}</span>
            </div>

            {/* Source name */}
            {sample.source_name && (
              <div>
                <p className="text-muted-foreground mb-0.5 text-xs">Source name</p>
                <p>{sample.source_name}</p>
              </div>
            )}

            <Separator />

            {/* Curation section */}
            {editing ? (
              <div className="space-y-3">
                <div className="space-y-1.5">
                  <Label>Status</Label>
                  <Select value={editStatus} onValueChange={setEditStatus}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {STATUSES.map((s) => <SelectItem key={s} value={s}>{s}</SelectItem>)}
                    </SelectContent>
                  </Select>
                </div>

                {editStatus === 'excluded' && (
                  <div className="space-y-1.5">
                    <Label>Exclusion reason</Label>
                    <Textarea
                      value={exclusionReason}
                      onChange={(e) => setExclusionReason(e.target.value)}
                      placeholder="Wrong organism, irrelevant condition…"
                      rows={2}
                    />
                  </div>
                )}

                <div className="space-y-1.5">
                  <Label>Notes</Label>
                  <Textarea
                    value={notes}
                    onChange={(e) => setNotes(e.target.value)}
                    placeholder="Add notes…"
                    rows={3}
                  />
                </div>
                <div className="flex gap-2">
                  <Button size="sm" onClick={handleSave} disabled={curate.isPending || pendingSave}>
                    Save
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>Cancel</Button>
                </div>
              </div>
            ) : (
              <div className="flex items-start justify-between gap-3">
                <div className="space-y-1">
                  <p className="text-muted-foreground text-xs">Curation status</p>
                  <Badge className={statusColors[sample.curation_status] ?? ''} variant="outline">
                    {sample.curation_status}
                  </Badge>
                  {/* curation-audit-trail: show curated_at */}
                  {sample.curated_at && (
                    <p className="text-muted-foreground text-xs mt-1">
                      Curated: {formatDateTime(sample.curated_at)}
                    </p>
                  )}
                  {sample.exclusion_reason && (
                    <p className="text-muted-foreground text-xs mt-1">
                      Reason: {sample.exclusion_reason}
                    </p>
                  )}
                  {sample.notes && (
                    <p className="text-muted-foreground text-xs mt-2">{sample.notes}</p>
                  )}
                </div>
                <Button size="sm" variant="outline" onClick={openEdit}>Edit</Button>
              </div>
            )}
          </div>
        </SheetContent>
      </Sheet>

      {/* curation-audit-trail: confirm exclusion without reason */}
      <AlertDialog open={confirmExcludeOpen} onOpenChange={setConfirmExcludeOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Exclude without reason?</AlertDialogTitle>
            <AlertDialogDescription>
              You are excluding this sample without providing an exclusion reason. This makes it
              harder to audit this decision later. Are you sure?
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setPendingSave(true);
                setConfirmExcludeOpen(false);
                doSave();
              }}
            >
              Exclude anyway
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
