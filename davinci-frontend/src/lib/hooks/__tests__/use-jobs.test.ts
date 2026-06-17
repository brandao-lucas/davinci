/**
 * Testes de regressão para useJobPolling (Fase 2 — patch de cache no término de job).
 *
 * O que cada teste trava:
 *  - patch imediato: se o setQueryData no useEffect for removido, o cache da lista
 *    não atualiza antes do refetch assíncrono → status na lista permanece 'running' → falha.
 *  - anti-loop: a query de polling individual ['jobs', projectId, jobId] NÃO deve ser
 *    invalidada ao atingir estado terminal — se fosse, forçaria um refetch que re-ativaria
 *    o polling (loop). O teste verifica que invalidateQueries foi chamada com exact:true
 *    apontando APENAS para ['jobs', projectId] (sem o jobId).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';
import type { PaginatedResponse } from '@/lib/types/api';
import type { IngestionJob } from '@/lib/types/job';

// ── Mocks ─────────────────────────────────────────────────────────────────────

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock('@/lib/api/jobs', () => ({
  jobsApi: {
    list: vi.fn(),
    get: vi.fn(),
    cancel: vi.fn(),
  },
}));

import { jobsApi } from '@/lib/api/jobs';
import { useJobPolling } from '../use-jobs';

// ── Helpers ───────────────────────────────────────────────────────────────────

const PROJECT_ID = 'proj-1';
const JOB_ID = 'job-uuid-abc';

function makeJob(overrides: Partial<IngestionJob> = {}): IngestionJob {
  return {
    id: JOB_ID,
    job_type: 'pubmed_search',
    status: 'running',
    parameters: {},
    records_processed: 0,
    records_inserted: 0,
    records_updated: 0,
    error_message: '',
    created_at: '2024-01-01T00:00:00Z',
    started_at: '2024-01-01T00:00:01Z',
    completed_at: null,
    ...overrides,
  };
}

function makeWrapper(queryClient: QueryClient) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client: queryClient }, children);
  };
}

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: Infinity },
      mutations: { retry: false },
    },
  });
}

/** Popula a lista de jobs no cache (query usada por useJobs). */
function seedJobsList(queryClient: QueryClient, jobs: IngestionJob[]) {
  const data: PaginatedResponse<IngestionJob> = {
    count: jobs.length,
    next: null,
    previous: null,
    results: jobs,
  };
  queryClient.setQueryData(['jobs', PROJECT_ID], data);
}

// ── Testes ────────────────────────────────────────────────────────────────────

describe('useJobPolling', () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    queryClient = makeQueryClient();
    vi.resetAllMocks();
  });

  it('reflete o status final do job na lista de jobs ao atingir estado terminal', async () => {
    // Arrange: lista com o job ainda em 'running'
    const runningJob = makeJob({ status: 'running' });
    seedJobsList(queryClient, [runningJob]);

    // jobsApi.get retorna o job em estado terminal 'completed'
    const completedJob = makeJob({ status: 'completed', completed_at: '2024-01-01T00:05:00Z' });
    vi.mocked(jobsApi.get).mockResolvedValue({
      data: completedJob,
      status: 200,
      statusText: 'OK',
      headers: {},
      config: {} as never,
    });

    const { result } = renderHook(
      () => useJobPolling(PROJECT_ID, JOB_ID),
      { wrapper: makeWrapper(queryClient) },
    );

    // Aguarda o hook buscar os dados e o useEffect executar o patch
    await waitFor(() => {
      expect(result.current.data?.status).toBe('completed');
    });

    // Assert: o patch imediato deve ter atualizado a lista de jobs
    await waitFor(() => {
      const listCache = queryClient.getQueryData<PaginatedResponse<IngestionJob>>(
        ['jobs', PROJECT_ID],
      );
      expect(listCache?.results[0].status).toBe('completed');
    });
  });

  it('não invalida a query de polling individual (anti-loop)', async () => {
    // Arrange: popula lista
    seedJobsList(queryClient, [makeJob({ status: 'running' })]);

    const completedJob = makeJob({ status: 'completed', completed_at: '2024-01-01T00:05:00Z' });
    vi.mocked(jobsApi.get).mockResolvedValue({
      data: completedJob,
      status: 200,
      statusText: 'OK',
      headers: {},
      config: {} as never,
    });

    // Espiona invalidateQueries no queryClient real
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

    renderHook(
      () => useJobPolling(PROJECT_ID, JOB_ID),
      { wrapper: makeWrapper(queryClient) },
    );

    // Aguarda o job ser marcado como terminal e o useEffect disparar
    await waitFor(() => {
      // Pelo menos uma chamada a invalidateQueries deve ter ocorrido
      expect(invalidateSpy).toHaveBeenCalled();
    });

    // Assert: invalidateQueries foi chamada com exact: true para a lista
    // e NUNCA foi chamada com a queryKey que inclui o jobId (o que causaria loop)
    const calls = invalidateSpy.mock.calls;

    // Deve haver chamada para ['jobs', PROJECT_ID] com exact: true
    const listInvalidation = calls.find(
      ([options]) =>
        JSON.stringify((options as { queryKey?: unknown }).queryKey) ===
        JSON.stringify(['jobs', PROJECT_ID]) &&
        (options as { exact?: boolean }).exact === true,
    );
    expect(listInvalidation).toBeDefined();

    // NÃO deve haver nenhuma chamada que inclua o JOB_ID na queryKey
    // (isso causaria loop invalidate → refetch do polling → novo terminal → invalidate...)
    const pollingInvalidation = calls.find(([options]) => {
      const key = (options as { queryKey?: unknown[] }).queryKey;
      return Array.isArray(key) && key.includes(JOB_ID);
    });
    expect(pollingInvalidation).toBeUndefined();
  });
});
