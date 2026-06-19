"""
query_builder — Construtor central de queries PubMed para o DaVinci.

Esta função é a ÚNICA fonte de verdade para a query booleana PubMed de um projeto.
Ela alimenta tanto o preview de magnitude quanto o job de ingestão, garantindo
paridade inegociável entre o que o usuário vê no painel e o que é realmente ingerido.

Semântica dos campos de DaVinciProject:
    query_term          — termo livre principal (nunca vazio)
    query_synonyms      — lista de sinônimos (OR simples com query_term)
    advanced_search_enabled — habilita a lógica MeSH abaixo
    selected_mesh       — lista de descritores MeSH selecionados, cada um com:
                            { "descriptor": str,   # nome canônico do descritor
                              "ui": str,            # UI MeSH (D123456 etc.)
                              "qualifiers": [str],  # subheadings selecionados
                              "mode": "and"|"or",   # como este bloco se junta
                              "major_only": bool }  # [majr] vs [mh]
    mesh_default_mode   — "and"|"or"; usado quando o bloco não tem "mode" explícito

Formato de saída PubMed:
    Sem MeSH ativo:
        (<query_term>) OR (<syn1>) OR (<syn2>)

    Com MeSH ativo (advanced_search_enabled=True e selected_mesh não-vazio):
        (<free_text_part>) AND (<mesh_block1>) AND (<mesh_block2>) OR (...)

    Os termos livres são agrupados entre parênteses como bloco "free_text".
    Os blocos MeSH são adicionados após, respeitando o "mode" de cada bloco:
        AND → (<termo_mesh>) AND <restante>
        OR  → (<termo_mesh>) OR <restante>

    O agrupamento segue a estrutura:
        free_text AND (mesh_and_block1) AND (mesh_and_block2) ...
        ... OR (mesh_or_block1) OR (mesh_or_block2) ...

    Isso é equivalente à query que o PubMed monta com o Advanced Search Builder.

Segurança (prevenção de injeção de query booleana):
    Os descritores MeSH vêm do cliente (via selected_mesh no body da requisição).
    Um termo malicioso poderia tentar escapar o contexto da tag de campo PubMed,
    por exemplo: `Diabetes"[mh] OR "evil`.
    A função `_escape_mesh_term` trata isso removendo aspas duplas, colchetes e
    parênteses — caracteres especiais que poderiam quebrar a gramática de tags
    do PubMed. Descritores excessivamente longos (>255 chars) são ignorados.

    Os termos livres (query_term e query_synonyms) são igualmente sanitizados
    por `_escape_free_text_term` antes de entrar na query. Esses campos são
    editáveis via PATCH, portanto também são superfície de injeção. A função
    usa o mesmo conjunto de caracteres removidos que `_escape_mesh_term`, mas
    num contexto sem tags de campo (sem aspas duplas delimitadoras), então o
    resultado é inserido diretamente como texto PubMed sem delimitadores extras.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Comprimento máximo de descritor aceito (mesmo limite do MeshService).
_MESH_DESCRIPTOR_MAX_LEN = 255

# Regex para remover caracteres que poderiam injetar tags PubMed:
# - aspas duplas (delimitam frases em PubMed)
# - colchetes (delimitam tags de campo como [mh], [majr], [ti])
# - parênteses (agrupadores booleanos)
# Conservamos: letras, dígitos, espaços, hífens, vírgulas, apostrofes simples, pontos.
_MESH_UNSAFE_RE = re.compile(r'["\[\]()]')


def _escape_mesh_term(term: str) -> str:
    """
    Sanitiza um termo MeSH antes de inseri-lo na query PubMed.

    Remove caracteres que poderiam injetar operadores ou tags de campo PubMed
    (aspas duplas, colchetes, parênteses). Trunca em _MESH_DESCRIPTOR_MAX_LEN.

    Retorna a string sanitizada, ou string vazia se o resultado for vazio.
    """
    if not term:
        return ''
    term = term[:_MESH_DESCRIPTOR_MAX_LEN]
    return _MESH_UNSAFE_RE.sub('', term).strip()


# Comprimento máximo aceito para um único termo livre.
# query_term tem max_length=500 no modelo; usamos o mesmo teto.
_FREE_TEXT_MAX_LEN = 500


def _escape_free_text_term(term: str) -> str:
    """
    Sanitiza um termo de texto livre (query_term ou synonym) antes de inseri-lo
    na query PubMed.

    Usa o mesmo conjunto de caracteres removidos que _escape_mesh_term:
    aspas duplas (delimitadores de frase PubMed), colchetes (tags de campo)
    e parênteses (agrupadores booleanos). Trunca em _FREE_TEXT_MAX_LEN.

    O resultado é inserido sem delimitadores adicionais na query final — ao
    contrário dos termos MeSH que são envoltos em aspas + tag de campo.

    Retorna a string sanitizada, ou string vazia se o resultado for vazio.
    """
    if not term:
        return ''
    term = term[:_FREE_TEXT_MAX_LEN]
    return _MESH_UNSAFE_RE.sub('', term).strip()


def _build_mesh_field_tag(descriptor: str, qualifier: str | None, major_only: bool) -> str | None:
    """
    Monta a tag de campo PubMed para um par (descriptor, qualifier).

    Exemplos:
        ("Diabetes Mellitus", None, False)   → '"Diabetes Mellitus"[mh]'
        ("Diabetes Mellitus", None, True)    → '"Diabetes Mellitus"[majr]'
        ("Diabetes Mellitus", "diagnosis", False) → '"Diabetes Mellitus/diagnosis"[mh]'
        ("Diabetes Mellitus", "diagnosis", True)  → '"Diabetes Mellitus/diagnosis"[majr]'

    Retorna None se o descriptor ficar vazio após sanitização.
    """
    safe_desc = _escape_mesh_term(descriptor)
    if not safe_desc:
        return None

    tag = '[majr]' if major_only else '[mh]'

    if qualifier:
        safe_qual = _escape_mesh_term(qualifier)
        if safe_qual:
            return f'"{safe_desc}/{safe_qual}"{tag}'
        # qualifier ficou vazio após sanitização — ignora qualifier
    return f'"{safe_desc}"{tag}'


def _build_mesh_block(mesh_entry: dict) -> str | None:
    """
    Monta o bloco PubMed para um único descriptor MeSH selecionado.

    Se o descriptor tiver qualifiers, gera um bloco OR de todos os pares
    (descriptor/qualifier)[tag]. Se não tiver qualifiers, gera um bloco
    simples "descriptor"[tag].

    Retorna None se o descriptor for inválido ou ficar vazio após sanitização.
    """
    descriptor = mesh_entry.get('descriptor', '')
    major_only = bool(mesh_entry.get('major_only', False))
    qualifiers = mesh_entry.get('qualifiers') or []

    if not descriptor or len(descriptor) > _MESH_DESCRIPTOR_MAX_LEN:
        logger.debug('build_mesh_block: descriptor ignorado (vazio ou muito longo): %r', descriptor)
        return None

    if qualifiers:
        parts = []
        for qual in qualifiers:
            tag = _build_mesh_field_tag(descriptor, qual, major_only)
            if tag:
                parts.append(tag)
        # Também inclui o descriptor sem qualifier para cobrir o termo geral
        base_tag = _build_mesh_field_tag(descriptor, None, major_only)
        if base_tag:
            parts.append(base_tag)
        if not parts:
            return None
        return '(' + ' OR '.join(parts) + ')'
    else:
        tag = _build_mesh_field_tag(descriptor, None, major_only)
        if not tag:
            return None
        return f'({tag})'


def _build_free_text_part(project) -> str:
    """
    Monta a parte de texto livre da query: query_term OR synonyms...

    Sanitiza cada termo com _escape_free_text_term antes de interpolá-lo,
    prevenindo injeção de tags de campo ou operadores booleanos PubMed via
    campos editáveis pelo cliente (query_term / query_synonyms via PATCH).

    Retorna string sempre não-vazia (query_term é obrigatório no modelo).
    Termos que ficam vazios após sanitização são ignorados; se todos ficarem
    vazios, retorna o query_term bruto truncado como fallback seguro de último
    recurso (nunca deve ocorrer em uso normal).
    """
    raw_parts = [project.query_term] + list(project.query_synonyms or [])
    safe_parts = [_escape_free_text_term(p) for p in raw_parts]
    safe_parts = [p for p in safe_parts if p]

    if not safe_parts:
        # Fallback de último recurso: trunca o term bruto sem caracteres de injeção
        fallback = (project.query_term or '')[:_FREE_TEXT_MAX_LEN].strip()
        safe_parts = [fallback] if fallback else ['unknown']

    # Cada parte é parentetizada para preservar a semântica em queries compostas
    return '(' + ' OR '.join(f'({p})' for p in safe_parts) + ')'


def build_pubmed_query(project) -> str:
    """
    Monta a string booleana PubMed completa para um DaVinciProject.

    Esta é a ÚNICA função que deve ser chamada para gerar a query de busca.
    É usada tanto no preview de magnitude quanto no job de ingestão.

    Se advanced_search_enabled=False ou selected_mesh=[]:
        Retorna a query simples de texto livre (comportamento legado):
        query_term OR synonym1 OR synonym2

    Se advanced_search_enabled=True e selected_mesh não-vazio:
        Monta query combinada:
        (free_text) AND (mesh_and_block1) AND (mesh_and_block2) OR (mesh_or_block1) ...

        Os blocos são agrupados por mode:
        - Blocos AND: (free_text) AND (block1) AND (block2)
        - Blocos OR:  ... OR (block1) OR (block2)

        A estrutura final é:
        ((free_text AND and_blocks) OR or_blocks)

    Nota: blocos com descriptor inválido (vazio, muito longo, ou que ficam vazios
    após sanitização) são silenciosamente ignorados.
    """
    free_text = _build_free_text_part(project)

    # Comportamento legado (sem MeSH ou MeSH desabilitado).
    # Sanitiza os termos livres pelo mesmo _escape_free_text_term usado em
    # _build_free_text_part, garantindo paridade entre preview e ingestão.
    if not project.advanced_search_enabled or not project.selected_mesh:
        raw_parts = [project.query_term] + list(project.query_synonyms or [])
        safe_parts = [_escape_free_text_term(p) for p in raw_parts]
        safe_parts = [p for p in safe_parts if p]
        if not safe_parts:
            safe_parts = [(project.query_term or 'unknown')[:_FREE_TEXT_MAX_LEN]]
        return ' OR '.join(safe_parts)

    # Separar blocos por mode
    mesh_default = project.mesh_default_mode or 'and'
    and_blocks = []
    or_blocks = []

    for entry in project.selected_mesh:
        block = _build_mesh_block(entry)
        if block is None:
            continue
        mode = entry.get('mode') or mesh_default
        if mode == 'or':
            or_blocks.append(block)
        else:
            # 'and' é o default seguro
            and_blocks.append(block)

    # Se nenhum bloco MeSH sobrou após sanitização, retorna comportamento legado
    # com termos livres sanitizados (mesma lógica do caminho legado acima).
    if not and_blocks and not or_blocks:
        logger.warning(
            'build_pubmed_query: nenhum bloco MeSH válido gerado para projeto %s — '
            'fallback para query simples.',
            project.id,
        )
        raw_parts = [project.query_term] + list(project.query_synonyms or [])
        safe_parts = [_escape_free_text_term(p) for p in raw_parts]
        safe_parts = [p for p in safe_parts if p]
        if not safe_parts:
            safe_parts = [(project.query_term or 'unknown')[:_FREE_TEXT_MAX_LEN]]
        return ' OR '.join(safe_parts)

    # Montar a query combinada
    # Estrutura: (free_text AND and_block1 AND and_block2 ... OR or_block1 OR or_block2 ...)
    # Equivalente ao que o PubMed Advanced Builder montaria.
    query_parts = [free_text]
    for block in and_blocks:
        query_parts.append(f'AND {block}')
    combined = ' '.join(query_parts)

    if or_blocks:
        or_part = ' OR '.join(or_blocks)
        combined = f'({combined}) OR {or_part}'

    return combined
