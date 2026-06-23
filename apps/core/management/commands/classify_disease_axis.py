"""
Management command — classify_disease_axis

Classifica `disease_axis` e computa `monogenic_gene_hit` para OmicDatasets.
(OmnisPathway Fase 3 — núcleo Django-only)

Uso:
    manage.py classify_disease_axis [opções]

Opções:
    --dataset ACCESSION     Processa apenas estes datasets (repetível).
                            Default: todos os datasets ativos.

    --source geo sra pride_archive ...
                            Filtra por source_db. Default: todas as fontes.

    --limit N               Máximo de datasets a processar. Útil para smoke.

    --batch-size N          Chunk size do iterador. Default: 200.

    --dry-run               Simula classificação sem gravar nada no banco.

    --skip-gene-hit         Pula a Parte 2 (monogenic_gene_hit).
                            Útil para testar só o classificador de eixo.

    --report                Emite relatório detalhado de cobertura em stdout
                            e salva em diagnostics/.

Exemplos:
    # Smoke: 20 datasets com relatório
    manage.py classify_disease_axis --limit 20 --report

    # Dataset específico em dry-run
    manage.py classify_disease_axis --dataset PXD068006 --dry-run

    # Classificar tudo com relatório
    manage.py classify_disease_axis --report

    # Só PRIDE, sem gene_hit
    manage.py classify_disease_axis --source pride_archive --skip-gene-hit
"""

import datetime
import logging
import os

from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Classifica disease_axis e computa monogenic_gene_hit '
        '(OmnisPathway Fase 3, Django-only).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dataset',
            nargs='+',
            metavar='ACCESSION',
            dest='accessions',
            help='Accessions específicos a processar. Default: todos.',
        )
        parser.add_argument(
            '--source',
            nargs='+',
            metavar='SOURCE',
            dest='source_dbs',
            help='Filtrar por source_db. Default: todas.',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=None,
            help='Máximo de datasets a processar.',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=200,
            dest='batch_size',
            help='Chunk size para iterador. Default: 200.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            dest='dry_run',
            default=False,
            help='Simula sem gravar.',
        )
        parser.add_argument(
            '--skip-gene-hit',
            action='store_true',
            dest='skip_gene_hit',
            default=False,
            help='Pula computação de monogenic_gene_hit (Parte 2).',
        )
        parser.add_argument(
            '--report',
            action='store_true',
            default=False,
            help='Emite relatório detalhado de cobertura.',
        )

    def handle(self, *args, **options):
        from apps.core.models import OmicDataset
        from apps.core.services.disease_axis_classifier_service import (
            classify_all,
            compute_high_value_queue_counts,
        )

        accessions = options['accessions']
        source_dbs = options['source_dbs']
        limit = options['limit']
        batch_size = options['batch_size']
        dry_run = options['dry_run']
        skip_gene_hit = options['skip_gene_hit']
        do_report = options['report']

        if dry_run:
            self.stdout.write(self.style.WARNING(
                'MODO DRY-RUN — nenhuma alteração será gravada no banco.'
            ))

        # Validar accessions se fornecidos
        if accessions:
            found = set(
                OmicDataset.objects.filter(accession__in=accessions)
                .values_list('accession', flat=True)
            )
            missing = set(accessions) - found
            if missing:
                self.stderr.write(self.style.WARNING(
                    f'Accessions não encontrados: {", ".join(sorted(missing))}'
                ))
            if not found:
                raise CommandError('Nenhum dataset encontrado.')

        # Contagem estimada para progress
        qs = OmicDataset.objects.filter(is_active=True)
        if accessions:
            qs = qs.filter(accession__in=accessions)
        if source_dbs:
            qs = qs.filter(source_db__in=source_dbs)
        total_count = qs.count()
        effective_count = min(total_count, limit) if limit else total_count

        self.stdout.write(
            f'Datasets alvo: {effective_count} (total no banco: {total_count})'
            f' | Fonte(s): {", ".join(source_dbs) if source_dbs else "todas"}'
            f' | Dry-run: {dry_run}'
            f' | Skip gene_hit: {skip_gene_hit}'
        )

        # --- Classificação em massa ---
        stats = classify_all(
            accessions=accessions,
            source_dbs=source_dbs,
            limit=limit,
            batch_size=batch_size,
            dry_run=dry_run,
            skip_gene_hit=skip_gene_hit,
        )

        self.stdout.write(self.style.SUCCESS(
            f'\nConcluído: {stats["processed"]} processados, '
            f'{stats["errors"]} erros, '
            f'{stats["changed_count"]} alterados.'
        ))

        # --- Distribuição dos 4 estados ---
        dist = stats['axis_distribution']
        total = stats['processed'] or 1
        self.stdout.write('\nDistribuição disease_axis:')
        for axis in ('monogenic', 'multifactorial', 'mixed', 'indeterminate'):
            n = dist.get(axis, 0)
            pct = 100 * n // total
            self.stdout.write(f'  {axis:15s}: {n:5d}  ({pct}%)')

        resolved = total - dist.get('indeterminate', 0)
        self.stdout.write(f'\n  Resolvidos: {resolved}/{total} ({100*resolved//total}%)')

        # --- Breakdown de método ---
        mb = stats['method_breakdown']
        self.stdout.write('\nMétodo de resolução (não mutuamente exclusivos por dataset):')
        self.stdout.write(f'  exact monogênico:     {mb.get("exact_mono", 0)}')
        self.stdout.write(f'  exact multifatorial:  {mb.get("exact_multi", 0)}')
        self.stdout.write(f'  fuzzy monogênico:     {mb.get("fuzzy_mono", 0)}')
        self.stdout.write(f'  fuzzy multifatorial:  {mb.get("fuzzy_multi", 0)}')
        self.stdout.write(f'  sem match:            {mb.get("no_match", 0)}')

        # --- Gene hit ---
        if not skip_gene_hit:
            gh = stats['gene_hit_count']
            self.stdout.write(
                f'\nDatasets com monogenic_gene_hit: {gh}/{total} ({100*gh//total}%)'
            )

        # --- Fila de alto valor ---
        if not dry_run:
            queue = compute_high_value_queue_counts()
            self.stdout.write('\nFila de alto valor (mixed + discordância):')
            self.stdout.write(f'  Grupo 1 — mixed:          {queue["mixed_count"]}')
            self.stdout.write(f'  Grupo 2 — discordância:   {queue["discordant_count"]}')
            self.stdout.write(f'  TOTAL fila alto valor:    {queue["total_high_value"]}')

            if queue['mixed_examples']:
                self.stdout.write('\n  Exemplos mixed:')
                for ex in queue['mixed_examples']:
                    self.stdout.write(
                        f'    {ex["accession"]}: mono={ex["mono_matched"]!r} '
                        f'multi={ex["multi_matched"]!r}'
                    )

            if queue['discordant_examples']:
                self.stdout.write('\n  Exemplos discordância:')
                for ex in queue['discordant_examples']:
                    self.stdout.write(
                        f'    {ex["accession"]} (axis={ex["disease_axis"]}): '
                        f'genes={ex["genes"]} conf={ex["confidence"]:.3f}'
                    )
        else:
            self.stdout.write(
                '\n(Fila de alto valor não computada em dry-run — requer dados gravados.)'
            )

        if not do_report:
            return

        # --- Relatório em diagnostics/ ---
        self._write_report(
            stats=stats,
            queue=queue if not dry_run else None,
            options=options,
            effective_count=effective_count,
            total_count=total_count,
            skip_gene_hit=skip_gene_hit,
        )

    def _write_report(
        self,
        *,
        stats: dict,
        queue: dict | None,
        options: dict,
        effective_count: int,
        total_count: int,
        skip_gene_hit: bool,
    ) -> None:
        """Grava relatório de cobertura em diagnostics/classify_disease_axis_YYYYMMDD_HHMMSS.log."""
        # Calcular caminho de diagnostics/
        # apps/core/management/commands/ → subir 5 níveis → davinci/
        base_dir = os.path.dirname(os.path.abspath(__file__))
        for _ in range(5):
            base_dir = os.path.dirname(base_dir)
        diag_dir = os.path.join(base_dir, 'diagnostics')
        os.makedirs(diag_dir, exist_ok=True)

        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        report_path = os.path.join(diag_dir, f'classify_disease_axis_{timestamp}.log')

        dist = stats['axis_distribution']
        mb = stats['method_breakdown']
        total = stats['processed'] or 1
        resolved = total - dist.get('indeterminate', 0)

        lines = [
            '=' * 72,
            f'Relatório de Cobertura — classify_disease_axis — {timestamp}',
            f'Datasets alvo: {effective_count} / banco: {total_count}',
            f'Fonte(s): {", ".join(options.get("source_dbs") or []) or "todas"}',
            f'Dry-run: {options.get("dry_run", False)}',
            f'Skip gene_hit: {skip_gene_hit}',
            f'Processados: {stats["processed"]} | Erros: {stats["errors"]} '
            f'| Alterados: {stats["changed_count"]}',
            '=' * 72,
            '',
            '── Distribuição disease_axis ──',
        ]

        for axis in ('monogenic', 'multifactorial', 'mixed', 'indeterminate'):
            n = dist.get(axis, 0)
            pct = 100 * n // total
            lines.append(f'  {axis:15s}: {n:5d}  ({pct}%)')

        lines += [
            f'  {"resolvidos":15s}: {resolved:5d}  ({100*resolved//total}%)',
            '',
            '── Método de resolução ──',
            f'  exact monogênico:     {mb.get("exact_mono", 0)}',
            f'  exact multifatorial:  {mb.get("exact_multi", 0)}',
            f'  fuzzy monogênico:     {mb.get("fuzzy_mono", 0)}',
            f'  fuzzy multifatorial:  {mb.get("fuzzy_multi", 0)}',
            f'  sem match:            {mb.get("no_match", 0)}',
            '',
        ]

        if not skip_gene_hit:
            gh = stats['gene_hit_count']
            lines += [
                '── monogenic_gene_hit ──',
                f'  Datasets com gene_hit: {gh}/{total} ({100*gh//total}%)',
                '',
            ]

        if queue:
            lines += [
                '── Fila de alto valor ──',
                f'  Grupo 1 — mixed:          {queue["mixed_count"]}',
                f'  Grupo 2 — discordância:   {queue["discordant_count"]}',
                f'  TOTAL fila alto valor:    {queue["total_high_value"]}',
                '',
                '  Exemplos mixed:',
            ]
            for ex in queue.get('mixed_examples', []):
                lines.append(
                    f'    {ex["accession"]}: mono={ex["mono_matched"]!r} '
                    f'multi={ex["multi_matched"]!r}'
                )
            lines += ['', '  Exemplos discordância:']
            for ex in queue.get('discordant_examples', []):
                lines.append(
                    f'    {ex["accession"]} (axis={ex["disease_axis"]}): '
                    f'genes={ex["genes"]} conf={ex["confidence"]:.3f}'
                )

        lines += ['', '=' * 72]
        content = '\n'.join(lines)

        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(content)

        self.stdout.write(self.style.SUCCESS(f'\nRelatório gravado em: {report_path}'))
