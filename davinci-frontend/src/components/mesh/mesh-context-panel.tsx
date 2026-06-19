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
import { useMeshDetail } from '@/lib/hooks/use-mesh';
import { useCuratePaper } from '@/lib/hooks/use-papers';
import { useQueryClient } from '@tanstack/react-query';
import type { MeSHReference } from '@/lib/types/mesh';

const curationColors: Record<string, string> = {
  pending: 'bg-amber-100 text-amber-800',
  included: 'bg-green-100 text-green-800',
  excluded: 'bg-red-100 text-red-800',
  maybe: 'bg-violet-100 text-violet-800',
};

/**
 * Escapa caracteres especiais de regex no descriptor antes de montar
 * a expressão, evitando erro com caracteres como parênteses, hifens, etc.
 * Descritores MeSH podem conter espaços e pontuação (ex.: "Diabetes Mellitus, Type 2").
 */
function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Renderiza o texto de um snippet realçando todas as ocorrências do
 * descriptor MeSH em negrito. O match é case-insensitive e respeita
 * fronteiras de palavra (\b) para não negritar substrings espúrias.
 * Descritores podem ser multi-palavra (ex.: "Diabetes Mellitus") — a
 * regex casa a frase inteira.
 * Não usa dangerouslySetInnerHTML — produz nós React puros.
 */
function HighlightedSnippet({
  text,
  descriptor,
}: {
  text: string;
  descriptor: string;
}) {
  // Split com grupo de captura: as ocorrências ficam nos índices
  // ímpares do array resultante (1, 3, 5, ...).
  const pattern = new RegExp(`(\\b${escapeRegex(descriptor)}\\b)`, 'gi');
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
  paper: MeSHReference;
  descriptor: string;
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
  descriptor,
  isComputing,
  isLast,
  projectId,
}: ReferenceRowProps) {
  const queryClient = useQueryClient();
  const curate = useCuratePaper(projectId);

  const isIncluded = paper.curation_status === 'included';

  const handleToggle = (checked: boolean) => {
    // ligado=included, desligado=pending (NUNCA excluded — Regra #2)
    const newStatus = checked ? 'included' : 'pending';
    curate.mutate(
      { paperId: paper.project_paper_id, data: { curation_status: newStatus } },
      {
        onSettled: () => {
          // Invalida o detalhe do descriptor para atualizar contagens included|total e major_topic_count
          queryClient.invalidateQueries({
            queryKey: ['mesh', projectId, 'detail', descriptor],
          });
          // Invalida a lista de MeSH (contagens e filtro included_only)
          queryClient.invalidateQueries({
            queryKey: ['mesh', projectId],
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
        <div className="flex items-center gap-1 shrink-0">
          {paper.is_major_topic && (
            <Badge className="bg-purple-100 text-purple-800" variant="outline">
              Major topic
            </Badge>
          )}
          <Badge
            className={`${curationColors[paper.curation_status] ?? ''}`}
            variant="outline"
          >
            {paper.curation_status}
          </Badge>
        </div>
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
            Nenhum snippet de contexto disponivel. MeSH nao garante presenca
            literal do descriptor no abstract.
          </p>
        )
      ) : (
        <ul className="space-y-1">
          {paper.snippets.map((snippet, si) => (
            <li
              key={si}
              className="rounded bg-muted/60 px-3 py-1.5 text-xs leading-relaxed border-l-2 border-purple-300"
            >
              <HighlightedSnippet text={snippet.sentence} descriptor={descriptor} />
            </li>
          ))}
        </ul>
      )}
      {!isLast && <Separator className="mt-4" />}
    </div>
  );
}

interface MeSHContextPanelProps {
  projectId: string;
  descriptor: string | null;
  onClose: () => void;
}

export function MeSHContextPanel({
  projectId,
  descriptor,
  onClose,
}: MeSHContextPanelProps) {
  const isOpen = descriptor !== null;

  const { data: detail, isLoading } = useMeshDetail(projectId, descriptor);

  const isComputing = detail?.context_status === 'computing';

  return (
    <Sheet open={isOpen} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="w-full sm:max-w-xl overflow-y-auto">
        {isLoading || !detail ? (
          <>
            <SheetHeader>
              <SheetTitle className="sr-only">Carregando contexto do descriptor MeSH</SheetTitle>
              <SheetDescription className="sr-only">
                Aguarde enquanto os dados do descriptor MeSH sao carregados.
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
              <SheetTitle className="text-base leading-tight pr-6">
                {detail.descriptor}
              </SheetTitle>
              <SheetDescription asChild>
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center gap-2 text-sm">
                    <a
                      href={detail.ncbi_mesh_url ?? `https://www.ncbi.nlm.nih.gov/mesh/?term=${encodeURIComponent(detail.descriptor)}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 text-blue-600 hover:underline"
                    >
                      NCBI MeSH
                      <ExternalLink className="h-3 w-3" />
                    </a>
                    <span className="text-muted-foreground">
                      {detail.unique_citations_included} incluidos /{' '}
                      {detail.unique_citations_total} total
                    </span>
                    {detail.major_topic_count > 0 && (
                      <Badge className="bg-purple-100 text-purple-800 text-xs" variant="outline">
                        {detail.major_topic_count} major topic
                      </Badge>
                    )}
                  </div>
                  {detail.qualifiers.length > 0 && (
                    <div className="flex flex-wrap gap-1">
                      {detail.qualifiers.map((q) => (
                        <Badge key={q} variant="secondary" className="text-xs">
                          {q}
                        </Badge>
                      ))}
                    </div>
                  )}
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
                  Nenhuma referencia encontrada para este descriptor neste projeto.
                </p>
              ) : (
                detail.references.map((entry, i) => (
                  <ReferenceRow
                    key={`${entry.pmid}-${i}`}
                    paper={entry}
                    descriptor={detail.descriptor}
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
