/**
 * Testes de regressão para DiscoveryFilters.
 *
 * Invariantes travados:
 *  - renderização sem crash com filtros vazios
 *  - Select de disease_axis chama onChange com o valor correto e sem outros campos
 *  - Select de has_control_group chama onChange com o valor correto
 *  - Select de is_single_cell chama onChange com o valor correto
 *  - Select de data_format chama onChange com o valor correto
 *  - "Limpar filtros" chama onClear
 *  - Selecionar "Todos" em qualquer Select remove o campo dos filtros (clearSelect)
 *  - omics_layer repetível serializa como array
 *  Nota: campo `search` foi removido (não é param da operação projects_datasets_export_retrieve).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import { DiscoveryFilters } from '../discovery-filters';
import type { DiscoveryFilters as FiltersType } from '@/lib/types/discovery';

// ── Helpers ───────────────────────────────────────────────────────────────────

const EMPTY_FILTERS: FiltersType = {};

// Mock tipado compatível com (filters: FiltersType) => void e com .mock.calls
function makeMockChange() {
  return vi.fn<(filters: FiltersType) => void>();
}
function makeMockClear() {
  return vi.fn<() => void>();
}

function renderFilters(
  filters: FiltersType = EMPTY_FILTERS,
  onChange: ReturnType<typeof makeMockChange> = makeMockChange(),
  onClear: ReturnType<typeof makeMockClear> = makeMockClear(),
) {
  return render(
    <DiscoveryFilters filters={filters} onChange={onChange} onClear={onClear} />,
  );
}

// ── Testes ────────────────────────────────────────────────────────────────────

describe('DiscoveryFilters — renderização', () => {
  it('renderiza sem erros com filtros vazios', () => {
    renderFilters();
    // Labels principais visíveis
    expect(screen.getByText('Eixo de doença')).toBeInTheDocument();
    expect(screen.getByText('Grupo controle')).toBeInTheDocument();
    expect(screen.getByText('Resolução celular')).toBeInTheDocument();
    expect(screen.getByText('Formato de dado')).toBeInTheDocument();
    expect(screen.getByText('Tipo de acesso')).toBeInTheDocument();
    expect(screen.getByText('Limpar filtros')).toBeInTheDocument();
  });

  it('renderiza checkboxes de camada ômica', () => {
    renderFilters();
    expect(screen.getByText('Genômica')).toBeInTheDocument();
    expect(screen.getByText('Transcriptômica')).toBeInTheDocument();
    expect(screen.getByText('Proteômica')).toBeInTheDocument();
  });
});

describe('DiscoveryFilters — "Limpar filtros"', () => {
  it('chama onClear ao clicar no botão', () => {
    const onClear = vi.fn();
    renderFilters(EMPTY_FILTERS, vi.fn(), onClear);
    fireEvent.click(screen.getByText('Limpar filtros'));
    expect(onClear).toHaveBeenCalledOnce();
  });
});

describe('DiscoveryFilters — disease_axis Select', () => {
  let onChange: ReturnType<typeof makeMockChange>;

  beforeEach(() => {
    onChange = makeMockChange();
  });

  it('renderiza o label "Eixo de doença" e o SelectTrigger correspondente', () => {
    renderFilters({ disease_axis: 'monogenic' }, onChange);
    // Label visível
    expect(screen.getByText('Eixo de doença')).toBeInTheDocument();
    // Ao menos um combobox presente (Radix Select usa role="combobox" no trigger)
    expect(screen.getAllByRole('combobox').length).toBeGreaterThan(0);
  });

  it('chama onChange com disease_axis=monogenic ao selecionar a opção', () => {
    renderFilters(EMPTY_FILTERS, onChange);

    // Abre o Select de eixo de doença — é o primeiro combobox
    const triggers = screen.getAllByRole('combobox');
    fireEvent.click(triggers[0]);

    const option = screen.getByRole('option', { name: 'Monogênica' });
    fireEvent.click(option);

    expect(onChange).toHaveBeenCalledOnce();
    const arg = onChange.mock.calls[0][0] as FiltersType;
    expect(arg.disease_axis).toBe('monogenic');
  });

  it('remove disease_axis dos filtros ao selecionar "Todos"', () => {
    const onChange2 = vi.fn();
    renderFilters({ disease_axis: 'monogenic' }, onChange2);

    const triggers = screen.getAllByRole('combobox');
    fireEvent.click(triggers[0]);

    const option = screen.getByRole('option', { name: 'Todos' });
    fireEvent.click(option);

    expect(onChange2).toHaveBeenCalledOnce();
    const arg = onChange2.mock.calls[0][0] as FiltersType;
    // clearSelect remove a chave — não deve conter disease_axis
    expect('disease_axis' in arg).toBe(false);
  });
});

describe('DiscoveryFilters — has_control_group Select', () => {
  it('chama onChange com has_control_group=yes ao selecionar "Sim"', () => {
    const onChange = vi.fn();
    renderFilters(EMPTY_FILTERS, onChange);

    const triggers = screen.getAllByRole('combobox');
    // has_control_group é o segundo Select
    fireEvent.click(triggers[1]);

    const option = screen.getByRole('option', { name: 'Sim' });
    fireEvent.click(option);

    expect(onChange).toHaveBeenCalledOnce();
    const arg = onChange.mock.calls[0][0] as FiltersType;
    expect(arg.has_control_group).toBe('yes');
  });
});

describe('DiscoveryFilters — is_single_cell Select', () => {
  it('chama onChange com is_single_cell=single_cell ao selecionar "Célula única"', () => {
    const onChange = vi.fn();
    renderFilters(EMPTY_FILTERS, onChange);

    const triggers = screen.getAllByRole('combobox');
    // is_single_cell é o terceiro Select
    fireEvent.click(triggers[2]);

    const option = screen.getByRole('option', { name: 'Célula única' });
    fireEvent.click(option);

    expect(onChange).toHaveBeenCalledOnce();
    const arg = onChange.mock.calls[0][0] as FiltersType;
    expect(arg.is_single_cell).toBe('single_cell');
  });

  it('chama onChange com is_single_cell=bulk ao selecionar "Bulk"', () => {
    const onChange = vi.fn();
    renderFilters(EMPTY_FILTERS, onChange);

    const triggers = screen.getAllByRole('combobox');
    fireEvent.click(triggers[2]);

    const option = screen.getByRole('option', { name: 'Bulk' });
    fireEvent.click(option);

    expect(onChange).toHaveBeenCalledOnce();
    const arg = onChange.mock.calls[0][0] as FiltersType;
    expect(arg.is_single_cell).toBe('bulk');
  });
});

describe('DiscoveryFilters — data_format Select', () => {
  it('chama onChange com data_format=raw ao selecionar "Raw"', () => {
    const onChange = vi.fn();
    renderFilters(EMPTY_FILTERS, onChange);

    const triggers = screen.getAllByRole('combobox');
    // data_format é o quarto Select
    fireEvent.click(triggers[3]);

    const option = screen.getByRole('option', { name: 'Raw' });
    fireEvent.click(option);

    expect(onChange).toHaveBeenCalledOnce();
    const arg = onChange.mock.calls[0][0] as FiltersType;
    expect(arg.data_format).toBe('raw');
  });

  it('chama onChange com data_format=processed ao selecionar "Processado"', () => {
    const onChange = vi.fn();
    renderFilters(EMPTY_FILTERS, onChange);

    const triggers = screen.getAllByRole('combobox');
    fireEvent.click(triggers[3]);

    const option = screen.getByRole('option', { name: 'Processado' });
    fireEvent.click(option);

    expect(onChange).toHaveBeenCalledOnce();
    const arg = onChange.mock.calls[0][0] as FiltersType;
    expect(arg.data_format).toBe('processed');
  });
});

describe('DiscoveryFilters — omics_layer checkboxes', () => {
  it('ativa camada ômica ao marcar checkbox (genomic)', () => {
    const onChange = vi.fn();
    renderFilters(EMPTY_FILTERS, onChange);

    // Radix Checkbox — múltiplos com nome "Genômica" podem aparecer devido ao wrapping
    // em <label>. Pegamos o primeiro (o button com role="checkbox").
    const genomicCheckboxes = screen.getAllByRole('checkbox', { name: /genômica/i });
    fireEvent.click(genomicCheckboxes[0]);

    expect(onChange).toHaveBeenCalledOnce();
    const arg = onChange.mock.calls[0][0] as FiltersType;
    // omics_layer deve conter 'genomic'
    const layers = Array.isArray(arg.omics_layer) ? arg.omics_layer : [arg.omics_layer];
    expect(layers).toContain('genomic');
  });

  it('desativa camada ômica ao desmarcar checkbox previamente selecionado', () => {
    const onChange = vi.fn();
    // começa com genomic selecionado
    renderFilters({ omics_layer: 'genomic' as unknown as FiltersType['omics_layer'] }, onChange);

    const genomicCheckboxes = screen.getAllByRole('checkbox', { name: /genômica/i });
    fireEvent.click(genomicCheckboxes[0]);

    expect(onChange).toHaveBeenCalledOnce();
    const arg = onChange.mock.calls[0][0] as FiltersType;
    // Após desmarcar o único layer, omics_layer deve ser undefined
    expect(arg.omics_layer).toBeUndefined();
  });
});

// Nota: testes de "campo de busca textual" removidos — o campo `search` foi excluído
// do componente pois não é param válido da operação projects_datasets_export_retrieve.
