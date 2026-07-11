"""
Management command — load_gene_roles

Popula o catálogo global GeneRole via OncoKB cancerGeneList.txt (TOKEN-FREE).

OmnisPathway Objetivo 2, Fase 2, Slice 2A.

Fonte:
  https://www.oncokb.org/api/v1/utils/cancerGeneList.txt
  Campo Gene Type resolve papel (oncogene/tsg) sem chamar a API.
  Arquivo traz CGC v99 e Vogelstein bundled.
  Acesso público TOKEN-FREE — nenhuma credencial necessária.

GeneRole é catálogo GLOBAL de referência (sem FK de projeto). O --project
é usado apenas para tracking (FK obrigatória do IngestionJob); a carga
atualiza o catálogo global independente do projeto.

Analogia com load_cptac_matrix:
  - --project obrigatório (tracking do job / Regra #3).
  - --async enfileira via Celery; padrão síncrono para bancada de prova.
  - Reporta n_genes/n_oncogene/n_tsg/n_dual/n_neither/n_unknown/source_version
    e n_upserted. Nunca vaza credencial (não há credencial neste fluxo).

Uso:
    # Síncrono (bancada de prova)
    .venv/bin/python manage.py load_gene_roles --project <UUID>

    # Assíncrono (produção — worker Celery deve estar ativo)
    .venv/bin/python manage.py load_gene_roles --project <UUID> --async

Idempotência:
    Gate verifica job ativo (PENDING/RUNNING) — não duplica job concorrente.
    O Rust usa UPSERT ON CONFLICT (gene_symbol, source) DO UPDATE —
    re-rodar após conclusão atualiza os registros sem duplicar.

Sensitive-data-handling:
    Nenhuma credencial envolvida neste fluxo. db_url nunca é logada.
    URL da fonte é pública — impressa no relatório para auditoria.
"""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        'Popula catálogo global GeneRole via OncoKB cancerGeneList.txt (TOKEN-FREE). '
        'OmnisPathway Obj 2, Fase 2, Slice 2A.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--project',
            metavar='UUID',
            required=True,
            help=(
                'UUID do DaVinciProject para tracking do job (obrigatório — '
                'FK do IngestionJob). A carga atualiza o catálogo global GeneRole.'
            ),
        )
        parser.add_argument(
            '--async',
            action='store_true',
            default=False,
            dest='use_async',
            help=(
                'Enfileira a carga via Celery (worker deve estar ativo). '
                'Padrão: execução síncrona (bancada de prova).'
            ),
        )

    def handle(self, *args, **options):
        from apps.core.models import DaVinciProject
        from apps.core.services.gene_role_load_service import (
            LOADER_VERSION,
            ONCOKB_GENE_LIST_URL,
            GeneRoleJobActiveError,
            GeneRoleLoadService,
        )

        project_uuid = options['project']
        use_async = options['use_async']

        # ── Resolve projeto ───────────────────────────────────────────────────
        try:
            project = DaVinciProject.objects.select_related('user').get(pk=project_uuid)
        except DaVinciProject.DoesNotExist:
            raise CommandError(f'Projeto não encontrado: {project_uuid}')

        self.stdout.write(f'Projeto (tracking): {project.title} (id={project.id})')
        self.stdout.write(f'Fonte: {ONCOKB_GENE_LIST_URL}')
        self.stdout.write(f'loader_version: {LOADER_VERSION}')
        self.stdout.write(
            'Catálogo alvo: GeneRole (global — não filtrado por projeto)'
        )

        if use_async:
            # ── Modo assíncrono: apenas enfileira ─────────────────────────────
            self.stdout.write('Modo: assíncrono (Celery)')
            try:
                job = GeneRoleLoadService.dispatch(project)
                self.stdout.write(self.style.SUCCESS(
                    f'Job enfileirado: {job.id} (status={job.status})\n'
                    f'Acompanhe o progresso via IngestionJob.id={job.id}'
                ))
            except GeneRoleJobActiveError as exc:
                self.stdout.write(self.style.WARNING(
                    f'Idempotência: job ativo encontrado — {exc}'
                ))
            return

        # ── Modo síncrono: executa end-to-end ─────────────────────────────────
        self.stdout.write('Modo: síncrono (aguarde — download e UPSERT em andamento)')
        self.stdout.write(
            'Atenção: rust_engine deve estar compilado '
            '(`maturin develop --release`)'
        )

        try:
            service = GeneRoleLoadService(project)
            result = service.run()
        except GeneRoleJobActiveError as exc:
            self.stdout.write(self.style.WARNING(
                f'Idempotência: job ativo encontrado.\n'
                f'  {exc}\n'
                f'  Aguarde o job concluir ou cancele-o manualmente.'
            ))
            return
        except ImportError:
            raise CommandError(
                'rust_engine não encontrado. Compile com: maturin develop --release'
            )
        except Exception as exc:
            raise CommandError(f'Carga de GeneRole falhou: {exc}') from exc

        # ── Relatório final (sem vazar credencial — não há nenhuma neste fluxo) ─
        self.stdout.write(self.style.SUCCESS('Carga de GeneRole concluída.'))
        self.stdout.write(f'  job_id:         {result["job_id"]}')
        self.stdout.write(f'  source_version: {result["source_version"]}')
        self.stdout.write(f'  n_genes:        {result["n_genes"]}')
        self.stdout.write(f'  n_oncogene:     {result["n_oncogene"]}')
        self.stdout.write(f'  n_tsg:          {result["n_tsg"]}')
        self.stdout.write(f'  n_dual:         {result["n_dual"]}')
        self.stdout.write(f'  n_neither:      {result["n_neither"]}')
        self.stdout.write(f'  n_unknown:      {result["n_unknown"]}')
        self.stdout.write(f'  n_upserted:     {result["n_upserted"]}')
        self.stdout.write(
            f'\nCatálogo GeneRole populado/atualizado no banco.\n'
            f'Fonte consultada: {ONCOKB_GENE_LIST_URL}'
        )
