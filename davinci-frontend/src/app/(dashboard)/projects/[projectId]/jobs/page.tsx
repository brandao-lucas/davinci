'use client';

import { use } from 'react';
import { PageHeader } from '@/components/layout/page-header';
import { JobStatusCard } from '@/components/jobs/job-status-card';
import { useJobs } from '@/lib/hooks/use-jobs';

export default function JobsPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = use(params);
  const { data, isLoading } = useJobs(projectId);
  const jobs = data?.results ?? [];

  return (
    <div className="space-y-4">
      <PageHeader title="Ingestion Jobs" description={`${jobs.length} jobs`} />

      {isLoading ? (
        <div className="space-y-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="h-28 bg-muted rounded-lg animate-pulse" />
          ))}
        </div>
      ) : jobs.length === 0 ? (
        <p className="text-center py-16 text-muted-foreground">No jobs yet. Start a search to ingest data.</p>
      ) : (
        <div className="space-y-3">
          {jobs.map((job) => (
            <JobStatusCard key={job.id} job={job} projectId={projectId} />
          ))}
        </div>
      )}
    </div>
  );
}
