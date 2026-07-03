/**
 * Testes de regressão para DiscoveryTable.
 *
 * Invariantes travados:
 *  - renderiza linhas a partir de um array de DiscoveryItem
 *  - access_controlled=true exibe badge "Controlado" e ícone de cadeado
 *  - access_controlled=false não exibe badge "Controlado"
 *  - matrix_pointer é renderizado como ponteiro/link (nunca como conteúdo de dados)
 *  - matrix_pointer ausente (null/undefined) renderiza "—" sem quebrar
 *  - monogenic_gene_hit é objeto com .genes — renderiza como badges de gene, não como bool
 *  - monogenic_gene_hit ausente não quebra
 *  - contract ausente (null) não quebra
 *  - lista vazia exibe mensagem de estado vazio
 */

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import React from 'react';
import { DiscoveryTable } from '../discovery-table';
import type { DiscoveryItem } from '@/lib/types/discovery';

// ── Fixtures ──────────────────────────────────────────────────────────────────

/**
 * Cria um DiscoveryItem mínimo válido.
 * Todos os campos readonly são obrigatórios no schema.
 */
function makeItem(overrides: Partial<DiscoveryItem> = {}): DiscoveryItem {
  return {
    accession: 'GSE000001',
    source_db: 'geo',
    omics_layers: ['genomic'],
    omics_count: 1,
    is_single_cell: 'unknown',
    has_control_group: 'unknown',
    disease_axis: 'indeterminate',
    data_format: 'raw',
    access_type: 'public',
    sample_join_key: [],
    contract_confidence: null,
    contract: null,
    access_controlled: false,
    snapshot_version: 'live',
    ...overrides,
  };
}

// ── Testes: estado vazio ──────────────────────────────────────────────────────

describe('DiscoveryTable — lista vazia', () => {
  it('exibe mensagem de estado vazio quando items=[]', () => {
    render(<DiscoveryTable items={[]} />);
    expect(
      screen.getByText('Nenhum dataset encontrado com os filtros atuais.'),
    ).toBeInTheDocument();
  });
});

// ── Testes: renderização de linhas ───────────────────────────────────────────

describe('DiscoveryTable — renderização de linhas', () => {
  it('exibe o accession do item na tabela', () => {
    const item = makeItem({ accession: 'PXD012345', source_db: 'pride' });
    render(<DiscoveryTable items={[item]} />);
    expect(screen.getByText('PXD012345')).toBeInTheDocument();
  });

  it('exibe múltiplos itens', () => {
    const items = [
      makeItem({ accession: 'GSE000001' }),
      makeItem({ accession: 'GSE000002' }),
    ];
    render(<DiscoveryTable items={items} />);
    expect(screen.getByText('GSE000001')).toBeInTheDocument();
    expect(screen.getByText('GSE000002')).toBeInTheDocument();
  });

  it('exibe badge de camada ômica', () => {
    const item = makeItem({ omics_layers: ['proteomic'] });
    render(<DiscoveryTable items={[item]} />);
    expect(screen.getByText('proteomic')).toBeInTheDocument();
  });
});

// ── Testes: access_controlled ─────────────────────────────────────────────────

describe('DiscoveryTable — access_controlled', () => {
  it('exibe badge "Controlado" quando access_controlled=true', () => {
    const item = makeItem({
      access_controlled: true,
      access_type: 'controlled',
    });
    render(<DiscoveryTable items={[item]} />);
    expect(screen.getByText('Controlado')).toBeInTheDocument();
  });

  it('exibe ícone de cadeado no accession quando access_controlled=true', () => {
    const item = makeItem({
      accession: 'phs001234',
      access_controlled: true,
    });
    render(<DiscoveryTable items={[item]} />);
    // aria-label do Lock icon
    expect(screen.getByLabelText('Acesso controlado')).toBeInTheDocument();
  });

  it('não exibe badge "Controlado" quando access_controlled=false', () => {
    const item = makeItem({ access_controlled: false, access_type: 'public' });
    render(<DiscoveryTable items={[item]} />);
    expect(screen.queryByText('Controlado')).not.toBeInTheDocument();
  });

  it('aplica cor de fundo diferente em linhas controladas', () => {
    const item = makeItem({ access_controlled: true });
    const { container } = render(<DiscoveryTable items={[item]} />);
    // A linha deve ter a classe bg-amber-50/50
    const row = container.querySelector('tr.bg-amber-50\\/50');
    expect(row).not.toBeNull();
  });
});

// ── Testes: matrix_pointer ────────────────────────────────────────────────────

describe('DiscoveryTable — matrix_pointer', () => {
  it('renderiza ponteiro HTTP como link com href correto', () => {
    const pointer = 'https://ftp.pride.ebi.ac.uk/pride/data/PXD012345/file.tsv';
    const item = makeItem({
      contract: { matrix_pointer: pointer },
    });
    render(<DiscoveryTable items={[item]} />);
    const link = screen.getByRole('link');
    expect(link).toBeInTheDocument();
    expect(link).toHaveAttribute('href', pointer);
    // Link abre em nova aba
    expect(link).toHaveAttribute('target', '_blank');
    // noopener noreferrer por segurança
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('renderiza ponteiro FTP como texto puro (não como link) — M8 007', () => {
    // ftp:// não recebe link clicável: apenas http/https são navegáveis pelo browser;
    // ftp é texto puro para evitar convidar navegação a protocolo não suportado.
    const pointer = 'ftp://ftp.pride.ebi.ac.uk/pride/data/archive/2021/03/PXD012345/file.tsv';
    const item = makeItem({ contract: { matrix_pointer: pointer } });
    render(<DiscoveryTable items={[item]} />);
    // Não deve haver elemento <a> clicável
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    // O texto truncado (40 chars) deve aparecer como span
    expect(screen.getByText(/ftp:\/\/ftp\.pride\.ebi\.ac\.uk\/pride\/data\/ar/)).toBeInTheDocument();
  });

  it('renderiza ponteiro não-URL como texto monospace (não como link)', () => {
    const pointer = 'phs001234.v1.p1';
    const item = makeItem({ contract: { matrix_pointer: pointer } });
    render(<DiscoveryTable items={[item]} />);
    // Não deve haver <a> (nenhum link)
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    // O texto do ponteiro deve aparecer na tela como texto
    expect(screen.getByText('phs001234.v1.p1')).toBeInTheDocument();
  });

  it('exibe "—" quando matrix_pointer é null', () => {
    const item = makeItem({ contract: { matrix_pointer: null } });
    render(<DiscoveryTable items={[item]} />);
    // "—" aparece como placeholder (pode aparecer em múltiplas células)
    const dashes = screen.getAllByText('—');
    expect(dashes.length).toBeGreaterThan(0);
  });

  it('exibe "—" quando contract é null (sem quebrar)', () => {
    const item = makeItem({ contract: null });
    render(<DiscoveryTable items={[item]} />);
    // Não deve lançar e deve exibir traço no lugar de matrix_pointer
    const dashes = screen.getAllByText('—');
    expect(dashes.length).toBeGreaterThan(0);
  });

  it('trunca ponteiro longo com reticências mas preserva href completo no link', () => {
    const longPointer = 'https://ftp.pride.ebi.ac.uk/pride/data/archive/2021/03/PXD012345/very_long_file_name.tsv';
    const item = makeItem({ contract: { matrix_pointer: longPointer } });
    render(<DiscoveryTable items={[item]} />);
    const link = screen.getByRole('link');
    // href mantém URL completa
    expect(link).toHaveAttribute('href', longPointer);
    // Texto visível é truncado (máx 40 chars + '…')
    expect(link.textContent).toContain('…');
    expect(link.textContent!.length).toBeLessThan(longPointer.length);
  });

  it('renderiza URL https como texto puro quando access_controlled=true — M8 007', () => {
    // Mesmo com https://, se access_controlled=true, NÃO deve haver link clicável.
    // Não convida navegação a recurso restrito.
    const pointer = 'https://dbgap.ncbi.nlm.nih.gov/protected/phs001234';
    const item = makeItem({
      access_controlled: true,
      contract: { matrix_pointer: pointer },
    });
    render(<DiscoveryTable items={[item]} />);
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });
});

// ── Testes: monogenic_gene_hit ────────────────────────────────────────────────

describe('DiscoveryTable — monogenic_gene_hit', () => {
  it('renderiza genes como badges quando monogenic_gene_hit tem genes[]', () => {
    const item = makeItem({
      contract: {
        monogenic_gene_hit: {
          genes: ['BRCA1', 'TP53'],
          confidence: 0.85,
          gene_details: [],
        },
      },
    });
    render(<DiscoveryTable items={[item]} />);
    expect(screen.getByText('BRCA1')).toBeInTheDocument();
    expect(screen.getByText('TP53')).toBeInTheDocument();
  });

  it('não renderiza "true" nem "false" — monogenic_gene_hit é objeto, não bool', () => {
    const item = makeItem({
      contract: {
        monogenic_gene_hit: {
          genes: ['CFTR'],
          confidence: 0.9,
          gene_details: [],
        },
      },
    });
    render(<DiscoveryTable items={[item]} />);
    // O campo NUNCA deve aparecer como string booleana
    expect(screen.queryByText('true')).not.toBeInTheDocument();
    expect(screen.queryByText('false')).not.toBeInTheDocument();
    // O gene deve aparecer
    expect(screen.getByText('CFTR')).toBeInTheDocument();
  });

  it('exibe "—" quando monogenic_gene_hit é undefined (sem contract.monogenic_gene_hit)', () => {
    const item = makeItem({ contract: { matrix_pointer: null } }); // sem monogenic_gene_hit
    render(<DiscoveryTable items={[item]} />);
    // "—" deve aparecer (para gene mono. entre outros)
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });

  it('exibe "—" quando genes[] é array vazio', () => {
    const item = makeItem({
      contract: {
        monogenic_gene_hit: {
          genes: [],
          confidence: 0,
          gene_details: [],
        },
      },
    });
    render(<DiscoveryTable items={[item]} />);
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
    // Não deve exibir boolean
    expect(screen.queryByText('true')).not.toBeInTheDocument();
  });

  it('não quebra quando contract é null', () => {
    const item = makeItem({ contract: null });
    // Não deve lançar
    expect(() => render(<DiscoveryTable items={[item]} />)).not.toThrow();
  });

  it('não quebra quando monogenic_gene_hit é null', () => {
    const item = makeItem({
      contract: {
        monogenic_gene_hit: null,
      },
    });
    expect(() => render(<DiscoveryTable items={[item]} />)).not.toThrow();
  });
});

// ── Testes: contract_confidence ───────────────────────────────────────────────

describe('DiscoveryTable — contract_confidence', () => {
  it('exibe valor de confiança numérico formatado', () => {
    const item = makeItem({
      disease_axis: 'monogenic',
      contract_confidence: {
        disease_axis: 0.92,
      },
    });
    render(<DiscoveryTable items={[item]} />);
    expect(screen.getByText('conf: 0.92')).toBeInTheDocument();
  });

  it('exibe "—" quando contract_confidence é null', () => {
    const item = makeItem({
      disease_axis: 'indeterminate',
      contract_confidence: null,
    });
    render(<DiscoveryTable items={[item]} />);
    // "conf: —" deve aparecer na coluna de eixo
    expect(screen.getAllByText('conf: —').length).toBeGreaterThan(0);
  });

  it('não quebra quando contract_confidence.disease_axis é sub-objeto com score', () => {
    const item = makeItem({
      disease_axis: 'monogenic',
      contract_confidence: {
        disease_axis: { score: 0.78, method: 'orphanet' },
      },
    });
    render(<DiscoveryTable items={[item]} />);
    expect(screen.getByText('conf: 0.78')).toBeInTheDocument();
  });
});
