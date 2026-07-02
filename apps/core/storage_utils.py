"""
Utilitários de object storage para o DaVinci.

Convenção de path namespacing (sem trailing slash):

  Dados isolados por projeto (arquivos raw, controlados, etc.):
    omics/{user_id}/{project_id}/{dataset_accession}/{filename}

  Dados públicos compartilhados (matrizes Parquet de acesso aberto):
    omics/_shared/{dataset_accession}/{filename}

Exemplos:
    omics/42/7/GSE123456/GSE123456_series_matrix.txt.gz
    omics/_shared/CPTAC-CCRCC-PROTEOME/cptac_ccrcc_proteome.parquet

O prefixo `_shared` é reservado — nunca coincide com um user_id real
(que são inteiros) e sinaliza que o dado é público e reutilizável entre
projetos.  O isolamento de acesso é via ProjectDataset; o dado em si não
é duplicado por projeto.

Nenhum componente do path é exposto diretamente ao cliente — o endpoint
de download do Django gera presigned URL ou proxy autenticado.

Sanitização de path (path-traversal guard) — shared namespace
--------------------------------------------------------------
``dataset_accession`` e ``filename`` passados para ``shared_omics_storage_key``
são validados antes de qualquer interpolação.  Somente caracteres do conjunto
``[A-Za-z0-9._-]`` são aceitos.  Qualquer outro valor levanta ``ValueError``
imediatamente, impedindo path traversal na object key (sequências '..', '/',
'\\\\', '\\0', espaços, paths absolutos, etc.).

Essa validação se aplica **apenas** ao namespace compartilhado (_shared), onde
accession vira componente de diretório num namespace comum a todos os projetos e
os inputs são constantes de datasets públicos (ex.: 'CPTAC-CCRCC-PROTEOME').

``omics_storage_key`` (namespace project-scoped) não aplica o allowlist estrito:
o ``filename`` ali vem de ``os.path.basename(local_path)`` (traversal já
neutralizado na origem) e accessions reais de GEO/PRIDE/SRA podem conter
caracteres fora do conjunto (espaço, parênteses, '+'), o que quebraria downloads
legítimos.

Caracteres aceitos pelo allowlist (shared): letras ASCII, dígitos, ponto, hífen, underscore.
Exemplos válidos : 'CPTAC-CCRCC-PROTEOME', 'cptac_ccrcc_proteome.parquet'
Exemplos rejeitados: '../secret', 'a/b', 'x\\\\y', 'foo bar', '/abs/path'
"""

from __future__ import annotations

import re

# Allowlist restrito: letras ASCII, dígitos, ponto, hífen, underscore.
# Intencionalmente não inclui '/', '\\', espaço, '\0' nem qualquer outro
# caractere que permita path traversal ou colisão de chaves.
_SAFE_PATH_COMPONENT_RE = re.compile(r'^[A-Za-z0-9._-]+$')


def _validate_path_component(value: str, label: str) -> None:
    """
    Valida que *value* é seguro para interpolação em object-storage key.

    Levanta ``ValueError`` se *value*:
    - for vazio;
    - contiver qualquer caractere fora de ``[A-Za-z0-9._-]``; ou
    - for exatamente ``'.'`` ou ``'..'`` (componentes de path relativo).

    Nota: valores que apenas *contêm* pontos internos são válidos
    (ex.: ``'v1.2.3'``, ``'file..name'``).  Apenas o componente que é
    *exatamente* ``'.'`` ou ``'..'`` é path traversal, pois ``'/'`` e
    ``'\\'`` já são bloqueados pelo regex.

    Args:
        value: Componente a validar (``dataset_accession`` ou ``filename``).
        label: Nome do argumento, usado na mensagem de erro.

    Raises:
        ValueError: Se *value* for vazio, contiver caractere não permitido,
            ou for um componente de path relativo (``'.'`` / ``'..'``).
    """
    if not value:
        raise ValueError(
            f"storage_utils: {label} não pode ser vazio."
        )
    if not _SAFE_PATH_COMPONENT_RE.match(value):
        raise ValueError(
            f"storage_utils: {label} contém caracteres não permitidos. "
            f"Aceito apenas [A-Za-z0-9._-]. Recebido: {value!r}"
        )
    if value in ('.', '..'):
        raise ValueError(
            f"storage_utils: {label} inválido: componente de path relativo "
            f"não permitido. Recebido: {value!r}"
        )


def omics_storage_key(
    user_id: int | str,
    project_id: int | str,
    dataset_accession: str,
    filename: str,
) -> str:
    """
    Retorna a chave (path) no object storage para um arquivo ômico
    isolado por projeto.

    Usar para dados de acesso controlado ou arquivos raw vinculados a um
    projeto específico.  Para matrizes Parquet de datasets públicos,
    prefira ``shared_omics_storage_key``.

    Não aplica o allowlist estrito de ``_validate_path_component``:
    ``filename`` chega via ``os.path.basename(local_path)`` (path traversal
    neutralizado na origem) e accessions reais de GEO/PRIDE/SRA podem conter
    caracteres fora de ``[A-Za-z0-9._-]`` (espaço, parênteses, '+').

    Args:
        user_id: PK do User dono do projeto.
        project_id: PK do Project ao qual o OmicDataset pertence.
        dataset_accession: Accession do dataset (ex. 'GSE123456', 'SRP999').
        filename: Nome do arquivo (ex. 'GSE123456_series_matrix.txt.gz').
            Deve ser um basename — nenhum componente de diretório é aceito
            intencionalmente (garantia do caller via os.path.basename).

    Returns:
        String sem leading/trailing slash adequada para uso como
        ``storage_key`` em ``DatasetFile.storage_key``.
    """
    return f"omics/{user_id}/{project_id}/{dataset_accession}/{filename}"


def shared_omics_storage_key(
    dataset_accession: str,
    filename: str,
) -> str:
    """
    Retorna a chave (path) no object storage para um arquivo ômico
    de dataset PÚBLICO, compartilhado entre todos os projetos.

    Usar exclusivamente para dados com ``access_type='public'``.  O dado
    público é idêntico independente de qual projeto disparou a carga, por
    isso o namespace é agnóstico a user/project — evita duplicação de
    Parquets e elimina acoplamento cross-project de storage_key.

    O prefixo ``_shared`` nunca coincide com um user_id real (inteiros),
    tornando o namespace inequívoco.

    Args:
        dataset_accession: Accession do dataset (ex. 'CPTAC-CCRCC-PROTEOME').
            Aceita apenas ``[A-Za-z0-9._-]``. Levanta ``ValueError`` se
            contiver '/', '..', '\\\\', '\\0', espaços ou path absoluto.
        filename: Nome do arquivo (ex. 'cptac_ccrcc_proteome.parquet').
            Aceita apenas ``[A-Za-z0-9._-]``. Levanta ``ValueError`` se
            contiver '/', '..', '\\\\', '\\0', espaços ou path absoluto.

    Returns:
        String sem leading/trailing slash, ex.:
        ``omics/_shared/CPTAC-CCRCC-PROTEOME/cptac_ccrcc_proteome.parquet``

    Raises:
        ValueError: Se ``dataset_accession`` ou ``filename`` contiverem
            caracteres não permitidos pelo allowlist de path.
    """
    _validate_path_component(dataset_accession, "dataset_accession")
    _validate_path_component(filename, "filename")
    return f"omics/_shared/{dataset_accession}/{filename}"
