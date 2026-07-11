"""
Management command — load_cnv_matrix

Carrega a matriz CNV CPTAC CCRCC (tumor-only, gene×amostra, log-ratio)
como OmicMatrix de forma reprodutível para um projeto DaVinci.

OmnisPathway Objetivo 2, Fase 2, Slice CNV — materialização da matriz.

Fonte (acesso público, sem credencial):
  https://www.linkedomics.org/data_download/CPTAC-CCRCC/
  HS_CPTAC_CCRCC_CNV_gene_Tumor.cct

Schema resultante:
  OmicMatrix.omics_layer='copy_number'
  OmicMatrix.data_format_level='log_ratio'
  OmicMatrix.feature_axis='gene'
  OmicDataset.access_type='public' → Parquet no namespace compartilhado
  Amostras: todas sample_role='tumor' (CNV tumor-only)

Analogia com load_cptac_matrix:
  - --project obrigatório (isolamento por usuário / Regra #3).
  - --async enfileira via Celery; padrão síncrono para bancada de prova.
  - Reporta n_features, n_samples, storage_key (sem vazar caminho físico
    nem credencial — não há credencial neste fluxo).

Uso:
    # Síncrono (bancada de prova, verificação end-to-end imediata)
    .venv/bin/python manage.py load_cnv_matrix --project <UUID>

    # Assíncrono (produção — worker Celery deve estar ativo)
    .venv/bin/python manage.py load_cnv_matrix --project <UUID> --async

Idempotência:
    Rodar duas vezes com o mesmo --project não duplica OmicMatrix nem
    IngestionJob. O segundo run levanta CnvMatrixAlreadyLoadedError ou
    CnvMatrixJobActiveError e o command reporta claramente o estado.

Pré-requisito de slice:
    Slice 2A (GeneRole) deve estar populado antes de rodar a derivação da seed
    CNV (passo seguinte). Este command apenas materializa a OmicMatrix —
    a derivação de VariantEffectSeed é passo posterior (fronteira pendente).

Sensitive-data-handling:
    Nenhuma credencial necessária (dados LinkedOmics públicos). db_url nunca
    logada. storage_key reportada é chave lógica, não caminho físico absoluto.
"""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        'Carrega a matriz CNV CPTAC CCRCC (tumor-only, gene×amostra, log-ratio) '
        'como OmicMatrix. OmnisPathway Obj 2, Fase 2, Slice CNV.'
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
        from apps.core.services.cnv_matrix_load_service import (
            CNV_CCRCC_ACCESSION,
            CNV_URL,
            LOADER_VERSION,
            CnvMatrixAlreadyLoadedError,
            CnvMatrixJobActiveError,
            CnvMatrixLoadService,
        )

        project_uuid = options['project']
        use_async = options['use_async']

        # ── Resolve projeto ───────────────────────────────────────────────────
        try:
            project = DaVinciProject.objects.select_related('user').get(pk=project_uuid)
        except DaVinciProject.DoesNotExist:
            raise CommandError(f'Projeto não encontrado: {project_uuid}')

        self.stdout.write(f'Projeto: {project.title} (id={project.id})')
        self.stdout.write(f'Estudo alvo: {CNV_CCRCC_ACCESSION}')
        self.stdout.write(f'loader_version: {LOADER_VERSION}')
        self.stdout.write(f'Fonte: {CNV_URL}')
        self.stdout.write(
            'Schema: omics_layer=copy_number | data_format_level=log_ratio | '
            'feature_axis=gene | access_type=public (namespace _shared)'
        )

        if use_async:
            # ── Modo assíncrono: apenas enfileira ─────────────────────────────
            self.stdout.write('Modo: assíncrono (Celery)')
            try:
                job = CnvMatrixLoadService.dispatch(project)
                self.stdout.write(self.style.SUCCESS(
                    f'Job enfileirado: {job.id} (status={job.status})\n'
                    f'Acompanhe o progresso via IngestionJob.id={job.id}'
                ))
            except CnvMatrixAlreadyLoadedError as exc:
                self.stdout.write(self.style.WARNING(
                    f'Idempotência: OmicMatrix CNV já existe — {exc}'
                ))
            except CnvMatrixJobActiveError as exc:
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
            service = CnvMatrixLoadService(project)
            result = service.run()
        except CnvMatrixAlreadyLoadedError as exc:
            self.stdout.write(self.style.WARNING(
                f'Idempotência: OmicMatrix CNV já existe para esta natural key.\n'
                f'  {exc}\n'
                f'  Nenhuma ação necessária.'
            ))
            return
        except CnvMatrixJobActiveError as exc:
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
            raise CommandError(f'Carga CNV falhou: {exc}') from exc

        # ── Relatório final (sem vazar caminho físico nem credencial) ─────────
        self.stdout.write(self.style.SUCCESS('Carga CNV concluída com sucesso.'))
        self.stdout.write(f'  job_id:       {result["job_id"]}')
        self.stdout.write(f'  storage_key:  {result["storage_key"]}')
        self.stdout.write(f'  n_features:   {result["n_features"]}')
        self.stdout.write(f'  n_samples:    {result["n_samples"]}')
        self.stdout.write(f'  checksum_md5: {result["checksum_md5"]}')
        self.stdout.write(
            f'\nOmicMatrix (copy_number/log_ratio/gene) e OmicMatrixSample '
            f'populados no banco.\n'
            f'Parquet disponível via default_storage[{result["storage_key"]}].\n'
            f'\nPróximo passo: derivar VariantEffectSeed (seed CNV v1) — '
            f'requer GeneRole populado (load_gene_roles) e é passo separado '
            f'(fronteira Rust/Django pendente de decisão).'
        )
