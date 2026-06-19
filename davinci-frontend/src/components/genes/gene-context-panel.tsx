'use client';

import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';
import { Badge } from '@/components/ui/badge';
import { Checkbox } from '@/components/ui/checkbox';
import { Separator } from '@/components/ui/separator';
import { Skeleton } from '@/components/ui/skeleton';
import { ExternalLink, Loader2 } from 'lucide-react';
import { useGeneDetail } from '@/lib/hooks/use-genes';
import { useCuratePaper } from '@/lib/hooks/use-papers';
import { useQueryClient } from '@tanstack/react-query';
import type { GeneReference } from '@/lib/types/gene';

const curationColors: Record<string, string> = {
  pending: 'bg-amber-100 text-amber-800',
  included: 'bg-green-100 text-green-800',
  excluded: 'bg-red-100 text-red-800',
  maybe: 'bg-violet-100 text-violet-800',
};

/**
 * Escapa caracteres especiais de regex no símbolo do gene antes de montar
 * a expressão, evitando erro com símbolos como TNFRSF1A, IL-6, etc.
 */
function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Renderiza o texto de um snippet realçando todas as ocorrências do
 * símbolo do gene em negrito. O match é case-insensitive e respeita
 * fronteiras de palavra (\b) para não negritar substrings espúrias
 * (ex.: não negritar "TNF" dentro de "TNFRSF1A").
 * Não usa dangerouslySetInnerHTML — produz nós React puros.
 */
function HighlightedSnippet({
  text,
  symbol,
}: {
  text: string;
  symbol: string;
}) {
  // Split com grupo de captura: as ocorrências do gene ficam nos índices
  // ímpares do array resultante (1, 3, 5, ...).
  const pattern = new RegExp(`(\\b${escapeRegex(symbol)}\\b)`, 'gi');
  const parts = text.split(pattern);

  return (
    <>
      {parts.map((part, i) =>
        i % 2 === 1 ? (
          <strong key={i} className="font-semibold text-foreground">
            {part}
          </strong>
        ) : (
          <span key={i}>{part}</span>
        ),
      )}
    </>
  );
}

interface ReferenceRowProps {
  paper: GeneReference;
  geneSymbol: string;
  isComputing: boolean;
  isLast: boolean;
  projectId: string;
}

/**
 * Linha de referência com toggle de curadoria.
 * Separado em componente próprio para isolar o hook useCuratePaper —
 * cada linha mantém estado independente de loading.
 */
function ReferenceRow({
  paper,
  geneSymbol,
  isComputing,
  isLast,
  projectId,
}: ReferenceRowProps) {
  const queryClient = useQueryClient();
  const curate = useCuratePaper(projectId);

  const isIncluded = paper.curation_status === 'included';

  const handleToggle = (checked: boolean) => {
    const newStatus = checked ? 'included' : 'pending';
    curate.mutate(
      { paperId: paper.project_paper_id, data: { curation_status: newStatus } },
      {
        onSettled: () => {
          // Invalida o detalhe do gene para atualizar contagens included|total
          queryClient.invalidateQueries({
            queryKey: ['genes', projectId, 'detail', geneSymbol],
          });
          // Invalida a lista de genes (contagens e filtro included_only)
          queryClient.invalidateQueries({
            queryKey: ['genes', projectId],
          });
        },
      },
    );
  };

  return (
    <div>
      <div className="flex flex-wrap items-start gap-2 mb-2">
        <a
          href={`https://pubmed.ncbi.nlm.nih.gov/${paper.pmid}/`}
          target="_blank"
          rel="noopener noreferrer"
          className="font-medium hover:underline flex-1 min-w-0"
        >
          {paper.title}
        </a>
        <Badge
          className={`${curationColors[paper.curation_status] ?? ''} shrink-0`}
          variant="outline"
        >
          {paper.curation_status}
        </Badge>
      </div>
      <p className="text-muted-foreground text-xs mb-2">
        {paper.journal}
        {paper.pub_year ? ` · ${paper.pub_year}` : ''}
        {' · '}
        <a
          href={`https://pubmed.ncbi.nlm.nih.gov/${paper.pmid}/`}
          target="_blank"
          rel="noopener noreferrer"
          className="text-blue-600 hover:underline"
        >
          PMID {paper.pmid}
        </a>
      </p>

      {/* Toggle de curadoria */}
      <label className="flex items-center gap-2 cursor-pointer text-xs mb-3 w-fit select-none">
        <Checkbox
          checked={isIncluded}
          disabled={curate.isPending}
          onCheckedChange={(v) => handleToggle(!!v)}
          aria-label={
            isIncluded
              ? `Remover artigo "${paper.title}" dos incluidos`
              : `Incluir artigo "${paper.title}"`
          }
        />
        <span className="text-muted-foreground">Incluido</span>
      </label>

      {paper.snippets.length === 0 ? (
        isComputing ? (
          <p className="text-xs text-muted-foreground italic">
            Aguardando derivacao de contexto...
          </p>
        ) : (
          <p className="text-xs text-muted-foreground italic">
            Nenhum snippet de contexto disponivel.
          </p>
        )
      ) : (
        <ul className="space-y-1">
          {paper.snippets.map((snippet, si) => (
            <li
              key={si}
              className="rounded bg-muted/60 px-3 py-1.5 text-xs leading-relaxed border-l-2 border-blue-300"
            >
              <HighlightedSnippet text={snippet.sentence} symbol={geneSymbol} />
            </li>
          ))}
        </ul>
      )}
      {!isLast && <Separator className="mt-4" />}
    </div>
  );
}

interface GeneContextPanelProps {
  projectId: string;
  geneSymbol: string | null;
  onClose: () => void;
}

export function GeneContextPanel({
  projectId,
  geneSymbol,
  onClose,
}: GeneContextPanelProps) {
  const isOpen = geneSymbol !== null;

  const { data: detail, isLoading } = useGeneDetail(projectId, geneSymbol);

  const isComputing = detail?.context_status === 'computing';

  return (
    <Sheet open={isOpen} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="w-full sm:max-w-xl overflow-y-auto">
        {isLoading || !detail ? (
          <>
            <SheetHeader>
              <SheetTitle className="sr-only">Carregando contexto do gene</SheetTitle>
              <SheetDescription className="sr-only">
                Aguarde enquanto os dados do gene sao carregados.
              </SheetDescription>
            </SheetHeader>
            <div className="space-y-4 pt-2">
              <Skeleton className="h-6 w-1/2" />
              <Skeleton className="h-4 w-1/3" />
              <Skeleton className="h-32 w-full" />
              <Skeleton className="h-24 w-full" />
            </div>
          </>
        ) : (
          <>
            <SheetHeader>
              <SheetTitle className="text-base leading-tight pr-6 font-mono">
                {detail.gene_symbol}
              </SheetTitle>
              <SheetDescription asChild>
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  {detail.entrez_id ? (
                    <a
                      href={`https://www.ncbi.nlm.nih.gov/gene/${detail.entrez_id}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 text-blue-600 hover:underline"
                    >
                      Entrez ID: {detail.entrez_id}
                      <ExternalLink className="h-3 w-3" />
                    </a>
                  ) : (
                    <a
                      href={`https://www.ncbi.nlm.nih.gov/gene/?term=${encodeURIComponent(detail.gene_symbol)}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 text-blue-600 hover:underline"
                    >
                      Buscar no NCBI
                      <ExternalLink className="h-3 w-3" />
                    </a>
                  )}
                  <span className="text-muted-foreground">
                    {detail.unique_citations_included} incluidos /{' '}
                    {detail.unique_citations_total} total
                  </span>
                </div>
              </SheetDescription>
            </SheetHeader>

            {isComputing && (
              <div className="mt-4 flex items-center gap-2 rounded-md bg-amber-50 border border-amber-200 px-3 py-2 text-sm text-amber-800">
                <Loader2 className="h-4 w-4 animate-spin shrink-0" />
                <span>
                  Derivando contextos dos abstracts em background. Os snippets serao
                  atualizados automaticamente em alguns instantes.
                </span>
              </div>
            )}

            <div className="mt-4 space-y-4 text-sm">
              {detail.references.length === 0 ? (
                <p className="text-muted-foreground">
                  Nenhuma referencia encontrada para este gene neste projeto.
                </p>
              ) : (
                detail.references.map((entry, i) => (
                  <ReferenceRow
                    key={`${entry.pmid}-${i}`}
                    paper={entry}
                    geneSymbol={detail.gene_symbol}
                    isComputing={isComputing}
                    isLast={i === detail.references.length - 1}
                    projectId={projectId}
                  />
                ))
              )}
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}
