'use client';

import { use, useState } from 'react';
import { PageHeader } from '@/components/layout/page-header';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { linksApi } from '@/lib/api/links';
import { useLinkSuggestions, useAddFromSuggestion } from '@/lib/hooks/use-link-suggestions';
import { truncate } from '@/lib/utils/format';
import { Check, X, ChevronLeft, ChevronRight, PlusCircle } from 'lucide-react';
import type { SuggestionType } from '@/lib/types/links';

const confidenceColors: Record<string, string> = {
  auto: 'bg-amber-100 text-amber-800',
  confirmed: 'bg-green-100 text-green-800',
  rejected: 'bg-red-100 text-red-800',
};

const suggestionTypeLabels: Record<string, string> = {
  dataset_missing: 'Dataset ausente',
  paper_missing: 'Paper ausente',
};

const SUGGESTION_PAGE_SIZE = 20;

export default function LinksPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = use(params);
  const queryClient = useQueryClient();

  // ── Aba: vínculos confirmados ──────────────────────────────────────────────
  const { data, isLoading } = useQuery({
    queryKey: ['links', projectId],
    queryFn: () => linksApi.list(projectId).then(r => r.data),
    enabled: !!projectId,
  });

  const confirm = useMutation({
    mutationFn: (id: number) => linksApi.confirm(projectId, id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['links', projectId] }),
  });

  const reject = useMutation({
    mutationFn: (id: number) => linksApi.reject(projectId, id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['links', projectId] }),
  });

  const links = data?.results ?? [];

  // ── Aba: sugestões de órfãos ───────────────────────────────────────────────
  const [suggestionType, setSuggestionType] = useState<SuggestionType | 'all'>('all');
  const [suggestionPage, setSuggestionPage] = useState(1);

  const suggestionFilters = {
    type: suggestionType !== 'all' ? suggestionType : undefined,
    page: suggestionPage,
    page_size: SUGGESTION_PAGE_SIZE,
  };

  const { data: suggestionsData, isLoading: suggestionsLoading } = useLinkSuggestions(
    projectId,
    suggestionFilters,
  );

  const addFromSuggestion = useAddFromSuggestion(projectId);

  const suggestions = suggestionsData?.results ?? [];
  const totalSuggestions = suggestionsData?.count ?? 0;
  const totalPages = Math.ceil(totalSuggestions / SUGGESTION_PAGE_SIZE);

  const handleTypeChange = (value: string) => {
    setSuggestionType(value as SuggestionType | 'all');
    setSuggestionPage(1);
  };

  return (
    <div className="space-y-4">
      <PageHeader
        title="Paper — Dataset Links"
        description={`${data?.count ?? '…'} vínculos confirmados`}
      />

      <Tabs defaultValue="confirmed">
        <TabsList>
          <TabsTrigger value="confirmed">
            Confirmados
            {data?.count != null && (
              <Badge variant="secondary" className="ml-2 text-xs">
                {data.count}
              </Badge>
            )}
          </TabsTrigger>
          <TabsTrigger value="suggestions">
            Sugestoes
            {totalSuggestions > 0 && (
              <Badge variant="secondary" className="ml-2 text-xs">
                {totalSuggestions}
              </Badge>
            )}
          </TabsTrigger>
        </TabsList>

        {/* ── Tab: vínculos confirmados ─────────────────────────────────── */}
        <TabsContent value="confirmed" className="mt-4">
          {isLoading ? (
            <div className="h-64 bg-muted rounded-lg animate-pulse" />
          ) : (
            <div className="rounded-md border overflow-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Paper</TableHead>
                    <TableHead>Dataset</TableHead>
                    <TableHead>Confidence</TableHead>
                    <TableHead className="w-24">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {links.length === 0 ? (
                    <TableRow>
                      <TableCell colSpan={4} className="h-24 text-center text-muted-foreground">
                        No links found.
                      </TableCell>
                    </TableRow>
                  ) : (
                    links.map((link) => (
                      <TableRow key={link.id}>
                        <TableCell>
                          <p className="text-xs font-mono">{link.paper_pmid}</p>
                          <p className="text-sm">{truncate(link.paper_title, 60)}</p>
                        </TableCell>
                        <TableCell>
                          <p className="text-xs font-mono">{link.dataset_accession}</p>
                          <p className="text-sm">{truncate(link.dataset_title, 60)}</p>
                        </TableCell>
                        <TableCell>
                          <Badge className={confidenceColors[link.confidence] ?? ''} variant="outline">
                            {link.confidence}
                          </Badge>
                        </TableCell>
                        <TableCell>
                          {link.confidence === 'auto' && (
                            <div className="flex gap-1">
                              <Button
                                size="icon"
                                variant="ghost"
                                className="h-7 w-7 text-green-600"
                                onClick={() => confirm.mutate(link.id)}
                              >
                                <Check className="h-4 w-4" />
                              </Button>
                              <Button
                                size="icon"
                                variant="ghost"
                                className="h-7 w-7 text-red-600"
                                onClick={() => reject.mutate(link.id)}
                              >
                                <X className="h-4 w-4" />
                              </Button>
                            </div>
                          )}
                        </TableCell>
                      </TableRow>
                    ))
                  )}
                </TableBody>
              </Table>
            </div>
          )}
        </TabsContent>

        {/* ── Tab: sugestoes de orfaos ──────────────────────────────────── */}
        <TabsContent value="suggestions" className="mt-4 space-y-3">
          <div className="flex items-center gap-3">
            <Select value={suggestionType} onValueChange={handleTypeChange}>
              <SelectTrigger className="w-52">
                <SelectValue placeholder="Todos os tipos" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">Todos os tipos</SelectItem>
                <SelectItem value="dataset_missing">Dataset ausente</SelectItem>
                <SelectItem value="paper_missing">Paper ausente</SelectItem>
              </SelectContent>
            </Select>
            <span className="text-sm text-muted-foreground">
              {totalSuggestions} sugestao(oes)
            </span>
          </div>

          {suggestionsLoading ? (
            <div className="h-64 bg-muted rounded-lg animate-pulse" />
          ) : (
            <div className="rounded-md border overflow-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Tipo</TableHead>
                    <TableHead>Paper</TableHead>
                    <TableHead>Dataset</TableHead>
                    <TableHead>Fonte</TableHead>
                    <TableHead className="w-40">Acao</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {suggestions.length === 0 ? (
                    <TableRow>
                      <TableCell colSpan={5} className="h-24 text-center text-muted-foreground">
                        Nenhuma sugestao encontrada.
                      </TableCell>
                    </TableRow>
                  ) : (
                    suggestions.map((s) => (
                      <TableRow key={s.global_link_id}>
                        <TableCell>
                          <Badge variant="outline" className="text-xs whitespace-nowrap">
                            {suggestionTypeLabels[s.suggestion_type] ?? s.suggestion_type}
                          </Badge>
                        </TableCell>
                        <TableCell>
                          <p className="text-xs font-mono">PMID {s.paper_pmid}</p>
                          <p className="text-sm text-muted-foreground">
                            {truncate(s.paper_title, 55)}
                          </p>
                          {s.project_paper_id != null && (
                            <Badge variant="secondary" className="text-xs mt-0.5">
                              no projeto
                            </Badge>
                          )}
                        </TableCell>
                        <TableCell>
                          <p className="text-xs font-mono">{s.dataset_accession}</p>
                          <p className="text-sm text-muted-foreground">
                            {truncate(s.dataset_title, 55)}
                          </p>
                          {s.omic_type && (
                            <Badge variant="secondary" className="text-xs mt-0.5">
                              {s.omic_type}
                            </Badge>
                          )}
                          {s.project_dataset_id != null && (
                            <Badge variant="secondary" className="text-xs mt-0.5">
                              no projeto
                            </Badge>
                          )}
                        </TableCell>
                        <TableCell>
                          <span className="text-xs font-mono text-muted-foreground">
                            {s.link_source}
                          </span>
                        </TableCell>
                        <TableCell>
                          <Button
                            size="sm"
                            variant="outline"
                            className="text-xs gap-1"
                            disabled={addFromSuggestion.isPending}
                            onClick={() => addFromSuggestion.mutate(s)}
                          >
                            <PlusCircle className="h-3.5 w-3.5" />
                            {addFromSuggestion.isPending ? 'Adicionando…' : 'Adicionar ao projeto'}
                          </Button>
                        </TableCell>
                      </TableRow>
                    ))
                  )}
                </TableBody>
              </Table>
            </div>
          )}

          {/* Paginacao */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between pt-1">
              <span className="text-sm text-muted-foreground">
                Pagina {suggestionPage} de {totalPages}
              </span>
              <div className="flex gap-1">
                <Button
                  size="icon"
                  variant="outline"
                  className="h-8 w-8"
                  disabled={suggestionPage <= 1}
                  onClick={() => setSuggestionPage((p) => Math.max(1, p - 1))}
                >
                  <ChevronLeft className="h-4 w-4" />
                </Button>
                <Button
                  size="icon"
                  variant="outline"
                  className="h-8 w-8"
                  disabled={suggestionPage >= totalPages}
                  onClick={() => setSuggestionPage((p) => Math.min(totalPages, p + 1))}
                >
                  <ChevronRight className="h-4 w-4" />
                </Button>
              </div>
            </div>
          )}
        </TabsContent>
      </Tabs>
    </div>
  );
}
