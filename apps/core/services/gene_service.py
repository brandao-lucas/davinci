"""
GeneService — agregação e derivação de contexto de genes por projeto.

Responsabilidades:
- list_genes_for_project(): query agregada única (sem N+1) de PaperGene por projeto.
- get_gene_detail():        detalhe de um gene com snippets do cache EntityContext.
- extract_gene_sentences(): derivação de sentenças por regex (chamada pela Celery task).

Fronteira Django↔Rust: derivação de sentença por split/regex de abstract é leve e
aceitável em Python (não é parse de XML nem NER pesado). Offsets robustos com
sentence-level tokenization são evolução futura via handoff ferris.

Normalização de entity_name:
    O campo EntityContext.entity_name é gravado e lido com o valor literal de
    PaperGene.gene_symbol (ex: 'TNF', 'BRCA1'). Não há normalização de caixa:
    o símbolo canônico vem do Rust via NER e é consistente dentro de um projeto.
    Lookups usam sempre o gene_symbol exato recebido da URL / PaperGene.
"""

import re
import logging

from django.db.models import Count, Sum, Max, Q

from apps.core.models import (
    DaVinciProject, Paper, PaperGene, ProjectPaper, EntityContext,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

# Tamanho máximo de gene_symbol aceito (item 4 do 007 — proteção contra ReDoS
# e custo de regex sobre N abstracts).
GENE_SYMBOL_MAX_LEN = 64

# Marcador sentinela: posição reservada para indicar "paper processado, sem snippets".
# Permite distinguir "nunca processado" (sem linha) de "processado, zero snippets"
# — elimina o loop infinito de context_status='computing' para genes ausentes
# do abstract ou papers sem abstract (item 1 do 007).
_SENTINEL_POSITION = -1
_SENTINEL_SENTENCE = ''

# ---------------------------------------------------------------------------
# Regex de fronteira de sentença (MVP ingênuo).
#
# LIMITAÇÃO CONHECIDA: o split em ". " falha em abreviações como "e.g.",
# "i.e.", "vs.", "Fig.", siglas com pontos, etc. Isso é aceitável no MVP
# (Passo 4 do plano 2026-06-19-pagina-genes-projeto.md). Para NLP robusto
# usar handoff ferris com rust_src/ tokenizer.
# ---------------------------------------------------------------------------
_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+')


def _split_sentences(text: str) -> list[str]:
    """Divide abstract em sentenças. MVP: split em pontuação + espaço."""
    if not text:
        return []
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s.strip()]


def _gene_sentence_re(gene_symbol: str) -> re.Pattern:
    """
    Compila regex para detectar gene_symbol com fronteira de palavra,
    case-insensitive, evitando substring espúria (ex: TNF em TNFRSF1A).

    Recebe gene_symbol já validado (≤ GENE_SYMBOL_MAX_LEN chars).
    re.escape() protege contra ReDoS para símbolos com caracteres especiais.
    """
    escaped = re.escape(gene_symbol)
    return re.compile(rf'\b{escaped}\b', re.IGNORECASE)


class GeneService:
    """
    Serviço de genes para o DaVinciProject.

    Todos os métodos recebem um projeto já validado (isolamento por
    request.user feito no ViewSet via _get_project()).
    """

    # ------------------------------------------------------------------
    # Passo 2 — Lista agregada
    # ------------------------------------------------------------------

    @staticmethod
    def list_genes_for_project(
        project: DaVinciProject,
        *,
        q: str | None = None,
        included_only: bool = False,
    ):
        """
        Retorna queryset anotado de PaperGene agrupado por gene_symbol para o projeto.

        Estratégia de query (sem fan-out):
        -----------------------------------
        1. Pré-computa os IDs de papers incluídos e todos os papers do projeto
           com duas subqueries simples.
        2. Faz GROUP BY gene_symbol em PaperGene filtrado pelos papers do projeto.
        3. Usa Count('paper', distinct=True, filter=...) com filtro Q(paper_id__in=ids)
           — isso evita o fan-out que aconteceria ao JOIN direto com ProjectPaper
           (cada PaperGene seria replicado pela quantidade de ProjectPapers do mesmo paper).
        4. entrez_id representativo via Max('entrez_id') — pega o maior não-nulo do grupo;
           como Entrez IDs são consistentes por símbolo, isso é equivalente ao "primeiro
           não-nulo" e evita subquery custosa de first-non-null.

        Parâmetros:
            q             — filtro icontains sobre gene_symbol (WHERE, pré-GROUP BY).
            included_only — quando True, aplica .filter(unique_citations_included__gt=0)
                            após o .annotate(), que o Django traduz em HAVING sobre a
                            agregação (não em WHERE). As contagens both included e total
                            continuam presentes no payload; o filtro apenas exclui genes
                            sem nenhuma citação incluída.

        Retorna um ValuesQuerySet que pode ser paginado e ordenado pelo caller.
        """
        # Pré-computa os conjuntos de paper_id do projeto para evitar fan-out de JOIN.
        # Usar subqueries em vez de list() mantém tudo no banco sem trazer IDs para Python.
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
            PaperGene.objects
            .filter(paper_id__in=all_paper_ids)
            .values('gene_symbol')
            .annotate(
                entrez_id=Max('entrez_id'),  # representativo não-nulo do grupo
                unique_citations_total=Count(
                    'paper',
                    distinct=True,
                ),
                unique_citations_included=Count(
                    'paper',
                    distinct=True,
                    filter=Q(paper_id__in=included_paper_ids),
                ),
                mention_count_total=Sum('mention_count'),
            )
        )

        if q:
            qs = qs.filter(gene_symbol__icontains=q)

        # Filtro HAVING: aplicado após annotate, o Django gera HAVING COUNT(...) > 0
        # em vez de WHERE — correto para filtros sobre agregações.
        if included_only:
            qs = qs.filter(unique_citations_included__gt=0)

        return qs

    # ------------------------------------------------------------------
    # Passo 3 — Detalhe de gene com snippets
    # ------------------------------------------------------------------

    @staticmethod
    def get_gene_detail(project: DaVinciProject, gene_symbol: str) -> dict | None:
        """
        Retorna o dict completo para o endpoint de detalhe de gene.

        - Valida comprimento do símbolo (item 4 do 007 — ReDoS / custo).
        - Busca todos os ProjectPaper do projeto que têm o gene.
        - Lê snippets do cache EntityContext (se disponível e fresco).
        - Detecta papers stale (sem nenhuma linha de contexto OU computed_at < paper.updated_at).
          A presença do marcador sentinela (sentence_position = -1) com computed_at
          fresco indica "processado, zero snippets" — o paper NÃO entra em computing.
        - Retorna context_status='computing' SOMENTE quando há paper sem cache fresco.

        Sem N+1: usa select_related e dict lookup para snippets.
        """
        # Item 4 (007) — rejeitar gene_symbol longo antes de qualquer regex.
        if len(gene_symbol) > GENE_SYMBOL_MAX_LEN:
            return None

        # Papers do projeto que citam o gene (qualquer status)
        project_papers = (
            ProjectPaper.objects
            .filter(project=project, paper__genes__gene_symbol=gene_symbol)
            .select_related('paper')
            .order_by('-paper__pub_year', 'paper__pmid')
        )

        if not project_papers.exists():
            return None

        # Métricas agregadas (order_by obrigatório antes de .first() sobre queryset anotado)
        gene_agg = (
            PaperGene.objects
            .filter(
                paper__in_projects__project=project,
                gene_symbol=gene_symbol,
            )
            .values('gene_symbol')
            .annotate(
                entrez_id=Max('entrez_id'),
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
            .order_by('gene_symbol')
            .first()
        )

        if not gene_agg:
            return None

        # Carregar todos os contextos do cache (uma query, indexada por paper_id).
        # Inclui sentinelas (sentence_position = -1) que marcam "processado, sem snippets".
        paper_ids = [pp.paper_id for pp in project_papers]
        cached_contexts = (
            EntityContext.objects
            .filter(
                paper_id__in=paper_ids,
                entity_type=EntityContext.EntityType.GENE,
                entity_name=gene_symbol,  # exato — mesma caixa que PaperGene.gene_symbol
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
            # Registrar computed_at independente do tipo de linha (sentinela ou snippet real)
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
                'snippets': snippets_by_paper.get(paper.pk, []),
            })

        return {
            'gene_symbol': gene_symbol,
            'entrez_id': gene_agg['entrez_id'],
            'unique_citations_included': gene_agg['unique_citations_included'],
            'unique_citations_total': gene_agg['unique_citations_total'],
            'references': references,
            'context_status': 'computing' if needs_compute else 'ready',
        }

    # ------------------------------------------------------------------
    # Passo 4 — Derivação de sentenças para um paper/gene
    # ------------------------------------------------------------------

    @staticmethod
    def extract_gene_sentences(paper: Paper, gene_symbol: str) -> list[dict]:
        """
        Extrai sentenças do abstract que contêm gene_symbol (com fronteira de palavra).

        Retorna lista de {'sentence': str, 'sentence_position': int}.
        Retorna lista vazia se abstract vazio ou gene não mencionado.

        Nota: gene_symbol deve ter ≤ GENE_SYMBOL_MAX_LEN chars (validado upstream).
        """
        sentences = _split_sentences(paper.abstract)
        if not sentences:
            return []

        pattern = _gene_sentence_re(gene_symbol)
        matches = []
        for pos, sentence in enumerate(sentences):
            if pattern.search(sentence):
                matches.append({'sentence': sentence, 'sentence_position': pos})
        return matches

    @staticmethod
    def derive_and_persist_contexts(project: DaVinciProject, gene_symbol: str) -> int:
        """
        Deriva e persiste snippets de EntityContext para gene_symbol em todos os
        papers do projeto.

        Idempotente: limpa contextos existentes (snippets + sentinelas) de cada
        paper antes de repovoar.

        Marcador sentinela:
            Quando um paper tem abstract mas o gene não aparece nele (ou quando
            o abstract está vazio), grava uma linha com sentence_position=-1 e
            sentence='' e computed_at=now. Isso permite que get_gene_detail()
            distinga "nunca processado" (sem linha) de "processado, zero snippets"
            — eliminando o loop infinito de context_status='computing' (item 1 do 007).

        Retorna o número de snippets REAIS persistidos (sentinelas não contam).
        """
        from django.utils import timezone

        project_papers = (
            ProjectPaper.objects
            .filter(project=project, paper__genes__gene_symbol=gene_symbol)
            .select_related('paper')
        )

        now = timezone.now()
        to_create = []
        real_snippet_count = 0

        for pp in project_papers:
            paper = pp.paper

            # Limpar contextos existentes deste paper para este gene (snippets + sentinelas)
            EntityContext.objects.filter(
                paper=paper,
                entity_type=EntityContext.EntityType.GENE,
                entity_name=gene_symbol,
            ).delete()

            if not paper.abstract:
                # Abstract vazio — gravar sentinela para marcar "processado"
                to_create.append(
                    EntityContext(
                        paper=paper,
                        entity_type=EntityContext.EntityType.GENE,
                        entity_name=gene_symbol,
                        sentence=_SENTINEL_SENTENCE,
                        sentence_position=_SENTINEL_POSITION,
                        computed_at=now,
                    )
                )
                continue

            snippets = GeneService.extract_gene_sentences(paper, gene_symbol)

            if snippets:
                real_snippet_count += len(snippets)
                for snippet in snippets:
                    to_create.append(
                        EntityContext(
                            paper=paper,
                            entity_type=EntityContext.EntityType.GENE,
                            entity_name=gene_symbol,
                            sentence=snippet['sentence'],
                            sentence_position=snippet['sentence_position'],
                            computed_at=now,
                        )
                    )
            else:
                # Gene não aparece no abstract — gravar sentinela para marcar "processado"
                to_create.append(
                    EntityContext(
                        paper=paper,
                        entity_type=EntityContext.EntityType.GENE,
                        entity_name=gene_symbol,
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
            'derive_and_persist_contexts: projeto=%s gene=%s snippets_reais=%d total_linhas=%d',
            project.id,
            gene_symbol,
            real_snippet_count,
            len(to_create),
        )
        return real_snippet_count
