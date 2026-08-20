"""
Management command — apply_pathway_fdr

Aplica Benjamini-Hochberg (FDR) sobre `PathwayActivityScore.method_version`
em DOIS ESCOPOS simultâneos (migration 0039) — OmnisPathway Obj 2, Fase 4.

    population   partição = pathway_id  → q_value_across_samples / ...
                 "esta via se destaca em mais pacientes que o acaso?"
    individual   partição = sample_id   → q_value_across_pathways / ...
                 "neste paciente, quais vias se destacam?"

Antes deste command, o BH era digitado no shell (SQL avulso) — este é o
comando que torna a passada REPRODUTÍVEL: mesmos dados, mesmo resultado,
sem depender de lembrar a query.

Idempotente: cada UPDATE é set-based sobre as MESMAS colunas
(`q_value_*`/`fdr_method_*`/`fdr_n_tests_*`); rodar de novo sobre dados
inalterados produz o mesmo resultado.

Degenerados (`null_sd == 0`, ~74% da bancada) ficam FORA do denominador do
BH e são rotulados por um UPDATE irmão: `fdr_method_* = 'benjamini_hochberg'`
com `q_value_* = NULL` — "considerado e excluído do denominador", distinto
de `fdr_method_* == ''` ("esta passada nunca rodou nesta linha").

Uso:
    # Sonda — conta avaliáveis/degenerados por escopo, sem gravar nada
    .venv/bin/python manage.py apply_pathway_fdr \\
        --method-version fase4-pfs-v2-372vias --dry-run

    # Aplica os dois escopos (default)
    .venv/bin/python manage.py apply_pathway_fdr \\
        --method-version fase4-pfs-v2-372vias

    # Só o escopo populacional
    .venv/bin/python manage.py apply_pathway_fdr \\
        --method-version fase4-pfs-v2-372vias --scope population
"""

from django.core.management.base import BaseCommand, CommandError

from apps.core.services.pathway_fdr_service import PathwayFdrScopeError, apply_fdr


class Command(BaseCommand):
    help = (
        'Aplica Benjamini-Hochberg (FDR) sobre PathwayActivityScore em dois '
        'escopos (population/individual) para um method_version. '
        'OmnisPathway Obj 2, Fase 4.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--method-version',
            metavar='VERSION',
            required=True,
            dest='method_version',
            help='method_version de PathwayActivityScore a corrigir (obrigatório).',
        )
        parser.add_argument(
            '--scope',
            metavar='SCOPE',
            default='both',
            choices=['both', 'population', 'individual'],
            help=(
                'Escopo(s) a aplicar: population (via através das amostras), '
                'individual (amostra através das vias) ou both (padrão: '
                'ambos).'
            ),
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            dest='dry_run',
            help='Conta avaliáveis/degenerados por escopo sem gravar nada.',
        )

    def handle(self, *args, **options):
        method_version = options['method_version']
        scope = options['scope']
        dry_run = options['dry_run']

        self.stdout.write(f'method_version: {method_version}')
        self.stdout.write(f'scope:          {scope}')
        self.stdout.write(f'dry_run:        {dry_run}')
        self.stdout.write('')

        try:
            report = apply_fdr(method_version=method_version, scope=scope, dry_run=dry_run)
        except PathwayFdrScopeError as exc:
            raise CommandError(str(exc)) from exc
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        for scope_name, data in report['scopes'].items():
            self.stdout.write(f'── {scope_name} — {data["label"]} ──')
            self.stdout.write(f'    partição:                {data["partition_col"]}')
            self.stdout.write(f'    avaliáveis (denominador do BH): {data["n_evaluable"]}')
            self.stdout.write(f'    degenerados (fora do BH):       {data["n_degenerate"]}')
            if dry_run:
                self.stdout.write(self.style.WARNING(
                    '    DRY RUN — nada foi gravado.'
                ))
            else:
                self.stdout.write(f'    linhas atualizadas (BH):        {data["rows_updated_bh"]}')
                self.stdout.write(f'    linhas rotuladas (degeneradas):  {data["rows_labeled_degenerate"]}')
            self.stdout.write('')

        if not dry_run:
            self.stdout.write(self.style.SUCCESS(
                f'FDR aplicado com sucesso para method_version={method_version!r}.'
            ))
