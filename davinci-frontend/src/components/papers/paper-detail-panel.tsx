'use client';

import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { Skeleton } from '@/components/ui/skeleton';
import type { PaperDetail } from '@/lib/types/paper';

interface PaperDetailPanelProps {
  /** ID do paper selecionado na lista (null = painel fechado). */
  paperId: number | null;
  /** Objeto de detalhe já buscado pelo pai via usePaper(). */
  detail: PaperDetail | undefined;
  /** True enquanto usePaper() está carregando. */
  isLoading: boolean;
  onClose: () => void;
}

export function PaperDetailPanel({ paperId, detail, isLoading, onClose }: PaperDetailPanelProps) {
  const isOpen = paperId !== null;
  const p = detail?.paper;

  return (
    <Sheet open={isOpen} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="w-full sm:max-w-xl overflow-y-auto">
        {isLoading || !p ? (
          // Skeleton enquanto o detalhe carrega
          <div className="space-y-4 pt-2">
            <Skeleton className="h-5 w-3/4" />
            <Skeleton className="h-4 w-1/2" />
            <Skeleton className="h-32 w-full" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-5/6" />
          </div>
        ) : (
          <>
            <SheetHeader>
              <SheetTitle className="text-base leading-tight pr-6">{p.title}</SheetTitle>
              <SheetDescription>
                PMID {p.pmid}{p.journal ? ` · ${p.journal}` : ''}{p.pub_year ? ` · ${p.pub_year}` : ''}
              </SheetDescription>
            </SheetHeader>

            <div className="mt-4 space-y-4 text-sm">
              <div className="flex flex-wrap items-center gap-2 text-muted-foreground">
                <span>{p.journal}</span>
                <span>·</span>
                <span>{p.pub_year}</span>
                <span>·</span>
                <span>PMID: {p.pmid}</span>
                {p.doi && (
                  <>
                    <span>·</span>
                    <a
                      href={`https://doi.org/${p.doi}`}
                      target="_blank"
                      rel="noreferrer"
                      className="underline hover:text-foreground"
                    >
                      DOI
                    </a>
                  </>
                )}
              </div>

              {p.authors.length > 0 && (
                <div>
                  <p className="font-medium mb-1">Authors</p>
                  <p className="text-muted-foreground">
                    {p.authors.slice(0, 5).map(a => `${a.initials ?? ''} ${a.last_name}`.trim()).join(', ')}
                    {p.authors.length > 5 && ` et al.`}
                  </p>
                </div>
              )}

              <Separator />

              <div>
                <p className="font-medium mb-1">Abstract</p>
                <p className="text-muted-foreground leading-relaxed">{p.abstract || 'No abstract available.'}</p>
              </div>

              {p.mesh_terms.length > 0 && (
                <>
                  <Separator />
                  <div>
                    <p className="font-medium mb-2">MeSH Terms</p>
                    <div className="flex flex-wrap gap-1.5">
                      {p.mesh_terms.map((m, i) => (
                        <Badge key={i} variant={m.is_major_topic ? 'default' : 'secondary'} className="text-xs">
                          {m.descriptor}
                        </Badge>
                      ))}
                    </div>
                  </div>
                </>
              )}

              {p.genes.length > 0 && (
                <>
                  <Separator />
                  <div>
                    <p className="font-medium mb-2">Genes</p>
                    <div className="flex flex-wrap gap-1.5">
                      {p.genes.map((g, i) => (
                        <Badge key={i} variant="outline" className="text-xs font-mono">
                          {g.gene_symbol} ×{g.mention_count}
                        </Badge>
                      ))}
                    </div>
                  </div>
                </>
              )}

              {p.variants.length > 0 && (
                <>
                  <Separator />
                  <div>
                    <p className="font-medium mb-2">Variants</p>
                    <div className="flex flex-wrap gap-1.5">
                      {p.variants.map((v, i) => (
                        <Badge key={i} variant="outline" className="text-xs font-mono">{v.rs_number}</Badge>
                      ))}
                    </div>
                  </div>
                </>
              )}

              {detail.notes && (
                <>
                  <Separator />
                  <div>
                    <p className="font-medium mb-1">Notes</p>
                    <p className="text-muted-foreground">{detail.notes}</p>
                  </div>
                </>
              )}
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}
