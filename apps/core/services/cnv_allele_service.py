"""
CnvAlleleService — refino CNV×alelo (Fase 2, Objetivo 2, slice 2.7).

Refina o CNV seed v1 (heurística dosagem×papel — `fase2-cnv-v1`) para o
ESTADO CONJUNTO CNV×alelo (`fase2-cnv-v2`), agora que o alelo existe (seed SNV
do incremento 2.6). Fecha a simplificação temporária declarada no CNV v1.

Fronteira de camadas (Regra #1 / django-rust-boundary):
  - 100% Django set-based. Ambos os insumos já estão em PG (VariantEffectSeed
    cnv-v1 e snv). NÃO reabre o Parquet CNV, NÃO chama Rust.

Cruzamento (chave do desenho):
  - cnv-v1 seeds vivem na matriz CNV (omics_layer='copy_number'); snv seeds
    vivem na matriz SOMÁTICA. A OmicSample é COMPARTILHADA entre as matrizes
    (mesmo accession f"{case}_tumor" via get_or_create) — logo o cruzamento é
    por (sample_id, gene_symbol): o MESMO OmicSample aparece nas duas matrizes.

Coexistência (migration 0034):
  - A natural key de VariantEffectSeed inclui method_version, então
    'fase2-cnv-v2' coexiste com 'fase2-cnv-v1' para o mesmo
    (matrix, sample, gene, variant_key='', evidence_type='cnv') — habilita
    benchmark entre métodos, não sobrescreve o v1.

Tabela de decisão (cnv_direction × estado do alelo SNV × papel) — v1 do CNV×alelo,
baseada nos contraexemplos travados em objetivo2.md:
  | cnv-v1        | SNV LoF no gene? | v2 direction | magnitude        | confidence | caso                        |
  |---------------|------------------|--------------|------------------|------------|-----------------------------|
  | inactivator   | sim              | inactivator  | max(cnv, 0.9)    | 0.95       | inativação BIALÉLICA        |
  | inactivator   | não              | inactivator  | cnv              | 0.60       | perda MONOALÉLICA           |
  | activator     | sim              | neutral      | cnv (down-weight)| 0.40       | amplificar alelo danoso ≠ GoF (contraexemplo) |
  | activator     | não              | activator    | cnv              | 0.70       | amplificação limpa (herdado)|

Limitação DECLARADA (Regra #-1):
  A v2 refina SÓ os (sample, gene) que TÊM cnv-v1 seed (activator/inactivator).
  Casos que o v1 pulou (amplificar TSG / deletar oncogene / dual / sub-threshold)
  NÃO são refinados aqui — exigiriam reabrir o Parquet CNV (Rust) para todas as
  dosagens. Fica adiado (2E-conjunto completo).

Isolamento (Regra #3): a matriz CNV é resolvida via ProjectDataset do projeto
(mesmo padrão de CnvSeedService.resolve_matrix). VariantEffectSeed é catálogo
derivado (sem FK de projeto, ancorado em matrix/sample) — mesmo padrão já aceito.

Sem IngestionJob: derivação rápida set-based (como resolve_variant_effects) —
o command roda o service direto e reporta. Idempotente via bulk_create
update_conflicts na natural key (inclui method_version).
"""

from __future__ import annotations

import logging

from apps.core.models import (
    DaVinciProject,
    GeneRole,
    OmicMatrix,
    ProjectDataset,
    VariantEffectSeed,
)

logger = logging.getLogger(__name__)

# Versões de método envolvidas.
CNV_V1_METHOD = 'fase2-cnv-v1'
SNV_METHOD_PREFIX = 'fase2-snv'  # snv seeds (qualquer versão snv)
METHOD_VERSION = 'fase2-cnv-v2'

EFFECT_SOURCE = 'cnv_allele_joint'
_CNV_OMICS_LAYER = 'copy_number'

# Confidences fixas por caso (proveniência na tabela de decisão acima).
_CONF_BIALLELIC = 0.95
_CONF_MONOALLELIC = 0.60
_CONF_AMPLIFIED_DAMAGED = 0.40
_CONF_ACTIVATOR_CLEAN = 0.70

_ACTIVE_STATUSES = (
    ProjectDataset.CurationStatus.INCLUDED,
    ProjectDataset.CurationStatus.QUEUED_DOWNLOAD,
    ProjectDataset.CurationStatus.DOWNLOADED,
    ProjectDataset.CurationStatus.PENDING,
)

_Direction = VariantEffectSeed.Direction


class CnvAlleleMatrixNotFoundError(Exception):
    """Levantado quando não é possível resolver a OmicMatrix CNV para o projeto."""


class CnvAllelePrerequisiteError(Exception):
    """Levantado quando faltam cnv-v1 seeds ou snv seeds para o refino."""


class CnvAlleleService:
    """
    Refino CNV×alelo. Uso síncrono (management command):

        service = CnvAlleleService(project)
        result = service.run()            # ou run(matrix_id=...)

    Relatório retornado:
        {
            'matrix_id': int,
            'method_version': 'fase2-cnv-v2',
            'n_cnv_v1_seeds': int,
            'n_v2_created': int,
            'n_biallelic_reinforced': int,
            'n_amplified_damaged_neutralized': int,
            'n_monoallelic': int,
            'n_activator_inherited': int,
            'n_activator': int,
            'n_inactivator': int,
            'n_neutral': int,
        }
    """

    def __init__(self, project: DaVinciProject):
        self.project = project

    # ─────────────────────────────────────────────────────────────────────────
    # Resolução de matriz (espelha CnvSeedService.resolve_matrix)
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def resolve_matrix(
        cls,
        project: DaVinciProject,
        matrix_id: int | None = None,
    ) -> OmicMatrix:
        """Resolve a OmicMatrix CNV (copy_number) do projeto, isolada via ProjectDataset."""
        dataset_ids = ProjectDataset.objects.filter(
            project=project,
            curation_status__in=_ACTIVE_STATUSES,
        ).values_list('dataset_id', flat=True)

        qs = OmicMatrix.objects.select_related('dataset').filter(
            dataset_id__in=dataset_ids,
            omics_layer=_CNV_OMICS_LAYER,
        )

        if matrix_id is not None:
            matrix = qs.filter(id=matrix_id).first()
            if matrix is None:
                raise CnvAlleleMatrixNotFoundError(
                    f'OmicMatrix id={matrix_id} com omics_layer=copy_number '
                    f'não encontrada ou não pertence ao projeto {project.id}.'
                )
            return matrix

        matrices = list(qs)
        if not matrices:
            raise CnvAlleleMatrixNotFoundError(
                f'Nenhuma OmicMatrix copy_number encontrada no projeto '
                f'{project.id}. Execute load_cnv_matrix + derive_cnv_seeds primeiro.'
            )
        if len(matrices) > 1:
            ids = [m.id for m in matrices]
            raise CnvAlleleMatrixNotFoundError(
                f'Múltiplas OmicMatrix copy_number no projeto {project.id}: {ids}. '
                f'Especifique --matrix.'
            )
        return matrices[0]

    # ─────────────────────────────────────────────────────────────────────────
    # Execução
    # ─────────────────────────────────────────────────────────────────────────

    def run(self, matrix_id: int | None = None) -> dict:
        """
        Deriva os seeds CNV×alelo (fase2-cnv-v2) para a matriz CNV do projeto.

        Set-based:
          1. Carrega cnv-v1 seeds da matriz (uma query).
          2. Carrega snv seeds relevantes (uma query: sample_id__in + gene_symbol__in).
          3. Cruza em memória por (sample_id, gene_symbol), aplica a tabela de decisão.
          4. bulk_create(update_conflicts=True) na natural key (inclui method_version).
        """
        if not GeneRole.objects.exists():
            # papel do gene é pré-requisito do cnv-v1; se o v1 existe, GeneRole
            # deve existir — checagem defensiva.
            raise CnvAllelePrerequisiteError(
                'GeneRole vazio — execute load_gene_roles (o refino depende do '
                'papel que fundamentou o cnv-v1).'
            )

        matrix = self.resolve_matrix(self.project, matrix_id)

        # ── Passo 1: cnv-v1 seeds da matriz ──────────────────────────────────
        cnv_v1 = list(
            VariantEffectSeed.objects.filter(
                matrix=matrix,
                evidence_type=VariantEffectSeed.EvidenceType.CNV,
                method_version=CNV_V1_METHOD,
            ).values(
                'sample_id', 'gene_symbol', 'direction', 'magnitude', 'gene_role',
            )
        )
        if not cnv_v1:
            raise CnvAllelePrerequisiteError(
                f'Nenhum seed cnv-v1 (method_version={CNV_V1_METHOD}) para a matriz '
                f'{matrix.id}. Execute derive_cnv_seeds primeiro.'
            )

        sample_ids = {s['sample_id'] for s in cnv_v1}
        gene_symbols = {s['gene_symbol'] for s in cnv_v1}

        # ── Passo 2: snv seeds relevantes (alelo) ────────────────────────────
        snv = VariantEffectSeed.objects.filter(
            evidence_type=VariantEffectSeed.EvidenceType.SNV,
            sample_id__in=sample_ids,
            gene_symbol__in=gene_symbols,
        ).values('sample_id', 'gene_symbol', 'direction')

        if not snv:
            raise CnvAllelePrerequisiteError(
                'Nenhum seed SNV encontrado para as amostras/genes com CNV — '
                'execute load_somatic_maf primeiro (o alelo vem do SNV).'
            )

        # Conjunto de (sample_id, gene_symbol) com pelo menos um SNV inativador (LoF).
        lof_pairs: set[tuple[int, str]] = set()
        for s in snv:
            if s['direction'] == _Direction.INACTIVATOR:
                lof_pairs.add((s['sample_id'], s['gene_symbol']))

        # ── Passo 3: aplicar a tabela de decisão ─────────────────────────────
        counters = {
            'n_biallelic_reinforced': 0,
            'n_amplified_damaged_neutralized': 0,
            'n_monoallelic': 0,
            'n_activator_inherited': 0,
            'n_activator': 0,
            'n_inactivator': 0,
            'n_neutral': 0,
        }
        to_create: list[VariantEffectSeed] = []

        for seed in cnv_v1:
            key = (seed['sample_id'], seed['gene_symbol'])
            has_lof = key in lof_pairs
            cnv_dir = seed['direction']
            cnv_mag = seed['magnitude']

            if cnv_dir == _Direction.INACTIVATOR:
                if has_lof:
                    v2_dir = _Direction.INACTIVATOR
                    v2_mag = max(cnv_mag, 0.9)
                    v2_conf = _CONF_BIALLELIC
                    counters['n_biallelic_reinforced'] += 1
                else:
                    v2_dir = _Direction.INACTIVATOR
                    v2_mag = cnv_mag
                    v2_conf = _CONF_MONOALLELIC
                    counters['n_monoallelic'] += 1
            elif cnv_dir == _Direction.ACTIVATOR:
                if has_lof:
                    # amplificar alelo danoso ≠ ativar (contraexemplo travado)
                    v2_dir = _Direction.NEUTRAL
                    v2_mag = cnv_mag
                    v2_conf = _CONF_AMPLIFIED_DAMAGED
                    counters['n_amplified_damaged_neutralized'] += 1
                else:
                    v2_dir = _Direction.ACTIVATOR
                    v2_mag = cnv_mag
                    v2_conf = _CONF_ACTIVATOR_CLEAN
                    counters['n_activator_inherited'] += 1
            else:
                # cnv-v1 só grava activator/inactivator; defensivo.
                v2_dir = _Direction.NEUTRAL
                v2_mag = cnv_mag
                v2_conf = _CONF_MONOALLELIC

            if v2_dir == _Direction.ACTIVATOR:
                counters['n_activator'] += 1
            elif v2_dir == _Direction.INACTIVATOR:
                counters['n_inactivator'] += 1
            else:
                counters['n_neutral'] += 1

            to_create.append(VariantEffectSeed(
                matrix=matrix,
                sample_id=seed['sample_id'],
                gene_symbol=seed['gene_symbol'],
                variant_key='',
                evidence_type=VariantEffectSeed.EvidenceType.CNV,
                direction=v2_dir,
                magnitude=v2_mag,
                confidence=v2_conf,
                gene_role=seed['gene_role'],
                effect_source=EFFECT_SOURCE,
                method_version=METHOD_VERSION,
            ))

        # ── Passo 4: bulk upsert (coexiste com cnv-v1 via method_version) ─────
        VariantEffectSeed.objects.bulk_create(
            to_create,
            update_conflicts=True,
            unique_fields=[
                'matrix', 'sample', 'gene_symbol', 'variant_key',
                'evidence_type', 'method_version',
            ],
            update_fields=[
                'direction', 'magnitude', 'confidence', 'gene_role', 'effect_source',
            ],
        )

        logger.info(
            'CNV×alelo v2 concluído (matrix %d): n_v2=%d biallelic=%d '
            'amplified_damaged=%d monoallelic=%d activator_inherited=%d',
            matrix.id, len(to_create),
            counters['n_biallelic_reinforced'],
            counters['n_amplified_damaged_neutralized'],
            counters['n_monoallelic'],
            counters['n_activator_inherited'],
        )

        return {
            'matrix_id': matrix.id,
            'method_version': METHOD_VERSION,
            'n_cnv_v1_seeds': len(cnv_v1),
            'n_v2_created': len(to_create),
            **counters,
        }
