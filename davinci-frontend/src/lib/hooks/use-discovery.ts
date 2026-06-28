import { useQuery, useMutation, keepPreviousData } from '@tanstack/react-query';
import { toast } from 'sonner';
import { discoveryApi } from '@/lib/api/discovery';
import { saveFile } from '@/lib/utils/export';
import { extractApiErrorMessage } from '@/lib/utils/api-error';
import type { DiscoveryFilters } from '@/lib/types/discovery';

// Hook de listagem paginada do catálogo de descoberta.
// keepPreviousData (React Query v5) mantém os dados da página anterior enquanto carrega a próxima,
// evitando pisca/blank entre páginas.
export function useDiscovery(projectId: string, filters: DiscoveryFilters, page = 1) {
  return useQuery({
    queryKey: ['discovery', projectId, filters, page],
    queryFn: () => discoveryApi.listJson(projectId, filters, page).then((r) => r.data),
    enabled: !!projectId,
    placeholderData: keepPreviousData,
  });
}

// Hook de download do catálogo (CSV ou JSON completo).
// Um único useMutation com discriminador de formato para reusar estado isPending.
export function useDownloadDiscovery(projectId: string) {
  return useMutation({
    mutationFn: async ({
      format,
      filters,
    }: {
      format: 'csv' | 'json';
      filters: DiscoveryFilters;
    }) => {
      if (format === 'csv') {
        const res = await discoveryApi.downloadCsv(projectId, filters);
        await saveFile(res.data, `davinci-discovery-${projectId}.csv`);
      } else {
        const json = await discoveryApi.downloadJsonAll(projectId, filters);
        await saveFile(json, `davinci-discovery-${projectId}.json`);
      }
    },
    onSuccess: (_data, variables) => {
      toast.success(
        variables.format === 'csv'
          ? 'CSV baixado com sucesso.'
          : 'JSON baixado com sucesso.',
      );
    },
    onError: (err) => {
      toast.error(extractApiErrorMessage(err, 'Falha ao baixar catálogo de descoberta'));
    },
  });
}
