import { useQuery } from '@tanstack/react-query';
import { genesApi } from '@/lib/api/genes';
import type { GeneFilters } from '@/lib/types/gene';

export function useGenes(projectId: string, filters?: GeneFilters) {
  return useQuery({
    queryKey: ['genes', projectId, filters],
    queryFn: () => genesApi.list(projectId, filters).then((r) => r.data),
    enabled: !!projectId,
  });
}

export function useGeneDetail(projectId: string, geneSymbol: string | null) {
  return useQuery({
    queryKey: ['genes', projectId, 'detail', geneSymbol],
    queryFn: () => genesApi.get(projectId, geneSymbol!).then((r) => r.data),
    enabled: !!projectId && !!geneSymbol,
    // Polling condicional: enquanto context_status === 'computing', repoll a cada 3 s.
    refetchInterval: (query) => {
      const data = query.state.data;
      return data?.context_status === 'computing' ? 3000 : false;
    },
  });
}
