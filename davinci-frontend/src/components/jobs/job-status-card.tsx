'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';
import { formatDateTime } from '@/lib/utils/format';
import { useCancelJob } from '@/lib/hooks/use-jobs';
import type { IngestionJob } from '@/lib/types/job';

const statusColors: Record<IngestionJob['status'], string> = {
  pending: 'bg-amber-100 text-amber-800',
  running: 'bg-blue-100 text-blue-800',
  completed: 'bg-green-100 text-green-800',
  failed: 'bg-red-100 text-red-800',
  cancelled: 'bg-gray-100 text-gray-800',
};

export function JobStatusCard({ job, projectId }: { job: IngestionJob; projectId: string }) {
  const cancelJob = useCancelJob(projectId);
  const isActive = job.status === 'pending' || job.status === 'running';

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <CardTitle className="text-sm font-medium">{job.job_type.replace(/_/g, ' ')}</CardTitle>
          <div className="flex items-center gap-2">
            <Badge className={statusColors[job.status]} variant="outline">{job.status}</Badge>
            {isActive && (
              <Button size="sm" variant="outline" onClick={() => cancelJob.mutate(job.id)}>
                Cancel
              </Button>
            )}
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        {isActive && <Progress className="h-1.5" />}

        <div className="grid grid-cols-3 gap-4 text-muted-foreground">
          <div>
            <p className="text-xs">Processed</p>
            <p className="font-medium text-foreground">{job.records_processed}</p>
          </div>
          <div>
            <p className="text-xs">Inserted</p>
            <p className="font-medium text-foreground">{job.records_inserted}</p>
          </div>
          <div>
            <p className="text-xs">Updated</p>
            <p className="font-medium text-foreground">{job.records_updated}</p>
          </div>
        </div>

        {job.error_message && (
          <p className="text-xs text-destructive bg-destructive/10 rounded p-2">{job.error_message}</p>
        )}

        <p className="text-xs text-muted-foreground">
          {job.started_at ? `Started ${formatDateTime(job.started_at)}` : `Queued ${formatDateTime(job.created_at)}`}
        </p>
      </CardContent>
    </Card>
  );
}
