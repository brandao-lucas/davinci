import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { projectsApi } from '@/lib/api/projects';
import type { CreateProjectInput, UpdateProjectInput } from '@/lib/types/project';

export function useProjects() {
  return useQuery({
    queryKey: ['projects'],
    queryFn: () => projectsApi.list().then(r => r.data),
  });
}

export function useProject(id: string) {
  return useQuery({
    queryKey: ['projects', id],
    queryFn: () => projectsApi.get(id).then(r => r.data),
    enabled: !!id,
  });
}

export function useProjectStats(id: string) {
  return useQuery({
    queryKey: ['projects', id, 'stats'],
    queryFn: () => projectsApi.getStats(id).then(r => r.data),
    enabled: !!id,
  });
}

export function useCreateProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: CreateProjectInput) => projectsApi.create(data).then(r => r.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
    },
  });
}

export function useUpdateProject(id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: UpdateProjectInput) => projectsApi.update(id, data).then(r => r.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      // Quando um campo de busca muda em status=searching, o backend reverte para draft
      // e aborta o job ativo. Invalida jobs para a UI refletir o job cancelado.
      queryClient.invalidateQueries({ queryKey: ['jobs', id] });
    },
  });
}

export function useDeleteProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => projectsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
    },
  });
}

export function useDispatchSearch(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => projectsApi.search(projectId).then(r => r.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['jobs', projectId] });
    },
  });
}

export function useDispatchOmicsSearch(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ sources, maxPerSource }: { sources?: string[]; maxPerSource?: number } = {}) =>
      projectsApi.omicsSearch(projectId, sources, maxPerSource).then(r => r.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['jobs', projectId] });
    },
  });
}
