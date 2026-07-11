"""
Management command — derive_cnv_seeds

Deriva VariantEffectSeed a partir da matriz CNV CPTAC CCRCC carregada
(OmicMatrix copy_number), usando a heurística dosagem × papel do gene
(seed CNV v1 heurístico, sem estado conjunto CNV×alelo).

OmnisPathway Objetivo 2, Fase 2, Slice CNV-seed (derivação).

Pré-requisitos (em ordem obrigatória):
  1. load_gene_roles  — popula GeneRole (papel: oncogene/tsg/...).
  2. load_cnv_matrix  — cria OmicMatrix(copy_number) e OmicMatrixSample.
  3. derive_cnv_seeds — este command. Rust lê o Parquet, resolve o sinal
                        (dosagem×papel) e faz COPY UPSERT em VariantEffectSeed.

Contrato Rust (ferris, pronto):
  rust_engine.seed_cnv_from_parquet(
      parquet_path: str,
      matrix_id: int,
      db_url: str,
      method_version: str = 'fase2-cnv-v1',
      gain_threshold: float = 0.3,
      loss_threshold: float = -0.3,
  ) -> CnvSeedManifest {
      n_seeds_written, n_activator, n_inactivator, n_neutral_skipped,
      n_genes_with_role, n_genes_skipped_no_role,
      n_cells_subthreshold, n_cells_null
  }

  O Rust:
    - Lê o Parquet LOCAL (Django baixa do object storage e passa o caminho).
    - Consulta GeneRole + OmicMatrixSample via db_url.
    - Faz COPY UPSERT em VariantEffectSeed (idempotente — natural key
      matrix/sample/gene_symbol/variant_key/evidence_type).
    - amplificação (log-ratio >= gain_threshold) + oncogene → activator
    - deleção (log-ratio <= loss_threshold) + tsg → inactivator
    - demais / sem papel → neutral (não inventa direção)

Isolamento (Regra #3):
  --project obrigatório: isola a OmicMatrix ao projeto do usuário.
  A matriz é resolvida via ProjectDataset (curation_status ativo) — sem
  acesso cross-project.

Idempotência:
  O Rust faz UPSERT na natural key. Rodar 2x com a mesma matriz produz
  o mesmo resultado sem duplicar seeds. Um job COMPLETED não bloqueia re-seed.
  Um job PENDING/RUNNING bloqueia (idempotência de concorrência).

Sensitive-data-handling:
  - db_url nunca logada nem impressa.
  - storage_key não é exibida no relatório (caminho lógico interno).
  - Não há credencial neste fluxo (LinkdOmics público, OncoKB token-free).

Uso:
    # Síncrono (bancada de prova, verificação end-to-end imediata)
    .venv/bin/python manage.py derive_cnv_seeds --project <UUID>

    # Síncrono com matrix específica
    .venv/bin/python manage.py derive_cnv_seeds --project <UUID> --matrix <ID>

    # Síncrono com thresholds customizados
    .venv/bin/python manage.py derive_cnv_seeds --project <UUID> \\
        --gain-threshold 0.5 --loss-threshold -0.5

    # Assíncrono (produção — worker Celery deve estar ativo)
    .venv/bin/python manage.py derive_cnv_seeds --project <UUID> --async

Pipeline completo dos 3 commands (em ordem):
    .venv/bin/python manage.py load_gene_roles --project <UUID>
    .venv/bin/python manage.py load_cnv_matrix --project <UUID>
    .venv/bin/python manage.py derive_cnv_seeds --project <UUID>
"""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        'Deriva VariantEffectSeed a partir da matriz CNV (dosagem × papel do gene). '
        'OmnisPathway Obj 2, Fase 2, Slice CNV-seed. '
        'Pré-requisitos: load_gene_roles + load_cnv_matrix.'
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
            dest='matrix_id',
            help=(
                'ID específico da OmicMatrix CNV a usar. '
                'Se omitido, auto-detecta a única matriz copy_number do projeto.'
            ),
        )
        parser.add_argument(
            '--async',
            action='store_true',
            default=False,
            dest='use_async',
            help=(
                'Enfileira a derivação via Celery (worker deve estar ativo). '
                'Padrão: execução síncrona (bancada de prova).'
            ),
        )
        parser.add_argument(
            '--gain-threshold',
            metavar='FLOAT',
            type=float,
            default=0.3,
            dest='gain_threshold',
            help=(
                'Log-ratio mínimo para considerar amplificação (default: 0.3). '
                'Valores abaixo deste e acima de --loss-threshold são "subthreshold" '
                '(neutro/skip). Repassado ao Rust.'
            ),
        )
        parser.add_argument(
            '--loss-threshold',
            metavar='FLOAT',
            type=float,
            default=-0.3,
            dest='loss_threshold',
            help=(
                'Log-ratio máximo para considerar deleção (default: -0.3). '
                'Deve ser negativo. Repassado ao Rust.'
            ),
        )

    def handle(self, *args, **options):
        from apps.core.models import DaVinciProject
        from apps.core.services.cnv_seed_service import (
            METHOD_VERSION,
            CnvSeedGeneRoleEmptyError,
            CnvSeedJobActiveError,
            CnvSeedMatrixNotFoundError,
            CnvSeedService,
        )

        project_uuid = options['project']
        matrix_id = options['matrix_id']
        use_async = options['use_async']
        gain_threshold = options['gain_threshold']
        loss_threshold = options['loss_threshold']

        # Validação básica dos thresholds
        if loss_threshold >= 0:
            raise CommandError(
                f'--loss-threshold deve ser negativo (recebido: {loss_threshold}).'
            )
        if gain_threshold <= 0:
            raise CommandError(
                f'--gain-threshold deve ser positivo (recebido: {gain_threshold}).'
            )

        # ── Resolve projeto ───────────────────────────────────────────────────
        try:
            project = DaVinciProject.objects.select_related('user').get(pk=project_uuid)
        except DaVinciProject.DoesNotExist:
            raise CommandError(f'Projeto não encontrado: {project_uuid}')

        self.stdout.write(f'Projeto: {project.title} (id={project.id})')
        self.stdout.write(f'Método: {METHOD_VERSION}')
        self.stdout.write(f'Thresholds: gain>={gain_threshold}, loss<={loss_threshold}')

        if matrix_id:
            self.stdout.write(f'Matriz CNV alvo (--matrix): id={matrix_id}')
        else:
            self.stdout.write('Matriz CNV: auto-detecção (única copy_number do projeto)')

        if use_async:
            # ── Modo assíncrono: apenas enfileira ─────────────────────────────
            self.stdout.write('Modo: assíncrono (Celery)')
            try:
                job = CnvSeedService.dispatch(
                    project,
                    matrix_id=matrix_id,
                    gain_threshold=gain_threshold,
                    loss_threshold=loss_threshold,
                )
                self.stdout.write(self.style.SUCCESS(
                    f'Job enfileirado: {job.id} (status={job.status})\n'
                    f'Acompanhe o progresso via IngestionJob.id={job.id}'
                ))
            except CnvSeedMatrixNotFoundError as exc:
                raise CommandError(
                    f'Matriz CNV não encontrada: {exc}\n'
                    f'Execute load_cnv_matrix --project {project_uuid} primeiro.'
                )
            except CnvSeedGeneRoleEmptyError as exc:
                raise CommandError(
                    f'Pré-check falhou: {exc}\n'
                    f'Execute load_gene_roles --project {project_uuid} primeiro.'
                )
            except CnvSeedJobActiveError as exc:
                self.stdout.write(self.style.WARNING(
                    f'Idempotência: job ativo encontrado — {exc}\n'
                    f'Aguarde o job concluir ou cancele-o manualmente.'
                ))
            return

        # ── Modo síncrono: executa end-to-end ─────────────────────────────────
        self.stdout.write('Modo: síncrono (aguarde — download + processamento Rust)')
        self.stdout.write(
            'Atenção: rust_engine deve estar compilado '
            '(`maturin develop --release`)'
        )

        try:
            service = CnvSeedService(project)
            result = service.run(
                matrix_id=matrix_id,
                gain_threshold=gain_threshold,
                loss_threshold=loss_threshold,
            )
        except CnvSeedMatrixNotFoundError as exc:
            raise CommandError(
                f'Matriz CNV não encontrada: {exc}\n'
                f'Execute load_cnv_matrix --project {project_uuid} primeiro.'
            )
        except CnvSeedGeneRoleEmptyError as exc:
            raise CommandError(
                f'Pré-check falhou: {exc}\n'
                f'Execute load_gene_roles --project {project_uuid} primeiro.'
            )
        except CnvSeedJobActiveError as exc:
            self.stdout.write(self.style.WARNING(
                f'Idempotência: job ativo encontrado — {exc}\n'
                f'Aguarde o job concluir ou cancele-o manualmente.'
            ))
            return
        except ImportError:
            raise CommandError(
                'rust_engine não encontrado. Compile com: maturin develop --release'
            )
        except Exception as exc:
            raise CommandError(f'Derivação de seed CNV falhou: {exc}') from exc

        # ── Relatório final ───────────────────────────────────────────────────
        # Nenhum caminho físico, storage_key nem credencial é exibido.
        self.stdout.write(self.style.SUCCESS(
            'Derivação de seed CNV concluída com sucesso.'
        ))
        self.stdout.write(f'  job_id:                   {result["job_id"]}')
        self.stdout.write(f'  matrix_id:                {result["matrix_id"]}')
        self.stdout.write(f'  method_version:           {result["method_version"]}')
        self.stdout.write('')
        self.stdout.write('  Seeds gravadas em VariantEffectSeed:')
        self.stdout.write(f'    n_seeds_written:        {result["n_seeds_written"]}')
        self.stdout.write(f'    n_activator:            {result["n_activator"]}')
        self.stdout.write(f'    n_inactivator:          {result["n_inactivator"]}')
        self.stdout.write(f'    n_neutral_skipped:      {result["n_neutral_skipped"]}')
        self.stdout.write('')
        self.stdout.write('  Cobertura de papel do gene:')
        self.stdout.write(f'    n_genes_with_role:      {result["n_genes_with_role"]}')
        self.stdout.write(f'    n_genes_skipped_no_role:{result["n_genes_skipped_no_role"]}')
        self.stdout.write('')
        self.stdout.write('  Filtros de threshold:')
        self.stdout.write(f'    n_cells_subthreshold:   {result["n_cells_subthreshold"]}')
        self.stdout.write(f'    n_cells_null:           {result["n_cells_null"]}')
        self.stdout.write('')

        # Avisos de diagnóstico úteis
        if result['n_genes_skipped_no_role'] > 0:
            pct = 0.0
            total = result['n_genes_with_role'] + result['n_genes_skipped_no_role']
            if total > 0:
                pct = 100.0 * result['n_genes_skipped_no_role'] / total
            self.stdout.write(self.style.WARNING(
                f'  AVISO: {result["n_genes_skipped_no_role"]} genes ({pct:.1f}%) '
                f'sem papel definido em GeneRole foram ignorados (neutro/skip). '
                f'Considere re-executar load_gene_roles com fonte atualizada.'
            ))

        if result['n_seeds_written'] == 0:
            self.stdout.write(self.style.WARNING(
                '  AVISO: nenhuma seed foi escrita. Verifique se:\n'
                '    (a) GeneRole foi carregado corretamente (load_gene_roles);\n'
                '    (b) Os thresholds (gain/loss) são adequados para os dados;\n'
                '    (c) A OmicMatrix CNV tem amostras e genes vinculados.'
            ))
        else:
            self.stdout.write(
                f'VariantEffectSeed populado com {result["n_seeds_written"]} seeds '
                f'(method_version={result["method_version"]}).\n'
                f'\nPipeline completo dos 3 commands:\n'
                f'  1. load_gene_roles   — OK (GeneRole populado)\n'
                f'  2. load_cnv_matrix   — OK (OmicMatrix copy_number)\n'
                f'  3. derive_cnv_seeds  — OK (VariantEffectSeed CNV v1)\n'
                f'\nPróximo passo (incremento SNV): load_clinvar + load_alphamissense '
                f'→ resolve_variant_effects → derive_snv_seeds.'
            )
