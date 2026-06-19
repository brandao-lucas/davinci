'use client';

import { use } from 'react';
import { PageHeader } from '@/components/layout/page-header';
import { ProjectStatsOverview } from '@/components/projects/project-stats-overview';
import { AdvancedSearchBlock } from '@/components/projects/advanced-search/AdvancedSearchBlock';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { useProject, useProjectStats, useDispatchSearch } from '@/lib/hooks/use-projects';
import { useJobs, useJobPolling } from '@/lib/hooks/use-jobs';
import { JobStatusCard } from '@/components/jobs/job-status-card';
import { descriptorsChangedSinceLastSearch } from '@/lib/utils/descriptor-diff';
import { Loader2, Play } from 'lucide-react';

export default function ProjectOverviewPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = use(params);
  const { data: project, isLoading } = useProject(projectId);
  const { data: stats } = useProjectStats(projectId);
  const { data: jobs } = useJobs(projectId);
  const dispatchSearch = useDispatchSearch(projectId);

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

  return (
    <div className="space-y-6">
      <PageHeader
        title={project.title}
        description={project.query_term}
        actions={
          <div className="flex items-center gap-3">
            <Badge variant="outline">{project.status}</Badge>
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

      {/* Bloco de Pesquisa Avançada — visível apenas em projetos draft */}
      {project.status === 'draft' && (
        <>
          <Separator />
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
    </div>
  );
}
