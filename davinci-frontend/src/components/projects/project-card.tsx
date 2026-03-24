'use client';

import Link from 'next/link';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { FileText, Database } from 'lucide-react';
import { formatDate } from '@/lib/utils/format';
import type { DaVinciProject } from '@/lib/types/project';

const statusColors: Record<DaVinciProject['status'], string> = {
  draft: 'bg-gray-100 text-gray-800',
  searching: 'bg-blue-100 text-blue-800',
  curating: 'bg-amber-100 text-amber-800',
  analyzing: 'bg-violet-100 text-violet-800',
  complete: 'bg-green-100 text-green-800',
};

export function ProjectCard({ project }: { project: DaVinciProject }) {
  return (
    <Link href={`/projects/${project.id}`}>
      <Card className="hover:shadow-md transition-shadow cursor-pointer h-full">
        <CardHeader className="pb-2">
          <div className="flex items-start justify-between gap-2">
            <CardTitle className="text-base line-clamp-2">{project.title}</CardTitle>
            <Badge className={statusColors[project.status]} variant="outline">
              {project.status}
            </Badge>
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm text-muted-foreground font-mono">{project.query_term}</p>

          {project.stats && (
            <div className="flex items-center gap-4 text-sm text-muted-foreground">
              <span className="flex items-center gap-1">
                <FileText className="h-3.5 w-3.5" />
                {project.stats.total_papers} papers
              </span>
              <span className="flex items-center gap-1">
                <Database className="h-3.5 w-3.5" />
                {project.stats.total_datasets} datasets
              </span>
            </div>
          )}

          <p className="text-xs text-muted-foreground">
            Created {formatDate(project.created_at)}
          </p>
        </CardContent>
      </Card>
    </Link>
  );
}
