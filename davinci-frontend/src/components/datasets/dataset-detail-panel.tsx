'use client';

import { useState } from 'react';
import Link from 'next/link';
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Separator } from '@/components/ui/separator';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select';
import { useCurateDataset, useDataset } from '@/lib/hooks/use-datasets';
import type { OmicDataset } from '@/lib/types/dataset';

interface DatasetDetailPanelProps {
  dataset: OmicDataset | null;
  projectId: string;
  onClose: () => void;
}

const STATUSES = ['pending', 'included', 'excluded', 'queued', 'downloaded'];

const statusColors: Record<string, string> = {
  pending: 'bg-amber-100 text-amber-800',
  included: 'bg-green-100 text-green-800',
  excluded: 'bg-red-100 text-red-800',
  queued: 'bg-blue-100 text-blue-800',
  downloaded: 'bg-teal-100 text-teal-800',
};

function externalUrl(dataset: OmicDataset): string | null {
  const acc = dataset.accession;
  switch (dataset.source_db.toLowerCase()) {
    // GEO e BioProject têm seus links providos pelas tags BioProject/GSE abaixo
    case 'geo': return null;
    case 'bioproject': return null;
    case 'sra': return `https://www.ncbi.nlm.nih.gov/sra/${acc}`;
    case 'gwas_catalog':
    case 'gwas': return `https://www.ebi.ac.uk/gwas/studies/${acc}`;
    default: return null;
  }
}

export function DatasetDetailPanel({ dataset, projectId, onClose }: DatasetDetailPanelProps) {
  const curate = useCurateDataset(projectId);
  const [editStatus, setEditStatus] = useState('');
  const [notes, setNotes] = useState('');
  const [editing, setEditing] = useState(false);

  // Busca o detalhe completo (inclui linked_papers serializado pelo backend).
  const { data: detail } = useDataset(projectId, dataset?.id ?? 0);
  const linkedPapers = detail?.linked_papers;

  if (!dataset) return null;

  const url = externalUrl(dataset);
  const extra = dataset.extra_metadata as Record<string, string | null> | undefined;
  // PRJNA mora em bioproject_id (GEO) ou no próprio accession (origem BioProject)
  const bioprojectAcc =
    dataset.bioproject_id ||
    (dataset.source_db.toLowerCase() === 'bioproject' ? dataset.accession : '');
  const gse = extra?.gse;

  const openEdit = () => {
    setEditStatus(dataset.curation_status ?? 'pending');
    setNotes(dataset.notes ?? '');
    setEditing(true);
  };

  const saveEdit = async () => {
    await curate.mutateAsync({ datasetId: dataset.id, data: { curation_status: editStatus, notes } });
    setEditing(false);
  };

  return (
    <Sheet open={!!dataset} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="w-full sm:max-w-xl overflow-y-auto">
        <SheetHeader>
          <SheetTitle className="text-base leading-tight pr-6">{dataset.title}</SheetTitle>
          <SheetDescription>
            {dataset.accession} · {dataset.source_db}{dataset.omic_type ? ` · ${dataset.omic_type}` : ''}
          </SheetDescription>
        </SheetHeader>

        <div className="mt-4 space-y-4 text-sm">
          {/* Meta row */}
          <div className="flex flex-wrap items-center gap-2 text-muted-foreground">
            <span>{dataset.source_db}</span>
            {dataset.omic_type && (
              <>
                <span>·</span>
                <Badge variant="secondary" className="text-xs">{dataset.omic_type}</Badge>
              </>
            )}
            {bioprojectAcc && (
              <>
                <span>·</span>
                <a
                  href={`https://www.ncbi.nlm.nih.gov/bioproject/${bioprojectAcc}`}
                  target="_blank"
                  rel="noreferrer"
                  className="underline hover:text-foreground"
                >
                  <Badge variant="outline" className="font-mono text-xs">
                    {bioprojectAcc}
                  </Badge>
                </a>
              </>
            )}
            {gse && (
              <>
                <span>·</span>
                <a
                  href={`https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE${gse}`}
                  target="_blank"
                  rel="noreferrer"
                  className="underline hover:text-foreground"
                >
                  <Badge variant="outline" className="font-mono text-xs">
                    GSE{gse}
                  </Badge>
                </a>
              </>
            )}
            {url && (
              <>
                <span>·</span>
                <a href={url} target="_blank" rel="noreferrer"
                  className="underline hover:text-foreground">
                  View source
                </a>
              </>
            )}
          </div>

          {/* Organism / samples / platform */}
          <div className="grid grid-cols-3 gap-3 text-xs">
            {dataset.organism && (
              <div>
                <p className="text-muted-foreground mb-0.5">Organism</p>
                <p className="italic">{dataset.organism}</p>
              </div>
            )}
            {dataset.n_samples != null && (
              <div>
                <p className="text-muted-foreground mb-0.5">Samples</p>
                <p>{dataset.n_samples.toLocaleString()}</p>
              </div>
            )}
            {dataset.platform && (
              <div>
                <p className="text-muted-foreground mb-0.5">Platform</p>
                <p className="truncate" title={dataset.platform}>{dataset.platform}</p>
              </div>
            )}
          </div>

          {/* View samples link */}
          <div>
            <Button size="sm" variant="outline" asChild>
              <Link href={`/projects/${projectId}/datasets/${dataset.id}/samples`}>
                View samples
              </Link>
            </Button>
          </div>

          <Separator />

          {/* Summary */}
          {dataset.summary && (
            <div>
              <p className="font-medium mb-1">Summary</p>
              <p className="text-muted-foreground leading-relaxed">{dataset.summary}</p>
            </div>
          )}

          <Separator />

          {/* Curation */}
          {editing ? (
            <div className="space-y-3">
              <div className="space-y-1.5">
                <Label>Status</Label>
                <Select value={editStatus} onValueChange={setEditStatus}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {STATUSES.map(s => <SelectItem key={s} value={s}>{s}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
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
                <Button size="sm" onClick={saveEdit} disabled={curate.isPending}>Save</Button>
                <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>Cancel</Button>
              </div>
            </div>
          ) : (
            <div className="flex items-start justify-between gap-3">
              <div className="space-y-1">
                <p className="text-muted-foreground text-xs">Curation status</p>
                <Badge className={statusColors[dataset.curation_status ?? ''] ?? ''} variant="outline">
                  {dataset.curation_status}
                </Badge>
                {dataset.notes && (
                  <p className="text-muted-foreground text-xs mt-2">{dataset.notes}</p>
                )}
              </div>
              <Button size="sm" variant="outline" onClick={openEdit}>Edit</Button>
            </div>
          )}

          {/* Papers vinculados */}
          {linkedPapers && linkedPapers.length > 0 && (
            <>
              <Separator />
              <div>
                <p className="font-medium mb-2 text-sm">Papers vinculados</p>
                <div className="space-y-2">
                  {linkedPapers.map((lp) => (
                    <div key={lp.id} className="flex items-start justify-between gap-2 text-xs">
                      <div className="min-w-0">
                        <p className="font-mono">PMID {lp.paper_pmid}</p>
                        {lp.paper_title && (
                          <p className="text-muted-foreground truncate" title={lp.paper_title}>
                            {lp.paper_title}
                          </p>
                        )}
                      </div>
                      <Badge
                        variant="outline"
                        className={
                          lp.confidence === 'confirmed'
                            ? 'bg-green-100 text-green-800 shrink-0'
                            : lp.confidence === 'rejected'
                            ? 'bg-red-100 text-red-800 shrink-0'
                            : 'bg-amber-100 text-amber-800 shrink-0'
                        }
                      >
                        {lp.confidence}
                      </Badge>
                    </div>
                  ))}
                </div>
              </div>
            </>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
