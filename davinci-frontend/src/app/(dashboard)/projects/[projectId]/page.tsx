'use client';

import { use, useState } from 'react';
import { PageHeader } from '@/components/layout/page-header';
import { ProjectStatsOverview } from '@/components/projects/project-stats-overview';
import { AdvancedSearchBlock } from '@/components/projects/advanced-search/AdvancedSearchBlock';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { useProject, useProjectStats, useDispatchSearch } from '@/lib/hooks/use-projects';
import { useJobs, useJobPolling } from '@/lib/hooks/use-jobs';
import { JobStatusCard } from '@/components/jobs/job-status-card';
import { descriptorsChangedSinceLastSearch } from '@/lib/utils/descriptor-diff';
import { Loader2, Play, PencilLine } from 'lucide-react';

export default function ProjectOverviewPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = use(params);
  const { data: project, isLoading } = useProject(projectId);
  const { data: stats } = useProjectStats(projectId);
  const { data: jobs } = useJobs(projectId);
  const dispatchSearch = useDispatchSearch(projectId);

  // Controla o diálogo de confirmação "Refinar busca" (só aparece em status=searching)
  const [refineConfirmOpen, setRefineConfirmOpen] = useState(false);
  const [refineBlockVisible, setRefineBlockVisible] = useState(false);

  const latestJob = jobs?.results?.[0];

  // Poll the latest job while it's active so the button reflects real-time state.
  // After pubmed_search completes, the backend auto-chains a geo_search job.
  // The polling invalidation (in useJobPolling) refreshes the list so that
  // latestJob transitions from pubmed_search → geo_search automatically.
  const latestJobId = latestJob?.id ?? '';
  useJobPolling(projectId, latestJobId);

  const jobIsActive = latestJob?.status === 'pending' || latestJob?.status === 'running';
  const isProcessing = dispatchSearch.isPending || jobIsActive;

  // Human-readable phase label based on the current active job type
  const phaseLabel: string = (() => {
    if (!isProcessing) return '';
    if (dispatchSearch.isPending) return 'Starting…';
    if (latestJob?.job_type === 'geo_search') return 'Fetching datasets…';
    return 'Fetching papers…';
  })();

  // Desabilita o botão quando os descritores não mudaram desde a última busca concluída
  const descriptorsChanged = project
    ? descriptorsChangedSinceLastSearch(project, jobs?.results, 'pubmed_search')
    : true;
  const alreadySearched = !descriptorsChanged;

  const isSearchDisabled = isProcessing || alreadySearched;

  let searchButtonTitle: string | undefined;
  if (dispatchSearch.isError) {
    searchButtonTitle = 'Failed to start search — check if Celery worker is running';
  } else if (alreadySearched && !isProcessing) {
    searchButtonTitle = 'Já buscado com os descritores atuais. Altere termo/sinônimos/datas para rebuscar.';
  }

  if (isLoading) {
    return <div className="h-40 bg-muted rounded-lg animate-pulse" />;
  }

  if (!project) return null;

  const isSearching = project.status === 'searching';
  const isDraft = project.status === 'draft';

  // O bloco de refinamento é exibido em draft (sempre) ou em searching (após confirmação)
  const showAdvancedBlock = isDraft || (isSearching && refineBlockVisible);

  return (
    <div className="space-y-6">
      <PageHeader
        title={project.title}
        description={project.query_term}
        actions={
          <div className="flex items-center gap-3">
            <Badge variant="outline">{project.status}</Badge>

            {/* Botão "Refinar busca" — aparece somente em status=searching */}
            {isSearching && (
              <Button
                variant="outline"
                onClick={() => setRefineConfirmOpen(true)}
              >
                <PencilLine className="h-4 w-4 mr-2" />
                Refinar busca
              </Button>
            )}

            <Button
              onClick={() => dispatchSearch.mutate()}
              disabled={isSearchDisabled}
              variant={dispatchSearch.isError ? 'destructive' : 'default'}
              title={searchButtonTitle}
            >
              {isProcessing
                ? <><Loader2 className="h-4 w-4 mr-2 animate-spin" />{phaseLabel}</>
                : dispatchSearch.isError
                  ? 'Failed — Retry'
                  : <><Play className="h-4 w-4 mr-2" />Start Search</>
              }
            </Button>
          </div>
        }
      />

      {stats && <ProjectStatsOverview stats={stats} projectId={projectId} />}

      {/* Bloco de Pesquisa Avançada — visível em draft ou em searching após confirmação */}
      {showAdvancedBlock && (
        <>
          <Separator />
          <div className="space-y-1">
            <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
              {isDraft ? 'Passo 2 de 3 — Refine sua busca' : 'Refinando busca em andamento'}
            </p>
            <p className="text-sm text-muted-foreground">
              {isDraft
                ? <>Adicione descritores MeSH para ampliar a cobertura da busca ou use o botão{' '}
                    <strong>Iniciar pesquisa</strong> no topo para buscar com o termo atual.</>
                : 'Altere os descritores MeSH e salve. Ao salvar, a busca em andamento será interrompida e o projeto voltará para rascunho.'}
            </p>
          </div>
          <AdvancedSearchBlock project={project} />
          <Separator />
        </>
      )}

      {latestJob && (
        <div>
          <h2 className="text-sm font-medium text-muted-foreground mb-3">Latest Job</h2>
          <JobStatusCard job={latestJob} projectId={projectId} />
        </div>
      )}

      {/* Diálogo de confirmação para refinamento durante busca em andamento */}
      <AlertDialog open={refineConfirmOpen} onOpenChange={setRefineConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Refinar busca em andamento?</AlertDialogTitle>
            <AlertDialogDescription>
              Refinar a busca vai interromper a busca em andamento. Os resultados já obtidos são mantidos.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancelar</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setRefineConfirmOpen(false);
                setRefineBlockVisible(true);
              }}
            >
              Continuar e refinar
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
