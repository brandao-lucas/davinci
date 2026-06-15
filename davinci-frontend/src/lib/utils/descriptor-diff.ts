/**
 * Utilitário para comparar descritores de um projeto com os parâmetros
 * salvos no último job concluído, permitindo desabilitar o botão "Start Search"
 * quando nada mudou desde a última busca.
 *
 * Comparação feita sobre campos CRUS (não a query combinada que o backend monta
 * com OR), para evitar divergências de normalização.
 */

import type { DaVinciProject } from '@/lib/types/project';
import type { IngestionJob } from '@/lib/types/job';

interface JobParameters {
  synonyms?: string[] | null;
  date_from?: number | string | null;
  date_to?: number | string | null;
  query?: string;
  [key: string]: unknown;
}

function normalizeSynonyms(synonyms: string[] | null | undefined): string[] {
  if (!synonyms || !Array.isArray(synonyms)) return [];
  return [...synonyms].sort();
}

function normalizeDate(val: number | string | null | undefined): string {
  if (val === null || val === undefined || val === '') return '';
  return String(val);
}

/**
 * Retorna `true` se os descritores do projeto MUDARAM em relação
 * ao último job concluído do tipo informado.
 *
 * Retorna `true` também quando não há job concluído (nenhuma busca foi feita
 * ainda — o botão deve ficar habilitado).
 */
export function descriptorsChangedSinceLastSearch(
  project: DaVinciProject,
  jobs: IngestionJob[] | undefined,
  jobType: 'pubmed_search' | 'geo_search',
): boolean {
  if (!jobs || jobs.length === 0) return true;

  const lastCompleted = jobs
    .filter((j) => j.job_type === jobType && j.status === 'completed')
    .sort((a, b) => new Date(b.completed_at ?? b.created_at).getTime() - new Date(a.completed_at ?? a.created_at).getTime())[0];

  if (!lastCompleted) return true;

  const params = lastCompleted.parameters as JobParameters;

  // Compara query_term (campo cru do projeto) com o query_term implícito.
  // O backend guarda `query` como o combined (termo + OR sinônimos), mas
  // guarda `synonyms` separado. Reconstruímos apenas o termo base:
  // Se o usuário mudou só os sinônimos, a comparação via `synonyms` e `query_term`
  // captura a mudança individualmente.

  const projectSynonymsNorm = normalizeSynonyms(project.query_synonyms);
  const jobSynonymsNorm = normalizeSynonyms(params.synonyms);

  const synonymsMatch =
    projectSynonymsNorm.length === jobSynonymsNorm.length &&
    projectSynonymsNorm.every((s, i) => s === jobSynonymsNorm[i]);

  // Verificamos o term principal comparando o que foi gravado em `query`:
  // o serviço monta `combined_query = query_term` quando não há sinônimos,
  // ou `query_term OR s1 OR s2` quando há. Para capturar mudança em query_term
  // sozinho, extraímos o primeiro token antes de " OR " caso haja sinônimos
  // no job — mas apenas se o job tiver sinônimos. Caso contrário, query == query_term.
  let jobQueryTerm: string;
  if (jobSynonymsNorm.length > 0 && params.query) {
    // "term OR syn1 OR syn2" → "term"
    jobQueryTerm = params.query.split(' OR ')[0].trim();
  } else {
    jobQueryTerm = (params.query ?? '').trim();
  }

  const queryTermMatch =
    (project.query_term ?? '').trim() === jobQueryTerm;

  const dateFromMatch =
    normalizeDate(project.date_from) === normalizeDate(params.date_from);

  const dateToMatch =
    normalizeDate(project.date_to) === normalizeDate(params.date_to);

  const allMatch = queryTermMatch && synonymsMatch && dateFromMatch && dateToMatch;

  // changed == true → botão habilitado; changed == false → botão desabilitado
  return !allMatch;
}
