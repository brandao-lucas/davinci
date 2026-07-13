"""
Management command — load_variant_effects

Popula VariantEffectRaw via ClinVar VCF GRCh38 + AlphaMissense hg38.

OmnisPathway Objetivo 2, Fase 2, Slice 2B.

Fontes:
  ClinVar VCF GRCh38 (sempre):
    https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/clinvar.vcf.gz
    Domínio público. O Rust extrai CLNSIG (germinativo) + oncogenicidade (somático).

  AlphaMissense hg38 (padrão; pode ser pulado com --skip-alphamissense):
    https://storage.googleapis.com/dm_alphamissense/AlphaMissense_hg38.tsv.gz
    ~643 MB. CC BY-NC-SA — NÃO commitar recortes. Requer mapa uniprot→gene.

Pré-requisito:
  GeneRole populado (source='oncokb'):
    manage.py load_gene_roles --project <UUID>

  Se GeneRole estiver vazio, o command aborta com mensagem clara.

Mapa uniprot→gene:
  Consultado via UniProt REST API (público, sem credencial) em lotes de 20.
  Resultado cacheado em diagnostics/cache/uniprot_gene_map.json (7 dias).
  Genes sem hit UniProt ficam fora do mapa — AM não filtra esses genes.

Filtro de escopo (Decisão de escala):
  Apenas variantes cujo gene_symbol está no allowlist de GeneRole (source='oncokb')
  são inseridas. Variantes de genes fora do allowlist são descartadas no Rust
  em streaming (evita explosão de VariantEffectRaw — AM cobre todo o missense).

Uso:
    # Síncrono (bancada de prova) — ClinVar + AlphaMissense
    .venv/bin/python manage.py load_variant_effects --project <UUID>

    # Síncrono — apenas ClinVar (mais rápido; AM pulado)
    .venv/bin/python manage.py load_variant_effects --project <UUID> \\
        --skip-alphamissense

    # Assíncrono (produção — worker Celery deve estar ativo)
    .venv/bin/python manage.py load_variant_effects --project <UUID> --async

    # Assíncrono sem AM
    .venv/bin/python manage.py load_variant_effects --project <UUID> \\
        --async --skip-alphamissense

Idempotência:
    Gate verifica job ativo (PENDING/RUNNING) — não duplica job concorrente.
    O Rust usa UPSERT ON CONFLICT (variant_key, source, gene_symbol) DO UPDATE.

Sensitive-data-handling:
    Nenhuma credencial neste fluxo. db_url nunca é logada. Caminhos físicos
    de arquivos temporários não são impressos no relatório.
"""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        'Popula VariantEffectRaw via ClinVar VCF GRCh38 + AlphaMissense hg38. '
        'OmnisPathway Obj 2, Fase 2, Slice 2B. '
        'Pré-requisito: load_gene_roles deve ter rodado.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--project',
            metavar='UUID',
            required=True,
            help=(
                'UUID do DaVinciProject para tracking do job (obrigatório — '
                'FK do IngestionJob). A carga atualiza o catálogo global '
                'VariantEffectRaw.'
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
        parser.add_argument(
            '--skip-alphamissense',
            action='store_true',
            default=False,
            dest='skip_alphamissense',
            help=(
                'Executa apenas ClinVar e pula o AlphaMissense (~643 MB). '
                'Útil para testes rápidos ou quando o mapa uniprot→gene não '
                'está disponível. AM pode ser carregado em execução posterior.'
            ),
        )

    def handle(self, *args, **options):
        from apps.core.models import DaVinciProject
        from apps.core.services.variant_effect_raw_service import (
            CLINVAR_VCF_URL,
            ALPHAMISSENSE_URL,
            LOADER_VERSION,
            GeneRoleNotPopulatedError,
            VariantEffectRawJobActiveError,
            VariantEffectRawService,
        )

        project_uuid = options['project']
        use_async = options['use_async']
        skip_alphamissense = options['skip_alphamissense']

        # ── Resolve projeto ───────────────────────────────────────────────────
        try:
            project = DaVinciProject.objects.select_related('user').get(
                pk=project_uuid
            )
        except DaVinciProject.DoesNotExist:
            raise CommandError(f'Projeto não encontrado: {project_uuid}')

        self.stdout.write(f'Projeto (tracking): {project.title} (id={project.id})')
        self.stdout.write(f'Fonte ClinVar: {CLINVAR_VCF_URL}')
        if skip_alphamissense:
            self.stdout.write(
                'AlphaMissense: PULADO (--skip-alphamissense)'
            )
        else:
            self.stdout.write(f'Fonte AlphaMissense: {ALPHAMISSENSE_URL}')
        self.stdout.write(f'loader_version: {LOADER_VERSION}')
        self.stdout.write(
            'Catálogo alvo: VariantEffectRaw (global — não filtrado por projeto)'
        )
        self.stdout.write(
            'Pré-requisito: GeneRole (source=oncokb) deve estar populado.'
        )

        if use_async:
            # ── Modo assíncrono: apenas enfileira ─────────────────────────────
            self.stdout.write('Modo: assíncrono (Celery)')
            try:
                job = VariantEffectRawService.dispatch(
                    project,
                    skip_alphamissense=skip_alphamissense,
                )
                self.stdout.write(self.style.SUCCESS(
                    f'Job enfileirado: {job.id} (status={job.status})\n'
                    f'Acompanhe o progresso via IngestionJob.id={job.id}'
                ))
            except GeneRoleNotPopulatedError as exc:
                raise CommandError(str(exc)) from exc
            except VariantEffectRawJobActiveError as exc:
                self.stdout.write(self.style.WARNING(
                    f'Idempotência: job ativo encontrado — {exc}'
                ))
            return

        # ── Modo síncrono: executa end-to-end ─────────────────────────────────
        self.stdout.write(
            'Modo: síncrono (aguarde — download e UPSERT em andamento)\n'
            'Atenção: ClinVar ~1GB e AlphaMissense ~643MB — pode demorar.'
        )
        self.stdout.write(
            'Atenção: rust_engine deve estar compilado '
            '(`maturin develop --release`)'
        )

        try:
            service = VariantEffectRawService(
                project,
                skip_alphamissense=skip_alphamissense,
            )
            result = service.run()
        except GeneRoleNotPopulatedError as exc:
            raise CommandError(str(exc)) from exc
        except VariantEffectRawJobActiveError as exc:
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
            raise CommandError(
                f'Carga de VariantEffectRaw falhou: {exc}'
            ) from exc

        # ── Relatório final (sem vazar credencial, sem caminho físico) ─────────
        self.stdout.write(self.style.SUCCESS(
            'Carga de VariantEffectRaw concluída.'
        ))
        self.stdout.write(f'  job_id:                    {result["job_id"]}')
        self.stdout.write(
            f'  genes no allowlist:        {result["n_genes_in_allowlist"]}'
        )

        self.stdout.write('')
        self.stdout.write('  --- ClinVar ---')
        self.stdout.write(
            f'  source_version:            {result["clinvar_source_version"]}'
        )
        self.stdout.write(
            f'  n_variants_processed:      {result["clinvar_n_variants_processed"]}'
        )
        self.stdout.write(
            f'  n_kept:                    {result["clinvar_n_kept"]}'
        )
        self.stdout.write(
            f'  n_skipped_offlist:         {result["clinvar_n_skipped_offlist"]}'
        )
        self.stdout.write(
            f'  n_skipped_no_gene:         {result["clinvar_n_skipped_no_gene"]}'
        )
        self.stdout.write(
            f'  n_upserted:                {result["clinvar_n_upserted"]}'
        )
        if result['clinvar_errors']:
            self.stdout.write(self.style.WARNING(
                f'  erros ClinVar ({len(result["clinvar_errors"])}): '
                + '; '.join(result['clinvar_errors'][:5])
                + (' ...' if len(result['clinvar_errors']) > 5 else '')
            ))

        self.stdout.write('')
        self.stdout.write('  --- AlphaMissense ---')
        if result['am_skipped']:
            self.stdout.write(self.style.WARNING(
                f'  PULADO: {result["am_skip_reason"]}'
            ))
        else:
            self.stdout.write(
                f'  source_version:            {result["am_source_version"]}'
            )
            self.stdout.write(
                f'  n_variants_processed:      {result["am_n_variants_processed"]}'
            )
            self.stdout.write(
                f'  n_kept:                    {result["am_n_kept"]}'
            )
            self.stdout.write(
                f'  n_skipped_no_map:          {result["am_n_skipped_no_map"]}'
            )
            self.stdout.write(
                f'  n_upserted:                {result["am_n_upserted"]}'
            )
            if result['am_errors']:
                self.stdout.write(self.style.WARNING(
                    f'  erros AM ({len(result["am_errors"])}): '
                    + '; '.join(result['am_errors'][:5])
                    + (' ...' if len(result['am_errors']) > 5 else '')
                ))

        self.stdout.write('')
        self.stdout.write('  --- Mapa UniProt→Gene ---')
        self.stdout.write(
            f'  genes com uniprot_id:      {result["n_genes_with_uniprot"]}'
        )
        self.stdout.write(
            f'  genes SEM uniprot_id:      {result["n_genes_without_uniprot"]}'
        )
        if result['n_genes_without_uniprot'] > 0:
            self.stdout.write(self.style.WARNING(
                f'  Aviso: {result["n_genes_without_uniprot"]} gene(s) sem '
                f'uniprot_id — variantes AM desses genes serão descartadas '
                f'pelo filtro allowlist do Rust.'
            ))

        self.stdout.write('')
        self.stdout.write(
            'VariantEffectRaw populado/atualizado no banco (UPSERT idempotente).'
        )
        self.stdout.write(
            'Próximo passo: manage.py resolve_variant_effects (Slice 2C)'
        )
