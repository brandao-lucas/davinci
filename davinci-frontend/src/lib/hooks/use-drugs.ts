import { useQuery } from '@tanstack/react-query';
import { drugsApi } from '@/lib/api/drugs';
import type { DrugFilters } from '@/lib/types/drug';

export function useDrugs(projectId: string, filters?: DrugFilters) {
  return useQuery({
    queryKey: ['drugs', projectId, filters],
    queryFn: () => drugsApi.list(projectId, filters).then((r) => r.data),
    enabled: !!projectId,
  });
}

export function useDrugDetail(projectId: string, drugNameLower: string | null) {
  return useQuery({
    queryKey: ['drugs', projectId, 'detail', drugNameLower],
    queryFn: () => drugsApi.get(projectId, drugNameLower!).then((r) => r.data),
    enabled: !!projectId && !!drugNameLower,
    // Polling condicional: enquanto context_status === 'computing', repoll a cada 3 s.
    refetchInterval: (query) => {
      const data = query.state.data;
      return data?.context_status === 'computing' ? 3000 : false;
    },
  });
}
