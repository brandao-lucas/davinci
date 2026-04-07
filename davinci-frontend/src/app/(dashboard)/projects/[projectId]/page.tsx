'use client';

import { use } from 'react';
import { PageHeader } from '@/components/layout/page-header';
import { ProjectStatsOverview } from '@/components/projects/project-stats-overview';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { useProject, useProjectStats, useDispatchSearch } from '@/lib/hooks/use-projects';
import { useJobs, useJobPolling } from '@/lib/hooks/use-jobs';
import { JobStatusCard } from '@/components/jobs/job-status-card';
import { Loader2, Play } from 'lucide-react';

export default function ProjectOverviewPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = use(params);
  const { data: project, isLoading } = useProject(projectId);
  const { data: stats } = useProjectStats(projectId);
  const { data: jobs } = useJobs(projectId);
  const dispatchSearch = useDispatchSearch(projectId);

  const latestJob = jobs?.results?.[0];

  // Poll the latest job while it's active so the button reflects real-time state
  const latestJobId = latestJob?.id ?? '';
  useJobPolling(projectId, latestJobId);

  const jobIsActive = latestJob?.status === 'pending' || latestJob?.status === 'running';
  const isProcessing = dispatchSearch.isPending || jobIsActive;

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
              disabled={isProcessing}
              variant={dispatchSearch.isError ? 'destructive' : 'default'}
              title={dispatchSearch.isError ? 'Failed to start search — check if Celery worker is running' : undefined}
            >
              {isProcessing
                ? <><Loader2 className="h-4 w-4 mr-2 animate-spin" />Processing request…</>
                : dispatchSearch.isError
                  ? 'Failed — Retry'
                  : <><Play className="h-4 w-4 mr-2" />Start Search</>
              }
            </Button>
          </div>
        }
      />

      {stats && <ProjectStatsOverview stats={stats} />}

      {latestJob && (
        <div>
          <h2 className="text-sm font-medium text-muted-foreground mb-3">Latest Job</h2>
          <JobStatusCard job={latestJob} projectId={projectId} />
        </div>
      )}
    </div>
  );
}
