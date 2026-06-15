import { AxiosError } from 'axios';

export function extractApiErrorMessage(err: unknown, fallback: string): string {
  if (err instanceof AxiosError) {
    const data = err.response?.data as { detail?: string } | undefined;
    if (data?.detail) return data.detail;
    if (err.response?.status === 401) return 'Sessão expirada. Faça login novamente.';
    if (err.response?.status === 403) return 'Acesso negado.';
    if (err.response?.status === 404) return 'Recurso não encontrado.';
    if (err.response?.status === 500) return 'Erro no servidor. Tente novamente.';
  }
  return fallback;
}
