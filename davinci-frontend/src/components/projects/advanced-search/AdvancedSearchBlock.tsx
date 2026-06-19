'use client';

import { useState, useCallback } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { Loader2, Sparkles, BarChart2, Save, AlertCircle } from 'lucide-react';
import { MeshSelector } from './MeshSelector';
import { MagnitudePanel } from './MagnitudePanel';
import { useSearchPreview, useSearchPreviewMutation } from '@/lib/hooks/use-advanced-search';
import { useUpdateProject } from '@/lib/hooks/use-projects';
import type { DaVinciProject } from '@/lib/types/project';
import type {
  SelectedMeshItem,
  MeshDefaultMode,
  PanelFlags,
  MagnitudePreview,
} from '@/lib/types/advanced-search';

interface AdvancedSearchBlockProps {
  project: DaVinciProject;
}

const EMPTY_PANEL_FLAGS: PanelFlags = {
  by_year: false,
  by_pub_type: false,
  open_access: false,
  year_buckets: null,
};

const FULL_PANEL_FLAGS: PanelFlags = {
  by_year: true,
  by_pub_type: true,
  open_access: true,
  year_buckets: null,
};

export function AdvancedSearchBlock({ project }: AdvancedSearchBlockProps) {
  // Estado local inicializado do snapshot salvo no projeto
  const initialMesh: SelectedMeshItem[] = Array.isArray(project.selected_mesh)
    ? (project.selected_mesh as SelectedMeshItem[])
    : [];
  const initialMode: MeshDefaultMode =
    (project.mesh_default_mode as MeshDefaultMode) ?? 'and';

  const [selectedMesh, setSelectedMesh] = useState<SelectedMeshItem[]>(initialMesh);
  const [meshDefaultMode, setMeshDefaultMode] = useState<MeshDefaultMode>(initialMode);
  const [fullPanelLoaded, setFullPanelLoaded] = useState(false);

  // Snapshot do magnitude salvo no projeto (usado antes do primeiro recálculo)
  const savedSnapshot: MagnitudePreview | null =
    project.magnitude_snapshot &&
    typeof project.magnitude_snapshot === 'object' &&
    'combined_count' in (project.magnitude_snapshot as object)
      ? (project.magnitude_snapshot as MagnitudePreview)
      : null;

  const updateProject = useUpdateProject(project.id);
  const fullPreviewMutation = useSearchPreviewMutation(project.id);

  // Core counts — recalculam ao vivo (debounce 400ms interno ao hook)
  const {
    data: livePreview,
    isLoading: liveLoading,
    isError: liveError,
  } = useSearchPreview(
    project.id,
    selectedMesh,
    meshDefaultMode,
    EMPTY_PANEL_FLAGS,
    selectedMesh.length > 0,
  );

  // Preview ativo — prefere live, cai no snapshot salvo
  const activePreview = livePreview ?? savedSnapshot;

  const handleSelectionChange = useCallback((items: SelectedMeshItem[]) => {
    setSelectedMesh(items);
    // Reset painel pesado ao mudar seleção
    setFullPanelLoaded(false);
  }, []);

  const handleDefaultModeChange = useCallback((mode: MeshDefaultMode) => {
    setMeshDefaultMode(mode);
    setFullPanelLoaded(false);
  }, []);

  async function handleSave() {
    await updateProject.mutateAsync({
      selected_mesh: selectedMesh,
      mesh_default_mode: meshDefaultMode,
      ...(livePreview ? { magnitude_snapshot: livePreview } : {}),
      advanced_search_enabled: selectedMesh.length > 0,
    });
  }

  async function handleAnalyzeFull() {
    const result = await fullPreviewMutation.mutateAsync({
      selected_mesh: selectedMesh,
      mesh_default_mode: meshDefaultMode,
      panel_flags: FULL_PANEL_FLAGS,
    });
    setFullPanelLoaded(true);
    // Auto-salva o snapshot completo
    await updateProject.mutateAsync({ magnitude_snapshot: result });
  }

  const isSaving = updateProject.isPending;
  const isAnalyzing = fullPreviewMutation.isPending;

  // Exibe o painel pesado se vieram do fullPreviewMutation OU se o livePreview já tem esses dados
  const previewForPanel: MagnitudePreview | null =
    fullPanelLoaded && fullPreviewMutation.data
      ? fullPreviewMutation.data
      : activePreview;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-primary" />
          <h2 className="text-sm font-semibold">Pesquisa Avançada</h2>
          <Badge variant="secondary" className="text-xs">Premium</Badge>
        </div>

        <div className="flex items-center gap-2">
          {selectedMesh.length > 0 && !fullPanelLoaded && (
            <Button
              variant="outline"
              size="sm"
              onClick={handleAnalyzeFull}
              disabled={isAnalyzing || selectedMesh.length === 0}
            >
              {isAnalyzing
                ? <><Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />Analisando…</>
                : <><BarChart2 className="h-3.5 w-3.5 mr-1.5" />Analisar magnitude completa</>
              }
            </Button>
          )}

          <Button
            variant="outline"
            size="sm"
            onClick={handleSave}
            disabled={isSaving}
          >
            {isSaving
              ? <><Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />Salvando…</>
              : <><Save className="h-3.5 w-3.5 mr-1.5" />Salvar configuração</>
            }
          </Button>
        </div>
      </div>

      <p className="text-xs text-muted-foreground leading-relaxed">
        Refine sua busca com vocabulário controlado MeSH antes de iniciar a ingestão.
        Os contadores atualizam ao vivo conforme você adiciona ou remove descritores.
        Use <strong>AND</strong> para maior precisão ou <strong>OR</strong> para maior recall em cada bloco.
      </p>

      <Separator />

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Seletor MeSH */}
        <MeshSelector
          projectId={project.id}
          selectedMesh={selectedMesh}
          meshDefaultMode={meshDefaultMode}
          onSelectionChange={handleSelectionChange}
          onDefaultModeChange={handleDefaultModeChange}
        />

        {/* Painel de magnitude */}
        <div className="space-y-4">
          {selectedMesh.length === 0 && !savedSnapshot && (
            <Card>
              <CardContent className="pt-6 text-center text-sm text-muted-foreground">
                <BarChart2 className="h-8 w-8 mx-auto mb-2 opacity-30" />
                <p>Selecione ao menos um descritor MeSH para ver a magnitude da busca.</p>
              </CardContent>
            </Card>
          )}

          {liveLoading && selectedMesh.length > 0 && (
            <Card>
              <CardContent className="pt-6 flex items-center justify-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                Calculando contagens…
              </CardContent>
            </Card>
          )}

          {liveError && (
            <Card className="border-destructive/50">
              <CardContent className="pt-4 flex items-center gap-2 text-sm text-destructive">
                <AlertCircle className="h-4 w-4 shrink-0" />
                Serviço indisponível (503). Verifique se o engine Rust está ativo.
              </CardContent>
            </Card>
          )}

          {previewForPanel && !liveLoading && (
            <>
              <MagnitudePanel preview={previewForPanel} />

              {!fullPanelLoaded && (
                <Card className="border-dashed">
                  <CardHeader className="pb-2">
                    <CardTitle className="text-xs text-muted-foreground">
                      Análise temporal, tipos de publicação e acesso aberto
                    </CardTitle>
                  </CardHeader>
                  <CardContent>
                    <Button
                      variant="outline"
                      size="sm"
                      className="w-full"
                      onClick={handleAnalyzeFull}
                      disabled={isAnalyzing}
                    >
                      {isAnalyzing
                        ? <><Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />Analisando…</>
                        : <><BarChart2 className="h-3.5 w-3.5 mr-1.5" />Analisar magnitude completa</>
                      }
                    </Button>
                    <p className="text-xs text-muted-foreground mt-2 text-center">
                      Executa até ~15 chamadas NCBI adicionais
                    </p>
                  </CardContent>
                </Card>
              )}
            </>
          )}

          {updateProject.isSuccess && (
            <p className="text-xs text-green-600 dark:text-green-400 text-center">
              Configuração salva com sucesso.
            </p>
          )}

          {updateProject.isError && (
            <p className="text-xs text-destructive text-center">
              Erro ao salvar. Tente novamente.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
