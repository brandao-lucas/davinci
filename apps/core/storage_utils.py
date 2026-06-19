"""
Utilitários de object storage para o DaVinci.

Convenção de path namespacing (sem trailing slash):

    omics/{user_id}/{project_id}/{dataset_accession}/{filename}

Exemplo:
    omics/42/7/GSE123456/GSE123456_series_matrix.txt.gz

O prefixo garante isolamento por usuário e projeto dentro do bucket
`davinci-omics`.  Nenhum componente do path é exposto diretamente ao
cliente — o endpoint de download do Django gera presigned URL ou proxy
autenticado (decisão D6 do plano).
"""

from __future__ import annotations


def omics_storage_key(
    user_id: int | str,
    project_id: int | str,
    dataset_accession: str,
    filename: str,
) -> str:
    """
    Retorna a chave (path) no object storage para um arquivo ômico.

    Args:
        user_id: PK do User dono do projeto.
        project_id: PK do Project ao qual o OmicDataset pertence.
        dataset_accession: Accession do dataset (ex. 'GSE123456', 'SRP999').
        filename: Nome do arquivo remoto (ex. 'GSE123456_series_matrix.txt.gz').

    Returns:
        String sem leading/trailing slash adequada para uso como
        ``storage_key`` em ``DatasetFile.storage_key``.
    """
    return f"omics/{user_id}/{project_id}/{dataset_accession}/{filename}"
