'use client';

import { use } from 'react';
import { PageHeader } from '@/components/layout/page-header';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { linksApi } from '@/lib/api/links';
import { truncate } from '@/lib/utils/format';
import { Check, X } from 'lucide-react';

const confidenceColors: Record<string, string> = {
  auto: 'bg-amber-100 text-amber-800',
  confirmed: 'bg-green-100 text-green-800',
  rejected: 'bg-red-100 text-red-800',
};

export default function LinksPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = use(params);
  const queryClient = useQueryClient();

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

  return (
    <div className="space-y-4">
      <PageHeader title="Paper — Dataset Links" description={`${data?.count ?? '…'} links`} />

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
                  <TableCell colSpan={4} className="h-24 text-center text-muted-foreground">No links found.</TableCell>
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
                          <Button size="icon" variant="ghost" className="h-7 w-7 text-green-600"
                            onClick={() => confirm.mutate(link.id)}>
                            <Check className="h-4 w-4" />
                          </Button>
                          <Button size="icon" variant="ghost" className="h-7 w-7 text-red-600"
                            onClick={() => reject.mutate(link.id)}>
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
    </div>
  );
}
