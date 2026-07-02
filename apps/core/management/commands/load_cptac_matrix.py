"""
Management command — load_cptac_matrix

Carrega a matriz de proteômica CPTAC CCRCC (tumor vs normal) de forma
reprodutível para um projeto DaVinci.

Fonte (acesso público, sem credencial):
  tumor:  https://www.linkedomics.org/data_download/CPTAC-CCRCC/HS_CPTAC_CCRCC_proteome_Tumor.cct
  normal: https://www.linkedomics.org/data_download/CPTAC-CCRCC/HS_CPTAC_CCRCC_proteome_Normal.cct

Analogia com export_catalog:
  - --project é obrigatório (isolamento por usuário / Regra #3).
  - --async enfileira via Celery; padrão síncrono para bancada de prova.
  - Reporta n_features, n_samples, roles, storage_key (sem vazar caminho
    físico nem credencial).

Uso:
    # Síncrono (bancada de prova, verificação end-to-end imediata)
    .venv/bin/python manage.py load_cptac_matrix --project <UUID>

    # Assíncrono (produção — worker Celery deve estar ativo)
    .venv/bin/python manage.py load_cptac_matrix --project <UUID> --async

Idempotência:
    Rodar duas vezes com o mesmo --project não duplica OmicMatrix nem
    IngestionJob.  O segundo run levanta MatrixAlreadyLoadedError ou
    MatrixJobActiveError e o command reporta claramente o estado.

Handoffs pendentes (não fazer agora):
  - Endpoint HTTP POST /projects/{pk}/datasets/load-matrix/ e status:
    decisão de escopo — prematuro sem datasets de catálogo.  Registrar
    como item futuro ao fechar a Fase 0.
  - source_db='tcga' é approximação temporária — cartografo deve adicionar
    SourceDB.CPTAC = 'cptac' via migration aditiva antes da Fase 1.
"""

import os

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        'Carrega a matriz proteômica CPTAC CCRCC (tumor vs normal) para um projeto. '
        'OmnisPathway Obj 2, Fase 0.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--project',
            metavar='UUID',
            required=True,
            help='UUID do DaVinciProject alvo (obrigatório — isolamento por projeto).',
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
        from apps.core.services.matrix_load_service import (
            CPTAC_CCRCC_ACCESSION,
            LOADER_VERSION,
            MatrixAlreadyLoadedError,
            MatrixJobActiveError,
            MatrixLoadService,
        )

        project_uuid = options['project']
        use_async = options['use_async']

        # ── Resolve projeto ───────────────────────────────────────────────────
        try:
            project = DaVinciProject.objects.select_related('user').get(pk=project_uuid)
        except DaVinciProject.DoesNotExist:
            raise CommandError(f'Projeto não encontrado: {project_uuid}')

        self.stdout.write(
            f'Projeto: {project.title} (id={project.id})'
        )
        self.stdout.write(
            f'Estudo alvo: {CPTAC_CCRCC_ACCESSION} | loader_version={LOADER_VERSION}'
        )

        if use_async:
            # ── Modo assíncrono: apenas enfileira ─────────────────────────────
            self.stdout.write('Modo: assíncrono (Celery)')
            try:
                service = MatrixLoadService(project)
                job = MatrixLoadService.dispatch(project)
                self.stdout.write(self.style.SUCCESS(
                    f'Job enfileirado: {job.id} (status={job.status})\n'
                    f'Acompanhe o progresso via IngestionJob.id={job.id}'
                ))
            except MatrixAlreadyLoadedError as exc:
                self.stdout.write(self.style.WARNING(
                    f'Idempotência: OmicMatrix já existe — {exc}'
                ))
            except MatrixJobActiveError as exc:
                self.stdout.write(self.style.WARNING(
                    f'Idempotência: job ativo encontrado — {exc}'
                ))
            return

        # ── Modo síncrono: executa end-to-end ─────────────────────────────────
        self.stdout.write('Modo: síncrono (aguarde — download pode demorar)')
        self.stdout.write(
            'Atenção: rust_engine deve estar compilado '
            '(`maturin develop --release`)'
        )

        try:
            service = MatrixLoadService(project)
            result = service.run()
        except MatrixAlreadyLoadedError as exc:
            self.stdout.write(self.style.WARNING(
                f'Idempotência: OmicMatrix já existe para esta natural key.\n'
                f'  {exc}\n'
                f'  Nenhuma ação necessária.'
            ))
            return
        except MatrixJobActiveError as exc:
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
            raise CommandError(f'Carga falhou: {exc}') from exc

        # ── Relatório final (sem vazar caminho físico nem credencial) ─────────
        self.stdout.write(self.style.SUCCESS('Carga concluída com sucesso.'))
        self.stdout.write(f'  job_id:          {result["job_id"]}')
        self.stdout.write(f'  storage_key:     {result["storage_key"]}')
        self.stdout.write(f'  n_features:      {result["n_features"]}')
        self.stdout.write(f'  n_samples:       {result["n_samples"]}')
        self.stdout.write(f'  checksum_md5:    {result["checksum_md5"]}')
        self.stdout.write(f'  genes_discarded: {result["genes_discarded"]}')
        self.stdout.write(f'  roles:')
        for role, count in result['roles'].items():
            self.stdout.write(f'    {role}: {count}')
        self.stdout.write(
            f'\nOmicMatrix e OmicMatrixSample populados no banco.\n'
            f'Parquet disponível via default_storage[{result["storage_key"]}].'
        )
