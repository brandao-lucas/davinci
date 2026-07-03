import apiClient from './client';
import type { DiscoveryPage, DiscoveryFilters } from '@/lib/types/discovery';

// Constrói URLSearchParams respeitando arrays repetíveis (ex: omics_layer=a&omics_layer=b).
// O axios padrão serializa arrays como omics_layer[]=a, que o backend Django rejeita.
// Passando URLSearchParams diretamente, o axios usa `.toString()` sem adicionar colchetes.
function buildParams(
  filters: DiscoveryFilters,
  extra: Record<string, string | number | boolean | undefined>,
): URLSearchParams {
  const p = new URLSearchParams();

  // Campos escalares do filtro (derivados dos params do schema OpenAPI)
  // Nota: `search` não é param da operação projects_datasets_export_retrieve — omitido.
  const scalar: (keyof DiscoveryFilters)[] = [
    'access_type',
    'data_format',
    'disease_axis',
    'disease_axis_confidence_min',
    'gene',
    'has_control_group',
    'has_control_group_confidence_min',
    'has_sample_join_key',
    'is_single_cell',
    'is_single_cell_confidence_min',
    'monogenic_gene_hit',
    'omics_count_max',
    'omics_count_min',
    'sample_join_key_confidence_min',
    'version',
  ];

  for (const key of scalar) {
    const value = filters[key];
    if (value !== undefined && value !== null && value !== '') {
      p.append(key, String(value));
    }
  }

  // omics_layer é array repetível — cada valor vira um param separado
  if (filters.omics_layer !== undefined) {
    const layers = Array.isArray(filters.omics_layer)
      ? filters.omics_layer
      : [filters.omics_layer];
    for (const layer of layers) {
      if (layer) p.append('omics_layer', layer);
    }
  }

  // Campos de controle extras (export_format, page, page_size)
  for (const [key, value] of Object.entries(extra)) {
    if (value !== undefined && value !== null) {
      p.append(key, String(value));
    }
  }

  return p;
}

export const discoveryApi = {
  // GET paginado — export_format=json (padrão)
  listJson: (projectId: string, filters: DiscoveryFilters, page = 1) =>
    apiClient.get<DiscoveryPage>(`/projects/${projectId}/datasets/export/`, {
      params: buildParams(filters, { export_format: 'json', page }),
    }),

  // GET CSV completo — StreamingHttpResponse, responseType 'text' para não parsear como JSON
  downloadCsv: (projectId: string, filters: DiscoveryFilters) =>
    apiClient.get<string>(`/projects/${projectId}/datasets/export/`, {
      params: buildParams(filters, { export_format: 'csv' }),
      responseType: 'text',
    }),

  // GET JSON completo — pagina com page_size=200 seguindo `next` para montar dump completo.
  // Trata 429 (throttle download_content) lançando erro com mensagem clara.
  // Cap de segurança: máximo 500 páginas (100 000 itens a page_size=200) para evitar loop infinito.
  downloadJsonAll: async (projectId: string, filters: DiscoveryFilters): Promise<string> => {
    const PAGE_SIZE = 200;
    const MAX_PAGES = 500;
    let page = 1;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const allItems: any[] = [];

    while (page <= MAX_PAGES) {
      const res = await apiClient.get<DiscoveryPage>(
        `/projects/${projectId}/datasets/export/`,
        {
          params: buildParams(filters, { export_format: 'json', page, page_size: PAGE_SIZE }),
        },
      );

      allItems.push(...res.data.results);

      if (!res.data.next) break;
      page++;
    }

    return JSON.stringify(allItems, null, 2);
  },
};
