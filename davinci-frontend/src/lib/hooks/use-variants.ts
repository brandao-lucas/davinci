import { useQuery } from '@tanstack/react-query';
import { variantsApi } from '@/lib/api/variants';
import type { VariantFilters } from '@/lib/types/variant';

export function useVariants(projectId: string, filters?: VariantFilters) {
  return useQuery({
    queryKey: ['variants', projectId, filters],
    queryFn: () => variantsApi.list(projectId, filters).then((r) => r.data),
    enabled: !!projectId,
  });
}

export function useVariantDetail(projectId: string, rsNumber: string | null) {
  return useQuery({
    queryKey: ['variants', projectId, 'detail', rsNumber],
    queryFn: () => variantsApi.get(projectId, rsNumber!).then((r) => r.data),
    enabled: !!projectId && !!rsNumber,
    // Polling condicional: enquanto context_status === 'computing', repoll a cada 3 s.
    refetchInterval: (query) => {
      const data = query.state.data;
      return data?.context_status === 'computing' ? 3000 : false;
    },
  });
}
