"""
VariantService — agregação e derivação de contexto de variantes por projeto.

Responsabilidades:
- list_variants_for_project(): query agregada única (sem N+1) de PaperVariant por projeto.
- get_variant_detail():        detalhe de uma variante com snippets do cache EntityContext.
- extract_variant_sentences(): derivação de sentenças por regex (chamada pela Celery task).

Fronteira Django↔Rust: derivação de sentença por split/regex de abstract é leve e
aceitável em Python (rs_number ex.: rs1801133 é token simples; re.escape+\\b cobre).
NER de rs_number e bulk insert vivem em Rust (regra #1).

Chave natural:
    rs_number (ex.: 'rs1801133'). Gravado e lido com o valor literal de
    PaperVariant.rs_number. Sem normalização de caixa — consistente com o Rust.
    Lookups usam sempre o rs_number exato recebido da URL / PaperVariant.

Anotação clínica:
    VariantAnnotation (PK = rs_number) é lida via in_bulk() pós-paginação na lista
    e via .filter().first() no detalhe. Pode ser None (D2: mostrar TODAS as variantes).
"""

import re
import logging

from django.db.models import Count, Sum, Q

from apps.core.models import (
    DaVinciProject, Paper, PaperVariant, ProjectPaper, EntityContext,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

# Tamanho máximo de rs_number aceito (anti-ReDoS e custo de regex sobre N abstracts).
# rs numbers reais têm até ~12 chars (rs + dígitos), mas usamos margem segura.
RS_NUMBER_MAX_LEN = 32

# Marcador sentinela: posição reservada para indicar "paper processado, sem snippets".
# Permite distinguir "nunca processado" (sem linha) de "processado, zero snippets"
# — elimina o loop infinito de context_status='computing' para variantes ausentes
# do abstract ou papers sem abstract.
_SENTINEL_POSITION = -1
_SENTINEL_SENTENCE = ''

# Regex de fronteira de sentença (MVP ingênuo — igual ao GeneService).
_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+')


def _split_sentences(text: str) -> list[str]:
    """Divide abstract em sentenças. MVP: split em pontuação + espaço."""
    if not text:
        return []
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s.strip()]


def _variant_sentence_re(rs_number: str) -> re.Pattern:
    """
    Compila regex para detectar rs_number com fronteira de palavra,
    case-insensitive.

    re.escape() protege contra ReDoS para rs_numbers com caracteres especiais.
    rs_number deve ter <= RS_NUMBER_MAX_LEN chars (validado upstream).
    """
    escaped = re.escape(rs_number)
    return re.compile(rf'\b{escaped}\b', re.IGNORECASE)


class VariantService:
    """
    Serviço de variantes genéticas para o DaVinciProject.

    Todos os métodos recebem um projeto já validado (isolamento por
    request.user feito no ViewSet via _get_project()).
    """

    # ------------------------------------------------------------------
    # Lista agregada
    # ------------------------------------------------------------------

    @staticmethod
    def list_variants_for_project(
        project: DaVinciProject,
        *,
        q: str | None = None,
        included_only: bool = False,
    ):
        """
        Retorna queryset anotado de PaperVariant agrupado por rs_number para o projeto.

        Estratégia de query (sem fan-out):
        -----------------------------------
        1. Pré-computa os IDs de papers incluídos e todos os papers do projeto
           com duas subqueries simples.
        2. Faz GROUP BY rs_number em PaperVariant filtrado pelos papers do projeto.
        3. Usa Count('paper', distinct=True, filter=...) com filtro Q(paper_id__in=ids)
           — evita o fan-out que aconteceria ao JOIN direto com ProjectPaper.
        4. mention_count_total = Sum('mention_count') (D4 do plano).

        Nota: VariantAnnotation é mesclada pós-paginação via in_bulk() no ViewSet (D5).

        Parâmetros:
            q             — filtro icontains sobre rs_number (WHERE, pré-GROUP BY).
            included_only — quando True, aplica HAVING unique_citations_included > 0.

        Retorna um ValuesQuerySet que pode ser paginado e ordenado pelo caller.
        """
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
            PaperVariant.objects
            .filter(paper_id__in=all_paper_ids)
            .values('rs_number')
            .annotate(
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
            qs = qs.filter(rs_number__icontains=q)

        # Filtro HAVING: aplicado após annotate, o Django gera HAVING COUNT(...) > 0
        # em vez de WHERE — correto para filtros sobre agregações.
        if included_only:
            qs = qs.filter(unique_citations_included__gt=0)

        return qs

    # ------------------------------------------------------------------
    # Detalhe de variante com snippets
    # ------------------------------------------------------------------

    @staticmethod
    def get_variant_detail(project: DaVinciProject, rs_number: str) -> dict | None:
        """
        Retorna o dict completo para o endpoint de detalhe de variante.

        - Valida comprimento do rs_number (anti-ReDoS).
        - Busca todos os ProjectPaper do projeto que têm a variante.
        - Lê snippets do cache EntityContext (se disponível e fresco).
        - Detecta papers stale (sem nenhuma linha de contexto OU computed_at < paper.updated_at).
          A presença do marcador sentinela (sentence_position = -1) com computed_at
          fresco indica "processado, zero snippets" — o paper NÃO entra em computing.
        - Retorna context_status='computing' SOMENTE quando há paper sem cache fresco.
        - Busca VariantAnnotation (pode ser None — D2).

        Sem N+1: usa select_related e dict lookup para snippets.
        """
        if len(rs_number) > RS_NUMBER_MAX_LEN:
            return None

        # Papers do projeto que citam a variante (qualquer status)
        project_papers = (
            ProjectPaper.objects
            .filter(project=project, paper__variants__rs_number=rs_number)
            .select_related('paper')
            .order_by('-paper__pub_year', 'paper__pmid')
        )

        if not project_papers.exists():
            return None

        # Métricas agregadas
        variant_agg = (
            PaperVariant.objects
            .filter(
                paper__in_projects__project=project,
                rs_number=rs_number,
            )
            .values('rs_number')
            .annotate(
                unique_citations_total=Count('paper', distinct=True),
                unique_citations_included=Count(
                    'paper',
                    distinct=True,
                    filter=Q(
                        paper__in_projects__project=project,
                        paper__in_projects__curation_status=ProjectPaper.CurationStatus.INCLUDED,
                    ),
                ),
                mention_count_total=Sum('mention_count'),
            )
            .order_by('rs_number')
            .first()
        )

        if not variant_agg:
            return None

        # Carregar todos os contextos do cache (uma query, indexada por paper_id).
        paper_ids = [pp.paper_id for pp in project_papers]
        cached_contexts = (
            EntityContext.objects
            .filter(
                paper_id__in=paper_ids,
                entity_type=EntityContext.EntityType.VARIANT,
                entity_name=rs_number,
            )
            .values('paper_id', 'sentence', 'sentence_position', 'computed_at')
            .order_by('paper_id', 'sentence_position')
        )

        # Agrupar snippets por paper_id, registrando o computed_at mais recente.
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
        needs_compute = False
        for pp in project_papers:
            paper = pp.paper
            pid = paper.pk
            computed_at = computed_at_by_paper.get(pid)

            if computed_at is None:
                needs_compute = True
                break

            if paper.updated_at and computed_at < paper.updated_at:
                needs_compute = True
                break

        # Buscar anotação clínica (pode ser None — D2)
        from apps.core.models import VariantAnnotation
        annotation = VariantAnnotation.objects.filter(rs_number=rs_number).first()
        annotation_data = None
        if annotation:
            annotation_data = {
                'gene_symbol': annotation.gene_symbol,
                'gene_name': annotation.gene_name,
                'entrez_id': annotation.entrez_id,
                'chromosome': annotation.chromosome,
                'position': annotation.position,
                'alleles': annotation.alleles,
                'maf': annotation.maf,
                'clinical_significance': annotation.clinical_significance,
            }

        # Montar lista de referências
        references = []
        for pp in project_papers:
            paper = pp.paper
            references.append({
                'project_paper_id': pp.pk,
                'pmid': paper.pmid,
                'title': paper.title,
                'pub_year': paper.pub_year,
                'journal': paper.journal,
                'curation_status': pp.curation_status,
                'snippets': snippets_by_paper.get(paper.pk, []),
            })

        return {
            'rs_number': rs_number,
            'unique_citations_included': variant_agg['unique_citations_included'],
            'unique_citations_total': variant_agg['unique_citations_total'],
            'mention_count_total': variant_agg['mention_count_total'],
            'annotation': annotation_data,
            'references': references,
            'context_status': 'computing' if needs_compute else 'ready',
        }

    # ------------------------------------------------------------------
    # Derivação de sentenças para um paper/variante
    # ------------------------------------------------------------------

    @staticmethod
    def extract_variant_sentences(paper: Paper, rs_number: str) -> list[dict]:
        """
        Extrai sentenças do abstract que contêm rs_number (com fronteira de palavra).

        Retorna lista de {'sentence': str, 'sentence_position': int}.
        Retorna lista vazia se abstract vazio ou variante não mencionada.

        Nota: rs_number deve ter <= RS_NUMBER_MAX_LEN chars (validado upstream).
        """
        sentences = _split_sentences(paper.abstract)
        if not sentences:
            return []

        pattern = _variant_sentence_re(rs_number)
        matches = []
        for pos, sentence in enumerate(sentences):
            if pattern.search(sentence):
                matches.append({'sentence': sentence, 'sentence_position': pos})
        return matches

    @staticmethod
    def derive_and_persist_contexts(project: DaVinciProject, rs_number: str) -> int:
        """
        Deriva e persiste snippets de EntityContext para rs_number em todos os
        papers do projeto.

        Idempotente: limpa contextos existentes (snippets + sentinelas) de cada
        paper antes de repovoar.

        Marcador sentinela:
            Quando um paper tem abstract mas a variante não aparece nele (ou quando
            o abstract está vazio), grava uma linha com sentence_position=-1 e
            sentence='' e computed_at=now. Isso permite que get_variant_detail()
            distinga "nunca processado" (sem linha) de "processado, zero snippets".

        Retorna o número de snippets REAIS persistidos (sentinelas não contam).
        """
        from django.utils import timezone

        project_papers = (
            ProjectPaper.objects
            .filter(project=project, paper__variants__rs_number=rs_number)
            .select_related('paper')
        )

        now = timezone.now()
        to_create = []
        real_snippet_count = 0

        for pp in project_papers:
            paper = pp.paper

            # Limpar contextos existentes deste paper para esta variante
            EntityContext.objects.filter(
                paper=paper,
                entity_type=EntityContext.EntityType.VARIANT,
                entity_name=rs_number,
            ).delete()

            if not paper.abstract:
                # Abstract vazio — gravar sentinela para marcar "processado"
                to_create.append(
                    EntityContext(
                        paper=paper,
                        entity_type=EntityContext.EntityType.VARIANT,
                        entity_name=rs_number,
                        sentence=_SENTINEL_SENTENCE,
                        sentence_position=_SENTINEL_POSITION,
                        computed_at=now,
                    )
                )
                continue

            snippets = VariantService.extract_variant_sentences(paper, rs_number)

            if snippets:
                real_snippet_count += len(snippets)
                for snippet in snippets:
                    to_create.append(
                        EntityContext(
                            paper=paper,
                            entity_type=EntityContext.EntityType.VARIANT,
                            entity_name=rs_number,
                            sentence=snippet['sentence'],
                            sentence_position=snippet['sentence_position'],
                            computed_at=now,
                        )
                    )
            else:
                # Variante não aparece no abstract — gravar sentinela
                to_create.append(
                    EntityContext(
                        paper=paper,
                        entity_type=EntityContext.EntityType.VARIANT,
                        entity_name=rs_number,
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
            'derive_and_persist_contexts: projeto=%s rs_number=%s snippets_reais=%d total_linhas=%d',
            project.id,
            rs_number,
            real_snippet_count,
            len(to_create),
        )
        return real_snippet_count
