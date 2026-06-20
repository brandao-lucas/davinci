"""
text_utils — utilitários de processamento de texto compartilhados entre services.

Módulo interno; sem dependências externas além de `re` (stdlib).
"""

import re

# ---------------------------------------------------------------------------
# Segmentação de sentenças para abstracts científicos/biomédicos
# ---------------------------------------------------------------------------
#
# Abordagem: proteção de abreviações conhecidas via substituição temporária
# de pontos internos, seguida de split por terminadores de sentença com
# lookbehind/lookahead, com restauração posterior.
#
# Passo 1 — Substituição de pontos "protegidos":
#   Percorre a lista _ABBREV_RE (regex de abreviações) e substitui o "." da
#   abreviação por um placeholder Unicode raro (U+E000, área de uso privado).
#   Isso preserva o ponto dentro de "e.g.", "Fig. 1", "0.5 mg", etc., sem
#   deixar que o split posterior os interprete como final de sentença.
#
# Passo 2 — Split por terminadores de sentença:
#   Usa _SENTENCE_SPLIT_RE, que divide após `.`, `?`, `!` ou `;` quando
#   seguidos de espaço + letra maiúscula (ou espaço + fim de string).
#   O lookbehind `(?<![A-Z])` evita quebrar em siglas como "U.S.A." —
#   após restauração, o ponto final da sigla não geraria split porque o
#   padrão de split exige espaço + maiúscula na frente.
#
# Passo 3 — Restauração:
#   Substitui os placeholders de volta para ".".
#
# Cobertura:
#   OK  Abreviações latinas: "e.g.", "i.e.", "et al.", "cf.", "ca.", "vs."
#   OK  Abreviações acadêmicas: "Fig.", "Figs.", "no.", "p.", "pp.", "approx."
#   OK  Abreviações de título: "Dr.", "St.", "Prof.", "Mr.", "Ms.", "Jr.", "Sr."
#   OK  Abreviações de unidade seguidas de número: "mg.", "kg.", "mL.", "μL."
#   OK  Decimais: "0.5", "p < 0.05", "1.2-fold"
#   OK  Siglas com pontos: "U.S.A.", "N.A." (lookbehind na regex de split)
#   OK  Terminadores múltiplos: `.`, `?`, `!`, `;`
#
# Limitações conhecidas:
#   - Abreviações não listadas em _ABBREV_PATTERNS podem ainda causar quebra
#     indevida (ex.: abreviações de revistas como "Nat. Med."). Abreviações
#     de uso muito específico devem ser adicionadas à lista conforme surgirem.
#   - Sentenças muito longas sem terminador (comum em texto corrido de alguns
#     publishers) chegam inteiras — comportamento idêntico ao split original.
#   - O placeholder U+E000 é suficientemente raro para não colidir com texto
#     de abstracts PubMed/PMC; se colisão for detectada, usar outro codepoint
#     da área de uso privado (U+E001–U+F8FF).
# ---------------------------------------------------------------------------

# Placeholder interno: substitui "." protegido antes do split.
_DOT_PLACEHOLDER = ''

# Abreviações cujo ponto interno NÃO deve gerar quebra de sentença.
# Ordem importa para abreviações que são prefixo de outras:
# colocar as mais longas primeiro (ex.: "Figs." antes de "Fig.").
_ABBREV_PATTERNS: list[re.Pattern] = [p for p in (re.compile(pat, re.IGNORECASE) for pat in [
    # Latinas comuns
    r'\be\.g\.',
    r'\bi\.e\.',
    r'\bet al\.',
    r'\bcf\.',
    r'\bca\.',
    r'\bvs\.',
    r'\bapprox\.',
    # Acadêmicas / editoriais
    r'\bFigs?\.',          # Fig. e Figs.
    r'\bEq\.',
    r'\bEqs\.',
    r'\bRef\.',
    r'\bRefs\.',
    r'\bSec\.',
    r'\bTab\.',
    r'\bTabs\.',
    r'\bSuppl?\.',          # Sup. / Suppl.
    # Numeração / páginas
    r'\bno\.',
    r'\bpp?\.',             # p. e pp.
    # Títulos pessoais
    r'\bDr\.',
    r'\bSt\.',
    r'\bProf\.',
    r'\bMr\.',
    r'\bMs\.',
    r'\bMrs\.',
    r'\bJr\.',
    r'\bSr\.',
    # Unidades de medida seguidas de espaço+dígito
    # (proteção: só quando o próximo token começa com dígito — ex.: "0.5 mg. Results" não quebra)
    r'\bmg\.',
    r'\bμg\.',
    r'\bng\.',
    r'\bpg\.',
    r'\bmL\.',
    r'\bμL\.',
    r'\bnL\.',
    r'\bkg\.',
    r'\bg\.',
    r'\bnM\.',
    r'\bμM\.',
    r'\bmM\.',
    r'\bnm\.',
    r'\bμm\.',
    r'\bcm\.',
    r'\bmm\.',
    r'\bkDa\.',
    r'\bDa\.',
    # Decimais e notação científica: número.número
    r'\d+\.\d+',
    # Siglas com pontos consecutivos: ex. "U.S.A.", "N.A."
    # Cobre sequências de 1 letra + ponto repetida (ao menos 2 vezes).
    r'(?:[A-Za-z]\.){2,}',
])]

# Regex de split de sentenças: quebra após [.!?;] seguido de um ou mais
# espaços. Abreviações já estão protegidas pelo placeholder acima, então
# não precisamos filtrar por maiúscula — o split pode ocorrer antes de
# qualquer token (maiúsculo ou minúsculo), exatamente como o splitter
# original. Isso garante que rs-numbers, genes e outras entidades que
# iniciam com minúsculo na segunda sentença sejam corretamente separados.
_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?;])\s+')

# Regex auxiliar para restauração do placeholder.
_PLACEHOLDER_RE = re.compile(re.escape(_DOT_PLACEHOLDER))


def split_sentences(text: str) -> list[str]:
    """
    Divide texto de abstract científico em sentenças.

    Trata abreviações biomédicas comuns (e.g., i.e., et al., Fig., vs., etc.),
    decimais, siglas com pontos e múltiplos terminadores de sentença.

    Retorna lista de strings; lista vazia se text for falsy.

    Contrato de posições:
        O índice (0-based) de cada sentença retornada é estável e sequencial —
        sentence_position=0 é a primeira sentença, sentence_position=N-1 é a
        última. Isso é idêntico ao contrato do splitter anterior.
    """
    if not text:
        return []

    protected = text.strip()

    # Passo 1: proteger pontos em abreviações e padrões especiais.
    for pattern in _ABBREV_PATTERNS:
        protected = pattern.sub(lambda m: m.group().replace('.', _DOT_PLACEHOLDER), protected)

    # Passo 2: dividir em terminadores de sentença.
    raw_sentences = _SENTENCE_SPLIT_RE.split(protected)

    # Passo 3: restaurar placeholders e limpar.
    sentences = []
    for s in raw_sentences:
        restored = _PLACEHOLDER_RE.sub('.', s).strip()
        if restored:
            sentences.append(restored)

    return sentences
