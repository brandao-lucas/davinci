"""
DatasetGeneService — carga idempotente de genes no nível de dataset.

Responsabilidades:
  - populate_from_paper_links(dataset):    popula DatasetGene com source='paper_link'
    a partir de PaperGene dos papers ligados ao dataset via DatasetPaperLink.
  - populate_from_dataset_metadata(dataset): popula DatasetGene com
    source='dataset_metadata' chamando rust_engine.extract_genes() sobre o
    texto composto dos campos textuais do OmicDataset.
  - populate_dataset(dataset, sources):    orquestra as duas fontes.
  - populate_all(sources, batch_size):     itera sobre todos os datasets ativos.

Unicidade:
    DatasetGene.unique_together = ['dataset', 'gene_symbol', 'source'].
    UPSERT via bulk_create com update_conflicts=True.
    Re-rodar atualiza mention_count e entrez_id sem duplicar.

Fronteira Django/Rust:
    O NER (extract_genes) roda no Rust e é chamado aqui como função síncrona.
    Django não faz parse de XML nem NER — apenas monta o texto e chama o binding.

Texto para NER (source='dataset_metadata'):
    Campos de OmicDataset concatenados em ordem de relevância semântica:
      1. title
      2. summary
      3. extra_metadata['contract']['disease_raw'] (lista → join, para PRIDE)
      4. extra_metadata['keywords'] (lista ou string, para PRIDE e outros)
    Campos nulos/vazios são silenciosamente omitidos.
    Texto resultante vazio → não cria nenhum registro.
"""

import logging

from django.db import transaction
from django.db.models import Max, Sum, Q

logger = logging.getLogger(__name__)

# Fontes suportadas
SOURCE_PAPER_LINK = 'paper_link'
SOURCE_DATASET_METADATA = 'dataset_metadata'
ALL_SOURCES = (SOURCE_PAPER_LINK, SOURCE_DATASET_METADATA)

# Tamanho de batch para bulk_create
_BATCH_SIZE = 500


def _build_metadata_text(dataset) -> str:
    """
    Monta o texto do dataset para NER a partir dos campos textuais.

    Ordem de concatenação:
      1. title (campo próprio)
      2. summary (campo próprio)
      3. extra_metadata['contract']['disease_raw'] (lista de strings, PRIDE)
      4. extra_metadata['keywords'] (lista ou string, PRIDE e outros)

    Campos ausentes/vazios são omitidos silenciosamente.
    Retorna string vazia se não há texto.
    """
    parts: list[str] = []

    if dataset.title:
        parts.append(dataset.title.strip())

    if dataset.summary:
        parts.append(dataset.summary.strip())

    extra = dataset.extra_metadata or {}

    # PRIDE: extra_metadata['contract']['disease_raw'] é lista de strings
    contract = extra.get('contract') or {}
    disease_raw = contract.get('disease_raw')
    if disease_raw:
        if isinstance(disease_raw, list):
            joined = ' '.join(str(v) for v in disease_raw if v)
        else:
            joined = str(disease_raw)
        if joined.strip():
            parts.append(joined.strip())

    # PRIDE: extra_metadata['keywords'] (lista de strings)
    keywords = extra.get('keywords')
    if keywords:
        if isinstance(keywords, list):
            joined_kw = ' '.join(str(k) for k in keywords if k)
        else:
            joined_kw = str(keywords)
        if joined_kw.strip():
            parts.append(joined_kw.strip())

    return ' '.join(parts)


class DatasetGeneService:
    """
    Serviço de população de DatasetGene.

    Todos os métodos são idempotentes (UPSERT via update_conflicts).
    """

    @staticmethod
    def populate_from_paper_links(dataset) -> dict:
        """
        Popula DatasetGene (source='paper_link') para um OmicDataset.

        Estratégia:
          - Consulta PaperGene filtrado pelos papers ligados ao dataset
            via DatasetPaperLink.
          - Agrupa por gene_symbol: soma mention_count; usa Max(entrez_id)
            como representativo (mesmo padrão de GeneService).
          - UPSERT via bulk_create com update_conflicts=True.

        Retorna dict com 'gene_count' (genes distintos encontrados).
        """
        from apps.core.models import DatasetGene, PaperGene

        # Agrega genes dos papers ligados ao dataset.
        gene_rows = (
            PaperGene.objects
            .filter(paper__datasetpaperlink__dataset=dataset)
            .values('gene_symbol')
            .annotate(
                total_mention_count=Sum('mention_count'),
                representative_entrez_id=Max('entrez_id'),
            )
        )

        if not gene_rows:
            logger.debug(
                'DatasetGeneService.paper_links: dataset=%s sem PaperGene ligados',
                dataset.accession,
            )
            return {'gene_count': 0}

        objs = [
            DatasetGene(
                dataset=dataset,
                gene_symbol=row['gene_symbol'],
                entrez_id=row['representative_entrez_id'],
                mention_count=row['total_mention_count'] or 1,
                source=SOURCE_PAPER_LINK,
            )
            for row in gene_rows
        ]

        DatasetGene.objects.bulk_create(
            objs,
            update_conflicts=True,
            unique_fields=['dataset', 'gene_symbol', 'source'],
            update_fields=['entrez_id', 'mention_count'],
        )

        logger.info(
            'DatasetGeneService.paper_links: dataset=%s genes=%d',
            dataset.accession,
            len(objs),
        )
        return {'gene_count': len(objs)}

    @staticmethod
    def populate_from_dataset_metadata(dataset) -> dict:
        """
        Popula DatasetGene (source='dataset_metadata') para um OmicDataset.

        Monta texto dos campos textuais do dataset, chama rust_engine.extract_genes()
        e faz UPSERT em DatasetGene.

        Retorna dict com 'gene_count' e 'text_length'.
        """
        import rust_engine
        from apps.core.models import DatasetGene

        text = _build_metadata_text(dataset)
        if not text:
            logger.debug(
                'DatasetGeneService.metadata: dataset=%s texto vazio — skip',
                dataset.accession,
            )
            return {'gene_count': 0, 'text_length': 0}

        gene_tuples: list[tuple[str, int]] = rust_engine.extract_genes(text)

        if not gene_tuples:
            logger.debug(
                'DatasetGeneService.metadata: dataset=%s text_len=%d sem genes encontrados',
                dataset.accession,
                len(text),
            )
            return {'gene_count': 0, 'text_length': len(text)}

        objs = [
            DatasetGene(
                dataset=dataset,
                gene_symbol=symbol,
                entrez_id=None,  # NER de metadados não resolve Entrez ID
                mention_count=count,
                source=SOURCE_DATASET_METADATA,
            )
            for symbol, count in gene_tuples
        ]

        DatasetGene.objects.bulk_create(
            objs,
            update_conflicts=True,
            unique_fields=['dataset', 'gene_symbol', 'source'],
            update_fields=['mention_count'],
        )

        logger.info(
            'DatasetGeneService.metadata: dataset=%s text_len=%d genes=%d',
            dataset.accession,
            len(text),
            len(objs),
        )
        return {'gene_count': len(objs), 'text_length': len(text)}

    @classmethod
    def populate_dataset(
        cls,
        dataset,
        *,
        sources: tuple[str, ...] = ALL_SOURCES,
    ) -> dict:
        """
        Orquestra a população de DatasetGene para um único dataset.

        Parâmetros:
            dataset — instância de OmicDataset.
            sources — subconjunto de ALL_SOURCES a processar.
                      Default: ambas as fontes.

        Retorna dict por fonte com contagens.
        """
        result: dict = {}

        if SOURCE_PAPER_LINK in sources:
            result[SOURCE_PAPER_LINK] = cls.populate_from_paper_links(dataset)

        if SOURCE_DATASET_METADATA in sources:
            result[SOURCE_DATASET_METADATA] = cls.populate_from_dataset_metadata(dataset)

        return result

    @classmethod
    def populate_all(
        cls,
        *,
        sources: tuple[str, ...] = ALL_SOURCES,
        batch_size: int = 100,
        limit: int | None = None,
    ) -> dict:
        """
        Itera sobre todos os OmicDatasets ativos e popula DatasetGene.

        Parâmetros:
            sources    — fontes a processar.
            batch_size — tamanho do chunk para queryset.iterator().
            limit      — máximo de datasets a processar (None = sem limite).

        Retorna dict com estatísticas agregadas.
        """
        from apps.core.models import OmicDataset

        qs = OmicDataset.objects.filter(is_active=True).order_by('id')
        if limit:
            qs = qs[:limit]

        totals: dict[str, int] = {src: 0 for src in sources}
        processed = 0
        errors = 0

        for dataset in qs.iterator(chunk_size=batch_size):
            try:
                result = cls.populate_dataset(dataset, sources=sources)
                for src in sources:
                    totals[src] += result.get(src, {}).get('gene_count', 0)
                processed += 1
            except Exception:
                errors += 1
                logger.exception(
                    'DatasetGeneService.populate_all: erro em dataset=%s',
                    dataset.accession,
                )

        logger.info(
            'DatasetGeneService.populate_all: processed=%d errors=%d totals=%s',
            processed,
            errors,
            totals,
        )
        return {'processed': processed, 'errors': errors, 'gene_counts': totals}
