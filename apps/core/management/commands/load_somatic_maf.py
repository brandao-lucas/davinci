"""
Management command: load_somatic_maf

Orquestra a carga do MAF somático GDC CPTAC-3 Kidney e a derivação de seeds SNV
(OmnisPathway Objetivo 2, Fase 2, Passo 2.6 — Slice 2D).

Fluxo único:
  1. Pré-checks: GeneRole(source='oncokb') populado + VariantEffectResolved populado.
  2. Resolve file_map via GDC API pública (sem credencial).
  3. Chama rust_engine.load_somatic_maf (Rust: fetch MAF em lote + Parquet burden).
  4. Upload Parquet + ORM set-based (OmicMatrix/OmicSample/OmicMatrixSample).
  5. Deriva VariantEffectSeed SNV set-based (lê TSV de ocorrências).

Uso:
    .venv/bin/python manage.py load_somatic_maf --project <project_id>
    .venv/bin/python manage.py load_somatic_maf --project <project_id> --async

Opções:
    --project   UUID do DaVinciProject (obrigatório)
    --async     Enfileira como task Celery em vez de executar sincronamente

Saída (stdout):
    n_files          Arquivos MAF processados pelo Rust
    n_samples        Amostras únicas na matriz somática
    n_occurrences    Ocorrências (variante×amostra) lidas do TSV
    n_seeds_created  Seeds SNV materializadas em VariantEffectSeed
    n_inactivator    Seeds com direction=inactivator
    n_activator      Seeds com direction=activator
    n_neutral        Seeds com direction=neutral
    n_truncating_lof     Ocorrências truncantes (LoF estrutural direto)
    n_missense_resolved  Ocorrências missense com VariantEffectResolved encontrado
    n_unannotated_skipped Ocorrências missense sem anotação (puladas)
    storage_key          Chave lógica do Parquet no object storage
    gdc_files_found      Arquivos retornados pela GDC API
    gdc_cases_found      Casos únicos retornados pela GDC API

Sensitive-data-handling:
    db_url nunca logada nem exibida. storage_key é chave lógica (sem caminho físico).
    GDC API é pública (sem credencial, sem token).
"""

import uuid

from django.core.management.base import BaseCommand, CommandError

from apps.core.models import DaVinciProject
from apps.core.services.somatic_maf_service import (
    GdcApiError,
    SomaticMafAlreadyLoadedError,
    SomaticMafGeneRoleEmptyError,
    SomaticMafJobActiveError,
    SomaticMafService,
    SomaticMafVariantEffectResolvedEmptyError,
)


class Command(BaseCommand):
    help = (
        'Carrega o MAF somático GDC CPTAC-3 Kidney e deriva seeds SNV '
        '(OmnisPathway Obj 2, Fase 2, Passo 2.6).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--project',
            type=str,
            required=True,
            help='UUID do DaVinciProject.',
        )
        parser.add_argument(
            '--async',
            dest='async_mode',
            action='store_true',
            default=False,
            help=(
                'Enfileira como task Celery (run_somatic_maf_load) em vez de '
                'executar sincronamente. Requer worker Celery ativo.'
            ),
        )

    def handle(self, *args, **options):
        project_id = options['project']
        async_mode = options['async_mode']

        # ── Resolve o projeto ─────────────────────────────────────────────────
        try:
            uuid.UUID(project_id)
        except ValueError:
            raise CommandError(f'--project deve ser um UUID válido. Recebido: {project_id!r}')

        try:
            project = DaVinciProject.objects.select_related('user').get(id=project_id)
        except DaVinciProject.DoesNotExist:
            raise CommandError(f'DaVinciProject não encontrado: {project_id}')

        self.stdout.write(
            f'Projeto: {project.id} ({project.user.email if project.user else "sem user"})'
        )
        self.stdout.write(
            f'Modo: {"async (Celery)" if async_mode else "sync (síncrono)"}'
        )

        # ── Executa o serviço ─────────────────────────────────────────────────
        service = SomaticMafService(project)

        if async_mode:
            try:
                job = SomaticMafService.dispatch(project)
            except SomaticMafGeneRoleEmptyError as exc:
                raise CommandError(str(exc))
            except SomaticMafVariantEffectResolvedEmptyError as exc:
                raise CommandError(str(exc))
            except SomaticMafAlreadyLoadedError as exc:
                self.stdout.write(
                    self.style.WARNING(f'Idempotência: {exc}')
                )
                return
            except SomaticMafJobActiveError as exc:
                self.stdout.write(
                    self.style.WARNING(f'Idempotência: {exc}')
                )
                return
            except GdcApiError as exc:
                raise CommandError(f'Erro GDC API: {exc}')

            self.stdout.write(
                self.style.SUCCESS(
                    f'SOMATIC_MAF_LOAD enfileirado (job {job.id}). '
                    f'Acompanhe via IngestionJob.'
                )
            )
            return

        # Modo síncrono
        self.stdout.write('Iniciando carga síncrona...')

        try:
            result = service.run()
        except SomaticMafGeneRoleEmptyError as exc:
            raise CommandError(str(exc))
        except SomaticMafVariantEffectResolvedEmptyError as exc:
            raise CommandError(str(exc))
        except SomaticMafAlreadyLoadedError as exc:
            self.stdout.write(self.style.WARNING(f'Idempotência: {exc}'))
            return
        except SomaticMafJobActiveError as exc:
            self.stdout.write(self.style.WARNING(f'Idempotência: {exc}'))
            return
        except GdcApiError as exc:
            raise CommandError(f'Erro GDC API: {exc}')
        except Exception as exc:
            raise CommandError(f'Erro durante a carga: {exc}')

        # ── Exibe o relatório ─────────────────────────────────────────────────
        self.stdout.write(self.style.SUCCESS('\nSOMATIC_MAF_LOAD concluido'))
        self.stdout.write(f'  job_id               : {result["job_id"]}')
        self.stdout.write(f'  matrix_id            : {result["matrix_id"]}')
        self.stdout.write(f'  storage_key          : {result["storage_key"]}')
        self.stdout.write(f'  gdc_files_found      : {result["gdc_files_found"]}')
        self.stdout.write(f'  gdc_cases_found      : {result["gdc_cases_found"]}')
        self.stdout.write(f'  n_files              : {result["n_files_processed"]}')
        self.stdout.write(f'  n_samples            : {result["n_samples"]}')
        self.stdout.write(f'  n_occurrences        : {result["n_occurrences"]}')
        self.stdout.write(f'  n_seeds_created      : {result["n_seeds_created"]}')
        self.stdout.write(f'  n_inactivator        : {result["n_inactivator"]}')
        self.stdout.write(f'  n_activator          : {result["n_activator"]}')
        self.stdout.write(f'  n_neutral            : {result["n_neutral"]}')
        self.stdout.write(f'  n_truncating_lof     : {result["n_truncating_lof"]}')
        self.stdout.write(f'  n_missense_resolved  : {result["n_missense_resolved"]}')
        self.stdout.write(f'  n_unannotated_skipped: {result["n_unannotated_skipped"]}')
        self.stdout.write(
            f'  n_skipped_synonymous : {result["n_variants_skipped_synonymous"]}'
        )
        self.stdout.write(
            f'  n_skipped_offlist    : {result["n_variants_skipped_offlist"]}'
        )

        if result.get('errors'):
            self.stdout.write(
                self.style.WARNING(f'\nErros nao-fatais ({len(result["errors"])}):')
            )
            for err in result['errors'][:10]:
                self.stdout.write(f'  - {err}')
            if len(result['errors']) > 10:
                self.stdout.write(
                    f'  ... e mais {len(result["errors"]) - 10} erros (ver IngestionJob)'
                )
