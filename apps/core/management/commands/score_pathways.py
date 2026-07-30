"""
Management command — score_pathways

Executa o motor PFS v1 (RWR assinado + leitura por Regras + escore contínuo
por permutação) — OmnisPathway Objetivo 2, Fase 4.

Pré-condições (em ordem obrigatória — cada uma falha alto com o command a
rodar se não satisfeita):
  1. load_pathway_topology  — grafo (Pathway/PathwayNode/PathwayEdge) das
                               vias pedidas.
  2. map_readouts            — PathwayReadoutFeature do mapping_version
                               pedido.
  3. backfill_matrix_features — OmicMatrixFeature catalogado NAS DUAS
                               matrizes de readout (fosfo + proteoma).
  4. pair_samples             — OmicSamplePairing(validated_overlap=True)
                               NAS DUAS matrizes (pareamento é POR MATRIZ —
                               a natural key é (matrix, case_id); pares da
                               matriz de proteoma NÃO valem para a de fosfo).

NÃO checa existência de VariantEffectSeed (Fase 2) como pré-condição: zero
sementes é um estado VÁLIDO do motor — produz escore degenerado com
n_seeds_on_graph=0 explicando o porquê, não um crash. É o estado real da
bancada em 2026-07-30 (ver ADENDO do plano da Fase 4). Se isso ocorrer, o
relatório abaixo destaca isso em um aviso bem visível — não é para ser
descoberto depois olhando z_score=0 sem explicação.

Gate de reprodutibilidade (D3): `--method-version` é PARTE DA NATURAL KEY
de PathwayActivityScore. Se já existirem escores para o method_version
pedido E os parâmetros deste run divergirem dos que os produziram, o
command falha exigindo um --method-version novo ou --force explícito —
sem isso, o COPY faria ON CONFLICT DO UPDATE e sobrescreveria o benchmark
anterior silenciosamente.

Contrato Rust (ferris, pronto — migration 0037):
  rust_engine.run_pfs_scoring(
      pathway_kegg_ids, phospho_parquet_path, proteome_parquet_path,
      phospho_matrix_id, proteome_matrix_id, seed_method_versions,
      mapping_version, method_version, db_url,
      restart=0.3, unsigned_weight=0.3, n_permutations=1000, rng_seed=42,
      min_regulon_targets=5, dry_run=False,
  ) -> PfsRunManifest

Sem endpoint HTTP (espelha Fases 0-3).

Sensitive-data-handling:
  db_url nunca logada nem impressa. storage_key/caminhos locais dos
  Parquets também não.

Uso:
    # Sonda de cobertura (D-4/D-5) sem gravar nada
    .venv/bin/python manage.py score_pathways --project <UUID> --dry-run

    # Execução real (default: as 3 vias, fase4-pfs-v1)
    .venv/bin/python manage.py score_pathways --project <UUID>

    # Via específica + método/versão custom
    .venv/bin/python manage.py score_pathways --project <UUID> \\
        --pathways hsa04151 --method-version fase4-pfs-v1-sensib

    # Sensibilidade unsigned_weight=0 (gatilho D-2, se aplicável)
    .venv/bin/python manage.py score_pathways --project <UUID> \\
        --unsigned-weight 0 --method-version fase4-pfs-v1-nounsigned

    # Assíncrono (worker Celery deve estar ativo)
    .venv/bin/python manage.py score_pathways --project <UUID> --async
"""

from django.core.management.base import BaseCommand, CommandError

_DEFAULT_PATHWAYS = 'hsa04151,hsa04010,hsa04115'
_DEFAULT_SEED_VERSIONS = 'fase2-cnv-v2,fase2-snv-v1,fase2-cnv-v1'


class Command(BaseCommand):
    help = (
        'Executa o motor PFS v1 (RWR assinado + leitura por Regras + '
        'escore contínuo por permutação). OmnisPathway Obj 2, Fase 4.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--project',
            metavar='UUID',
            required=True,
            help='UUID do DaVinciProject alvo (obrigatório — isolamento por projeto).',
        )
        parser.add_argument(
            '--pathways',
            metavar='IDS',
            default=_DEFAULT_PATHWAYS,
            help=f'IDs KEGG separados por vírgula (padrão: {_DEFAULT_PATHWAYS}).',
        )
        parser.add_argument(
            '--seed-method-versions',
            metavar='VERSIONS',
            default=_DEFAULT_SEED_VERSIONS,
            dest='seed_method_versions',
            help=(
                'Versões de VariantEffectSeed consideradas, separadas por '
                f'vírgula (padrão: {_DEFAULT_SEED_VERSIONS} — precedência '
                'D3 é fixa no Rust: cnv-v2 > snv-v1 > cnv-v1, '
                'independentemente da ordem aqui). Mudar esta lista sem '
                'mudar --method-version dispara o gate de reprodutibilidade.'
            ),
        )
        parser.add_argument(
            '--mapping-version',
            metavar='VERSION',
            default='fase3-readout-v1',
            dest='mapping_version',
            help='Versão do mapeamento readout→feature (padrão: fase3-readout-v1).',
        )
        parser.add_argument(
            '--method-version',
            metavar='VERSION',
            default='fase4-pfs-v1',
            dest='method_version',
            help=(
                'Versão do método do PFS — PARTE DA NATURAL KEY de '
                'PathwayActivityScore (padrão: fase4-pfs-v1). Use um valor '
                'novo ao mudar qualquer parâmetro numérico para não '
                'colidir com um benchmark já gravado (gate de '
                'reprodutibilidade, D3).'
            ),
        )
        parser.add_argument(
            '--permutations',
            metavar='N',
            type=int,
            default=1000,
            dest='n_permutations',
            help='Nº de permutações (B) da distribuição nula (padrão: 1000).',
        )
        parser.add_argument(
            '--restart',
            metavar='FLOAT',
            type=float,
            default=0.3,
            help='Restart (r) do RWR assinado (padrão: 0.3).',
        )
        parser.add_argument(
            '--unsigned-weight',
            metavar='FLOAT',
            type=float,
            default=0.3,
            dest='unsigned_weight',
            help='Peso das arestas sign=0 (padrão: 0.3).',
        )
        parser.add_argument(
            '--rng-seed',
            metavar='N',
            type=int,
            default=42,
            dest='rng_seed',
            help='Semente-base do PRNG counter-based da permutação (padrão: 42).',
        )
        parser.add_argument(
            '--min-regulon-targets',
            metavar='N',
            type=int,
            default=5,
            dest='min_regulon_targets',
            help='Mínimo de alvos com valor para o ULM da Regra 2 (padrão: 5).',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            default=False,
            help=(
                'Ignora o gate de reprodutibilidade (D3) e sobrescreve o '
                'method_version existente mesmo com parâmetros divergentes.'
            ),
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            dest='dry_run',
            help=(
                'Calcula tudo (sonda de cobertura D-4/D-5) sem gravar '
                'PathwayActivityScore.'
            ),
        )
        parser.add_argument(
            '--async',
            action='store_true',
            default=False,
            dest='use_async',
            help=(
                'Enfileira o run via Celery (worker deve estar ativo). '
                'Padrão: execução síncrona (bancada de prova).'
            ),
        )

    def handle(self, *args, **options):
        from apps.core.models import DaVinciProject
        from apps.core.services.pathway_scoring_service import (
            DEFAULT_PATHWAY_KEGG_IDS,
            DEFAULT_SEED_METHOD_VERSIONS,
            PathwayScoringService,
            PfsFeaturesNotCataloguedError,
            PfsGraphNotLoadedError,
            PfsJobActiveError,
            PfsMatrixNotFoundError,
            PfsPairingMissingError,
            PfsReadoutsNotMappedError,
            PfsReproducibilityGateError,
            PfsScoringError,
        )

        project_uuid = options['project']
        mapping_version = options['mapping_version']
        method_version = options['method_version']
        n_permutations = options['n_permutations']
        restart = options['restart']
        unsigned_weight = options['unsigned_weight']
        rng_seed = options['rng_seed']
        min_regulon_targets = options['min_regulon_targets']
        force = options['force']
        dry_run = options['dry_run']
        use_async = options['use_async']

        raw_pathways = options['pathways']
        pathway_ids = [p.strip() for p in raw_pathways.split(',') if p.strip()]
        if not pathway_ids:
            pathway_ids = DEFAULT_PATHWAY_KEGG_IDS

        raw_seed_versions = options['seed_method_versions']
        seed_method_versions = [
            v.strip() for v in raw_seed_versions.split(',') if v.strip()
        ]
        if not seed_method_versions:
            seed_method_versions = DEFAULT_SEED_METHOD_VERSIONS

        # ── Resolve projeto ───────────────────────────────────────────────
        try:
            project = DaVinciProject.objects.select_related('user').get(pk=project_uuid)
        except DaVinciProject.DoesNotExist:
            raise CommandError(f'Projeto não encontrado: {project_uuid}')

        self.stdout.write(f'Projeto: {project.title} (id={project.id})')
        self.stdout.write(f'Vias:                 {pathway_ids}')
        self.stdout.write(f'method_version:       {method_version}')
        self.stdout.write(f'seed_method_versions: {seed_method_versions}')
        self.stdout.write(f'mapping_version:      {mapping_version}')
        self.stdout.write(
            f'restart={restart}  unsigned_weight={unsigned_weight}  '
            f'n_permutations={n_permutations}  rng_seed={rng_seed}  '
            f'min_regulon_targets={min_regulon_targets}'
        )
        self.stdout.write(f'dry_run={dry_run}  force={force}  async={use_async}')
        self.stdout.write('')

        common_kwargs = dict(
            pathway_kegg_ids=pathway_ids,
            method_version=method_version,
            seed_method_versions=seed_method_versions,
            mapping_version=mapping_version,
            restart=restart,
            unsigned_weight=unsigned_weight,
            n_permutations=n_permutations,
            rng_seed=rng_seed,
            min_regulon_targets=min_regulon_targets,
            force=force,
            dry_run=dry_run,
        )

        if use_async:
            self.stdout.write('Modo: assíncrono (Celery)')
            try:
                job = PathwayScoringService.dispatch(project, **common_kwargs)
                self.stdout.write(self.style.SUCCESS(
                    f'Job enfileirado: {job.id} (status={job.status})\n'
                    f'Acompanhe o progresso via IngestionJob.id={job.id}'
                ))
            except PfsScoringError as exc:
                raise CommandError(str(exc)) from exc
            return

        # ── Modo síncrono: executa end-to-end ─────────────────────────────
        self.stdout.write(
            'Modo: síncrono (aguarde — download de 2 Parquets + Rust)'
        )
        self.stdout.write(
            'Atenção: rust_engine deve estar compilado '
            '(`maturin develop --release`)'
        )

        try:
            service = PathwayScoringService(project)
            report = service.run(**common_kwargs)
        except PfsJobActiveError as exc:
            self.stdout.write(self.style.WARNING(
                f'Idempotência: job ativo encontrado — {exc}'
            ))
            return
        except PfsReproducibilityGateError as exc:
            raise CommandError(str(exc)) from exc
        except (
            PfsMatrixNotFoundError,
            PfsGraphNotLoadedError,
            PfsReadoutsNotMappedError,
            PfsFeaturesNotCataloguedError,
            PfsPairingMissingError,
        ) as exc:
            raise CommandError(f'Pré-condição não satisfeita: {exc}') from exc
        except ImportError:
            raise CommandError(
                'rust_engine não encontrado. Compile com: maturin develop --release'
            )
        except Exception as exc:
            raise CommandError(f'Run do motor PFS falhou: {exc}') from exc

        self._print_report(report)

    # ─────────────────────────────────────────────────────────────────────
    # Relatório
    # ─────────────────────────────────────────────────────────────────────

    def _print_report(self, report: dict) -> None:
        if report['dry_run']:
            self.stdout.write(self.style.WARNING(
                'DRY RUN — nada foi persistido em PathwayActivityScore.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                'Run do motor PFS concluído com sucesso.'
            ))

        self.stdout.write(f'  job_id:                  {report["job_id"]}')
        self.stdout.write(f'  method_version:          {report["method_version"]}')
        self.stdout.write(f'  n_pathways:              {report["n_pathways"]}')
        self.stdout.write(f'  n_pathways_no_readout:   {report["n_pathways_no_readout"]}')
        self.stdout.write(f'  n_cases_scored:          {report["n_cases_scored"]}')
        self.stdout.write(f'  n_rows_written:          {report["n_rows_written"]}')
        self.stdout.write('')

        # ── Diagnóstico ordenado anti-p-hacking (adendo do plano) ─────────
        self.stdout.write('Cobertura de SEMENTE (D4/D-5):')
        self.stdout.write(f'    n_seeds_total:               {report["n_seeds_total"]}')
        self.stdout.write(f'    n_seeds_on_graph:             {report["n_seeds_on_graph"]}')
        self.stdout.write(f'    n_seeds_off_graph:            {report["n_seeds_off_graph"]}')
        self.stdout.write(f'    n_seeds_masked_by_neutral_v2: {report["n_seeds_masked_by_neutral_v2"]}')
        seed_ratio = report['seed_coverage_ratio']
        if seed_ratio is not None:
            self.stdout.write(f'    razão on/(on+off):            {seed_ratio:.1%}')
        else:
            self.stdout.write('    razão on/(on+off):            N/A (nenhuma seed considerada)')
        self.stdout.write('')

        self.stdout.write('Cobertura de READOUT (denominador correto = with_value, D9/adendo):')
        self.stdout.write(f'    n_readouts_mapped (candidatos, NÃO usar p/ normalizar): {report["n_readouts_mapped"]}')
        self.stdout.write(f'    n_readouts_with_value (denominador REAL do escore):     {report["n_readouts_with_value"]}')
        self.stdout.write(f'    n_readouts_missing (fantasma = mapped - with_value):    {report["n_readouts_missing"]}')
        self.stdout.write(f'    n_readouts_phospho (Regra 1 contribuiu):                {report["n_readouts_phospho"]}')
        self.stdout.write(f'    n_readouts_tf (Regra 2 contribuiu):                     {report["n_readouts_tf"]}')
        self.stdout.write(f'    n_tf_below_min_targets:                                 {report["n_tf_below_min_targets"]}')
        readout_ratio = report['readout_coverage_ratio']
        if readout_ratio is not None:
            self.stdout.write(f'    razão with_value/mapped:                                {readout_ratio:.1%}')
        else:
            self.stdout.write('    razão with_value/mapped:                                N/A (n_readouts_mapped=0)')
        self.stdout.write('')

        self.stdout.write('Outros contadores de diagnóstico:')
        self.stdout.write(f'    n_tumor_unpaired_skipped: {report["n_tumor_unpaired_skipped"]}')
        self.stdout.write(f'    n_zero_sd_null:           {report["n_zero_sd_null"]}')
        self.stdout.write(f'    n_signed_edges:           {report["n_signed_edges"]}')
        self.stdout.write(f'    n_unsigned_edges:         {report["n_unsigned_edges"]}')
        self.stdout.write('')
        self.stdout.write(
            f'  tempos (ms): load={report["elapsed_ms_load"]} '
            f'graph_precompute={report["elapsed_ms_graph_precompute"]} '
            f'readout_and_permutation={report["elapsed_ms_readout_and_permutation"]} '
            f'copy={report["elapsed_ms_copy"]} total={report["elapsed_ms_total"]}'
        )
        self.stdout.write('')

        # ── Banner de destaque: zero sementes no grafo ────────────────────
        if report['zero_seeds_on_graph']:
            banner = '!' * 74
            self.stdout.write(self.style.ERROR(
                f'\n{banner}\n'
                f'!!  ZERO SEMENTES NO GRAFO (n_seeds_on_graph=0)\n'
                f'!!  Todo z_score sera DEGENERADO (z=0 por convencao, D7) —\n'
                f'!!  isto e SINTOMA DE COBERTURA DE SEED, NAO bug do motor.\n'
                f'!!  Rode a Fase 2 antes de reexecutar:\n'
                f'!!    derive_cnv_seeds / derive_cnv_allele_seeds /\n'
                f'!!    resolve_variant_effects (+ load_somatic_maf p/ SNV)\n'
                f'{banner}\n'
            ))
        elif report['seed_coverage_ratio'] is not None and report['seed_coverage_ratio'] < 0.05:
            self.stdout.write(self.style.WARNING(
                f'AVISO: cobertura de semente muito baixa '
                f'({report["seed_coverage_ratio"]:.1%} on-graph) — z pode ficar '
                f'proximo de zero por falta de substrato, nao por ausencia de '
                f'sinal biologico. Verifique a Fase 2 antes de interpretar.'
            ))

        if report['n_readouts_with_value'] == 0:
            self.stdout.write(self.style.ERROR(
                'AVISO: n_readouts_with_value=0 — nenhuma via tem readout com '
                'valor real no Parquet. O escore nao pode ser calculado para '
                'nenhum caso. Verifique map_readouts e backfill_matrix_features.'
            ))

        if not report['dry_run']:
            self.stdout.write(
                f'\nPathwayActivityScore: {report["n_rows_written"]} linhas '
                f'gravadas (COPY UPSERT, method_version='
                f'{report["method_version"]!r}).\n'
                f'Diagnostico anti-p-hacking: se os z derem ~0 em toda a '
                f'coorte, a ordem de investigacao e (1) cobertura de seed, '
                f'(2) cobertura de readout, (3) fracao de arestas sem sinal, '
                f'(4) numero de pares — NUNCA ajustar parametro para "dar '
                f'sinal".'
            )
