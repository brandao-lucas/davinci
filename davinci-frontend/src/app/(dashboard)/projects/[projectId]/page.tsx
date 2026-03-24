'use client';

import { use } from 'react';
import { PageHeader } from '@/components/layout/page-header';
import { ProjectStatsOverview } from '@/components/projects/project-stats-overview';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { useProject, useProjectStats, useDispatchSearch } from '@/lib/hooks/use-projects';
import { useJobs } from '@/lib/hooks/use-jobs';
import { JobStatusCard } from '@/components/jobs/job-status-card';
import { Play } from 'lucide-react';

export default function ProjectOverviewPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = use(params);
  const { data: project, isLoading } = useProject(projectId);
  const { data: stats } = useProjectStats(projectId);
  const { data: jobs } = useJobs(projectId);
  const dispatchSearch = useDispatchSearch(projectId);

  const latestJob = jobs?.results?.[0];

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
              disabled={dispatchSearch.isPending}
            >
              <Play className="h-4 w-4 mr-2" />
              {dispatchSearch.isPending ? 'Starting…' : 'Start Search'}
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
