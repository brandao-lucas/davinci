"""
Management command — diagnose_seed_connectivity

Checagem PRÉ-EXECUÇÃO que responde "vale a pena rodar o PFS nesta bancada?"
em segundos — ANTES de gastar um run inteiro (RWR + permutação + FDR) para
descobrir isso pelo resultado. OmnisPathway Obj 2, Fase 4.

Motivação (registrada, não hipotética): numa bancada real, o motor PFS
produziu resultado fraco e a causa só apareceu no FIM da investigação —
VHL (driver de ~90% dos carcinomas renais, 219 sementes) é NÓ ISOLADO no
KEGG, sem nenhuma aresta de saída. Uma semente sobre um nó sem saída não
propaga; o escore não tem substrato para detectar nada. Medido
sistematicamente, o caso VHL revelou-se regra, não exceção (~45% dos nós-
gene semeados nas 372 vias sem saída). Hoje essa informação só existe
depois de rodar `score_pathways` + `apply_pathway_fdr` e olhar
`report_pathway_activity` — tarde demais para decidir se vale a pena rodar.

A métrica é o GRAU DE SAÍDA (`out_degree`), não o grau total: a semente
propaga PARA FORA do nó. Um nó com entradas mas zero saídas (ex.: HIF1A em
hsa04066) é tão inútil para semeadura quanto um nó totalmente isolado.

Três categorias (mutuamente exclusivas) por nó-gene semeado:
  sem saída   out_degree == 0                        — não propaga.
  saída cega  out_degree > 0, todas as arestas sign=0 — propaga sem
                                                          direção.
  útil        >=1 aresta de saída com sign != 0       — substrato real.

Seções emitidas:
  Global      totais e percentuais das três categorias.
  Por via     mesma quebra, piores primeiro (maior fração sem saída),
              com contagem de sementes envolvidas — onde o run será estéril.
  Por gene    genes semeados isolados em TODAS as vias onde aparecem,
              ordenados por nº de sementes — lista acionável (VHL, PBRM1,
              BAP1, …).
  Veredito    se a fração "útil" cai abaixo do patamar, avisa
              explicitamente que a execução tende a produzir resultado
              degenerado, e por quê.

Sementes só DIRECIONAIS (`direction != 'neutral'`) contam — neutras não
semeiam. Junção semente↔nó por `gene_symbol` UPPERCASE nos dois lados.
Isolamento por projeto: `VariantEffectSeed` não tem FK de projeto — o
escopo vem de `matrix -> dataset -> ProjectDataset(project=...,
curation_status ativo)`, mesmo padrão de `PathwayScoringService`.

Uso:
    # Texto no stdout (padrão) — versões direcionais atuais, todas as vias
    .venv/bin/python manage.py diagnose_seed_connectivity --project <UUID>

    # Restringe a vias específicas
    .venv/bin/python manage.py diagnose_seed_connectivity --project <UUID> \\
        --pathways hsa04066 hsa04150

    # Versões de seed customizadas (CSV ou múltiplos args, mesmo formato de
    # score_pathways/load_regulons)
    .venv/bin/python manage.py diagnose_seed_connectivity --project <UUID> \\
        --seed-method-versions fase2-cnv-v2,fase2-snv-v1,fase2-cnv-v1

    # JSON, gravado em diagnostics/exports/ (gitignored)
    .venv/bin/python manage.py diagnose_seed_connectivity --project <UUID> \\
        --format json

    # Top 50 vias/genes em vez do padrão (20)
    .venv/bin/python manage.py diagnose_seed_connectivity --project <UUID> --top 50
"""

from django.core.management.base import BaseCommand, CommandError

from apps.core.services.seed_connectivity_service import DEFAULT_SEED_METHOD_VERSIONS


class Command(BaseCommand):
    help = (
        'Diagnóstico PRÉ-EXECUÇÃO de conectividade de semente: classifica '
        'cada nó-gene semeado em sem-saída / saída-cega / útil (grau de '
        'SAÍDA, não total) e avisa se a bancada tende a produzir resultado '
        'degenerado — ANTES de rodar score_pathways/apply_pathway_fdr. '
        'OmnisPathway Obj 2, Fase 4.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--project',
            metavar='UUID',
            required=True,
            help='UUID do DaVinciProject alvo (obrigatório — isolamento por projeto).',
        )
        parser.add_argument(
            '--seed-method-versions',
            metavar='VERSIONS',
            nargs='+',
            default=None,
            dest='seed_method_versions',
            help=(
                'Versões de VariantEffectSeed consideradas. Aceita múltiplos '
                'argumentos e/ou lista separada por vírgula (mesmo formato '
                'de --pathways). Padrão: as direcionais atuais '
                f'({",".join(DEFAULT_SEED_METHOD_VERSIONS)}).'
            ),
        )
        parser.add_argument(
            '--pathways',
            metavar='KEGG_ID',
            nargs='+',
            default=None,
            help=(
                'Restringe a uma ou mais vias KEGG. Aceita múltiplos '
                'argumentos (--pathways hsa04151 hsa04010), lista separada '
                'por vírgula ou uma mistura dos dois. Padrão: TODAS as vias '
                'carregadas.'
            ),
        )
        parser.add_argument(
            '--top',
            metavar='N',
            type=int,
            default=20,
            help='Quantos genes/vias listar nas seções "por via" e "por gene" (padrão: 20).',
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
                '(gitignored) com nome derivado do projeto + timestamp. '
                'Ignorado com --format text (vai para stdout).'
            ),
        )

    def _resolve_csv_list(self, tokens):
        if not tokens:
            return None
        raw = []
        for token in tokens:
            raw.extend(p.strip() for p in token.split(',') if p.strip())
        seen = set()
        out = []
        for item in raw:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out or None

    def handle(self, *args, **options):
        from apps.core.models import DaVinciProject
        from apps.core.services.seed_connectivity_service import (
            SeedConnectivityEmptyError,
            build_report,
            render_text,
            write_json,
        )

        project_uuid = options['project']
        seed_method_versions = self._resolve_csv_list(options['seed_method_versions'])
        pathway_ids = self._resolve_csv_list(options['pathways'])
        top = options['top']
        output_format = options['output_format']
        output_path = options['output_path']

        try:
            project = DaVinciProject.objects.get(pk=project_uuid)
        except DaVinciProject.DoesNotExist:
            raise CommandError(f'Projeto não encontrado: {project_uuid}')

        try:
            report = build_report(
                project,
                seed_method_versions=seed_method_versions,
                pathway_ids=pathway_ids,
                top=top,
            )
        except SeedConnectivityEmptyError as exc:
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
                saved_path = write_json(report, project.id)

            self.stdout.write(self.style.SUCCESS(f'Diagnóstico JSON gravado em: {saved_path}'))
            return

        self.stdout.write(render_text(report))
