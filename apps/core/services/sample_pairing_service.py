"""
SamplePairingService — pareamento tumor↔normal nível-amostra.

OmnisPathway Objetivo 2, Fase 1, passo 1.2.

Responsabilidade:
  Dada uma OmicMatrix, inspecionar seus OmicMatrixSample, extrair o case_id
  de cada OmicSample, agrupar por case_id, e materializar um OmicSamplePairing
  para cada case_id que tenha SIMULTANEAMENTE um sample tumor E um sample normal
  na mesma matriz (sobreposição real, não coincidência de chave assumida).

Fronteira de camadas (Regra #1 / django-rust-boundary):
  - Django ORM set-based puro — ZERO Rust, NÃO abre o Parquet.
  - Trabalha apenas sobre metadados Postgres: OmicMatrixSample + OmicSample.

Isolamento (firebase-auth-guard / Regra #3):
  - A matrix é resolvida via ProjectDataset do projeto do usuário.
  - OmicSamplePairing é catálogo derivado (global, ancorado em matrix/dataset).
  - Não há FK de projeto no par — o isolamento é garantido na resolução da
    matrix de entrada (somente matrizes de datasets vinculados ao projeto).

Precedência de extração de case_id:
  PRIORIDADE 1 — OmicSample.characteristics['case_id']
    Fonte estruturada gravada pelo loader da Fase 0 (matrix_load_service.py,
    linha ~558). É a fonte canônica e deve estar presente em 100% das amostras
    carregadas pela Fase 0.
  FALLBACK 2 — derivar do OmicSample.accession removendo sufixo _tumor/_normal
    (rsplit por '_', 2 partes, primeiro segmento). Cobre amostras legado ou
    carregadas por outros loaders sem characteristics['case_id'].
  Discrepância entre as duas fontes é logada como WARNING (não é erro — a fonte
  primária é characteristics).

Idempotência:
  bulk_create(ignore_conflicts=True) na UniqueConstraint(matrix, case_id) do
  OmicSamplePairing. Rodar 2x não duplica. Os pares já existentes são
  contados separadamente no relatório (n_pairs_existing).

Catálogo derivado:
  OmicSamplePairing não é curadoria de usuário — a Regra #2 (rastreabilidade)
  NÃO se aplica. Delete/regeneração são permitidos.

Versão do algoritmo de pareamento:
  PAIRING_VERSION = 'fase1-v1'
  Muda quando a lógica de extração/agrupamento mudar de forma incompatível.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from django.db import transaction

from apps.core.models import (
    DaVinciProject,
    OmicDataset,
    OmicMatrix,
    OmicMatrixSample,
    OmicSamplePairing,
    ProjectDataset,
)

logger = logging.getLogger(__name__)

# Versão do algoritmo de pareamento — incrementar quando a lógica mudar.
PAIRING_VERSION = 'fase1-v1'

# Constante do estudo CCRCC (usado na auto-detecção por omics_layer).
_CCRCC_ACCESSION = 'CPTAC-CCRCC-PROTEOME'
_CCRCC_OMICS_LAYER = 'proteomic'


class MatrixNotFoundError(Exception):
    """Levantado quando não é possível resolver a OmicMatrix para o projeto."""


class SamplePairingService:
    """
    Serviço de pareamento nível-amostra para uma OmicMatrix.

    Uso típico:
        service = SamplePairingService(matrix)
        report = service.run(dry_run=False)

    O relatório retornado tem a seguinte shape:
        {
            'matrix_id': int,
            'matrix_dataset': str,        # accession do dataset
            'matrix_layer': str,          # omics_layer
            'pairing_version': str,       # PAIRING_VERSION
            'n_matrix_samples': int,      # total de OmicMatrixSample na matriz
            'n_case_ids_total': int,      # case_ids distintos encontrados
            'n_pairs_created': int,       # pares novos materializados
            'n_pairs_existing': int,      # pares já existentes (idempotência)
            'n_unpaired_tumor_only': int, # case_ids só com tumor (sem par)
            'n_unpaired_normal_only': int,# case_ids só com normal (sem par)
            # DIAGNÓSTICO — deve ser vazio na granularidade da Fase 0:
            'multi_aliquota_tumor': list[str],   # case_ids com >1 sample tumor
            'multi_aliquota_normal': list[str],  # case_ids com >1 sample normal
            'fallback_accession_used': list[str],# accessions que usaram fallback
            'characteristics_missing': list[str],# accessions sem characteristics
            'dry_run': bool,
        }
    """

    def __init__(self, matrix: OmicMatrix):
        self.matrix = matrix

    # ─────────────────────────────────────────────────────────────────────────
    # API pública
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def resolve_matrix(cls, project: DaVinciProject, matrix_id: int | None = None) -> OmicMatrix:
        """
        Resolve a OmicMatrix para o projeto dado.

        Isolamento: só resolve matrizes de datasets vinculados ao projeto via
        ProjectDataset — garante que não há acesso cross-project.

        Args:
            project: DaVinciProject do usuário (isolamento).
            matrix_id: ID específico de OmicMatrix. Se None, auto-detecta a
                       única matriz CCRCC proteome vinculada ao projeto.

        Returns:
            OmicMatrix resolvida.

        Raises:
            MatrixNotFoundError: não encontrada ou não pertence ao projeto.
        """
        # Datasets vinculados ao projeto com status ativo (finding A1).
        # Exclui 'excluded' para que datasets descartados pelo pesquisador
        # não sejam elegíveis a pareamento.
        # O matrix_load_service grava ProjectDataset com curation_status='included',
        # portanto o happy path load → pair não é afetado por este filtro.
        _ACTIVE_STATUSES = (
            ProjectDataset.CurationStatus.INCLUDED,
            ProjectDataset.CurationStatus.QUEUED_DOWNLOAD,
            ProjectDataset.CurationStatus.DOWNLOADED,
            ProjectDataset.CurationStatus.PENDING,
        )
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
                raise MatrixNotFoundError(
                    f'OmicMatrix id={matrix_id} não encontrada ou não pertence '
                    f'ao projeto {project.id}.'
                )
            return matrix

        # Auto-detecção: matriz CCRCC proteome do projeto
        matrix = qs.filter(
            dataset__accession=_CCRCC_ACCESSION,
            omics_layer=_CCRCC_OMICS_LAYER,
        ).first()
        if matrix is None:
            raise MatrixNotFoundError(
                f'Nenhuma OmicMatrix CCRCC proteome encontrada no projeto '
                f'{project.id}. Execute load_cptac_matrix primeiro.'
            )
        return matrix

    def run(self, dry_run: bool = False) -> dict:
        """
        Executa o pareamento (ou simula se dry_run=True).

        Fluxo:
          1. Carrega OmicMatrixSample com select_related('sample') — UMA query.
          2. Para cada amostra, extrai case_id (características → fallback accession).
          3. Agrupa por case_id × sample_role. Detecta multi-alíquota.
          4. Para cada case_id com tumor E normal: prepara OmicSamplePairing.
          5. (se não dry_run) bulk_create(ignore_conflicts=True).
          6. Retorna relatório estruturado.

        Args:
            dry_run: Se True, executa tudo (incluindo agrupamento e contagens)
                     mas NÃO persiste nenhum OmicSamplePairing.

        Returns:
            dict com shape documentado na docstring da classe.
        """
        # ── Passo 1: uma query set-based ─────────────────────────────────────
        matrix_samples = list(
            OmicMatrixSample.objects.filter(
                matrix=self.matrix,
            ).select_related('sample')
        )

        logger.info(
            'SamplePairingService: matrix_id=%d, n_matrix_samples=%d, dry_run=%s',
            self.matrix.id,
            len(matrix_samples),
            dry_run,
        )

        # ── Passo 2: extração de case_id por amostra ─────────────────────────
        # Estrutura: role → case_id → list[OmicSample]
        # (list para detectar multi-alíquota — deve ter exatamente 1 elemento)
        by_role_and_case: dict[str, dict[str, list]] = {
            'tumor': defaultdict(list),
            'normal': defaultdict(list),
        }

        fallback_used: list[str] = []
        characteristics_missing: list[str] = []

        for oms in matrix_samples:
            sample = oms.sample
            role = oms.sample_role  # fonte canônica validada por CheckConstraint

            case_id, used_fallback, had_missing = _extract_case_id(sample)

            if had_missing:
                characteristics_missing.append(sample.accession)
            if used_fallback:
                fallback_used.append(sample.accession)

            if role in ('tumor', 'normal'):
                by_role_and_case[role][case_id].append(sample)
            # role='unknown' não entra no pareamento — não há papel definido

        # ── Passo 3: detectar multi-alíquota (diagnóstico) ───────────────────
        multi_tumor = [
            cid for cid, samples in by_role_and_case['tumor'].items()
            if len(samples) > 1
        ]
        multi_normal = [
            cid for cid, samples in by_role_and_case['normal'].items()
            if len(samples) > 1
        ]

        if multi_tumor:
            logger.warning(
                'SamplePairingService: %d case_ids com múltiplas alíquotas tumor '
                '(não serão pareados): %s',
                len(multi_tumor),
                multi_tumor[:10],  # máx. 10 no log para não poluir
            )
        if multi_normal:
            logger.warning(
                'SamplePairingService: %d case_ids com múltiplas alíquotas normal '
                '(não serão pareados): %s',
                len(multi_normal),
                multi_normal[:10],
            )

        # ── Passo 4: agrupamento e separação paired / unpaired ────────────────
        all_tumor_case_ids = set(by_role_and_case['tumor'].keys())
        all_normal_case_ids = set(by_role_and_case['normal'].keys())

        # Multi-alíquotas são excluídas do pareamento (produto cartesiano espúrio)
        multi_set = set(multi_tumor) | set(multi_normal)
        valid_tumor = all_tumor_case_ids - multi_set
        valid_normal = all_normal_case_ids - multi_set

        paired_case_ids = valid_tumor & valid_normal
        unpaired_tumor_only = valid_tumor - valid_normal
        unpaired_normal_only = valid_normal - valid_tumor
        n_case_ids_total = len(all_tumor_case_ids | all_normal_case_ids)

        logger.info(
            'SamplePairingService: n_case_ids_total=%d, paired=%d, '
            'unpaired_tumor_only=%d, unpaired_normal_only=%d, '
            'multi_aliquota=%d',
            n_case_ids_total,
            len(paired_case_ids),
            len(unpaired_tumor_only),
            len(unpaired_normal_only),
            len(multi_set),
        )

        # ── Passo 5: preparar objetos OmicSamplePairing ──────────────────────
        pairings_to_create = []
        for case_id in sorted(paired_case_ids):
            tumor_sample = by_role_and_case['tumor'][case_id][0]
            normal_sample = by_role_and_case['normal'][case_id][0]
            pairings_to_create.append(
                OmicSamplePairing(
                    matrix=self.matrix,
                    tumor_sample=tumor_sample,
                    normal_sample=normal_sample,
                    case_id=case_id,
                    pairing_basis=OmicSamplePairing.PairingBasis.SHARED_CASE_ID,
                    confidence=1.0,
                    validated_overlap=True,  # tumor E normal confirmados na mesma matriz
                    pairing_version=PAIRING_VERSION,
                )
            )

        # ── Passo 6: persistência (ou dry_run) ───────────────────────────────
        n_pairs_created = 0
        n_pairs_existing = 0

        if dry_run:
            logger.info(
                'SamplePairingService: dry_run=True — %d pares NÃO gravados.',
                len(pairings_to_create),
            )
            # Em dry_run, "criados" = quantos seriam criados; "existing" = quantos
            # já existem no banco (para feedback informativo).
            n_pairs_existing = OmicSamplePairing.objects.filter(
                matrix=self.matrix,
            ).count()
            n_pairs_created = len(pairings_to_create)
        else:
            n_pairs_created, n_pairs_existing = self._persist(pairings_to_create)

        report = {
            'matrix_id': self.matrix.id,
            'matrix_dataset': self.matrix.dataset.accession,
            'matrix_layer': self.matrix.omics_layer,
            'pairing_version': PAIRING_VERSION,
            'n_matrix_samples': len(matrix_samples),
            'n_case_ids_total': n_case_ids_total,
            'n_pairs_created': n_pairs_created,
            'n_pairs_existing': n_pairs_existing,
            'n_unpaired_tumor_only': len(unpaired_tumor_only),
            'n_unpaired_normal_only': len(unpaired_normal_only),
            'multi_aliquota_tumor': sorted(multi_tumor),
            'multi_aliquota_normal': sorted(multi_normal),
            'fallback_accession_used': sorted(fallback_used),
            'characteristics_missing': sorted(characteristics_missing),
            'dry_run': dry_run,
        }

        logger.info(
            'SamplePairingService: concluído — pairs_created=%d, pairs_existing=%d, '
            'dry_run=%s',
            n_pairs_created,
            n_pairs_existing,
            dry_run,
        )

        return report

    # ─────────────────────────────────────────────────────────────────────────
    # Implementação interna
    # ─────────────────────────────────────────────────────────────────────────

    def _persist(self, pairings: list[OmicSamplePairing]) -> tuple[int, int]:
        """
        Persiste os pares via bulk_create(ignore_conflicts=True).

        Idempotência garantida pela UniqueConstraint(matrix, case_id): rodar 2x
        não duplica. O PostgreSQL ignora o conflito silenciosamente.

        Returns:
            (n_created, n_existing) — contagens para o relatório.
        """
        existing_before = OmicSamplePairing.objects.filter(
            matrix=self.matrix,
        ).count()

        if pairings:
            with transaction.atomic():
                OmicSamplePairing.objects.bulk_create(
                    pairings,
                    ignore_conflicts=True,
                )
            logger.info(
                'SamplePairingService: bulk_create concluído (matrix=%d, '
                'n_candidatos=%d)',
                self.matrix.id,
                len(pairings),
            )

        existing_after = OmicSamplePairing.objects.filter(
            matrix=self.matrix,
        ).count()

        n_created = existing_after - existing_before
        n_existing = existing_before

        return n_created, n_existing


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de módulo
# ─────────────────────────────────────────────────────────────────────────────

def _extract_case_id(sample) -> tuple[str, bool, bool]:
    """
    Extrai o case_id de um OmicSample.

    Precedência:
      1. sample.characteristics['case_id']  — fonte canônica (Fase 0)
      2. Derivar de sample.accession removendo sufixo _tumor/_normal

    Valida consistência entre as duas fontes: se ambas existem mas divergem,
    loga WARNING e mantém characteristics (fonte primária).

    Returns:
        (case_id, used_fallback, had_missing_characteristics)
        - used_fallback: True se characteristics['case_id'] estava ausente
        - had_missing_characteristics: True se characteristics era None/vazio
    """
    chars = sample.characteristics or {}
    char_case_id: str | None = chars.get('case_id') if isinstance(chars, dict) else None

    # Derivar do accession como fallback/validação
    accession_case_id = _derive_case_id_from_accession(sample.accession)

    had_missing = char_case_id is None
    used_fallback = False

    if char_case_id:
        # Fonte primária disponível — validar contra accession
        if accession_case_id and accession_case_id != char_case_id:
            logger.warning(
                'case_id diverge entre characteristics e accession para '
                'OmicSample %s: characteristics=%r vs accession_derivado=%r — '
                'usando characteristics (fonte primária)',
                sample.accession,
                char_case_id,
                accession_case_id,
            )
        return char_case_id, False, False
    else:
        # Fallback: derivar do accession
        used_fallback = True
        if had_missing:
            logger.debug(
                'OmicSample %s: characteristics["case_id"] ausente — '
                'usando fallback do accession: %r',
                sample.accession,
                accession_case_id,
            )
        return accession_case_id or sample.accession, used_fallback, had_missing


def _derive_case_id_from_accession(accession: str) -> str | None:
    """
    Deriva o case_id removendo o sufixo _tumor/_normal do accession.

    Exemplos:
        'C3L-00004_tumor'  → 'C3L-00004'
        'C3L-00004_normal' → 'C3L-00004'
        'C3L-00004'        → None  (sem sufixo reconhecido — não deriva)
        'GSM123456'        → None  (sem sufixo — não é padrão CPTAC)

    Retorna None se o accession não termina com _tumor ou _normal (não force
    uma derivação que pode ser incorreta para amostras de outros loaders).
    """
    for suffix in ('_tumor', '_normal'):
        if accession.endswith(suffix):
            return accession[: -len(suffix)]
    return None
