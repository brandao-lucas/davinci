"""
MatrixFeatureCatalogService — backfill de OmicMatrixFeature para matrizes
ômicas já carregadas.

OmnisPathway Objetivo 2, Fase 3 (promoção A→C, migration 0036).

Responsabilidade:
  1. Resolver a(s) OmicMatrix do projeto (isolamento via ProjectDataset,
     mesma lista de status ativos usada por CnvSeedService/SamplePairingService).
     Sem --matrix-id: processa todas as matrizes do projeto. Com --matrix-id:
     processa apenas a matriz indicada (deve pertencer ao projeto).
  2. Download do Parquet de OmicMatrix.storage_key via default_storage para
     um tempfile local (mesmo padrão de CnvSeedService._execute). NÃO abre
     o Parquet em Python.
  3. Chama rust_engine.catalog_matrix_features(parquet_path, matrix_id,
     db_url) → manifest{n_features, n_inserted, n_updated}. O Rust lê o
     Parquet LOCAL e faz COPY/UPSERT em OmicMatrixFeature via db_url
     (idempotente pela NK matrix+feature_key).
  4. Cleanup automático do tmp (TemporaryDirectory).
  5. Validação pós-carga: OmicMatrixFeature.objects.filter(matrix=m).count()
     deve bater EXATAMENTE com OmicMatrix.n_features. Divergência é reportada
     como erro (MatrixFeatureCountMismatchError) — sinal de que o Parquet e o
     metadado (n_features) discordam.

Fronteira de camadas (Regra #1 / django-rust-boundary):
  - Rust: lê o Parquet local, faz COPY/UPSERT em OmicMatrixFeature via db_url.
    NÃO faz download, NÃO faz upload.
  - Django: resolve a matriz (ORM set-based, isolamento por projeto), baixa o
    Parquet via default_storage, chama o Rust, valida a contagem pós-carga.
    NÃO abre o Parquet, NÃO conta features linha a linha em Python — a
    validação usa apenas COUNT(*) via ORM.

Isolamento (firebase-auth-guard / Regra #3):
  - project deve pertencer ao request.user antes de chegar aqui (comando
    operado por humano com --project explícito, mesmo padrão de
    load_phospho_matrix / load_cnv_matrix).
  - OmicMatrix resolvida via ProjectDataset com curation_status ativo — não
    há acesso cross-project. Mesmo padrão de CnvSeedService.resolve_matrix.

Sensitive-data-handling:
  - db_url construída a partir de settings.DATABASES['default'] — NUNCA
    logada, NUNCA persistida, NUNCA retornada ao caller.
  - storage_key (chave lógica) e caminho local do tempfile não são logados.

Idempotência:
  A NK de OmicMatrixFeature (matrix, feature_key) garante que rodar o
  backfill 2x para a mesma matriz não duplica — o Rust faz UPSERT
  (contrato do pyfunction catalog_matrix_features).

Modo --dry-run:
  NÃO baixa o Parquet nem chama o Rust (o Rust escreve direto no Postgres
  via db_url — não há como "simular" a chamada sem escrever). --dry-run
  apenas resolve e reporta quais matrizes SERIAM processadas, com a
  contagem atual de OmicMatrixFeature já catalogada (antes do backfill).
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile

from django.conf import settings
from django.core.files.storage import default_storage

from apps.core.models import (
    DaVinciProject,
    OmicMatrix,
    OmicMatrixFeature,
    ProjectDataset,
)

logger = logging.getLogger(__name__)

# Statuses de ProjectDataset considerados ativos (consistência com
# CnvSeedService / SamplePairingService).
_ACTIVE_STATUSES = (
    ProjectDataset.CurationStatus.INCLUDED,
    ProjectDataset.CurationStatus.QUEUED_DOWNLOAD,
    ProjectDataset.CurationStatus.DOWNLOADED,
    ProjectDataset.CurationStatus.PENDING,
)


class MatrixFeatureCatalogMatrixNotFoundError(Exception):
    """Levantado quando não é possível resolver a(s) OmicMatrix do projeto."""


class MatrixFeatureCountMismatchError(Exception):
    """
    Levantado quando, após o backfill, a contagem de OmicMatrixFeature não
    bate exatamente com OmicMatrix.n_features.

    Sinal de que o Parquet e o metadado n_features discordam — não deve ser
    engolido silenciosamente.
    """

    def __init__(self, matrix: OmicMatrix, n_catalogued: int):
        self.matrix = matrix
        self.n_catalogued = n_catalogued
        super().__init__(
            f"Divergência pós-backfill na matrix_id={matrix.id}: "
            f"OmicMatrixFeature catalogou {n_catalogued} feature(s), mas "
            f"OmicMatrix.n_features={matrix.n_features}. Parquet e metadado "
            f"discordam — investigar antes de confiar no catálogo desta matriz."
        )


class MatrixFeatureCatalogService:
    """
    Serviço de backfill de OmicMatrixFeature a partir do Parquet já
    carregado de uma OmicMatrix.

    Uso (management command backfill_matrix_features):
        service = MatrixFeatureCatalogService(project)
        matrices = service.resolve_matrices(project, matrix_id)
        for matrix in matrices:
            result = service.run(matrix, dry_run=False)

    Retorno de run():
        {
            'matrix_id': int,
            'n_features_expected': int | None,   # OmicMatrix.n_features
            'n_catalogued_before': int,           # count() antes do backfill
            'n_inserted': int | None,             # None em dry_run
            'n_updated': int | None,              # None em dry_run
            'n_catalogued_after': int,            # count() depois (= before em dry_run)
            'match': bool,                        # n_catalogued_after == n_features_expected
            'dry_run': bool,
        }

    Raises:
        MatrixFeatureCountMismatchError: divergência pós-backfill (apenas
            quando dry_run=False — em dry_run não há backfill real a validar).
    """

    def __init__(self, project: DaVinciProject):
        self.project = project

    # ─────────────────────────────────────────────────────────────────────────
    # Resolução de matrizes (espelha CnvSeedService.resolve_matrix — Fase 2)
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def resolve_matrices(
        cls,
        project: DaVinciProject,
        matrix_id: int | None = None,
    ) -> list[OmicMatrix]:
        """
        Resolve a(s) OmicMatrix do projeto para o backfill.

        Isolamento: só resolve matrizes de datasets vinculados ao projeto via
        ProjectDataset com curation_status ativo.

        Args:
            project: DaVinciProject do usuário.
            matrix_id: ID específico. Se None, retorna todas as matrizes do
                       projeto (qualquer omics_layer/feature_axis).

        Returns:
            Lista de OmicMatrix (não vazia).

        Raises:
            MatrixFeatureCatalogMatrixNotFoundError: nenhuma matriz encontrada
                ou matrix_id não pertence ao projeto.
        """
        dataset_ids = ProjectDataset.objects.filter(
            project=project,
            curation_status__in=_ACTIVE_STATUSES,
        ).values_list('dataset_id', flat=True)

        qs = OmicMatrix.objects.select_related('dataset').filter(
            dataset_id__in=dataset_ids,
        )

        if matrix_id is not None:
            matrix = qs.filter(id=matrix_id).first()
            if matrix is None:
                raise MatrixFeatureCatalogMatrixNotFoundError(
                    f'OmicMatrix id={matrix_id} não encontrada ou não '
                    f'pertence ao projeto {project.id}.'
                )
            return [matrix]

        matrices = list(qs.order_by('id'))
        if not matrices:
            raise MatrixFeatureCatalogMatrixNotFoundError(
                f'Nenhuma OmicMatrix encontrada no projeto {project.id}. '
                f'Carregue ao menos uma matriz antes do backfill.'
            )
        return matrices

    # ─────────────────────────────────────────────────────────────────────────
    # Execução
    # ─────────────────────────────────────────────────────────────────────────

    def run(self, matrix: OmicMatrix, dry_run: bool = False) -> dict:
        """
        Executa o backfill de OmicMatrixFeature para UMA matriz.

        Args:
            matrix: OmicMatrix já resolvida (via resolve_matrices).
            dry_run: se True, NÃO baixa o Parquet nem chama o Rust — apenas
                     reporta o estado atual do catálogo para esta matriz.

        Returns:
            dict com o manifesto do backfill (ver shape na docstring da classe).

        Raises:
            MatrixFeatureCountMismatchError: divergência pós-backfill
                (apenas se dry_run=False).
        """
        n_catalogued_before = OmicMatrixFeature.objects.filter(matrix=matrix).count()

        if dry_run:
            match = (
                matrix.n_features is not None
                and n_catalogued_before == matrix.n_features
            )
            logger.info(
                'MatrixFeatureCatalogService (dry_run): matrix_id=%d, '
                'n_catalogued_before=%d, n_features_expected=%s',
                matrix.id,
                n_catalogued_before,
                matrix.n_features,
            )
            return {
                'matrix_id': matrix.id,
                'n_features_expected': matrix.n_features,
                'n_catalogued_before': n_catalogued_before,
                'n_inserted': None,
                'n_updated': None,
                'n_catalogued_after': n_catalogued_before,
                'match': match,
                'dry_run': True,
            }

        import rust_engine

        manifest = self._execute(matrix, rust_engine)

        n_catalogued_after = OmicMatrixFeature.objects.filter(matrix=matrix).count()
        match = (
            matrix.n_features is not None
            and n_catalogued_after == matrix.n_features
        )

        result = {
            'matrix_id': matrix.id,
            'n_features_expected': matrix.n_features,
            'n_catalogued_before': n_catalogued_before,
            'n_inserted': manifest.n_inserted,
            'n_updated': manifest.n_updated,
            'n_catalogued_after': n_catalogued_after,
            'match': match,
            'dry_run': False,
        }

        if not match:
            raise MatrixFeatureCountMismatchError(matrix, n_catalogued_after)

        logger.info(
            'MatrixFeatureCatalogService concluído: matrix_id=%d, '
            'n_inserted=%d, n_updated=%d, n_catalogued_after=%d '
            '(match=%s)',
            matrix.id,
            manifest.n_inserted,
            manifest.n_updated,
            n_catalogued_after,
            match,
        )
        return result

    def _execute(self, matrix: OmicMatrix, rust_engine) -> object:
        """
        Núcleo do backfill: download do Parquet → Rust → manifesto.

        rust_engine passado explicitamente para facilitar mock em testes.

        Regra #1: NÃO abre o conteúdo do Parquet em Python. Apenas baixa
        (I/O de orquestração aceito) e passa o caminho local ao Rust.
        """
        storage_key = matrix.storage_key
        if not storage_key:
            raise ValueError(
                f'OmicMatrix id={matrix.id} não tem storage_key definida — '
                'a matriz foi carregada corretamente?'
            )

        with tempfile.TemporaryDirectory(prefix='davinci_matrixfeat_') as tmp_dir:
            local_parquet = os.path.join(tmp_dir, 'matrix.parquet')

            logger.info(
                'MatrixFeatureCatalogService: downloading Parquet '
                '(matrix_id=%d, caminho local omitido)',
                matrix.id,
            )
            try:
                with default_storage.open(storage_key, 'rb') as remote_f:
                    with open(local_parquet, 'wb') as local_f:
                        shutil.copyfileobj(remote_f, local_f)
            except Exception as exc:
                raise RuntimeError(
                    f'Falha ao baixar Parquet da OmicMatrix id={matrix.id}: {exc}'
                ) from exc

            logger.info(
                'MatrixFeatureCatalogService: Parquet baixado (matrix_id=%d). '
                'Chamando rust_engine.catalog_matrix_features.',
                matrix.id,
            )

            # db_url — NUNCA logada.
            db = settings.DATABASES['default']
            db_url = (
                f"postgresql://{db['USER']}:{db['PASSWORD']}"
                f"@{db['HOST']}:{db['PORT']}/{db['NAME']}"
            )

            manifest = rust_engine.catalog_matrix_features(
                parquet_path=local_parquet,
                matrix_id=matrix.id,
                db_url=db_url,
            )

        # ── Tmpdir limpo automaticamente ao sair do with-block ────────────────
        return manifest
