"""
Management command — resolve_variant_effects

Combina VariantEffectRaw (ClinVar/AlphaMissense/dbNSFP) em VariantEffectResolved
aplicando precedência de magnitude (ClinVar>AlphaMissense>dbNSFP) e resolução
de direção (activator/inactivator/neutral) por magnitude + papel do gene (GeneRole).

OmnisPathway Objetivo 2, Fase 2, Slice 2C (passo 2.5 do plano-base).

Lógica implementada em apps/core/services/variant_effect_resolve_service.py:

  §Tabela G — Âncora categórico→score (v1, travada no plano):
  ┌──────────────┬────────────────────────────────────────┬───────────┬────────────┐
  │ Fonte        │ Entrada crua                           │ magnitude │ confidence │
  ├──────────────┼────────────────────────────────────────┼───────────┼────────────┤
  │ ClinVar      │ Pathogenic                             │ 1.0       │ 0.9        │
  │ ClinVar      │ Likely pathogenic                      │ 0.9       │ 0.9        │
  │ ClinVar      │ Benign                                 │ 0.0       │ 0.9        │
  │ ClinVar      │ Likely benign                          │ 0.1       │ 0.9        │
  │ ClinVar      │ Uncertain significance / conflitante   │ cai p/ AM │ —          │
  │ ClinVar      │ Oncogenic (campo oncogenicity)         │ 1.0       │ 0.9        │
  │ ClinVar      │ Likely oncogenic                       │ 0.9       │ 0.9        │
  │ AlphaMissense│ am_pathogenicity ∈ [0,1]               │ = valor   │ 0.7        │
  │ dbNSFP       │ raw_magnitude ∈ [0,1]                  │ = valor   │ 0.6        │
  └──────────────┴────────────────────────────────────────┴───────────┴────────────┘

  Regras de direção (Decisão J — travadas):
    magnitude >= 0.5 (danoso):
      + role=tsg              → inactivator
      + role=oncogene         → activator  [heurística v1; refinar via OncoKB K]
      + role=oncogene_and_tsg → neutral + n_dual_flagged++  (Decisão J)
      + role=neither/unknown  → neutral
    magnitude < 0.5 (benigno/tolerado) → neutral (sem importar o papel)

Pré-requisitos:
  1. load_gene_roles  — popula GeneRole (papel: oncogene/tsg/...).
  2. load_variant_effects --source clinvar  — popula VariantEffectRaw ClinVar.
  3. load_variant_effects --source alphamissense  — popula VariantEffectRaw AM.
  4. resolve_variant_effects  — ESTE command.

Processamento:
  Set-based Django puro — ZERO Rust, ZERO parse de arquivo.
  Processa em chunks (padrão: 5000 pares) para escalar a dezenas de milhares
  de (variant_key, gene_symbol) sem explodir a memória.

Idempotência:
  bulk_create(update_conflicts=True) na natural key
  (variant_key, gene_symbol, resolution_version). Rodar 2x atualiza os
  registros sem duplicar. Re-processamento com nova lógica: trocar
  --resolution-version 'fase2-snv-v2' (coexiste com v1).

Sem IngestionJob:
  A resolução é derivação set-based rápida (sem Rust pesado, sem I/O de rede).
  Não cria IngestionJob — é command direto, adequado p/ bancada de prova e
  re-processamento. Se precisar de tracking assíncrono futuro, criar Celery
  task que chame VariantEffectResolveService.run() e use um job genérico.

Sensitive-data-handling:
  Nenhuma credencial neste fluxo. O service acessa apenas tabelas PG locais
  (VariantEffectRaw, GeneRole, VariantEffectResolved). Nenhuma URL externa.

Uso:
    # Básico (processa tudo)
    .venv/bin/python manage.py resolve_variant_effects

    # Dry-run (simula sem gravar)
    .venv/bin/python manage.py resolve_variant_effects --dry-run

    # Restringir a genes específicos das vias-alvo
    .venv/bin/python manage.py resolve_variant_effects --genes TP53 PIK3CA PTEN

    # Re-processamento com versão diferente (coexiste com a anterior)
    .venv/bin/python manage.py resolve_variant_effects \\
        --resolution-version fase2-snv-v2

    # Ajuste de chunk size (memória vs. velocidade)
    .venv/bin/python manage.py resolve_variant_effects --chunk-size 2000

Pipeline completo (Fase 2, incremento SNV):
    .venv/bin/python manage.py load_gene_roles --project <UUID>
    .venv/bin/python manage.py load_variant_effects --source clinvar  [ferris/2.4]
    .venv/bin/python manage.py load_variant_effects --source alphamissense  [ferris/2.4]
    .venv/bin/python manage.py resolve_variant_effects        [este command/2.5]
    .venv/bin/python manage.py load_somatic_maf --project <UUID>   [ferris/2.6]
    .venv/bin/python manage.py derive_snv_seeds --project <UUID>   [vitruvio/2.6]
"""

import sys

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        'Combina VariantEffectRaw em VariantEffectResolved aplicando precedência '
        'ClinVar>AlphaMissense>dbNSFP + direção por papel do gene. '
        'OmnisPathway Obj 2, Fase 2, Slice 2C (passo 2.5). '
        'Set-based Django puro — zero Rust, zero parse de arquivo.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            dest='dry_run',
            help=(
                'Simula a resolução sem gravar nada em VariantEffectResolved. '
                'Útil para validar a lógica de precedência e direção com fixtures.'
            ),
        )
        parser.add_argument(
            '--genes',
            nargs='+',
            metavar='SYMBOL',
            default=None,
            dest='gene_symbols',
            help=(
                'Restringe a resolução a esses gene_symbols (espaço-separados). '
                'Útil para inspecionar genes das vias-alvo (TP53, PIK3CA, PTEN ...). '
                'Se omitido, processa TODOS os genes em VariantEffectRaw.'
            ),
        )
        parser.add_argument(
            '--resolution-version',
            metavar='VERSION',
            default=None,
            dest='resolution_version',
            help=(
                'Versão do algoritmo de resolução (parte da natural key). '
                'Padrão: constante RESOLUTION_VERSION do service '
                '(atualmente "fase2-snv-v1"). '
                'Trocar para "fase2-snv-v2" permite reprocessar sem apagar v1.'
            ),
        )
        parser.add_argument(
            '--chunk-size',
            metavar='N',
            type=int,
            default=5000,
            dest='chunk_size',
            help=(
                'Número de pares (variant_key, gene_symbol) processados por chunk. '
                'Padrão: 5000. Reduzir se houver pressão de memória; '
                'aumentar para dados pequenos (ex.: fixtures de teste).'
            ),
        )

    def handle(self, *args, **options):
        from apps.core.services.variant_effect_resolve_service import (
            RESOLUTION_VERSION,
            VariantEffectResolveService,
        )

        dry_run: bool = options['dry_run']
        gene_symbols: list[str] | None = options['gene_symbols']
        resolution_version: str = options['resolution_version'] or RESOLUTION_VERSION
        chunk_size: int = options['chunk_size']

        # Validação básica
        if chunk_size <= 0:
            raise CommandError(
                f'--chunk-size deve ser positivo (recebido: {chunk_size}).'
            )

        # ── Cabeçalho ─────────────────────────────────────────────────────────
        self.stdout.write('resolve_variant_effects — OmnisPathway Obj 2, Fase 2, Slice 2C')
        self.stdout.write(f'  resolution_version: {resolution_version}')
        self.stdout.write(f'  chunk_size:         {chunk_size}')
        if gene_symbols:
            self.stdout.write(f'  gene_symbols:       {", ".join(gene_symbols)}')
        else:
            self.stdout.write('  gene_symbols:       todos (sem filtro)')
        if dry_run:
            self.stdout.write(self.style.WARNING(
                '  dry_run=True — NADA será gravado em VariantEffectResolved.'
            ))
        self.stdout.write('')

        # ── Execução ──────────────────────────────────────────────────────────
        service = VariantEffectResolveService(
            resolution_version=resolution_version,
            gene_symbols=gene_symbols,
            chunk_size=chunk_size,
        )

        try:
            report = service.run(dry_run=dry_run)
        except Exception as exc:
            raise CommandError(
                f'Resolução de efeitos de variante falhou: {exc}'
            ) from exc

        # ── Relatório final ───────────────────────────────────────────────────
        if report.n_pairs_processed == 0:
            self.stdout.write(self.style.WARNING(
                'Nenhum par (variant_key, gene_symbol) encontrado em '
                'VariantEffectRaw. Pré-requisitos:\n'
                '  1. load_gene_roles\n'
                '  2. load_variant_effects --source clinvar\n'
                '  3. load_variant_effects --source alphamissense'
            ))
            return

        self.stdout.write(self.style.SUCCESS(
            'Resolução concluída.' if not dry_run
            else 'Simulação (dry-run) concluída — nada foi gravado.'
        ))

        self.stdout.write('')
        self.stdout.write('  Pares processados:')
        self.stdout.write(f'    n_pairs_processed:       {report.n_pairs_processed}')
        self.stdout.write(f'    n_no_magnitude:          {report.n_no_magnitude}  '
                          '(sem magnitude resolvivel — skipped)')
        self.stdout.write(f'    n_resolved:              {report.n_resolved}')

        self.stdout.write('')
        self.stdout.write('  Distribuicao por fonte vencedora:')
        for src, count in sorted(report.n_by_source.items()):
            pct = 100.0 * count / report.n_resolved if report.n_resolved else 0.0
            self.stdout.write(f'    {src:<20} {count:>8}  ({pct:.1f}%)')

        self.stdout.write('')
        self.stdout.write('  Distribuicao de direcao:')
        self.stdout.write(f'    n_activator:             {report.n_activator}')
        self.stdout.write(f'    n_inactivator:           {report.n_inactivator}')
        self.stdout.write(f'    n_neutral:               {report.n_neutral}')

        self.stdout.write('')
        self.stdout.write('  Diagnostico de papel do gene:')
        self.stdout.write(f'    n_dual_flagged:          {report.n_dual_flagged}  '
                          '(oncogene_and_tsg -> neutral, Decisao J)')
        self.stdout.write(f'    n_no_gene_role:          {report.n_no_gene_role}  '
                          '(neither/unknown/ausente -> neutral)')
        self.stdout.write(f'    n_clinvar_vus_fell_to_am:{report.n_clinvar_vus_fell_to_am}  '
                          '(ClinVar VUS -> caiu p/ AlphaMissense)')

        self.stdout.write('')
        self.stdout.write(f'  Duracao:                   {report.duration_seconds:.1f}s')
        self.stdout.write('')

        # ── Avisos de diagnóstico ─────────────────────────────────────────────
        if report.n_no_gene_role > 0 and report.n_resolved > 0:
            pct = 100.0 * report.n_no_gene_role / report.n_resolved
            self.stdout.write(self.style.WARNING(
                f'AVISO: {report.n_no_gene_role} pares ({pct:.1f}%) sem papel '
                f'de gene definido foram resolvidos como direction=neutral. '
                f'Execute load_gene_roles para ampliar a cobertura de papel.'
            ))

        if report.n_dual_flagged > 0:
            self.stdout.write(self.style.WARNING(
                f'AVISO: {report.n_dual_flagged} pares com role=oncogene_and_tsg '
                f'foram resolvidos como direction=neutral (Decisao J: ambiguidade '
                f'nao e resoluta; revisar manualmente ou aguardar OncoKB API).'
            ))

        if report.n_no_magnitude > 0:
            pct = (
                100.0 * report.n_no_magnitude
                / (report.n_pairs_processed)
                if report.n_pairs_processed > 0 else 0.0
            )
            self.stdout.write(self.style.WARNING(
                f'AVISO: {report.n_no_magnitude} pares ({pct:.1f}%) sem magnitude '
                f'resolvivel foram pulados (nenhuma fonte com valor nao-VUS). '
                f'Isso e esperado se ClinVar VUS nao tem cobertura AlphaMissense.'
            ))

        if report.n_resolved == 0:
            self.stdout.write(self.style.WARNING(
                'AVISO: nenhum VariantEffectResolved foi produzido. Verifique:\n'
                '  (a) VariantEffectRaw tem dados? (load_variant_effects)\n'
                '  (b) Os gene_symbols (--genes) existem em VariantEffectRaw?\n'
                '  (c) Todas as linhas ClinVar sao VUS e nao ha AM/dbNSFP?\n'
            ))
            sys.exit(1)
        elif not dry_run:
            self.stdout.write(
                f'VariantEffectResolved populado: {report.n_resolved} linhas '
                f'(version={resolution_version}).\n'
                '\nPipeline Fase 2 — proximos passos:\n'
                '  4. load_somatic_maf --project <UUID>   [ferris, passo 2.6]\n'
                '  5. derive_snv_seeds --project <UUID>   [vitruvio, passo 2.6]'
            )
