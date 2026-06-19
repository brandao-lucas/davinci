"""
DrugService — agregação e derivação de contexto de medicamentos por projeto.

Responsabilidades:
- list_drugs_for_project(): query agregada única (sem N+1) de PaperDrug por projeto.
- get_drug_detail():        detalhe de um medicamento com snippets do cache EntityContext.
- extract_drug_sentences(): derivação de sentenças por regex (chamada pela Celery task).

Fronteira Django↔Rust: derivação de sentença por split/regex de abstract é leve e
aceitável em Python (não é parse de XML nem NER pesado). Offsets robustos com
sentence-level tokenization são evolução futura via handoff ferris.

Normalização de entity_name:
    O campo EntityContext.entity_name é gravado e lido com drug_name_lower
    (chave canônica). A chave é consistente entre a task de derivação e o lookup
    do detalhe. O drug_name representativo (Max do grupo) é usado apenas para
    construção de URLs e exibição.

Links externos:
    - DrugBank: https://go.drugbank.com/drugs/<drugbank_id> (quando drugbank_id presente)
    - PubChem:  https://pubchem.ncbi.nlm.nih.gov/#query=<drug_name> (sempre)
    Ambas as URLs são montadas no backend com URL-encoding adequado.
"""

import re
import logging
from urllib.parse import quote as url_quote

from django.db.models import Count, Sum, Max, Q

from apps.core.models import (
    DaVinciProject, Paper, PaperDrug, ProjectPaper, EntityContext,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

# Tamanho máximo de drug_name_lower aceito (proteção contra ReDoS e custo de
# regex sobre N abstracts).
DRUG_NAME_MAX_LEN = 255

# Marcador sentinela: posição reservada para indicar "paper processado, sem snippets".
# Permite distinguir "nunca processado" (sem linha) de "processado, zero snippets"
# — elimina o loop infinito de context_status='computing' para medicamentos ausentes
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


def _drug_sentence_re(drug_name: str) -> re.Pattern:
    """
    Compila regex para detectar drug_name com fronteira de palavra,
    case-insensitive.

    Drug name pode ser multi-palavra (ex: 'Metformin', 'Aspirin').
    re.escape() protege contra ReDoS para nomes com caracteres especiais.

    Recebe drug_name já validado (≤ DRUG_NAME_MAX_LEN chars).
    """
    escaped = re.escape(drug_name)
    return re.compile(rf'\b{escaped}\b', re.IGNORECASE)


def _drugbank_url(drugbank_id: str) -> str | None:
    """Monta URL do DrugBank para o drugbank_id. Retorna None se vazio."""
    if drugbank_id:
        return f'https://go.drugbank.com/drugs/{drugbank_id}'
    return None


def _pubchem_search_url(drug_name: str) -> str:
    """Monta URL de busca PubChem para o nome do medicamento (sempre presente)."""
    return f'https://pubchem.ncbi.nlm.nih.gov/#query={url_quote(drug_name)}'


class DrugService:
    """
    Serviço de medicamentos para o DaVinciProject.

    Todos os métodos recebem um projeto já validado (isolamento por
    request.user feito no ViewSet via _get_project()).
    """

    # ------------------------------------------------------------------
    # Passo 2 — Lista agregada
    # ------------------------------------------------------------------

    @staticmethod
    def list_drugs_for_project(
        project: DaVinciProject,
        *,
        q: str | None = None,
        included_only: bool = False,
    ):
        """
        Retorna queryset anotado de PaperDrug agrupado por drug_name_lower para o projeto.

        Estratégia de query (sem fan-out):
        -----------------------------------
        1. Pré-computa os IDs de papers incluídos e todos os papers do projeto
           com duas subqueries simples.
        2. Faz GROUP BY drug_name_lower em PaperDrug filtrado pelos papers do projeto.
        3. Usa Count('paper', distinct=True, filter=...) com filtro Q(paper_id__in=ids)
           — isso evita o fan-out que aconteceria ao JOIN direto com ProjectPaper
           (cada PaperDrug seria replicado pela quantidade de ProjectPapers do mesmo paper).
        4. drug_name representativo via Max('drug_name') — pega o maior do grupo;
           como o nome vem do NER e é consistente, isso é equivalente ao "representativo".
        5. drugbank_id representativo via Max('drugbank_id') — pega o primeiro não-vazio
           do grupo (strings vazias ficam abaixo em ordenação lexicográfica).

        Parâmetros:
            q             — filtro icontains sobre drug_name_lower (WHERE, pré-GROUP BY).
            included_only — quando True, aplica .filter(unique_citations_included__gt=0)
                            após o .annotate(), que o Django traduz em HAVING sobre a
                            agregação (não em WHERE). As contagens both included e total
                            continuam presentes no payload; o filtro apenas exclui
                            medicamentos sem nenhuma citação incluída.

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
            PaperDrug.objects
            .filter(paper_id__in=all_paper_ids)
            .values('drug_name_lower')
            .annotate(
                drug_name=Max('drug_name'),          # representativo do grupo
                drugbank_id=Max('drugbank_id'),       # primeiro não-vazio (Max sobre strings)
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
            qs = qs.filter(drug_name_lower__icontains=q)

        # Filtro HAVING: aplicado após annotate, o Django gera HAVING COUNT(...) > 0
        # em vez de WHERE — correto para filtros sobre agregações.
        if included_only:
            qs = qs.filter(unique_citations_included__gt=0)

        return qs

    # ------------------------------------------------------------------
    # Passo 3 — Detalhe de medicamento com snippets
    # ------------------------------------------------------------------

    @staticmethod
    def get_drug_detail(project: DaVinciProject, drug_name_lower: str) -> dict | None:
        """
        Retorna o dict completo para o endpoint de detalhe de medicamento.

        - Valida comprimento do nome (proteção contra ReDoS / custo de regex).
        - Busca todos os ProjectPaper do projeto que têm o medicamento.
        - Lê snippets do cache EntityContext (se disponível e fresco).
        - Detecta papers stale (sem nenhuma linha de contexto OU computed_at < paper.updated_at).
          A presença do marcador sentinela (sentence_position = -1) com computed_at
          fresco indica "processado, zero snippets" — o paper NÃO entra em computing.
        - Retorna context_status='computing' SOMENTE quando há paper sem cache fresco.

        Sem N+1: usa select_related e dict lookup para snippets.

        Chave canônica: entity_name = drug_name_lower (consistente entre task e lookup).
        """
        if len(drug_name_lower) > DRUG_NAME_MAX_LEN:
            return None

        # Papers do projeto que citam o medicamento (qualquer status)
        project_papers = (
            ProjectPaper.objects
            .filter(project=project, paper__drugs__drug_name_lower=drug_name_lower)
            .select_related('paper')
            .order_by('-paper__pub_year', 'paper__pmid')
        )

        if not project_papers.exists():
            return None

        # Métricas agregadas (order_by obrigatório antes de .first() sobre queryset anotado)
        drug_agg = (
            PaperDrug.objects
            .filter(
                paper__in_projects__project=project,
                drug_name_lower=drug_name_lower,
            )
            .values('drug_name_lower')
            .annotate(
                drug_name=Max('drug_name'),
                drugbank_id=Max('drugbank_id'),
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
            .order_by('drug_name_lower')
            .first()
        )

        if not drug_agg:
            return None

        # Carregar todos os contextos do cache (uma query, indexada por paper_id).
        # Inclui sentinelas (sentence_position = -1) que marcam "processado, sem snippets".
        # entity_name = drug_name_lower (chave canônica — consistente com a task).
        paper_ids = [pp.paper_id for pp in project_papers]
        cached_contexts = (
            EntityContext.objects
            .filter(
                paper_id__in=paper_ids,
                entity_type=EntityContext.EntityType.DRUG,
                entity_name=drug_name_lower,
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

        drug_name = drug_agg['drug_name']
        drugbank_id = drug_agg['drugbank_id'] or ''

        return {
            'drug_name': drug_name,
            'drugbank_id': drugbank_id,
            'unique_citations_included': drug_agg['unique_citations_included'],
            'unique_citations_total': drug_agg['unique_citations_total'],
            'drugbank_url': _drugbank_url(drugbank_id),
            'pubchem_search_url': _pubchem_search_url(drug_name),
            'references': references,
            'context_status': 'computing' if needs_compute else 'ready',
        }

    # ------------------------------------------------------------------
    # Passo 4 — Derivação de sentenças para um paper/medicamento
    # ------------------------------------------------------------------

    @staticmethod
    def extract_drug_sentences(paper: Paper, drug_name: str) -> list[dict]:
        """
        Extrai sentenças do abstract que contêm drug_name (com fronteira de palavra).

        Usa drug_name representativo (não drug_name_lower) para o match no abstract,
        mas registra entity_name=drug_name_lower (chave canônica) no EntityContext.

        Retorna lista de {'sentence': str, 'sentence_position': int}.
        Retorna lista vazia se abstract vazio ou drug não mencionado.

        Nota: drug_name deve ter ≤ DRUG_NAME_MAX_LEN chars (validado upstream).
        """
        sentences = _split_sentences(paper.abstract)
        if not sentences:
            return []

        pattern = _drug_sentence_re(drug_name)
        matches = []
        for pos, sentence in enumerate(sentences):
            if pattern.search(sentence):
                matches.append({'sentence': sentence, 'sentence_position': pos})
        return matches

    @staticmethod
    def derive_and_persist_contexts(project: DaVinciProject, drug_name_lower: str) -> int:
        """
        Deriva e persiste snippets de EntityContext para drug_name_lower em todos
        os papers do projeto que mencionam o medicamento.

        Idempotente: limpa contextos existentes (snippets + sentinelas) de cada
        paper antes de repovoar.

        Chave canônica: entity_name = drug_name_lower (consistente com lookup).
        Match no abstract: usa drug_name representativo (Max do grupo) para
        a regex, pois o abstract contém o nome original (não lowercase).

        Marcador sentinela:
            Quando um paper tem abstract mas o medicamento não aparece nele (ou
            quando o abstract está vazio), grava uma linha com sentence_position=-1
            e sentence='' e computed_at=now. Isso permite que get_drug_detail()
            distinga "nunca processado" (sem linha) de "processado, zero snippets"
            — eliminando o loop infinito de context_status='computing'.

        Retorna o número de snippets REAIS persistidos (sentinelas não contam).
        """
        from django.utils import timezone

        project_papers = (
            ProjectPaper.objects
            .filter(project=project, paper__drugs__drug_name_lower=drug_name_lower)
            .select_related('paper')
        )

        # drug_name representativo (para match no abstract, que tem o nome original)
        drug_name_rep = (
            PaperDrug.objects
            .filter(
                paper__in_projects__project=project,
                drug_name_lower=drug_name_lower,
            )
            .values_list('drug_name', flat=True)
            .order_by('drug_name')
            .first()
        ) or drug_name_lower  # fallback se não encontrar (não deveria ocorrer)

        now = timezone.now()
        to_create = []
        real_snippet_count = 0

        for pp in project_papers:
            paper = pp.paper

            # Limpar contextos existentes deste paper para este medicamento (snippets + sentinelas)
            EntityContext.objects.filter(
                paper=paper,
                entity_type=EntityContext.EntityType.DRUG,
                entity_name=drug_name_lower,
            ).delete()

            if not paper.abstract:
                # Abstract vazio — gravar sentinela para marcar "processado"
                to_create.append(
                    EntityContext(
                        paper=paper,
                        entity_type=EntityContext.EntityType.DRUG,
                        entity_name=drug_name_lower,
                        sentence=_SENTINEL_SENTENCE,
                        sentence_position=_SENTINEL_POSITION,
                        computed_at=now,
                    )
                )
                continue

            snippets = DrugService.extract_drug_sentences(paper, drug_name_rep)

            if snippets:
                real_snippet_count += len(snippets)
                for snippet in snippets:
                    to_create.append(
                        EntityContext(
                            paper=paper,
                            entity_type=EntityContext.EntityType.DRUG,
                            entity_name=drug_name_lower,
                            sentence=snippet['sentence'],
                            sentence_position=snippet['sentence_position'],
                            computed_at=now,
                        )
                    )
            else:
                # Medicamento não aparece no abstract — gravar sentinela para marcar "processado"
                to_create.append(
                    EntityContext(
                        paper=paper,
                        entity_type=EntityContext.EntityType.DRUG,
                        entity_name=drug_name_lower,
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
            'derive_and_persist_contexts: projeto=%s drug=%s snippets_reais=%d total_linhas=%d',
            project.id,
            drug_name_lower,
            real_snippet_count,
            len(to_create),
        )
        return real_snippet_count
