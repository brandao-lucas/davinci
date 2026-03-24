'use client';

import { PageHeader } from '@/components/layout/page-header';
import { ProjectCard } from '@/components/projects/project-card';
import { CreateProjectDialog } from '@/components/projects/create-project-dialog';
import { useProjects } from '@/lib/hooks/use-projects';

export default function ProjectsPage() {
  const { data, isLoading } = useProjects();

  return (
    <div>
      <PageHeader
        title="Projects"
        description="Manage your systematic review projects"
        actions={<CreateProjectDialog />}
      />

      {isLoading ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="h-40 rounded-lg bg-muted animate-pulse" />
          ))}
        </div>
      ) : data?.results.length === 0 ? (
        <div className="text-center py-24 text-muted-foreground">
          <p className="text-lg">No projects yet.</p>
          <p className="text-sm mt-1">Create your first project to get started.</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {data?.results.map((project) => (
            <ProjectCard key={project.id} project={project} />
          ))}
        </div>
      )}
    </div>
  );
}
