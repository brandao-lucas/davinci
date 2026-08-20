"""
Management command — report_pathway_activity

Emite os números do PFS (OmnisPathway Obj 2, Fase 4) de forma REPRODUTÍVEL —
substitui as consultas ad-hoc digitadas no shell que produziam o relatório
até agora.

NOMENCLATURA — leia antes de interpretar a saída: este relatório usa
"atribuível" / "não atribuível", NUNCA "ativa/inativa". O PFS não mede se a
via está biologicamente ligada — mede se a alteração genômica do paciente
EXPLICA a leitura proteômica observada melhor que o acaso (permutação da
posição da semente no grafo). "Atribuível" = há pelo menos um paciente em
que essa explicação passa no teste de significância (FDR, escopo
populacional); "não atribuível" = a via foi testada mas nunca passou.
Ver docstring de `apps.core.services.pathway_report_service`.

Seções emitidas:
  Resumo         total de pares, avaliáveis, degenerados, B efetivo.
  Taxonomia      atribuível / não atribuível / bloqueada-sem-semente /
                 bloqueada-sem-readout — soma o total de vias do catálogo.
  Populacional   por via atribuível: pacientes atribuíveis, avaliáveis,
                 z mediano, q mínimo, fração de arestas sem sinal.
  Individual     por paciente: nº de vias abaixo do limiar.
  Sensibilidade  pares significativos (escopo populacional) a q<0,05 /
                 0,01 / 0,001.
  Agrupamento    pacientes com mais de uma via atribuível, pares de vias
                 co-ocorrentes.

Pré-requisito: rode `apply_pathway_fdr` para este `method_version` antes —
sem isso `q_value_across_samples`/`q_value_across_pathways` estão todos
NULL e as seções Populacional/Individual/Sensibilidade/Agrupamento saem
vazias (não é erro; é reflexo fiel do estado dos dados).

Uso:
    # Texto no stdout (padrão)
    .venv/bin/python manage.py report_pathway_activity \\
        --method-version fase4-pfs-v2-372vias

    # JSON, gravado em diagnostics/exports/ (gitignored)
    .venv/bin/python manage.py report_pathway_activity \\
        --method-version fase4-pfs-v2-372vias --format json

    # Limiar de significância customizado
    .venv/bin/python manage.py report_pathway_activity \\
        --method-version fase4-pfs-v2-372vias --threshold 0.01
"""

from django.core.management.base import BaseCommand, CommandError

from apps.core.services.pathway_report_service import (
    PathwayReportEmptyError,
    build_report,
    render_text,
    write_json,
)


class Command(BaseCommand):
    help = (
        'Emite o relatório reprodutível do PFS (resumo, taxonomia '
        'atribuível/não atribuível, populacional, individual, '
        'sensibilidade, agrupamento) para um method_version. '
        'OmnisPathway Obj 2, Fase 4.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--method-version',
            metavar='VERSION',
            required=True,
            dest='method_version',
            help='method_version de PathwayActivityScore a reportar (obrigatório).',
        )
        parser.add_argument(
            '--threshold',
            metavar='FLOAT',
            type=float,
            default=0.05,
            help='Limiar de significância do q-valor (padrão: 0.05).',
        )
        parser.add_argument(
            '--format',
            metavar='FORMAT',
            default='text',
            choices=['text', 'json'],
            dest='output_format',
            help='Formato de saída: text (stdout, padrão) ou json.',
        )
        parser.add_argument(
            '--output',
            metavar='PATH',
            default=None,
            dest='output_path',
            help=(
                'Com --format json: caminho explícito do arquivo. Sem esta '
                'flag, grava em settings.REPO_ROOT/diagnostics/exports/ '
                '(gitignored) com nome derivado do method_version + '
                'timestamp. Ignorado com --format text (vai para stdout).'
            ),
        )

    def handle(self, *args, **options):
        method_version = options['method_version']
        threshold = options['threshold']
        output_format = options['output_format']
        output_path = options['output_path']

        try:
            report = build_report(method_version=method_version, threshold=threshold)
        except PathwayReportEmptyError as exc:
            raise CommandError(str(exc)) from exc
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        if output_format == 'json':
            import json as _json

            if output_path:
                with open(output_path, 'w', encoding='utf-8') as f:
                    _json.dump(report, f, ensure_ascii=False, indent=2)
                saved_path = output_path
            else:
                saved_path = write_json(report, method_version)

            self.stdout.write(self.style.SUCCESS(f'Relatório JSON gravado em: {saved_path}'))
            return

        self.stdout.write(render_text(report))
