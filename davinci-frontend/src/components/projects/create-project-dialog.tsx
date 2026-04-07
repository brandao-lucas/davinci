'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { useCreateProject } from '@/lib/hooks/use-projects';
import { projectsApi } from '@/lib/api/projects';
import { Plus } from 'lucide-react';

const schema = z.object({
  title: z.string().min(1, 'Title is required'),
  description: z.string().optional(),
  query_term: z.string().min(1, 'Query term is required'),
  date_from: z.string().optional(),
  date_to: z.string().optional(),
});

type FormData = z.infer<typeof schema>;

export function CreateProjectDialog() {
  const [open, setOpen] = useState(false);
  const router = useRouter();
  const createProject = useCreateProject();

  const { register, handleSubmit, formState: { errors }, reset } = useForm<FormData>({
    resolver: zodResolver(schema),
  });

  const onSubmit = async (data: FormData) => {
    const payload = {
      title: data.title,
      description: data.description,
      query_term: data.query_term,
      date_from: data.date_from ? parseInt(data.date_from, 10) : undefined,
      date_to: data.date_to ? parseInt(data.date_to, 10) : undefined,
    };
    const project = await createProject.mutateAsync(payload);
    try {
      await projectsApi.search(project.id);
    } catch {
      // Project created; search can be started manually from the project page
    }
    reset();
    setOpen(false);
    router.push(`/projects/${project.id}`);
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button>
          <Plus className="h-4 w-4 mr-2" />
          New Project
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Create New Project</DialogTitle>
          <DialogDescription>Fill in the details to start a new systematic review project.</DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="title">Title</Label>
            <Input id="title" {...register('title')} placeholder="Transcriptomics in Heart Failure" />
            {errors.title && <p className="text-xs text-destructive">{errors.title.message}</p>}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="query_term">Query Term</Label>
            <Input id="query_term" {...register('query_term')} placeholder="heart failure transcriptomics" />
            {errors.query_term && <p className="text-xs text-destructive">{errors.query_term.message}</p>}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="description">Description (optional)</Label>
            <Textarea id="description" {...register('description')} rows={2} />
          </div>

          <div className="flex gap-4">
            <div className="flex-1 space-y-1.5">
              <Label htmlFor="date_from">From year</Label>
              <Input id="date_from" type="number" {...register('date_from')} placeholder="2010" />
            </div>
            <div className="flex-1 space-y-1.5">
              <Label htmlFor="date_to">To year</Label>
              <Input id="date_to" type="number" {...register('date_to')} placeholder="2024" />
            </div>
          </div>

          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={createProject.isPending}>
              {createProject.isPending ? 'Creating…' : 'Create Project'}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
