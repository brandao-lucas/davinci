"""
Management command — classify_contract

Backfill em massa dos classificadores de categorização do contrato OmnisPathway (Fase 2).

Uso:
    manage.py classify_contract [opções]

Opções:
    --axis has_control_group is_single_cell   Classifica apenas os eixos listados.
                                              Default: todos (has_control_group,
                                              is_single_cell, sample_join_key,
                                              data_format, access_type).

    --source geo sra pride_archive           Filtra datasets por source_db.
                                              Default: todas as fontes.

    --dry-run                                 Mostra o que seria feito sem gravar.
                                              Útil para inspecionar o texto que o
                                              classificador recebe.

    --report                                  Gera relatório de cobertura em
                                              diagnostics/classify_contract_YYYYMMDD_HHMMSS.log.
                                              Inclui: % classificado por eixo/fonte,
                                              % fila de curadoria (score < 0.5),
                                              % unknown sem sinal.

    --batch-size N                            Tamanho do batch por queryset.iterator().
                                              Default: 200.

    --limit N                                 Processa no máximo N datasets.
                                              Útil para testes parciais.

Exemplos:
    # Backfill completo
    manage.py classify_contract --report

    # Dry-run inspecionando GEO e SRA
    manage.py classify_contract --source geo sra --dry-run

    # Classificar só has_control_group com relatório
    manage.py classify_contract --axis has_control_group --report
"""

import json
import os
import datetime
import logging

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

logger = logging.getLogger(__name__)

ALL_AXES = [
    'has_control_group',
    'is_single_cell',
    'sample_join_key',
    'data_format',
    'access_type',
]


class Command(BaseCommand):
    help = 'Backfill em massa dos classificadores de categorização do contrato OmnisPathway (Fase 2).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--axis',
            nargs='+',
            choices=ALL_AXES,
            metavar='AXIS',
            help=(
                f'Eixos a classificar. Escolhas: {", ".join(ALL_AXES)}. '
                'Default: todos.'
            ),
        )
        parser.add_argument(
            '--source',
            nargs='+',
            metavar='SOURCE',
            help=(
                'Filtrar por source_db (geo, sra, pride_archive, arrayexpress, '
                'tcga, bioproject, gwas_catalog). Default: todas.'
            ),
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            dest='dry_run',
            default=False,
            help='Exibe o que seria feito sem gravar nenhuma alteração.',
        )
        parser.add_argument(
            '--report',
            action='store_true',
            default=False,
            help='Gera relatório de cobertura em diagnostics/.',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=200,
            dest='batch_size',
            help='Tamanho do batch. Default: 200.',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=None,
            help='Máximo de datasets a processar. Default: sem limite.',
        )

    def handle(self, *args, **options):
        from apps.core.models import OmicDataset
        from apps.core.services.contract_classifier_service import classify_all_axes

        axes = options['axis'] or ALL_AXES
        sources = options['source']
        dry_run = options['dry_run']
        do_report = options['report']
        batch_size = options['batch_size']
        limit = options['limit']

        if dry_run:
            self.stdout.write(self.style.WARNING('MODO DRY-RUN — nenhuma alteração será gravada.'))

        # Montar queryset base
        qs = OmicDataset.objects.filter(is_active=True)
        if sources:
            qs = qs.filter(source_db__in=sources)
        qs = qs.order_by('id')

        total_count = qs.count()
        if limit:
            qs = qs[:limit]
            effective_count = min(total_count, limit)
        else:
            effective_count = total_count

        self.stdout.write(
            f'Datasets alvo: {effective_count} (total no banco: {total_count})'
            f' | Eixos: {", ".join(axes)}'
            f' | Fontes: {", ".join(sources) if sources else "todas"}'
        )

        # Estrutura de coleta para relatório
        # Por eixo: {total, classified_high (>=0.5), classified_low (<0.5, unknown), no_signal}
        report_data: dict[str, dict] = {
            ax: {'total': 0, 'high_confidence': 0, 'low_confidence_queue': 0, 'no_signal': 0, 'skipped': 0}
            for ax in axes
        }
        # Por fonte × eixo
        by_source: dict[str, dict] = {}

        processed = 0
        errors = 0

        for dataset in qs.iterator(chunk_size=batch_size):
            processed += 1
            source = dataset.source_db

            if source not in by_source:
                by_source[source] = {
                    ax: {'total': 0, 'high_confidence': 0, 'low_confidence_queue': 0, 'no_signal': 0}
                    for ax in axes
                }

            try:
                result = classify_all_axes(dataset, axes=axes, dry_run=dry_run)

                if dry_run:
                    if processed <= 5:
                        self.stdout.write(
                            f'  [{processed}] {dataset.accession} ({source}): '
                            f'text_length={result.get("text_length", "?")} '
                            f'preview={result.get("text_preview", "")[:80]!r}'
                        )
                    continue

                # Coletar métricas para o relatório
                confidence = dataset.contract_confidence or {}
                for ax in axes:
                    stats = report_data[ax]
                    src_stats = by_source[source][ax]
                    stats['total'] += 1
                    src_stats['total'] += 1

                    ax_result = result.get(ax, {})

                    # data_format / access_type não usam contract_confidence
                    if ax in ('data_format', 'access_type'):
                        action = ax_result.get('action', '')
                        if action in ('updated',):
                            stats['high_confidence'] += 1
                            src_stats['high_confidence'] += 1
                        elif action in ('skipped', 'skipped_pride', 'skipped_source'):
                            stats['skipped'] += 1
                        else:
                            stats['no_signal'] += 1
                            src_stats['no_signal'] += 1
                    else:
                        score = confidence.get(ax)
                        if score is None:
                            stats['no_signal'] += 1
                            src_stats['no_signal'] += 1
                        elif score >= 0.5:
                            stats['high_confidence'] += 1
                            src_stats['high_confidence'] += 1
                        else:
                            stats['low_confidence_queue'] += 1
                            src_stats['low_confidence_queue'] += 1

            except Exception as exc:
                errors += 1
                logger.exception(
                    'classify_contract: erro ao processar dataset=%s', dataset.accession
                )
                if errors <= 10:
                    self.stderr.write(
                        self.style.ERROR(f'Erro em {dataset.accession}: {exc}')
                    )

            if processed % 100 == 0:
                self.stdout.write(f'  Processados: {processed}/{effective_count} ...')

        if dry_run:
            self.stdout.write(self.style.SUCCESS(
                f'Dry-run concluído. {processed} datasets inspecionados (sem gravação).'
            ))
            return

        self.stdout.write(self.style.SUCCESS(
            f'Concluído: {processed} datasets processados, {errors} erros.'
        ))

        if not do_report:
            return

        # ── Gerar relatório em diagnostics/ ──────────────────────────────────
        diagnostics_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))
            )))),
            'diagnostics',
        )
        os.makedirs(diagnostics_dir, exist_ok=True)

        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        report_path = os.path.join(diagnostics_dir, f'classify_contract_{timestamp}.log')

        lines = [
            '=' * 70,
            f'Relatório de Cobertura — classify_contract — {timestamp}',
            f'Axes: {", ".join(axes)}',
            f'Fontes: {", ".join(sources) if sources else "todas"}',
            f'Datasets processados: {processed}',
            f'Erros: {errors}',
            '=' * 70,
            '',
            '── Cobertura por eixo (global) ──',
        ]

        for ax in axes:
            s = report_data[ax]
            total = s['total'] or 1
            lines += [
                f'',
                f'  Eixo: {ax}',
                f'    Total:                {s["total"]}',
                f'    Alta confiança (>=0.5): {s["high_confidence"]} ({100*s["high_confidence"]//total}%)',
                f'    Fila curadoria (<0.5):  {s["low_confidence_queue"]} ({100*s["low_confidence_queue"]//total}%)',
                f'    Sem sinal / unknown:    {s["no_signal"]} ({100*s["no_signal"]//total}%)',
                f'    Ignorados (skipped):    {s["skipped"]}',
            ]

        lines += ['', '── Cobertura por fonte ──']
        for source, ax_data in sorted(by_source.items()):
            lines.append(f'  Fonte: {source}')
            for ax in axes:
                s = ax_data.get(ax, {})
                total = s.get('total', 0) or 1
                hc = s.get('high_confidence', 0)
                lc = s.get('low_confidence_queue', 0)
                ns = s.get('no_signal', 0)
                lines.append(
                    f'    {ax}: total={s.get("total",0)} '
                    f'alta={hc}({100*hc//total}%) '
                    f'fila={lc}({100*lc//total}%) '
                    f'sem_sinal={ns}({100*ns//total}%)'
                )

        lines += ['', '=' * 70]
        report_content = '\n'.join(lines)

        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(report_content)

        self.stdout.write(self.style.SUCCESS(f'Relatório gravado em: {report_path}'))
        self.stdout.write(report_content)
