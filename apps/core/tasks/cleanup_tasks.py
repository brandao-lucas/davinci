"""
Celery beat tasks de manutenção de integridade de storage.

Responsabilidade: remover bytes órfãos/failed do object storage e logar o que
foi limpo.  NUNCA deleta registro DatasetFile com download_status='downloaded'
(curation-audit-trail).

Regra #2 (curation-audit-trail): só remove bytes físicos de arquivos cujo
DatasetFile está 'failed' ou cujo storage_key aponta para objeto inexistente.
Registros 'downloaded' são invioláveis — perder o byte é reversível (re-baixa);
perder o registro de auditoria não é.
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.core.files.storage import default_storage

from apps.core.models import DatasetFile

logger = logging.getLogger(__name__)


@shared_task
def cleanup_orphan_files() -> dict:
    """
    Remove do object storage os bytes de DatasetFile em estado 'failed' ou
    cujo storage_key aponta para objeto inexistente.

    Invariantes de segurança (curation-audit-trail):
    - Nunca apaga arquivo de registro com download_status='downloaded'.
    - Nunca deleta registros do banco — apenas limpa bytes do storage e reseta
      storage_key para '' nos casos de objeto ausente.
    - Loga contagem detalhada do que foi limpo.

    Retorna: dict com contadores para observabilidade.
    """
    deleted_failed = 0
    skipped_missing_key = 0
    storage_errors = 0
    marked_missing = 0

    # ── Passo 1: arquivos 'failed' com storage_key preenchido ─────────────────
    # O Rust ou o upload Django falhou após gravar o storage_key no banco, mas
    # antes de marcar 'downloaded'.  O byte pode existir no storage — limpar.
    failed_with_key = DatasetFile.objects.filter(
        download_status=DatasetFile.DownloadStatus.FAILED,
        storage_key__gt='',  # storage_key != ''
    )

    for df in failed_with_key.iterator():
        try:
            if default_storage.exists(df.storage_key):
                default_storage.delete(df.storage_key)
                logger.info(
                    'cleanup_orphan_files: removido storage_key=%s (DatasetFile id=%s, status=failed)',
                    df.storage_key,
                    df.id,
                )
                deleted_failed += 1
            else:
                skipped_missing_key += 1

            # Reseta storage_key independente: o registro fica, bytes foram limpos
            DatasetFile.objects.filter(id=df.id).update(storage_key='')

        except Exception as exc:
            logger.error(
                'cleanup_orphan_files: falha ao remover storage_key=%s (DatasetFile id=%s): %s',
                df.storage_key,
                df.id,
                exc,
            )
            storage_errors += 1

    # ── Passo 2: arquivos 'downloaded' com storage_key ausente no storage ──────
    # Inconsistência: banco diz 'downloaded' mas objeto sumiu do storage.
    # NÃO apagamos o registro (curation-audit-trail), mas marcamos erro para
    # que o operador saiba que precisa re-baixar.
    # ATENÇÃO: esta checagem pode ser cara se houver muitos registros downloaded.
    # Ajustar o queryset conforme o volume (ex: limitar a downloaded_at antigas).
    downloaded_with_key = DatasetFile.objects.filter(
        download_status=DatasetFile.DownloadStatus.DOWNLOADED,
        storage_key__gt='',
    )

    for df in downloaded_with_key.iterator():
        try:
            if not default_storage.exists(df.storage_key):
                # Objeto sumiu: marca error_message mas mantém status e registro
                DatasetFile.objects.filter(id=df.id).update(
                    error_message=(
                        'storage_key ausente no object storage — '
                        'arquivo pode precisar ser re-baixado'
                    ),
                )
                logger.warning(
                    'cleanup_orphan_files: DatasetFile id=%s marcado como ausente no storage '
                    '(storage_key=%s existe no banco mas não no storage)',
                    df.id,
                    df.storage_key,
                )
                marked_missing += 1
        except Exception as exc:
            logger.error(
                'cleanup_orphan_files: falha ao verificar storage_key=%s (DatasetFile id=%s): %s',
                df.storage_key,
                df.id,
                exc,
            )
            storage_errors += 1

    summary = {
        'deleted_failed_bytes': deleted_failed,
        'skipped_already_absent': skipped_missing_key,
        'marked_missing_in_storage': marked_missing,
        'storage_errors': storage_errors,
    }
    logger.info('cleanup_orphan_files concluído: %s', summary)
    return summary
