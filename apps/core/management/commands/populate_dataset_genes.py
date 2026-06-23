"""
Management command — populate_dataset_genes

Popula DatasetGene para OmicDatasets a partir de duas fontes:
  - paper_link:       genes herdados dos papers ligados via DatasetPaperLink → PaperGene.
  - dataset_metadata: genes extraídos por NER (rust_engine) sobre titulo/resumo/metadados.

(OmnisPathway Fase 3, P2)

Uso:
    manage.py populate_dataset_genes [opções]

Opções:
    --dataset ACCESSION   Processa apenas o dataset com este accession.
                          Pode ser repetido para múltiplos datasets.

    --source paper_link dataset_metadata
                          Fontes a processar. Default: ambas.

    --batch-size N        Tamanho do chunk para iteração em massa. Default: 100.

    --limit N             Máximo de datasets a processar em modo "todos".
                          Default: sem limite.

    --dry-run             Exibe o que seria feito sem gravar nada no banco.

Exemplos:
    # Todas as fontes, todos os datasets
    manage.py populate_dataset_genes

    # Dataset específico
    manage.py populate_dataset_genes --dataset PRJNA1116214

    # Apenas paper_link para todos
    manage.py populate_dataset_genes --source paper_link

    # Dry-run
    manage.py populate_dataset_genes --dry-run --limit 5
"""

import logging

from django.core.management.base import BaseCommand, CommandError

from apps.core.services.dataset_gene_service import (
    ALL_SOURCES,
    SOURCE_PAPER_LINK,
    SOURCE_DATASET_METADATA,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Popula DatasetGene via paper_link (PaperGene dos papers ligados) '
        'e/ou dataset_metadata (NER sobre título/resumo). (OmnisPathway Fase 3, P2)'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dataset',
            nargs='+',
            metavar='ACCESSION',
            dest='accessions',
            help='Processar apenas estes datasets (por accession). Default: todos.',
        )
        parser.add_argument(
            '--source',
            nargs='+',
            choices=list(ALL_SOURCES),
            metavar='SOURCE',
            dest='sources',
            help=(
                f'Fontes a processar: {", ".join(ALL_SOURCES)}. '
                'Default: ambas.'
            ),
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=100,
            dest='batch_size',
            help='Tamanho do chunk para iteracao em massa. Default: 100.',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=None,
            help='Maximo de datasets a processar. Default: sem limite.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            dest='dry_run',
            default=False,
            help='Exibe o que seria feito sem gravar nenhuma alteracao no banco.',
        )

    def handle(self, *args, **options):
        from apps.core.models import OmicDataset
        from apps.core.services.dataset_gene_service import DatasetGeneService

        accessions = options['accessions']
        sources = tuple(options['sources']) if options['sources'] else ALL_SOURCES
        batch_size = options['batch_size']
        limit = options['limit']
        dry_run = options['dry_run']

        if dry_run:
            self.stdout.write(self.style.WARNING(
                'MODO DRY-RUN — nenhum registro sera gravado no banco.'
            ))

        self.stdout.write(
            f'Fontes: {", ".join(sources)} | '
            f'Datasets: {"especificos: " + ", ".join(accessions) if accessions else "todos"}'
        )

        if accessions:
            # Modo de dataset(s) específico(s)
            datasets = list(OmicDataset.objects.filter(accession__in=accessions))
            if not datasets:
                raise CommandError(
                    f'Nenhum OmicDataset encontrado para accessions: {accessions}'
                )
            missing = set(accessions) - {ds.accession for ds in datasets}
            if missing:
                self.stderr.write(self.style.WARNING(
                    f'Accessions nao encontrados: {", ".join(missing)}'
                ))

            for dataset in datasets:
                if dry_run:
                    self._dry_run_dataset(dataset, sources)
                    continue

                result = DatasetGeneService.populate_dataset(dataset, sources=sources)
                self.stdout.write(
                    f'  {dataset.accession} ({dataset.source_db}): '
                    + ' | '.join(
                        f'{src}={result.get(src, {}).get("gene_count", 0)} genes'
                        for src in sources
                    )
                )

        else:
            # Modo em massa
            if dry_run:
                qs = OmicDataset.objects.filter(is_active=True).order_by('id')
                if limit:
                    qs = qs[:limit]
                count = qs.count() if not limit else min(OmicDataset.objects.filter(is_active=True).count(), limit)
                for dataset in qs[:5]:
                    self._dry_run_dataset(dataset, sources)
                self.stdout.write(self.style.SUCCESS(
                    f'Dry-run: inspecionados ate 5 datasets (total estimado: {count}). Sem gravacao.'
                ))
                return

            stats = DatasetGeneService.populate_all(
                sources=sources,
                batch_size=batch_size,
                limit=limit,
            )

            self.stdout.write(self.style.SUCCESS(
                f'\nConcluido:\n'
                f'  Datasets processados: {stats["processed"]}\n'
                f'  Erros:                {stats["errors"]}\n'
                + '\n'.join(
                    f'  Genes [{src}]:          {stats["gene_counts"].get(src, 0)}'
                    for src in sources
                )
            ))

    def _dry_run_dataset(self, dataset, sources):
        """Inspeciona um dataset sem gravar."""
        import rust_engine
        from apps.core.models import PaperGene
        from apps.core.services.dataset_gene_service import _build_metadata_text

        self.stdout.write(f'  {dataset.accession} ({dataset.source_db}):')

        if SOURCE_PAPER_LINK in sources:
            n_genes = (
                PaperGene.objects
                .filter(paper__datasetpaperlink__dataset=dataset)
                .values('gene_symbol')
                .distinct()
                .count()
            )
            self.stdout.write(f'    paper_link: {n_genes} gene_symbols distintos nos papers ligados')

        if SOURCE_DATASET_METADATA in sources:
            text = _build_metadata_text(dataset)
            if text:
                genes = rust_engine.extract_genes(text)
                self.stdout.write(
                    f'    dataset_metadata: text_len={len(text)} genes_extraidos={len(genes)}'
                )
                if genes:
                    self.stdout.write(
                        f'    primeiros genes: {genes[:5]}'
                    )
            else:
                self.stdout.write('    dataset_metadata: texto vazio — nenhum gene seria extraido')
