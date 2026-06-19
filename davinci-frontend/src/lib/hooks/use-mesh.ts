import { useQuery } from '@tanstack/react-query';
import { meshApi } from '@/lib/api/mesh';
import type { MeSHFilters } from '@/lib/types/mesh';

export function useMesh(projectId: string, filters?: MeSHFilters) {
  return useQuery({
    queryKey: ['mesh', projectId, filters],
    queryFn: () => meshApi.list(projectId, filters).then((r) => r.data),
    enabled: !!projectId,
  });
}

export function useMeshDetail(projectId: string, descriptor: string | null) {
  return useQuery({
    queryKey: ['mesh', projectId, 'detail', descriptor],
    queryFn: () => meshApi.get(projectId, descriptor!).then((r) => r.data),
    enabled: !!projectId && !!descriptor,
    // Polling condicional: enquanto context_status === 'computing', repoll a cada 3 s.
    refetchInterval: (query) => {
      const data = query.state.data;
      return data?.context_status === 'computing' ? 3000 : false;
    },
  });
}
