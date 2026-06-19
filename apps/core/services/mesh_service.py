"""
MeshService — agregação e derivação de contexto de termos MeSH por projeto.

Responsabilidades:
- list_mesh_for_project(): query agregada única (sem N+1) de PaperMeSHTerm por projeto.
- get_mesh_detail():        detalhe de um descriptor com snippets do cache EntityContext.
- extract_mesh_sentences(): derivação de sentenças por regex (chamada pela Celery task).

Fronteira Django↔Rust: derivação de sentença por split/regex de abstract é leve e
aceitável em Python (não é parse de XML nem NER pesado). Offsets robustos com
sentence-level tokenization são evolução futura via handoff ferris.

Normalização de entity_name:
    O campo EntityContext.entity_name é gravado e lido com o valor literal de
    PaperMeSHTerm.descriptor (ex: 'Diabetes Mellitus', 'Neoplasms'). Não há
    normalização de caixa: o descriptor canônico vem do ingestor MeSH e é
    consistente dentro de um projeto. Lookups usam sempre o descriptor exato
    recebido da URL / PaperMeSHTerm.

Nota sobre snippets zero:
    MeSH não garante presença literal do descriptor no abstract (ex. o paper pode
    ter sido indexado com "Diabetes Mellitus" sem mencionar a frase no abstract).
    Muitos descriptors terão zero snippets — o marcador sentinela (sentence_position=-1)
    cobre esse caso, evitando loop infinito de context_status='computing'.
"""

import re
import logging

from django.db.models import Count, Q
from urllib.parse import quote as url_quote

from apps.core.models import (
    DaVinciProject, Paper, PaperMeSHTerm, ProjectPaper, EntityContext,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

# Tamanho máximo de descriptor aceito (proteção contra ReDoS e custo de regex
# sobre N abstracts).
MESH_DESCRIPTOR_MAX_LEN = 255

# Marcador sentinela: posição reservada para indicar "paper processado, sem snippets".
# Permite distinguir "nunca processado" (sem linha) de "processado, zero snippets"
# — elimina o loop infinito de context_status='computing' para descriptors ausentes
# do abstract ou papers sem abstract.
_SENTINEL_POSITION = -1
_SENTINEL_SENTENCE = ''

# ---------------------------------------------------------------------------
# Regex de fronteira de sentença (MVP ingênuo).
#
# LIMITAÇÃO CONHECIDA: o split em ". " falha em abreviações como "e.g.",
# "i.e.", "vs.", "Fig.", siglas com pontos, etc. Isso é aceitável no MVP.
# Para NLP robusto usar handoff ferris com rust_src/ tokenizer.
# ---------------------------------------------------------------------------
_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+')


def _split_sentences(text: str) -> list[str]:
    """Divide abstract em sentenças. MVP: split em pontuação + espaço."""
    if not text:
        return []
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s.strip()]


def _mesh_sentence_re(descriptor: str) -> re.Pattern:
    """
    Compila regex para detectar descriptor MeSH com fronteira de palavra,
    case-insensitive.

    Descriptor pode ser multi-palavra (ex: 'Diabetes Mellitus') — a regex
    casa a frase inteira, não palavras individuais.
    re.escape() protege contra ReDoS para descriptors com caracteres especiais.

    Recebe descriptor já validado (≤ MESH_DESCRIPTOR_MAX_LEN chars).
    """
    escaped = re.escape(descriptor)
    return re.compile(rf'\b{escaped}\b', re.IGNORECASE)


def _ncbi_mesh_url(descriptor: str) -> str:
    """Monta URL de busca NCBI MeSH para o descriptor."""
    return f'https://www.ncbi.nlm.nih.gov/mesh/?term={url_quote(descriptor)}'


class MeshService:
    """
    Serviço de termos MeSH para o DaVinciProject.

    Todos os métodos recebem um projeto já validado (isolamento por
    request.user feito no ViewSet via _get_project()).
    """

    # ------------------------------------------------------------------
    # Passo 2 — Lista agregada
    # ------------------------------------------------------------------

    @staticmethod
    def list_mesh_for_project(
        project: DaVinciProject,
        *,
        q: str | None = None,
        included_only: bool = False,
    ):
        """
        Retorna queryset anotado de PaperMeSHTerm agrupado por descriptor para o projeto.

        Estratégia de query (sem fan-out):
        -----------------------------------
        1. Pré-computa os IDs de papers incluídos e todos os papers do projeto
           com duas subqueries simples.
        2. Faz GROUP BY descriptor em PaperMeSHTerm filtrado pelos papers do projeto.
        3. Usa Count('paper', distinct=True, filter=...) com filtro Q(paper_id__in=ids)
           — isso evita o fan-out que aconteceria ao JOIN direto com ProjectPaper
           (cada PaperMeSHTerm seria replicado pela quantidade de ProjectPapers do mesmo paper).
        4. major_topic_count: papers distintos included onde is_major_topic=True —
           métrica PRIMÁRIA; ordenação default por ela.

        Parâmetros:
            q             — filtro icontains sobre descriptor (WHERE, pré-GROUP BY).
            included_only — quando True, aplica .filter(unique_citations_included__gt=0)
                            após o .annotate(), que o Django traduz em HAVING sobre a
                            agregação (não em WHERE). As contagens included e total
                            continuam presentes no payload; o filtro apenas exclui
                            descriptors sem nenhuma citação incluída.

        Retorna um ValuesQuerySet que pode ser paginado e ordenado pelo caller.
        """
        # Pré-computa os conjuntos de paper_id do projeto para evitar fan-out de JOIN.
        all_paper_ids = (
            ProjectPaper.objects
            .filter(project=project)
            .values('paper_id')
        )
        included_paper_ids = (
            ProjectPaper.objects
            .filter(project=project, curation_status=ProjectPaper.CurationStatus.INCLUDED)
            .values('paper_id')
        )

        qs = (
            PaperMeSHTerm.objects
            .filter(paper_id__in=all_paper_ids)
            .values('descriptor')
            .annotate(
                major_topic_count=Count(
                    'paper',
                    distinct=True,
                    filter=Q(is_major_topic=True) & Q(paper_id__in=included_paper_ids),
                ),
                unique_citations_included=Count(
                    'paper',
                    distinct=True,
                    filter=Q(paper_id__in=included_paper_ids),
                ),
                unique_citations_total=Count(
                    'paper',
                    distinct=True,
                ),
            )
        )

        if q:
            qs = qs.filter(descriptor__icontains=q)

        # Filtro HAVING: aplicado após annotate, o Django gera HAVING COUNT(...) > 0
        # em vez de WHERE — correto para filtros sobre agregações.
        # O filtro é sobre unique_citations_included (não major_topic_count):
        # um descriptor pode ter major_topic_count=0 mas ainda ter citações incluídas.
        if included_only:
            qs = qs.filter(unique_citations_included__gt=0)

        return qs

    # ------------------------------------------------------------------
    # Passo 3 — Detalhe de descriptor com snippets
    # ------------------------------------------------------------------

    @staticmethod
    def get_mesh_detail(project: DaVinciProject, descriptor: str) -> dict | None:
        """
        Retorna o dict completo para o endpoint de detalhe de descriptor MeSH.

        - Valida comprimento do descriptor (proteção contra ReDoS / custo).
        - Busca todos os ProjectPaper do projeto que têm o descriptor.
        - Lê snippets do cache EntityContext (se disponível e fresco).
        - Detecta papers stale (sem nenhuma linha de contexto OU computed_at < paper.updated_at).
          A presença do marcador sentinela (sentence_position = -1) com computed_at
          fresco indica "processado, zero snippets" — o paper NÃO entra em computing.
        - Retorna context_status='computing' SOMENTE quando há paper sem cache fresco.
        - Agrega qualifiers distintos não-vazios entre os papers do projeto.

        Sem N+1: usa select_related e dict lookup para snippets.
        """
        if len(descriptor) > MESH_DESCRIPTOR_MAX_LEN:
            return None

        # Papers do projeto que citam o descriptor (qualquer status)
        project_papers = (
            ProjectPaper.objects
            .filter(project=project, paper__mesh_terms__descriptor=descriptor)
            .select_related('paper')
            .order_by('-paper__pub_year', 'paper__pmid')
        )

        if not project_papers.exists():
            return None

        # Métricas agregadas (order_by obrigatório antes de .first() sobre queryset anotado)
        mesh_agg = (
            PaperMeSHTerm.objects
            .filter(
                paper__in_projects__project=project,
                descriptor=descriptor,
            )
            .values('descriptor')
            .annotate(
                major_topic_count=Count(
                    'paper',
                    distinct=True,
                    filter=Q(
                        is_major_topic=True,
                        paper__in_projects__project=project,
                        paper__in_projects__curation_status=ProjectPaper.CurationStatus.INCLUDED,
                    ),
                ),
                unique_citations_total=Count('paper', distinct=True),
                unique_citations_included=Count(
                    'paper',
                    distinct=True,
                    filter=Q(
                        paper__in_projects__project=project,
                        paper__in_projects__curation_status=ProjectPaper.CurationStatus.INCLUDED,
                    ),
                ),
            )
            .order_by('descriptor')
            .first()
        )

        if not mesh_agg:
            return None

        # Qualifiers distintos não-vazios entre os papers do projeto para este descriptor
        qualifiers = list(
            PaperMeSHTerm.objects
            .filter(
                paper__in_projects__project=project,
                descriptor=descriptor,
            )
            .exclude(qualifier='')
            .values_list('qualifier', flat=True)
            .distinct()
            .order_by('qualifier')
        )

        # Carregar todos os contextos do cache (uma query, indexada por paper_id).
        # Inclui sentinelas (sentence_position = -1) que marcam "processado, sem snippets".
        paper_ids = [pp.paper_id for pp in project_papers]
        cached_contexts = (
            EntityContext.objects
            .filter(
                paper_id__in=paper_ids,
                entity_type=EntityContext.EntityType.MESH,
                entity_name=descriptor,  # exato — mesma caixa que PaperMeSHTerm.descriptor
            )
            .values('paper_id', 'sentence', 'sentence_position', 'computed_at')
            .order_by('paper_id', 'sentence_position')
        )

        # Agrupar snippets por paper_id, registrando o computed_at mais recente.
        # A sentinela (sentence_position = -1) atualiza computed_at_by_paper mas
        # NÃO é incluída na lista de snippets entregue ao caller.
        snippets_by_paper: dict[int, list[dict]] = {}
        computed_at_by_paper: dict[int, object] = {}
        for ctx in cached_contexts:
            pid = ctx['paper_id']
            if pid not in computed_at_by_paper or (
                ctx['computed_at'] and computed_at_by_paper[pid]
                and ctx['computed_at'] > computed_at_by_paper[pid]
            ):
                computed_at_by_paper[pid] = ctx['computed_at']
            if pid not in snippets_by_paper:
                snippets_by_paper[pid] = []
            # Omitir a sentinela da lista de snippets entregue ao caller
            if ctx['sentence_position'] != _SENTINEL_POSITION:
                snippets_by_paper[pid].append({
                    'sentence': ctx['sentence'],
                    'sentence_position': ctx['sentence_position'],
                })

        # Verificar staleness por paper.
        # needs_compute=True apenas se algum paper não tiver linha (sentinela ou snippet)
        # com computed_at fresco (>= paper.updated_at).
        needs_compute = False
        for pp in project_papers:
            paper = pp.paper
            pid = paper.pk
            computed_at = computed_at_by_paper.get(pid)

            if computed_at is None:
                # Nenhuma linha de contexto para este paper — cache frio.
                needs_compute = True
                break

            # Stale: computed_at anterior ao updated_at do paper (re-ingestão detectada)
            if paper.updated_at and computed_at < paper.updated_at:
                needs_compute = True
                break

        # Montar is_major_topic por paper a partir dos PaperMeSHTerm
        # (um paper pode ter o descriptor como major topic ou não)
        major_topic_by_paper: dict[int, bool] = {}
        for term in PaperMeSHTerm.objects.filter(
            paper_id__in=paper_ids,
            descriptor=descriptor,
        ).values('paper_id', 'is_major_topic'):
            pid = term['paper_id']
            # Se any row com is_major_topic=True, o paper conta como major topic
            if term['is_major_topic']:
                major_topic_by_paper[pid] = True
            elif pid not in major_topic_by_paper:
                major_topic_by_paper[pid] = False

        # Montar lista de referências (sentinela já excluída de snippets_by_paper)
        references = []
        for pp in project_papers:
            paper = pp.paper
            references.append({
                'project_paper_id': pp.pk,  # PK de ProjectPaper — usada no PATCH /papers/<pk>/
                'pmid': paper.pmid,
                'title': paper.title,
                'pub_year': paper.pub_year,
                'journal': paper.journal,
                'curation_status': pp.curation_status,
                'is_major_topic': major_topic_by_paper.get(paper.pk, False),
                'snippets': snippets_by_paper.get(paper.pk, []),
            })

        return {
            'descriptor': descriptor,
            'major_topic_count': mesh_agg['major_topic_count'],
            'unique_citations_included': mesh_agg['unique_citations_included'],
            'unique_citations_total': mesh_agg['unique_citations_total'],
            'qualifiers': qualifiers,
            'ncbi_mesh_url': _ncbi_mesh_url(descriptor),
            'references': references,
            'context_status': 'computing' if needs_compute else 'ready',
        }

    # ------------------------------------------------------------------
    # Passo 4 — Derivação de sentenças para um paper/descriptor
    # ------------------------------------------------------------------

    @staticmethod
    def extract_mesh_sentences(paper: Paper, descriptor: str) -> list[dict]:
        """
        Extrai sentenças do abstract que contêm o descriptor MeSH
        (com fronteira de palavra, case-insensitive, multi-palavra).

        Retorna lista de {'sentence': str, 'sentence_position': int}.
        Retorna lista vazia se abstract vazio ou descriptor não mencionado.

        Nota: descriptor deve ter ≤ MESH_DESCRIPTOR_MAX_LEN chars (validado upstream).
        """
        sentences = _split_sentences(paper.abstract)
        if not sentences:
            return []

        pattern = _mesh_sentence_re(descriptor)
        matches = []
        for pos, sentence in enumerate(sentences):
            if pattern.search(sentence):
                matches.append({'sentence': sentence, 'sentence_position': pos})
        return matches

    @staticmethod
    def derive_and_persist_contexts(project: DaVinciProject, descriptor: str) -> int:
        """
        Deriva e persiste snippets de EntityContext para descriptor em todos os
        papers do projeto que o mencionam.

        Idempotente: limpa contextos existentes (snippets + sentinelas) de cada
        paper antes de repovoar.

        Marcador sentinela:
            Quando um paper tem abstract mas o descriptor não aparece nele (ou quando
            o abstract está vazio), grava uma linha com sentence_position=-1 e
            sentence='' e computed_at=now. Isso permite que get_mesh_detail()
            distinga "nunca processado" (sem linha) de "processado, zero snippets"
            — eliminando o loop infinito de context_status='computing'.

            NOTA: Em MeSH, o descriptor não precisa aparecer literalmente no abstract
            para o paper ser indexado com esse termo. Zero snippets é o caso comum
            e esperado — o sentinela cobre sem loop infinito.

        Retorna o número de snippets REAIS persistidos (sentinelas não contam).
        """
        from django.utils import timezone

        project_papers = (
            ProjectPaper.objects
            .filter(project=project, paper__mesh_terms__descriptor=descriptor)
            .select_related('paper')
        )

        now = timezone.now()
        to_create = []
        real_snippet_count = 0

        for pp in project_papers:
            paper = pp.paper

            # Limpar contextos existentes deste paper para este descriptor (snippets + sentinelas)
            EntityContext.objects.filter(
                paper=paper,
                entity_type=EntityContext.EntityType.MESH,
                entity_name=descriptor,
            ).delete()

            if not paper.abstract:
                # Abstract vazio — gravar sentinela para marcar "processado"
                to_create.append(
                    EntityContext(
                        paper=paper,
                        entity_type=EntityContext.EntityType.MESH,
                        entity_name=descriptor,
                        sentence=_SENTINEL_SENTENCE,
                        sentence_position=_SENTINEL_POSITION,
                        computed_at=now,
                    )
                )
                continue

            snippets = MeshService.extract_mesh_sentences(paper, descriptor)

            if snippets:
                real_snippet_count += len(snippets)
                for snippet in snippets:
                    to_create.append(
                        EntityContext(
                            paper=paper,
                            entity_type=EntityContext.EntityType.MESH,
                            entity_name=descriptor,
                            sentence=snippet['sentence'],
                            sentence_position=snippet['sentence_position'],
                            computed_at=now,
                        )
                    )
            else:
                # Descriptor não aparece literalmente no abstract — gravar sentinela
                to_create.append(
                    EntityContext(
                        paper=paper,
                        entity_type=EntityContext.EntityType.MESH,
                        entity_name=descriptor,
                        sentence=_SENTINEL_SENTENCE,
                        sentence_position=_SENTINEL_POSITION,
                        computed_at=now,
                    )
                )

        if to_create:
            EntityContext.objects.bulk_create(
                to_create,
                ignore_conflicts=True,  # proteção extra contra race condition
            )

        logger.info(
            'derive_and_persist_contexts: projeto=%s descriptor=%s snippets_reais=%d total_linhas=%d',
            project.id,
            descriptor,
            real_snippet_count,
            len(to_create),
        )
        return real_snippet_count
