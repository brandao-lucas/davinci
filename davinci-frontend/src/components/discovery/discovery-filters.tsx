'use client';

import { Label } from '@/components/ui/label';
import { Input } from '@/components/ui/input';
import { Checkbox } from '@/components/ui/checkbox';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Button } from '@/components/ui/button';
import type { DiscoveryFilters } from '@/lib/types/discovery';

// Camadas ômicas aceitas pelo endpoint (containment, repetível)
const OMICS_LAYERS = [
  { value: 'genomic', label: 'Genômica' },
  { value: 'transcriptomic', label: 'Transcriptômica' },
  { value: 'proteomic', label: 'Proteômica' },
  { value: 'metabolomic', label: 'Metabolômica' },
  { value: 'epigenomic', label: 'Epigenômica' },
  { value: 'metagenomic', label: 'Metagenômica' },
  { value: 'microbiome', label: 'Microbioma' },
] as const;

interface DiscoveryFiltersProps {
  filters: DiscoveryFilters;
  onChange: (filters: DiscoveryFilters) => void;
  onClear: () => void;
}

export function DiscoveryFilters({ filters, onChange, onClear }: DiscoveryFiltersProps) {
  // omics_layer pode ser string (schema) ou string[] na prática — trabalhamos como array
  const selectedLayers: string[] = Array.isArray(filters.omics_layer)
    ? (filters.omics_layer as string[])
    : filters.omics_layer
      ? [filters.omics_layer as string]
      : [];

  function toggleLayer(layer: string) {
    const next = selectedLayers.includes(layer)
      ? selectedLayers.filter((l) => l !== layer)
      : [...selectedLayers, layer];
    // Quando vazio, remover o campo; caso contrário enviar como array
    onChange({
      ...filters,
      // O tipo do schema declara como string, mas o buildParams trata array
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      omics_layer: next.length > 0 ? (next as any) : undefined,
    });
  }

  function set<K extends keyof DiscoveryFilters>(key: K, value: DiscoveryFilters[K]) {
    onChange({ ...filters, [key]: value });
  }

  function clearSelect(key: keyof DiscoveryFilters) {
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    const { [key]: _, ...rest } = filters;
    onChange(rest as DiscoveryFilters);
  }

  function parseFloat01(val: string): number | undefined {
    const n = parseFloat(val);
    if (isNaN(n)) return undefined;
    return Math.min(1, Math.max(0, n));
  }

  return (
    <div className="space-y-4 text-sm">
      {/* Busca textual */}
      <div className="space-y-1.5">
        <Label>Busca</Label>
        <Input
          placeholder="Termo livre…"
          value={filters.search ?? ''}
          onChange={(e) => set('search', e.target.value || undefined)}
        />
      </div>

      {/* Camadas ômicas (multiselect via checkboxes) */}
      <div className="space-y-1.5">
        <Label>Camada ômica</Label>
        <div className="space-y-1 pt-0.5">
          {OMICS_LAYERS.map(({ value, label }) => (
            <label key={value} className="flex items-center gap-2 cursor-pointer">
              <Checkbox
                checked={selectedLayers.includes(value)}
                onCheckedChange={() => toggleLayer(value)}
              />
              <span>{label}</span>
            </label>
          ))}
        </div>
      </div>

      {/* Contagem de camadas */}
      <div className="space-y-1.5">
        <Label>N.o de camadas</Label>
        <div className="flex gap-1 items-center">
          <Input
            type="number"
            min={1}
            placeholder="Min"
            className="w-16 text-xs"
            value={filters.omics_count_min ?? ''}
            onChange={(e) =>
              set('omics_count_min', e.target.value ? parseInt(e.target.value) : undefined)
            }
          />
          <span className="text-muted-foreground">–</span>
          <Input
            type="number"
            min={1}
            placeholder="Max"
            className="w-16 text-xs"
            value={filters.omics_count_max ?? ''}
            onChange={(e) =>
              set('omics_count_max', e.target.value ? parseInt(e.target.value) : undefined)
            }
          />
        </div>
      </div>

      {/* Eixo de doença */}
      <div className="space-y-1.5">
        <Label>Eixo de doença</Label>
        <Select
          value={filters.disease_axis ?? 'all'}
          onValueChange={(v) =>
            v === 'all' ? clearSelect('disease_axis') : set('disease_axis', v)
          }
        >
          <SelectTrigger>
            <SelectValue placeholder="Todos" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Todos</SelectItem>
            <SelectItem value="monogenic">Monogênica</SelectItem>
            <SelectItem value="multifactorial">Multifatorial</SelectItem>
            <SelectItem value="mixed">Mista</SelectItem>
            <SelectItem value="indeterminate">Indeterminado</SelectItem>
          </SelectContent>
        </Select>
        <div className="space-y-1 pt-0.5">
          <Label className="text-xs text-muted-foreground">Confiança mín. (0–1)</Label>
          <Input
            type="number"
            min={0}
            max={1}
            step={0.01}
            placeholder="0.0"
            className="text-xs"
            value={filters.disease_axis_confidence_min ?? ''}
            onChange={(e) =>
              set('disease_axis_confidence_min', parseFloat01(e.target.value))
            }
          />
        </div>
      </div>

      {/* Grupo controle */}
      <div className="space-y-1.5">
        <Label>Grupo controle</Label>
        <Select
          value={filters.has_control_group ?? 'all'}
          onValueChange={(v) =>
            v === 'all' ? clearSelect('has_control_group') : set('has_control_group', v)
          }
        >
          <SelectTrigger>
            <SelectValue placeholder="Todos" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Todos</SelectItem>
            <SelectItem value="yes">Sim</SelectItem>
            <SelectItem value="no">Não</SelectItem>
            <SelectItem value="unknown">Desconhecido</SelectItem>
          </SelectContent>
        </Select>
        <div className="space-y-1 pt-0.5">
          <Label className="text-xs text-muted-foreground">Confiança mín. (0–1)</Label>
          <Input
            type="number"
            min={0}
            max={1}
            step={0.01}
            placeholder="0.0"
            className="text-xs"
            value={filters.has_control_group_confidence_min ?? ''}
            onChange={(e) =>
              set('has_control_group_confidence_min', parseFloat01(e.target.value))
            }
          />
        </div>
      </div>

      {/* Resolução celular — IsSingleCellEnum: single_cell | bulk | unknown */}
      <div className="space-y-1.5">
        <Label>Resolução celular</Label>
        <Select
          value={filters.is_single_cell ?? 'all'}
          onValueChange={(v) =>
            v === 'all' ? clearSelect('is_single_cell') : set('is_single_cell', v)
          }
        >
          <SelectTrigger>
            <SelectValue placeholder="Todos" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Todos</SelectItem>
            <SelectItem value="single_cell">Célula única</SelectItem>
            <SelectItem value="bulk">Bulk</SelectItem>
            <SelectItem value="unknown">Desconhecido</SelectItem>
          </SelectContent>
        </Select>
        <div className="space-y-1 pt-0.5">
          <Label className="text-xs text-muted-foreground">Confiança mín. (0–1)</Label>
          <Input
            type="number"
            min={0}
            max={1}
            step={0.01}
            placeholder="0.0"
            className="text-xs"
            value={filters.is_single_cell_confidence_min ?? ''}
            onChange={(e) =>
              set('is_single_cell_confidence_min', parseFloat01(e.target.value))
            }
          />
        </div>
      </div>

      {/* Formato de dado — DataFormatEnum: raw | processed | unknown */}
      <div className="space-y-1.5">
        <Label>Formato de dado</Label>
        <Select
          value={filters.data_format ?? 'all'}
          onValueChange={(v) =>
            v === 'all' ? clearSelect('data_format') : set('data_format', v)
          }
        >
          <SelectTrigger>
            <SelectValue placeholder="Todos" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Todos</SelectItem>
            <SelectItem value="raw">Raw</SelectItem>
            <SelectItem value="processed">Processado</SelectItem>
            <SelectItem value="unknown">Desconhecido</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {/* Tipo de acesso */}
      <div className="space-y-1.5">
        <Label>Tipo de acesso</Label>
        <Select
          value={filters.access_type ?? 'all'}
          onValueChange={(v) =>
            v === 'all' ? clearSelect('access_type') : set('access_type', v)
          }
        >
          <SelectTrigger>
            <SelectValue placeholder="Todos" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Todos</SelectItem>
            <SelectItem value="public">Público</SelectItem>
            <SelectItem value="controlled">Controlado</SelectItem>
            <SelectItem value="unknown">Desconhecido</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {/* Gene */}
      <div className="space-y-1.5">
        <Label>Gene</Label>
        <Input
          placeholder="Ex: BRCA1"
          value={filters.gene ?? ''}
          onChange={(e) => set('gene', e.target.value || undefined)}
        />
      </div>

      {/* Toggles booleanos */}
      <div className="space-y-2 pt-1">
        <Label className="text-xs uppercase tracking-wide text-muted-foreground">
          Flags
        </Label>

        <label className="flex items-center gap-2 cursor-pointer">
          <Checkbox
            checked={!!filters.has_sample_join_key}
            onCheckedChange={(v) =>
              set('has_sample_join_key', v ? true : undefined)
            }
          />
          <span>Com chave de pareamento</span>
        </label>

        {filters.has_sample_join_key && (
          <div className="pl-6 space-y-1">
            <Label className="text-xs text-muted-foreground">Confiança mín. (0–1)</Label>
            <Input
              type="number"
              min={0}
              max={1}
              step={0.01}
              placeholder="0.0"
              className="text-xs"
              value={filters.sample_join_key_confidence_min ?? ''}
              onChange={(e) =>
                set('sample_join_key_confidence_min', parseFloat01(e.target.value))
              }
            />
          </div>
        )}

        <label className="flex items-center gap-2 cursor-pointer">
          <Checkbox
            checked={!!filters.monogenic_gene_hit}
            onCheckedChange={(v) =>
              set('monogenic_gene_hit', v ? true : undefined)
            }
          />
          <span>Com gene monogênico</span>
        </label>
      </div>

      {/* Limpar filtros */}
      <Button variant="outline" size="sm" className="w-full" onClick={onClear}>
        Limpar filtros
      </Button>
    </div>
  );
}
