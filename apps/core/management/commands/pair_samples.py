"""
Management command — pair_samples

Materializa os pares tumor↔normal nível-amostra para uma OmicMatrix via
SamplePairingService (lógica set-based Django, zero Rust, não abre o Parquet).

OmnisPathway Objetivo 2, Fase 1, passo 1.2.

Analogia com load_cptac_matrix e export_catalog:
  - --project é obrigatório (isolamento por usuário / Regra #3).
  - --matrix é opcional (default: auto-detecta a matriz CCRCC proteome do
    projeto — único dataset com OmicMatrix na Fase 0).
  - --dry-run: simula, reporta contagens e diagnósticos, NÃO grava. Serve
    para validar suposições antes de materializar (ver §Uso / Suposições).

Idempotência:
  Rodar 2× com o mesmo --project/--matrix não duplica OmicSamplePairing.
  A segunda execução reporta n_pairs_created=0, n_pairs_existing=N.

Não faz:
  - Não abre o Parquet.
  - Não chama Rust.
  - Não cria endpoint HTTP.
  - Não vaza caminho físico nem credencial nos logs ou stdout.

Suposições que o --dry-run valida:
  1. Granularidade 1:1 — cada case_id aparece no máximo 1× por role na matriz.
     Se multi_aliquota_tumor/normal for vazio, a UniqueConstraint(matrix, case_id)
     do OmicSamplePairing é segura.
  2. characteristics['case_id'] populado em 100% das amostras da Fase 0.
     Se characteristics_missing for vazio, o fallback de accession nunca é usado.
  3. Contagem de pares esperada: ≤ n_normal (84), pois todo normal tem case_id
     com tumor (subconjunto dos 110 tumores).

Uso:
    # Dry-run (validar suposições ANTES de materializar)
    .venv/bin/python manage.py pair_samples --project <UUID> --dry-run

    # Materializar pares
    .venv/bin/python manage.py pair_samples --project <UUID>

    # Matriz específica (ID de OmicMatrix)
    .venv/bin/python manage.py pair_samples --project <UUID> --matrix <ID>

    # Segunda execução (idempotente)
    .venv/bin/python manage.py pair_samples --project <UUID>
    # → n_pairs_created=0, n_pairs_existing=N
"""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        'Materializa pares tumor↔normal (OmicSamplePairing) para uma OmicMatrix. '
        'OmnisPathway Obj 2, Fase 1. Lógica set-based Django, zero Rust.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--project',
            metavar='UUID',
            required=True,
            help='UUID do DaVinciProject alvo (obrigatório — isolamento por projeto).',
        )
        parser.add_argument(
            '--matrix',
            metavar='ID',
            type=int,
            default=None,
            help=(
                'ID inteiro de OmicMatrix específica. '
                'Padrão: auto-detecta a matriz CCRCC proteome do projeto.'
            ),
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            dest='dry_run',
            help=(
                'Simula o pareamento e reporta contagens/diagnósticos '
                'SEM gravar OmicSamplePairing. '
                'Use antes de materializar para validar as suposições de '
                'granularidade e preenchimento de characteristics.'
            ),
        )

    def handle(self, *args, **options):
        from apps.core.models import DaVinciProject
        from apps.core.services.sample_pairing_service import (
            PAIRING_VERSION,
            MatrixNotFoundError,
            SamplePairingService,
        )

        project_uuid = options['project']
        matrix_id = options['matrix']
        dry_run = options['dry_run']

        # ── Resolve projeto ───────────────────────────────────────────────────
        try:
            project = DaVinciProject.objects.select_related('user').get(pk=project_uuid)
        except DaVinciProject.DoesNotExist:
            raise CommandError(f'Projeto não encontrado: {project_uuid}')

        self.stdout.write(f'Projeto: {project.title} (id={project.id})')
        self.stdout.write(f'Pairing version: {PAIRING_VERSION}')

        if dry_run:
            self.stdout.write(self.style.WARNING(
                'Modo: dry-run (simulação — nenhum OmicSamplePairing será gravado)'
            ))
        else:
            self.stdout.write('Modo: execução real')

        # ── Resolve OmicMatrix ────────────────────────────────────────────────
        try:
            matrix = SamplePairingService.resolve_matrix(project, matrix_id=matrix_id)
        except MatrixNotFoundError as exc:
            raise CommandError(str(exc))

        self.stdout.write(
            f'OmicMatrix: id={matrix.id}, dataset={matrix.dataset.accession}, '
            f'layer={matrix.omics_layer}, feature_axis={matrix.feature_axis}'
        )

        # ── Executa o service ─────────────────────────────────────────────────
        try:
            service = SamplePairingService(matrix)
            report = service.run(dry_run=dry_run)
        except Exception as exc:
            raise CommandError(f'Pareamento falhou: {exc}') from exc

        # ── Relatório (sem vazar caminho físico nem credencial) ───────────────
        self._print_report(report)

    def _print_report(self, report: dict) -> None:
        """Imprime o relatório estruturado de forma legível."""
        dry_run = report['dry_run']
        self.stdout.write('')

        if dry_run:
            self.stdout.write(self.style.WARNING(
                '=== RELATÓRIO DE SIMULAÇÃO (dry-run) ==='
            ))
        else:
            paired_ok = report['n_pairs_created'] > 0 or report['n_pairs_existing'] > 0
            style = self.style.SUCCESS if paired_ok else self.style.WARNING
            self.stdout.write(style('=== RELATÓRIO DE PAREAMENTO ==='))

        self.stdout.write(f'  matrix_id:              {report["matrix_id"]}')
        self.stdout.write(f'  dataset:                {report["matrix_dataset"]}')
        self.stdout.write(f'  camada:                 {report["matrix_layer"]}')
        self.stdout.write(f'  pairing_version:        {report["pairing_version"]}')
        self.stdout.write('')
        self.stdout.write(f'  amostras na matriz:     {report["n_matrix_samples"]}')
        self.stdout.write(f'  case_ids distintos:     {report["n_case_ids_total"]}')
        self.stdout.write('')

        if dry_run:
            self.stdout.write(f'  pares que seriam criados: {report["n_pairs_created"]}')
            self.stdout.write(f'  pares já existentes:      {report["n_pairs_existing"]}')
        else:
            self.stdout.write(f'  pares criados:          {report["n_pairs_created"]}')
            self.stdout.write(f'  pares já existentes:    {report["n_pairs_existing"]}')

        self.stdout.write(f'  unpaired (só tumor):    {report["n_unpaired_tumor_only"]}')
        self.stdout.write(f'  unpaired (só normal):   {report["n_unpaired_normal_only"]}')
        self.stdout.write('')

        # ── Diagnósticos ─────────────────────────────────────────────────────
        self.stdout.write('--- DIAGNÓSTICOS ---')

        multi_tumor = report['multi_aliquota_tumor']
        if multi_tumor:
            self.stdout.write(self.style.ERROR(
                f'  [ATENCAO] case_ids com >1 tumor ({len(multi_tumor)}): '
                f'{", ".join(multi_tumor)}'
            ))
            self.stdout.write(self.style.WARNING(
                '  -> UniqueConstraint(matrix, case_id) NÃO é segura com '
                'multi-alíquota. Sinalize handoff cartografo.'
            ))
        else:
            self.stdout.write('  multi-aliquota tumor:   nenhum (granularidade 1:1 confirmada)')

        multi_normal = report['multi_aliquota_normal']
        if multi_normal:
            self.stdout.write(self.style.ERROR(
                f'  [ATENCAO] case_ids com >1 normal ({len(multi_normal)}): '
                f'{", ".join(multi_normal)}'
            ))
        else:
            self.stdout.write('  multi-aliquota normal:  nenhum (granularidade 1:1 confirmada)')

        chars_missing = report['characteristics_missing']
        if chars_missing:
            self.stdout.write(self.style.WARNING(
                f'  [AVISO] amostras sem characteristics["case_id"] '
                f'({len(chars_missing)}) — fallback de accession usado: '
                f'{", ".join(chars_missing[:5])}'
                + (' ...' if len(chars_missing) > 5 else '')
            ))
        else:
            self.stdout.write(
                '  characteristics[case_id]: presente em 100% das amostras'
            )

        fallback = report['fallback_accession_used']
        if fallback:
            self.stdout.write(self.style.WARNING(
                f'  [AVISO] fallback de accession usado em {len(fallback)} '
                f'amostra(s): {", ".join(fallback[:5])}'
                + (' ...' if len(fallback) > 5 else '')
            ))
        else:
            self.stdout.write(
                '  fallback de accession:  não usado (fonte primária suficiente)'
            )

        self.stdout.write('')

        if dry_run:
            self.stdout.write(self.style.WARNING(
                'Simulação concluída. Nenhum dado foi gravado.\n'
                'Execute sem --dry-run para materializar os pares.'
            ))
        else:
            total = report['n_pairs_created'] + report['n_pairs_existing']
            if total > 0:
                self.stdout.write(self.style.SUCCESS(
                    f'Pareamento concluído. Total de pares ativos: {total} '
                    f'(criados={report["n_pairs_created"]}, '
                    f'existentes={report["n_pairs_existing"]}).'
                ))
            else:
                self.stdout.write(self.style.WARNING(
                    'Nenhum par materializado. Verifique se a matriz tem '
                    'amostras tumor E normal para algum case_id.'
                ))
