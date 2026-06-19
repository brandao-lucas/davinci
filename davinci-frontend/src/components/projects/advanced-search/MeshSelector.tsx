'use client';

import { useState } from 'react';
import { Checkbox } from '@/components/ui/checkbox';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Separator } from '@/components/ui/separator';
import { ChevronDown, ChevronRight, Loader2, Search } from 'lucide-react';
import { formatNumber } from '@/lib/utils/format';
import { useMeshSuggest } from '@/lib/hooks/use-advanced-search';
import type { MeshSuggestion, SelectedMeshItem, MeshDefaultMode } from '@/lib/types/advanced-search';

interface MeshSelectorProps {
  projectId: string;
  selectedMesh: SelectedMeshItem[];
  meshDefaultMode: MeshDefaultMode;
  onSelectionChange: (items: SelectedMeshItem[]) => void;
  onDefaultModeChange: (mode: MeshDefaultMode) => void;
}

/** Item expansível de descritor MeSH com qualifiers e controles de precisão */
function MeshDescriptorRow({
  suggestion,
  selectedItem,
  onToggle,
  onQualifierToggle,
  onModeToggle,
  onMajorOnlyToggle,
}: {
  suggestion: MeshSuggestion;
  selectedItem: SelectedMeshItem | undefined;
  onToggle: () => void;
  onQualifierToggle: (q: string) => void;
  onModeToggle: () => void;
  onMajorOnlyToggle: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const isSelected = !!selectedItem;

  return (
    <div className="border rounded-lg overflow-hidden">
      <div className="flex items-center gap-3 p-3 bg-card hover:bg-accent/30 transition-colors">
        <Checkbox
          checked={isSelected}
          onCheckedChange={onToggle}
          id={`mesh-${suggestion.ui}`}
        />

        <button
          type="button"
          className="flex-1 text-left"
          onClick={() => setExpanded(v => !v)}
        >
          <div className="flex items-center gap-2">
            {expanded
              ? <ChevronDown className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
              : <ChevronRight className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
            }
            <span className="font-medium text-sm">{suggestion.descriptor}</span>
            <span className="text-xs text-muted-foreground font-mono">{suggestion.ui}</span>
          </div>
          <div className="flex items-center gap-2 mt-0.5 ml-5">
            <Badge variant="outline" className="text-xs h-4 px-1">
              {formatNumber(suggestion.pubmed_count)} artigos
            </Badge>
            {suggestion.tree_numbers.slice(0, 2).map(t => (
              <span key={t} className="text-xs text-muted-foreground font-mono">{t}</span>
            ))}
          </div>
        </button>

        {isSelected && (
          <div className="flex items-center gap-2 shrink-0">
            <button
              type="button"
              onClick={onModeToggle}
              className={`text-xs px-2 py-0.5 rounded border font-medium transition-colors ${
                selectedItem.mode === 'and'
                  ? 'bg-blue-50 text-blue-700 border-blue-200 dark:bg-blue-950 dark:text-blue-300 dark:border-blue-800'
                  : 'bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950 dark:text-amber-300 dark:border-amber-800'
              }`}
              title={selectedItem.mode === 'and' ? 'AND — precisão (clique para OR)' : 'OR — recall (clique para AND)'}
            >
              {selectedItem.mode === 'and' ? 'AND' : 'OR'}
            </button>
            <button
              type="button"
              onClick={onMajorOnlyToggle}
              className={`text-xs px-2 py-0.5 rounded border font-medium transition-colors ${
                selectedItem.major_only
                  ? 'bg-violet-50 text-violet-700 border-violet-200 dark:bg-violet-950 dark:text-violet-300 dark:border-violet-800'
                  : 'text-muted-foreground border-border'
              }`}
              title="Tópico principal [majr] — restringe a papers que têm este MeSH como assunto principal"
            >
              major
            </button>
          </div>
        )}
      </div>

      {expanded && (
        <div className="border-t bg-muted/30 p-3 space-y-2">
          {suggestion.scope_note && (
            <p className="text-xs text-muted-foreground italic leading-relaxed">
              {suggestion.scope_note}
            </p>
          )}

          {suggestion.allowable_qualifiers.length > 0 && (
            <div>
              <p className="text-xs font-medium mb-1.5">Subheadings (qualifiers)</p>
              <div className="flex flex-wrap gap-1.5">
                {suggestion.allowable_qualifiers.map(q => {
                  const active = selectedItem?.qualifiers.includes(q);
                  return (
                    <button
                      key={q}
                      type="button"
                      onClick={() => isSelected && onQualifierToggle(q)}
                      disabled={!isSelected}
                      className={`text-xs px-2 py-0.5 rounded-full border transition-colors ${
                        active
                          ? 'bg-primary text-primary-foreground border-primary'
                          : isSelected
                            ? 'border-border hover:bg-accent'
                            : 'border-border text-muted-foreground cursor-not-allowed opacity-50'
                      }`}
                    >
                      {q}
                    </button>
                  );
                })}
              </div>
              {!isSelected && (
                <p className="text-xs text-muted-foreground mt-1">Selecione o descritor para ativar qualifiers.</p>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function MeshSelector({
  projectId,
  selectedMesh,
  meshDefaultMode,
  onSelectionChange,
  onDefaultModeChange,
}: MeshSelectorProps) {
  const [searchTerm, setSearchTerm] = useState('');
  const { data: suggestions, isLoading, isError } = useMeshSuggest(
    projectId,
    searchTerm || undefined,
  );

  function isItemSelected(ui: string) {
    return selectedMesh.find(s => s.ui === ui);
  }

  function handleToggle(suggestion: MeshSuggestion) {
    const existing = isItemSelected(suggestion.ui);
    if (existing) {
      onSelectionChange(selectedMesh.filter(s => s.ui !== suggestion.ui));
    } else {
      const newItem: SelectedMeshItem = {
        descriptor: suggestion.descriptor,
        ui: suggestion.ui,
        qualifiers: [],
        mode: meshDefaultMode,
        major_only: false,
      };
      onSelectionChange([...selectedMesh, newItem]);
    }
  }

  function handleQualifierToggle(ui: string, qualifier: string) {
    onSelectionChange(
      selectedMesh.map(s => {
        if (s.ui !== ui) return s;
        const has = s.qualifiers.includes(qualifier);
        return {
          ...s,
          qualifiers: has
            ? s.qualifiers.filter(q => q !== qualifier)
            : [...s.qualifiers, qualifier],
        };
      }),
    );
  }

  function handleModeToggle(ui: string) {
    onSelectionChange(
      selectedMesh.map(s =>
        s.ui !== ui ? s : { ...s, mode: s.mode === 'and' ? 'or' : 'and' },
      ),
    );
  }

  function handleMajorOnlyToggle(ui: string) {
    onSelectionChange(
      selectedMesh.map(s =>
        s.ui !== ui ? s : { ...s, major_only: !s.major_only },
      ),
    );
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="text-sm font-semibold">Descritores MeSH</CardTitle>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <span>Modo padrão:</span>
            <button
              type="button"
              onClick={() => onDefaultModeChange(meshDefaultMode === 'and' ? 'or' : 'and')}
              className={`px-2 py-0.5 rounded border font-semibold transition-colors ${
                meshDefaultMode === 'and'
                  ? 'bg-blue-50 text-blue-700 border-blue-200 dark:bg-blue-950 dark:text-blue-300 dark:border-blue-800'
                  : 'bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950 dark:text-amber-300 dark:border-amber-800'
              }`}
            >
              {meshDefaultMode === 'and' ? 'AND (precisão)' : 'OR (recall)'}
            </button>
          </div>
        </div>

        <div className="relative mt-2">
          <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-muted-foreground" />
          <Input
            className="pl-8 h-8 text-sm"
            placeholder="Buscar termo MeSH…"
            value={searchTerm}
            onChange={e => setSearchTerm(e.target.value)}
          />
        </div>
      </CardHeader>

      <CardContent className="space-y-2">
        {selectedMesh.length > 0 && (
          <div className="mb-3">
            <p className="text-xs font-medium text-muted-foreground mb-1.5">
              {selectedMesh.length} descritor{selectedMesh.length !== 1 ? 'es' : ''} selecionado{selectedMesh.length !== 1 ? 's' : ''}
            </p>
            <div className="flex flex-wrap gap-1.5">
              {selectedMesh.map(item => (
                <Badge
                  key={item.ui}
                  variant="secondary"
                  className="gap-1 cursor-pointer text-xs"
                  onClick={() => onSelectionChange(selectedMesh.filter(s => s.ui !== item.ui))}
                >
                  {item.descriptor}
                  {item.qualifiers.length > 0 && (
                    <span className="text-muted-foreground">/{item.qualifiers.join('/')}</span>
                  )}
                  <span className={`font-mono ${item.mode === 'and' ? 'text-blue-600' : 'text-amber-600'}`}>
                    [{item.mode}]
                  </span>
                  {item.major_only && <span className="text-violet-600">[majr]</span>}
                  <span className="ml-0.5 opacity-60">×</span>
                </Badge>
              ))}
            </div>
            <Separator className="mt-3" />
          </div>
        )}

        {isLoading && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground py-4 justify-center">
            <Loader2 className="h-4 w-4 animate-spin" />
            Buscando sugestões MeSH…
          </div>
        )}

        {isError && (
          <p className="text-sm text-destructive py-2">
            Erro ao buscar sugestões. Verifique se o serviço Rust está ativo.
          </p>
        )}

        {!isLoading && !isError && suggestions && suggestions.length === 0 && (
          <p className="text-sm text-muted-foreground py-2 text-center">
            Nenhum descritor MeSH encontrado.
          </p>
        )}

        {!isLoading && suggestions && suggestions.map(s => (
          <MeshDescriptorRow
            key={s.ui}
            suggestion={s}
            selectedItem={isItemSelected(s.ui)}
            onToggle={() => handleToggle(s)}
            onQualifierToggle={q => handleQualifierToggle(s.ui, q)}
            onModeToggle={() => handleModeToggle(s.ui)}
            onMajorOnlyToggle={() => handleMajorOnlyToggle(s.ui)}
          />
        ))}
      </CardContent>
    </Card>
  );
}
