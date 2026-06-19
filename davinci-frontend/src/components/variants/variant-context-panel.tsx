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
import { useVariantDetail } from '@/lib/hooks/use-variants';
import { useCuratePaper } from '@/lib/hooks/use-papers';
import { useQueryClient } from '@tanstack/react-query';
import type { VariantReference } from '@/lib/types/variant';

const curationColors: Record<string, string> = {
  pending: 'bg-amber-100 text-amber-800',
  included: 'bg-green-100 text-green-800',
  excluded: 'bg-red-100 text-red-800',
  maybe: 'bg-violet-100 text-violet-800',
};

/**
 * Mapa de cores do Badge de significancia clinica por severidade.
 * Cor codificada por risco (vermelho = patogenico, verde = benigno, etc.).
 */
const clinicalSignificanceColors: Record<string, string> = {
  pathogenic: 'bg-red-100 text-red-800 border-red-200',
  'likely pathogenic': 'bg-red-50 text-red-700 border-red-200',
  'uncertain significance': 'bg-amber-100 text-amber-800 border-amber-200',
  'likely benign': 'bg-green-50 text-green-700 border-green-200',
  benign: 'bg-green-100 text-green-800 border-green-200',
  'drug response': 'bg-blue-100 text-blue-800 border-blue-200',
  'risk factor': 'bg-orange-100 text-orange-800 border-orange-200',
  association: 'bg-violet-100 text-violet-800 border-violet-200',
  protective: 'bg-teal-100 text-teal-800 border-teal-200',
  'conflicting interpretations': 'bg-amber-100 text-amber-800 border-amber-200',
  other: 'bg-slate-100 text-slate-700 border-slate-200',
};

function clinicalBadgeClass(sig: string): string {
  const key = sig.toLowerCase();
  return clinicalSignificanceColors[key] ?? 'bg-slate-100 text-slate-700 border-slate-200';
}

/**
 * Escapa caracteres especiais de regex no rs_number antes de montar a expressao.
 */
function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Renderiza o texto de um snippet realcando todas as ocorrencias do
 * rs_number em negrito. O match e case-insensitive.
 * Nao usa dangerouslySetInnerHTML — produz nos React puros.
 */
function HighlightedSnippet({
  text,
  rsNumber,
}: {
  text: string;
  rsNumber: string;
}) {
  const pattern = new RegExp(`(${escapeRegex(rsNumber)})`, 'gi');
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
  paper: VariantReference;
  rsNumber: string;
  isComputing: boolean;
  isLast: boolean;
  projectId: string;
}

/**
 * Linha de referencia com toggle de curadoria.
 * Separado em componente proprio para isolar o hook useCuratePaper —
 * cada linha mantem estado independente de loading.
 */
function ReferenceRow({
  paper,
  rsNumber,
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
          // Invalida o detalhe da variante para atualizar contagens included|total
          queryClient.invalidateQueries({
            queryKey: ['variants', projectId, 'detail', rsNumber],
          });
          // Invalida a lista de variantes (contagens e filtro included_only)
          queryClient.invalidateQueries({
            queryKey: ['variants', projectId],
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
              <HighlightedSnippet text={snippet.sentence} rsNumber={rsNumber} />
            </li>
          ))}
        </ul>
      )}
      {!isLast && <Separator className="mt-4" />}
    </div>
  );
}

interface VariantContextPanelProps {
  projectId: string;
  rsNumber: string | null;
  onClose: () => void;
}

export function VariantContextPanel({
  projectId,
  rsNumber,
  onClose,
}: VariantContextPanelProps) {
  const isOpen = rsNumber !== null;

  const { data: detail, isLoading } = useVariantDetail(projectId, rsNumber);

  const isComputing = detail?.context_status === 'computing';
  const ann = detail?.annotation ?? null;

  return (
    <Sheet open={isOpen} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="w-full sm:max-w-xl overflow-y-auto">
        {isLoading || !detail ? (
          <>
            <SheetHeader>
              <SheetTitle className="sr-only">Carregando contexto da variante</SheetTitle>
              <SheetDescription className="sr-only">
                Aguarde enquanto os dados da variante sao carregados.
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
                {detail.rs_number}
              </SheetTitle>
              <SheetDescription asChild>
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <a
                    href={`https://www.ncbi.nlm.nih.gov/snp/${encodeURIComponent(detail.rs_number)}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 text-blue-600 hover:underline"
                  >
                    dbSNP
                    <ExternalLink className="h-3 w-3" />
                  </a>
                  <span className="text-muted-foreground">
                    {detail.unique_citations_included} incluidos /{' '}
                    {detail.unique_citations_total} total
                  </span>
                </div>
              </SheetDescription>
            </SheetHeader>

            {/* ── Bloco de anotacao clinica — visivel apenas quando annotation != null ── */}
            {ann && (
              <div className="mt-4 rounded-md border bg-muted/30 px-4 py-3 space-y-2 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  {ann.gene_symbol && (
                    <a
                      href={
                        ann.entrez_id
                          ? `https://www.ncbi.nlm.nih.gov/gene/${ann.entrez_id}`
                          : `https://www.ncbi.nlm.nih.gov/gene/?term=${encodeURIComponent(ann.gene_symbol)}`
                      }
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 font-mono font-semibold text-blue-600 hover:underline"
                    >
                      {ann.gene_symbol}
                      <ExternalLink className="h-3 w-3" />
                    </a>
                  )}
                  {ann.gene_name && (
                    <span className="text-muted-foreground text-xs">{ann.gene_name}</span>
                  )}
                </div>

                <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                  {ann.chromosome && ann.position != null && (
                    <span>
                      <span className="font-medium text-foreground">Posicao:</span>{' '}
                      chr{ann.chromosome}:{ann.position.toLocaleString()}
                    </span>
                  )}
                  {ann.chromosome && ann.position == null && (
                    <span>
                      <span className="font-medium text-foreground">Cromossomo:</span>{' '}
                      chr{ann.chromosome}
                    </span>
                  )}
                  {ann.alleles && (
                    <span>
                      <span className="font-medium text-foreground">Alelos:</span>{' '}
                      <span className="font-mono">{ann.alleles}</span>
                    </span>
                  )}
                  {ann.maf != null && (
                    <span>
                      <span className="font-medium text-foreground">MAF:</span>{' '}
                      <span className="font-mono">{ann.maf.toFixed(4)}</span>
                    </span>
                  )}
                </div>

                {ann.clinical_significance && (
                  <div>
                    <Badge
                      variant="outline"
                      className={`text-xs ${clinicalBadgeClass(ann.clinical_significance)}`}
                    >
                      {ann.clinical_significance}
                    </Badge>
                  </div>
                )}
              </div>
            )}

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
                  Nenhuma referencia encontrada para esta variante neste projeto.
                </p>
              ) : (
                detail.references.map((entry, i) => (
                  <ReferenceRow
                    key={`${entry.pmid}-${i}`}
                    paper={entry}
                    rsNumber={detail.rs_number}
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
