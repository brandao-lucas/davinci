/**
 * Testes de regressão para useCuratePaper e useBulkCurate (Fase 2 — optimistic update).
 *
 * O que cada teste trava:
 *  - useCuratePaper/optimistic: se o onMutate parar de chamar setQueriesData,
 *    o cache não muda antes da API resolver → getQueriesData retorna status original → falha.
 *  - useCuratePaper/rollback: se o onError parar de restaurar o snapshot,
 *    o cache continua com o patch errado → status não volta ao original → falha.
 *  - useCuratePaper/auditoria: se applyPatchToPaper zerasse curated_at/exclusion_reason/notes,
 *    os valores originais somem → falha.
 *  - useBulkCurate/patch múltiplos: se setQueriesData não cobrir todos os ids do conjunto,
 *    pelo menos um paper fica com status original → falha.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';
import type { PaginatedResponse } from '@/lib/types/api';
import type { Paper } from '@/lib/types/paper';

// ── Mocks de módulos externos ─────────────────────────────────────────────────

// sonner: não precisa de DOM/toast real nos testes de lógica de cache
vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

// papersApi: mock completo — nenhuma chamada de rede real
vi.mock('@/lib/api/papers', () => ({
  papersApi: {
    list: vi.fn(),
    get: vi.fn(),
    curate: vi.fn(),
    bulkCurate: vi.fn(),
    search: vi.fn(),
  },
}));

// Importa após vi.mock para pegar a versão mockada
import { papersApi } from '@/lib/api/papers';
import { useCuratePaper, useBulkCurate } from '../use-papers';

// ── Helpers ───────────────────────────────────────────────────────────────────

const PROJECT_ID = 'proj-1';

/** Cria um Paper mínimo válido para o type-system. */
function makePaper(overrides: Partial<Paper> = {}): Paper {
  return {
    id: 1,
    pmid: 12345678,
    pmc_id: '',
    doi: '',
    title: 'Artigo de teste',
    abstract: '',
    journal: 'Nature',
    pub_year: 2024,
    pub_month: null,
    pub_type: 'Journal Article',
    curation_status: 'pending',
    exclusion_reason: undefined,
    notes: undefined,
    relevance_score: null,
    clinical_categories: [],
    user_categories: [],
    added_at: '2024-01-01T00:00:00Z',
    curated_at: null,
    ...overrides,
  };
}

/** Wrapper com QueryClientProvider isolado por teste. */
function makeWrapper(queryClient: QueryClient) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client: queryClient }, children);
  };
}

/** Cria QueryClient com retry e gcTime desabilitados para testes síncronos. */
function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: Infinity },
      mutations: { retry: false },
    },
  });
}

/** Popula o cache com uma lista paginada de papers. */
function seedPapersList(queryClient: QueryClient, papers: Paper[]) {
  const paginatedData: PaginatedResponse<Paper> = {
    count: papers.length,
    next: null,
    previous: null,
    results: papers,
  };
  queryClient.setQueryData(['papers', PROJECT_ID, undefined], paginatedData);
}

// ── Testes ────────────────────────────────────────────────────────────────────

describe('useCuratePaper', () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    queryClient = makeQueryClient();
    vi.resetAllMocks();
  });

  it('aplica patch otimista no cache imediatamente (antes da API resolver)', async () => {
    // Arrange: popula cache com um paper em status 'pending'
    const paper = makePaper({ id: 10, curation_status: 'pending' });
    seedPapersList(queryClient, [paper]);

    // API devolve uma promessa que nunca resolve durante o teste —
    // garante que lemos o cache ANTES da API completar.
    let resolveApi!: (v: Paper) => void;
    const apiPromise = new Promise<Paper>((res) => { resolveApi = res; });
    vi.mocked(papersApi.curate).mockReturnValueOnce(
      Promise.resolve({ data: paper, status: 200, statusText: 'OK', headers: {}, config: {} as never }),
    );
    // Sobrescreve com promessa pendente para controlar timing
    vi.mocked(papersApi.curate).mockImplementationOnce(
      () => apiPromise.then((d) => ({ data: d, status: 200, statusText: 'OK', headers: {}, config: {} as never })) as never,
    );

    const { result } = renderHook(
      () => useCuratePaper(PROJECT_ID),
      { wrapper: makeWrapper(queryClient) },
    );

    // Act: dispara a mutação — NÃO aguarda resolução da API
    act(() => {
      result.current.mutate({ paperId: 10, data: { curation_status: 'included' } });
    });

    // Assert: o cache já deve refletir o patch otimista ANTES de a API resolver
    await waitFor(() => {
      const cached = queryClient.getQueryData<PaginatedResponse<Paper>>(
        ['papers', PROJECT_ID, undefined],
      );
      expect(cached?.results[0].curation_status).toBe('included');
    });

    // Cleanup: resolve a promessa para não vazar estado
    resolveApi(makePaper({ id: 10, curation_status: 'included' }));
  });

  it('faz rollback do cache em erro da API', async () => {
    // Arrange: paper com status 'pending'
    const paper = makePaper({ id: 10, curation_status: 'pending' });
    seedPapersList(queryClient, [paper]);

    // API rejeita
    vi.mocked(papersApi.curate).mockRejectedValueOnce(new Error('Servidor indisponível'));

    const { result } = renderHook(
      () => useCuratePaper(PROJECT_ID),
      { wrapper: makeWrapper(queryClient) },
    );

    // Act: dispara mutação e aguarda resolução (erro)
    await act(async () => {
      result.current.mutate({ paperId: 10, data: { curation_status: 'included' } });
    });

    // Aguarda o onError ter rodado e restaurado o snapshot
    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });

    // Assert: cache deve ter voltado ao estado original
    const cached = queryClient.getQueryData<PaginatedResponse<Paper>>(
      ['papers', PROJECT_ID, undefined],
    );
    expect(cached?.results[0].curation_status).toBe('pending');
  });

  it('preserva curated_at, exclusion_reason e notes preexistentes no patch otimista', async () => {
    // Arrange: paper com trilha de auditoria preexistente
    const paper = makePaper({
      id: 10,
      curation_status: 'included',
      curated_at: '2024-06-01T10:00:00Z',
      exclusion_reason: 'fora do escopo',
      notes: 'revisado pelo dr. Silva',
    });
    seedPapersList(queryClient, [paper]);

    // API nunca resolve (testamos só o optimistic)
    let resolveApi!: (v: Paper) => void;
    const apiPromise = new Promise<Paper>((res) => { resolveApi = res; });
    vi.mocked(papersApi.curate).mockImplementationOnce(
      () => apiPromise.then((d) => ({ data: d, status: 200, statusText: 'OK', headers: {}, config: {} as never })) as never,
    );

    const { result } = renderHook(
      () => useCuratePaper(PROJECT_ID),
      { wrapper: makeWrapper(queryClient) },
    );

    // Act: patch que altera só o status; NÃO envia exclusion_reason nem notes
    act(() => {
      result.current.mutate({ paperId: 10, data: { curation_status: 'excluded' } });
    });

    // Assert: campos de auditoria preexistentes intactos no patch otimista
    await waitFor(() => {
      const cached = queryClient.getQueryData<PaginatedResponse<Paper>>(
        ['papers', PROJECT_ID, undefined],
      );
      const p = cached?.results[0];
      expect(p?.curation_status).toBe('excluded');
      // curated_at não deve ser apagado pelo patch otimista
      expect(p?.curated_at).toBe('2024-06-01T10:00:00Z');
      // exclusion_reason e notes preservados quando não enviados no patch
      expect(p?.exclusion_reason).toBe('fora do escopo');
      expect(p?.notes).toBe('revisado pelo dr. Silva');
    });

    resolveApi(paper);
  });
});

describe('useBulkCurate', () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    queryClient = makeQueryClient();
    vi.resetAllMocks();
  });

  it('aplica patch otimista em múltiplos papers simultaneamente', async () => {
    // Arrange: lista com 3 papers, todos 'pending'
    const papers = [
      makePaper({ id: 1, curation_status: 'pending' }),
      makePaper({ id: 2, curation_status: 'pending' }),
      makePaper({ id: 3, curation_status: 'pending' }),
    ];
    seedPapersList(queryClient, papers);

    // API nunca resolve (testamos o patch otimista)
    let resolveApi!: (v: { updated: number }) => void;
    const apiPromise = new Promise<{ updated: number }>((res) => { resolveApi = res; });
    vi.mocked(papersApi.bulkCurate).mockImplementationOnce(
      () => apiPromise.then((d) => ({ data: d, status: 200, statusText: 'OK', headers: {}, config: {} as never })) as never,
    );

    const { result } = renderHook(
      () => useBulkCurate(PROJECT_ID),
      { wrapper: makeWrapper(queryClient) },
    );

    // Act: curar papers 1 e 3 (não o 2)
    act(() => {
      result.current.mutate({
        paper_ids: [1, 3],
        curation_status: 'included',
      });
    });

    // Assert: ids 1 e 3 atualizados; id 2 intocado
    await waitFor(() => {
      const cached = queryClient.getQueryData<PaginatedResponse<Paper>>(
        ['papers', PROJECT_ID, undefined],
      );
      const byId = Object.fromEntries(cached!.results.map((p) => [p.id, p]));
      expect(byId[1].curation_status).toBe('included');
      expect(byId[2].curation_status).toBe('pending'); // não tocado
      expect(byId[3].curation_status).toBe('included');
    });

    resolveApi({ updated: 2 });
  });
});
