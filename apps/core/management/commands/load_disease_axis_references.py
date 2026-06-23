"""
Management command — load_disease_axis_references

Popula DiseaseAxisReference e MendelianGene com as âncoras de eixo de doença
do OmnisPathway (Fase 3).

Fontes suportadas:
    gwas_catalog — GWAS Catalog TSV (âncora multifatorial)
    orphanet     — Orphadata product6 XML (âncora monogênica: nomes + genes)
    g2p          — Gene2Phenotype CSV   (âncora monogênica: nomes + genes)
    all          — todas as fontes acima (padrão)

Uso:
    manage.py load_disease_axis_references [opções]

Opções:
    --source {gwas_catalog,orphanet,g2p,all}
                Fonte a carregar (padrão: all).

    --force     Rebaixa o arquivo mesmo se já estiver em cache local.
                Sem --force, reutiliza diagnostics/cache/<arquivo>.

    --dry-run   Aplica apenas ao GWAS Catalog (fontes XML/CSV não têm dry-run
                incremental — são parse completo mesmo). Exibe estatísticas sem
                gravar nenhum registro no banco.

Exemplos:
    # Carga completa (todas as fontes)
    manage.py load_disease_axis_references

    # Só Orphanet, forçando rebaixamento
    manage.py load_disease_axis_references --source orphanet --force

    # Dry-run GWAS Catalog
    manage.py load_disease_axis_references --source gwas_catalog --dry-run
"""

import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)

_VALID_SOURCES = ('gwas_catalog', 'orphanet', 'g2p', 'all')


class Command(BaseCommand):
    help = (
        'Carrega âncoras de eixo de doença em DiseaseAxisReference e MendelianGene '
        '(OmnisPathway Fase 3 — P4/monogênica).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--source',
            choices=_VALID_SOURCES,
            default='all',
            help=(
                'Fonte a carregar: gwas_catalog | orphanet | g2p | all '
                '(padrão: all).'
            ),
        )
        parser.add_argument(
            '--force',
            action='store_true',
            default=False,
            help='Rebaixa o arquivo mesmo se já estiver em cache local.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            dest='dry_run',
            default=False,
            help=(
                'Parse e exibe estatísticas sem gravar no banco. '
                'Aplicável apenas a --source gwas_catalog.'
            ),
        )

    def handle(self, *args, **options):
        from apps.core.services.disease_axis_service import DiseaseAxisReferenceService

        source = options['source']
        force = options['force']
        dry_run = options['dry_run']

        if dry_run and source not in ('gwas_catalog', 'all'):
            self.stdout.write(self.style.WARNING(
                f'--dry-run ignorado para source={source!r} '
                '(suportado apenas para gwas_catalog).'
            ))
            dry_run = False

        run_gwas = source in ('gwas_catalog', 'all')
        run_orphanet = source in ('orphanet', 'all')
        run_g2p = source in ('g2p', 'all')

        # ------------------------------------------------------------------ GWAS
        if run_gwas:
            self._run_gwas(DiseaseAxisReferenceService, force=force, dry_run=dry_run)

        # ------------------------------------------------------------------ Orphanet
        if run_orphanet:
            self._run_orphanet(DiseaseAxisReferenceService, force=force)

        # ------------------------------------------------------------------ G2P
        if run_g2p:
            self._run_g2p(DiseaseAxisReferenceService, force=force)

    # ------------------------------------------------------------------
    # Handlers por fonte
    # ------------------------------------------------------------------

    def _run_gwas(self, svc, *, force: bool, dry_run: bool):
        import os
        from apps.core.services.disease_axis_service import (
            GWAS_FTP_URL,
            _CACHE_SUBPATH_GWAS,
            normalize_trait_name,
        )

        self.stdout.write('\n[gwas_catalog] Iniciando...')

        if dry_run:
            self.stdout.write(self.style.WARNING(
                'MODO DRY-RUN — nenhum registro sera gravado no banco.'
            ))
            cache_path = svc._cache_path(_CACHE_SUBPATH_GWAS)
            cache_hit = os.path.exists(cache_path) and not force
            if cache_hit:
                self.stdout.write(f'  Cache local: {cache_path}')
            else:
                self.stdout.write(f'  Baixando de: {GWAS_FTP_URL}')
                svc._download(GWAS_FTP_URL, cache_path)
                self.stdout.write('  Download concluido.')

            names, trait_col = svc._parse_gwas_tsv(cache_path)
            self.stdout.write(
                f'  Coluna usada:    {trait_col!r}\n'
                f'  Nomes distintos: {len(names)}\n'
                f'  Exemplos (5 primeiros):'
            )
            for name in names[:5]:
                self.stdout.write(f'    {name!r} → {normalize_trait_name(name)!r}')
            self.stdout.write(self.style.SUCCESS('  Dry-run concluido. Nenhuma gravacao realizada.'))
            return

        stats = svc.load_gwas_catalog(force=force)
        self.stdout.write(self.style.SUCCESS(
            f'  Cache hit:       {stats["cache_hit"]}\n'
            f'  Coluna usada:    {stats["trait_column"]!r}\n'
            f'  Nomes distintos: {stats["total_names"]}\n'
            f'  Inseridos:       {stats["inserted"]}\n'
            f'  Atualizados:     {stats["updated"]}\n'
            f'  Source version:  {stats["source_version"]}'
        ))

    def _run_orphanet(self, svc, *, force: bool):
        self.stdout.write('\n[orphanet] Iniciando...')
        self.stdout.write('  (parse streaming do XML — pode levar alguns segundos)')
        stats = svc.load_orphanet(force=force)
        self.stdout.write(self.style.SUCCESS(
            f'  Cache hit:           {stats["cache_hit"]}\n'
            f'  Source version:      {stats["source_version"]}\n'
            f'  Doencas total:       {stats["diseases_total"]}\n'
            f'  Doencas inseridas:   {stats["diseases_inserted"]}\n'
            f'  Doencas atualizadas: {stats["diseases_updated"]}\n'
            f'  Genes total:         {stats["genes_total"]}\n'
            f'  Genes inseridos:     {stats["genes_inserted"]}\n'
            f'  Genes atualizados:   {stats["genes_updated"]}'
        ))

    def _run_g2p(self, svc, *, force: bool):
        self.stdout.write('\n[g2p] Iniciando...')
        stats = svc.load_g2p(force=force)
        self.stdout.write(self.style.SUCCESS(
            f'  Cache hit:           {stats["cache_hit"]}\n'
            f'  Source version:      {stats["source_version"]}\n'
            f'  Doencas total:       {stats["diseases_total"]}\n'
            f'  Doencas inseridas:   {stats["diseases_inserted"]}\n'
            f'  Doencas atualizadas: {stats["diseases_updated"]}\n'
            f'  Genes total:         {stats["genes_total"]}\n'
            f'  Genes inseridos:     {stats["genes_inserted"]}\n'
            f'  Genes atualizados:   {stats["genes_updated"]}'
        ))
