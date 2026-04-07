'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem,
  DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  Dialog, DialogContent, DialogDescription, DialogHeader,
  DialogTitle, DialogFooter,
} from '@/components/ui/dialog';
import { FileText, Database, MoreVertical, Pencil, Trash2 } from 'lucide-react';
import { formatDate } from '@/lib/utils/format';
import { useDeleteProject, useUpdateProject } from '@/lib/hooks/use-projects';
import type { DaVinciProject } from '@/lib/types/project';

const statusColors: Record<DaVinciProject['status'], string> = {
  draft: 'bg-gray-100 text-gray-800',
  searching: 'bg-blue-100 text-blue-800',
  curating: 'bg-amber-100 text-amber-800',
  analyzing: 'bg-violet-100 text-violet-800',
  complete: 'bg-green-100 text-green-800',
};

const editSchema = z.object({
  title: z.string().min(1, 'Title is required'),
  description: z.string().optional(),
  query_term: z.string().min(1, 'Query term is required'),
  date_from: z.string().optional(),
  date_to: z.string().optional(),
});

type EditFormData = z.infer<typeof editSchema>;

export function ProjectCard({ project }: { project: DaVinciProject }) {
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const deleteProject = useDeleteProject();
  const updateProject = useUpdateProject(project.id);

  const isDraft = project.status === 'draft';

  const { register, handleSubmit, formState: { errors }, reset } = useForm<EditFormData>({
    resolver: zodResolver(editSchema),
    defaultValues: {
      title: project.title,
      description: project.description,
      query_term: project.query_term,
      date_from: project.date_from?.toString() ?? '',
      date_to: project.date_to?.toString() ?? '',
    },
  });

  const onEdit = async (data: EditFormData) => {
    await updateProject.mutateAsync({
      title: data.title,
      description: data.description,
      query_term: data.query_term,
      date_from: data.date_from ? parseInt(data.date_from, 10) : undefined,
      date_to: data.date_to ? parseInt(data.date_to, 10) : undefined,
    });
    setEditOpen(false);
    reset();
  };

  const handleDelete = async () => {
    await deleteProject.mutateAsync(project.id);
    setConfirmDelete(false);
  };

  return (
    <>
      <Card className="hover:shadow-md transition-shadow h-full">
        <CardHeader className="pb-2">
          <div className="flex items-start justify-between gap-2">
            <Link href={`/projects/${project.id}`} className="flex-1 min-w-0">
              <CardTitle className="text-base line-clamp-2 hover:underline">
                {project.title}
              </CardTitle>
            </Link>
            <div className="flex items-center gap-1 shrink-0">
              <Badge className={statusColors[project.status]} variant="outline">
                {project.status}
              </Badge>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button variant="ghost" size="icon" className="h-7 w-7">
                    <MoreVertical className="h-4 w-4" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  {isDraft && (
                    <DropdownMenuItem onClick={() => setEditOpen(true)}>
                      <Pencil className="h-4 w-4 mr-2" />
                      Edit
                    </DropdownMenuItem>
                  )}
                  {isDraft && <DropdownMenuSeparator />}
                  <DropdownMenuItem
                    className="text-destructive focus:text-destructive"
                    onClick={() => setConfirmDelete(true)}
                  >
                    <Trash2 className="h-4 w-4 mr-2" />
                    Delete
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          <Link href={`/projects/${project.id}`} className="block">
            <p className="text-sm text-muted-foreground font-mono">{project.query_term}</p>

            {project.stats && (
              <div className="flex items-center gap-4 text-sm text-muted-foreground mt-3">
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

            <p className="text-xs text-muted-foreground mt-3">
              Created {formatDate(project.created_at)}
            </p>
          </Link>
        </CardContent>
      </Card>

      {/* Edit dialog — only reachable for draft projects */}
      <Dialog open={editOpen} onOpenChange={setEditOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Edit Project</DialogTitle>
            <DialogDescription>Update the project details.</DialogDescription>
          </DialogHeader>
          <form onSubmit={handleSubmit(onEdit)} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor={`title-${project.id}`}>Title</Label>
              <Input id={`title-${project.id}`} {...register('title')} />
              {errors.title && <p className="text-xs text-destructive">{errors.title.message}</p>}
            </div>

            <div className="space-y-1.5">
              <Label htmlFor={`query_term-${project.id}`}>Query Term</Label>
              <Input id={`query_term-${project.id}`} {...register('query_term')} />
              {errors.query_term && <p className="text-xs text-destructive">{errors.query_term.message}</p>}
            </div>

            <div className="space-y-1.5">
              <Label htmlFor={`description-${project.id}`}>Description (optional)</Label>
              <Textarea id={`description-${project.id}`} {...register('description')} rows={2} />
            </div>

            <div className="flex gap-4">
              <div className="flex-1 space-y-1.5">
                <Label htmlFor={`date_from-${project.id}`}>From year</Label>
                <Input id={`date_from-${project.id}`} type="number" {...register('date_from')} placeholder="2010" />
              </div>
              <div className="flex-1 space-y-1.5">
                <Label htmlFor={`date_to-${project.id}`}>To year</Label>
                <Input id={`date_to-${project.id}`} type="number" {...register('date_to')} placeholder="2025" />
              </div>
            </div>

            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setEditOpen(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={updateProject.isPending}>
                {updateProject.isPending ? 'Saving…' : 'Save'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* Delete confirmation */}
      <Dialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Delete &ldquo;{project.title}&rdquo;?</DialogTitle>
            <DialogDescription>
              This action cannot be undone. The project and all its data will be permanently deleted.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmDelete(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={handleDelete}
              disabled={deleteProject.isPending}
            >
              {deleteProject.isPending ? 'Deleting…' : 'Delete'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
