'use client';

import { use } from 'react';
import { PageHeader } from '@/components/layout/page-header';
import { ProjectStatsOverview } from '@/components/projects/project-stats-overview';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { useProjectStats } from '@/lib/hooks/use-projects';

export default function AnalysisPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = use(params);
  const { data: stats, isLoading } = useProjectStats(projectId);

  if (isLoading) return <div className="h-64 bg-muted rounded-lg animate-pulse" />;
  if (!stats) return <p className="text-muted-foreground">No stats available yet.</p>;

  return (
    <div className="space-y-6">
      <PageHeader title="Analysis" description="Visual summary of your curation" />

      <ProjectStatsOverview stats={stats} />

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Top Genes</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex flex-wrap gap-2">
              {stats.top_genes.map((g, i) => (
                <Badge key={i} variant="outline" className="font-mono">
                  {g.gene} <span className="ml-1 text-muted-foreground">×{g.count}</span>
                </Badge>
              ))}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Top MeSH Terms</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex flex-wrap gap-2">
              {stats.top_mesh_terms.map((m, i) => (
                <Badge key={i} variant="secondary">
                  {m.term} <span className="ml-1">×{m.count}</span>
                </Badge>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
