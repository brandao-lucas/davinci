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
import { Plus, Search, Sparkles } from 'lucide-react';

const schema = z.object({
  title: z.string().min(1, 'Título obrigatório'),
  description: z.string().optional(),
  query_term: z.string().min(1, 'Termo de busca obrigatório'),
  date_from: z.string().optional(),
  date_to: z.string().optional(),
});

type FormData = z.infer<typeof schema>;

// Ação escolhida pelo usuário ao submeter o formulário
type SubmitAction = 'skip' | 'refine';

export function CreateProjectDialog() {
  const [open, setOpen] = useState(false);
  const [submitAction, setSubmitAction] = useState<SubmitAction | null>(null);
  const router = useRouter();
  const createProject = useCreateProject();

  const { register, handleSubmit, formState: { errors }, reset } = useForm<FormData>({
    resolver: zodResolver(schema),
  });

  const isPending = createProject.isPending || submitAction !== null;

  const onSubmit = async (data: FormData, action: SubmitAction) => {
    setSubmitAction(action);
    try {
      const payload = {
        title: data.title,
        description: data.description,
        query_term: data.query_term,
        date_from: data.date_from ? parseInt(data.date_from, 10) : undefined,
        date_to: data.date_to ? parseInt(data.date_to, 10) : undefined,
      };

      const project = await createProject.mutateAsync(payload);

      if (action === 'skip') {
        // Pular MeSH: desabilita explicitamente a busca avançada e dispara
        // a busca simples por termo — equivalente ao comportamento anterior
        // de auto-busca. A ordem de await garante que o PATCH persista antes
        // de o job de busca ser iniciado.
        await projectsApi.update(project.id, { advanced_search_enabled: false });
        try {
          await projectsApi.search(project.id);
        } catch {
          // Busca pode ser iniciada manualmente na página do projeto
        }
      }
      // action === 'refine': não dispara busca — o usuário refinará via
      // AdvancedSearchBlock na página do draft e clicará em "Iniciar pesquisa".

      reset();
      setOpen(false);
      router.push(`/projects/${project.id}`);
    } finally {
      setSubmitAction(null);
    }
  };

  const handleSkip = handleSubmit((data) => onSubmit(data, 'skip'));
  const handleRefine = handleSubmit((data) => onSubmit(data, 'refine'));

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button>
          <Plus className="h-4 w-4 mr-2" />
          Novo Projeto
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Criar novo projeto</DialogTitle>
          <DialogDescription>
            Preencha os dados básicos. Você poderá refinar os descritores MeSH antes de iniciar a busca ou ir direto à busca simples.
          </DialogDescription>
        </DialogHeader>

        <form className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="title">Título</Label>
            <Input id="title" {...register('title')} placeholder="Transcriptômica em Insuficiência Cardíaca" />
            {errors.title && <p className="text-xs text-destructive">{errors.title.message}</p>}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="query_term">Termo de busca</Label>
            <Input id="query_term" {...register('query_term')} placeholder="heart failure transcriptomics" />
            {errors.query_term && <p className="text-xs text-destructive">{errors.query_term.message}</p>}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="description">Descrição (opcional)</Label>
            <Textarea id="description" {...register('description')} rows={2} />
          </div>

          <div className="flex gap-4">
            <div className="flex-1 space-y-1.5">
              <Label htmlFor="date_from">Ano inicial</Label>
              <Input id="date_from" type="number" {...register('date_from')} placeholder="2010" />
            </div>
            <div className="flex-1 space-y-1.5">
              <Label htmlFor="date_to">Ano final</Label>
              <Input id="date_to" type="number" {...register('date_to')} placeholder="2024" />
            </div>
          </div>

          {createProject.isError && (
            <p className="text-xs text-destructive">
              Erro ao criar projeto. Tente novamente.
            </p>
          )}

          <div className="border-t pt-4 space-y-2">
            <p className="text-xs text-muted-foreground">
              Como deseja prosseguir?
            </p>
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
              <Button
                type="button"
                variant="outline"
                onClick={() => setOpen(false)}
                disabled={isPending}
              >
                Cancelar
              </Button>
              <div className="flex flex-col gap-2 sm:flex-row">
                <Button
                  type="button"
                  variant="secondary"
                  onClick={handleSkip}
                  disabled={isPending}
                  title="Cria o projeto e inicia a busca simples por termo agora"
                >
                  <Search className="h-4 w-4 mr-2" />
                  {submitAction === 'skip' ? 'Iniciando busca…' : 'Pular — busca simples'}
                </Button>
                <Button
                  type="button"
                  onClick={handleRefine}
                  disabled={isPending}
                  title="Cria o projeto e abre o refinamento com descritores MeSH antes de buscar (recurso avançado)"
                >
                  <Sparkles className="h-4 w-4 mr-2" />
                  {submitAction === 'refine' ? 'Criando…' : 'Refinar com MeSH'}
                </Button>
              </div>
            </div>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
