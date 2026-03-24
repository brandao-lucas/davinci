'use client';

import { Sheet, SheetContent, SheetHeader, SheetTitle } from '@/components/ui/sheet';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import type { Paper } from '@/lib/types/paper';

interface PaperDetailPanelProps {
  paper: Paper | null;
  onClose: () => void;
}

export function PaperDetailPanel({ paper, onClose }: PaperDetailPanelProps) {
  if (!paper) return null;

  return (
    <Sheet open={!!paper} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="w-full sm:max-w-xl overflow-y-auto">
        <SheetHeader>
          <SheetTitle className="text-base leading-tight pr-6">{paper.title}</SheetTitle>
        </SheetHeader>

        <div className="mt-4 space-y-4 text-sm">
          <div className="flex items-center gap-2 text-muted-foreground">
            <span>{paper.journal}</span>
            <span>·</span>
            <span>{paper.pub_year}</span>
            <span>·</span>
            <span>PMID: {paper.pmid}</span>
          </div>

          {paper.authors.length > 0 && (
            <div>
              <p className="font-medium mb-1">Authors</p>
              <p className="text-muted-foreground">
                {paper.authors.slice(0, 5).map(a => `${a.initials} ${a.last_name}`).join(', ')}
                {paper.authors.length > 5 && ` et al.`}
              </p>
            </div>
          )}

          <Separator />

          <div>
            <p className="font-medium mb-1">Abstract</p>
            <p className="text-muted-foreground leading-relaxed">{paper.abstract || 'No abstract available.'}</p>
          </div>

          {paper.mesh_terms.length > 0 && (
            <>
              <Separator />
              <div>
                <p className="font-medium mb-2">MeSH Terms</p>
                <div className="flex flex-wrap gap-1.5">
                  {paper.mesh_terms.map((m, i) => (
                    <Badge key={i} variant={m.is_major_topic ? 'default' : 'secondary'} className="text-xs">
                      {m.descriptor}
                    </Badge>
                  ))}
                </div>
              </div>
            </>
          )}

          {paper.genes.length > 0 && (
            <>
              <Separator />
              <div>
                <p className="font-medium mb-2">Genes</p>
                <div className="flex flex-wrap gap-1.5">
                  {paper.genes.map((g, i) => (
                    <Badge key={i} variant="outline" className="text-xs font-mono">
                      {g.gene_symbol} ×{g.mention_count}
                    </Badge>
                  ))}
                </div>
              </div>
            </>
          )}

          {paper.variants.length > 0 && (
            <>
              <Separator />
              <div>
                <p className="font-medium mb-2">Variants</p>
                <div className="flex flex-wrap gap-1.5">
                  {paper.variants.map((v, i) => (
                    <Badge key={i} variant="outline" className="text-xs font-mono">{v}</Badge>
                  ))}
                </div>
              </div>
            </>
          )}

          {paper.notes && (
            <>
              <Separator />
              <div>
                <p className="font-medium mb-1">Notes</p>
                <p className="text-muted-foreground">{paper.notes}</p>
              </div>
            </>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
