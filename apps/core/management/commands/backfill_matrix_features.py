"""
Management command — backfill_matrix_features

Popula OmicMatrixFeature (catálogo A→C, migration 0036) para matrizes ômicas
JÁ CARREGADAS — o Parquet já está em default_storage; re-rodar a ingestão
inteira só para catalogar features seria desperdício.

OmnisPathway Objetivo 2, Fase 3 (promoção A→C).

Fluxo:
  1. Resolve OmicMatrix(es) do projeto (isolamento via ProjectDataset).
     Sem --matrix-id: processa todas as matrizes do projeto.
  2. Baixa o Parquet via default_storage para tempfile local.
  3. Chama rust_engine.catalog_matrix_features(parquet_path, matrix_id,
     db_url) → manifest{n_features, n_inserted, n_updated}.
  4. Valida: OmicMatrixFeature.objects.filter(matrix=m).count() deve bater
     EXATAMENTE com OmicMatrix.n_features. Divergência é reportada como erro.

Pré-requisito para o mapeamento readout→feature (map_readouts, Fase 3, Passo
3.4): a partir da promoção A→C, ReadoutMappingService valida feature_key
contra OmicMatrixFeature da matriz alvo. Sem este backfill, map_readouts
falha alto (OmicMatrixFeatureNotCataloguedError) — rode este command antes.

Idempotência:
  A NK de OmicMatrixFeature (matrix, feature_key) garante que re-rodar o
  backfill para a mesma matriz não duplica (UPSERT no Rust).

Sem endpoint HTTP (só management command).

Sensitive-data-handling:
    db_url NUNCA logada. storage_key reportada é chave lógica, não caminho
    físico absoluto.
"""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        'Popula OmicMatrixFeature (catálogo A→C) para matrizes ômicas já '
        'carregadas, a partir do Parquet já presente em default_storage. '
        'OmnisPathway Obj 2, Fase 3 (promoção A→C).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--project',
            metavar='UUID',
            required=True,
            help='UUID do DaVinciProject alvo (obrigatório — isolamento por projeto).',
        )
        parser.add_argument(
            '--matrix-id',
            metavar='ID',
            type=int,
            default=None,
            dest='matrix_id',
            help=(
                'ID específico de OmicMatrix a processar. Sem esta opção, '
                'processa todas as matrizes vinculadas ao projeto.'
            ),
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            dest='dry_run',
            help=(
                'Resolve e reporta as matrizes que seriam processadas '
                '(com a contagem atual de OmicMatrixFeature), sem baixar o '
                'Parquet nem chamar o Rust.'
            ),
        )

    def handle(self, *args, **options):
        from apps.core.models import DaVinciProject
        from apps.core.services.matrix_feature_catalog_service import (
            MatrixFeatureCatalogMatrixNotFoundError,
            MatrixFeatureCatalogService,
            MatrixFeatureCountMismatchError,
        )

        project_uuid = options['project']
        matrix_id = options['matrix_id']
        dry_run = options['dry_run']

        # ── Resolve projeto ───────────────────────────────────────────────────
        try:
            project = DaVinciProject.objects.select_related('user').get(pk=project_uuid)
        except DaVinciProject.DoesNotExist:
            raise CommandError(f'Projeto não encontrado: {project_uuid}')

        self.stdout.write(f'Projeto: {project.title} (id={project.id})')
        self.stdout.write(f'dry_run: {dry_run}')
        if matrix_id is not None:
            self.stdout.write(f'matrix_id: {matrix_id} (matriz única)')
        else:
            self.stdout.write('matrix_id: nao informado — todas as matrizes do projeto')
        self.stdout.write('')

        service = MatrixFeatureCatalogService(project)

        try:
            matrices = service.resolve_matrices(project, matrix_id)
        except MatrixFeatureCatalogMatrixNotFoundError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(f'{len(matrices)} matriz(es) resolvida(s) para o backfill:')
        for m in matrices:
            self.stdout.write(
                f'  - matrix_id={m.id} '
                f'({m.dataset.accession} · {m.omics_layer}/{m.feature_axis} '
                f'[{m.loader_version}]) — n_features(metadado)={m.n_features}'
            )
        self.stdout.write('')

        if not dry_run:
            self.stdout.write(
                'Atenção: rust_engine deve estar compilado '
                '(`maturin develop --release`)'
            )

        n_ok = 0
        n_failed = 0

        for matrix in matrices:
            self.stdout.write(f'--- matrix_id={matrix.id} ---')
            try:
                result = service.run(matrix, dry_run=dry_run)
            except MatrixFeatureCountMismatchError as exc:
                n_failed += 1
                self.stdout.write(self.style.ERROR(f'  DIVERGÊNCIA: {exc}'))
                continue
            except ImportError:
                raise CommandError(
                    'rust_engine não encontrado. Compile com: '
                    'maturin develop --release'
                )
            except Exception as exc:
                n_failed += 1
                self.stdout.write(self.style.ERROR(
                    f'  Falha no backfill da matrix_id={matrix.id}: {exc}'
                ))
                continue

            n_ok += 1
            if dry_run:
                self.stdout.write(
                    f'  [dry-run] n_catalogued_atual={result["n_catalogued_before"]} '
                    f'/ n_features_expected={result["n_features_expected"]} '
                    f'(match={result["match"]})'
                )
            else:
                self.stdout.write(self.style.SUCCESS(
                    f'  n_inserted={result["n_inserted"]}, '
                    f'n_updated={result["n_updated"]}, '
                    f'n_catalogued_after={result["n_catalogued_after"]}, '
                    f'n_features_expected={result["n_features_expected"]}, '
                    f'match={result["match"]}'
                ))

        self.stdout.write('')
        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'DRY RUN — nada foi baixado nem persistido. '
                f'{n_ok} matriz(es) inspecionada(s), {n_failed} com erro.'
            ))
        else:
            self.stdout.write(
                f'Backfill concluído: {n_ok} matriz(es) com sucesso, '
                f'{n_failed} com divergência/erro.'
            )
            if n_failed:
                raise CommandError(
                    f'{n_failed} matriz(es) com divergência ou falha no backfill '
                    f'— ver detalhes acima.'
                )
            self.stdout.write(
                '\nOmicMatrixFeature populado. Próximo passo: rodar '
                'map_readouts (agora valida contra o catálogo real de '
                'features em vez do grafo KEGG).'
            )
