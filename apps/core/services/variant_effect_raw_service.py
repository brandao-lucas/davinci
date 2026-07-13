"""
VariantEffectRawService — orquestração Django da carga de efeito cru de variante.

OmnisPathway Objetivo 2, Fase 2, Slice 2B.

Responsabilidade:
  1. Pré-check: GeneRole populado (source='oncokb') — constrói a allowlist.
  2. Gate de idempotência: não duplicar job ativo de VARIANT_EFFECT_RAW_LOAD.
  3. Criar IngestionJob(job_type=VARIANT_EFFECT_RAW_LOAD).
  4. Construir mapa uniprot_id→gene_symbol para o AlphaMissense via UniProt
     REST (streaming por lote, resiliente a falhas parciais).
  5. Chamar rust_engine.load_clinvar_effects(url, dest_dir, db_url, gene_allowlist)
     → ClinVarLoadManifest{n_variants_processed, n_kept, n_skipped_offlist,
       n_skipped_no_gene, n_upserted, source_version, errors}.
  6. Chamar rust_engine.load_alphamissense_effects(url, dest_dir, db_url,
       uniprot_to_gene, gene_allowlist)
     → AlphaMissenseLoadManifest{n_variants_processed, n_kept,
       n_skipped_no_map, n_upserted, source_version, errors, handoff_required}.
     Se handoff_required=True (mapa vazio), AM é marcado como pulado.
  7. O Rust faz COPY/UPSERT direto em VariantEffectRaw — este service NÃO
     persiste VariantEffectRaw via ORM.
  8. Marcar IngestionJob COMPLETED com contadores combinados.

Fronteira de camadas (Regra #1 / django-rust-boundary):
  - Rust: fetch HTTP (ClinVar VCF ~1GB; AlphaMissense TSV ~643MB), parse
    streaming, filtro allowlist na origem, COPY/UPSERT em VariantEffectRaw.
    NÃO é necessário processar os arquivos em Python.
  - Django: pré-check/gate, allowlist de gene_symbols de GeneRole,
    construção do mapa uniprot→gene (rede leve: UniProt REST batch), db_url
    sem logar, IngestionJob. NÃO abre os arquivos grandes.

Mapa uniprot→gene (AlphaMissense):
  AlphaMissense indexa variantes por UniProt ID (ex.: "Q9Y6X9") e não por
  gene_symbol. Para filtrar pelas vias-alvo sem baixar os ~643 MB completos,
  o Rust precisa do mapa {uniprot_id → gene_symbol} dos genes do GeneRole.
  Fonte: UniProt REST API (sem credencial, público).
    URL: https://rest.uniprot.org/uniprotkb/search
    Query por gene_exact + organism_id=9606 + reviewed:true (Swiss-Prot).
  Batch: grupos de 20 genes por request (evita URL muito longa).
  Resiliência: timeout de 30s por request, 3 retries com backoff exponencial.
  Fallback gracioso: gene sem hit UniProt → ignorado no mapa (AM não filtra
  esse gene, e as variantes associadas serão descartadas pelo filtro allowlist
  do Rust). Conta e reporta n_genes_without_uniprot.
  Cache em disco: resultado gravado em diagnostics/cache/uniprot_gene_map.json
  para evitar re-fetch em re-execuções (re-gerado se o arquivo tiver > 7 dias).

Idempotência:
  Gate verifica job ativo PENDING/RUNNING de VARIANT_EFFECT_RAW_LOAD.
  O Rust usa UPSERT ON CONFLICT (variant_key, source, gene_symbol) DO UPDATE.

Sensitive-data-handling:
  - Nenhuma credencial neste fluxo (ClinVar e AM são públicos; UniProt não
    exige autenticação). Não logar nada que passe por aqui.
  - db_url construída de settings.DATABASES — NUNCA logada nem gravada em
    IngestionJob.parameters.
  - URLs das fontes são públicas — registradas em parameters para auditoria.

Pré-requisito:
  GeneRole populado (load_gene_roles --project <UUID> deve ter rodado).
  Se GeneRole.objects.filter(source='oncokb').count() == 0, o service levanta
  GeneRoleNotPopulatedError com mensagem clara.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone as dt_timezone
from typing import Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from django.conf import settings
from django.utils import timezone

from apps.core.models import DaVinciProject, GeneRole, IngestionJob

logger = logging.getLogger(__name__)

# ── URLs das fontes (públicas — registradas em parameters para auditoria) ──────
CLINVAR_VCF_URL = (
    'https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/clinvar.vcf.gz'
)
ALPHAMISSENSE_URL = (
    'https://storage.googleapis.com/dm_alphamissense/AlphaMissense_hg38.tsv.gz'
)

# ── UniProt REST API (sem credencial, público) ─────────────────────────────────
UNIPROT_API_BASE = 'https://rest.uniprot.org/uniprotkb/search'
UNIPROT_BATCH_SIZE = 20          # genes por request
UNIPROT_TIMEOUT_S = 30           # timeout HTTP por request
UNIPROT_MAX_RETRIES = 3          # retries por batch
UNIPROT_BACKOFF_BASE_S = 2.0     # base do backoff exponencial (2, 4, 8s)

# ── Cache do mapa uniprot→gene em disco ───────────────────────────────────────
UNIPROT_CACHE_FILENAME = 'uniprot_gene_map.json'
UNIPROT_CACHE_MAX_AGE_DAYS = 7   # re-gerar se mais antigo que N dias

# Versão do loader
LOADER_VERSION = '0.1.0-clinvar-alphamissense-snv'


# ── Exceções ──────────────────────────────────────────────────────────────────

class GeneRoleNotPopulatedError(Exception):
    """Levantado quando GeneRole está vazio — load_gene_roles deve rodar antes."""

    def __init__(self):
        super().__init__(
            'GeneRole não populado (source=oncokb). '
            'Execute `manage.py load_gene_roles --project <UUID>` antes de '
            'load_variant_effects.'
        )


class VariantEffectRawJobActiveError(Exception):
    """Levantado quando já há um VARIANT_EFFECT_RAW_LOAD pending/running."""

    def __init__(self, job: IngestionJob):
        self.job = job
        super().__init__(
            f'VARIANT_EFFECT_RAW_LOAD já ativo (job {job.id}, '
            f'status={job.status}) — dispatch ignorado (idempotência)'
        )


# ── Helper: mapa uniprot_id → gene_symbol ─────────────────────────────────────

def _cache_path() -> str:
    """Retorna o caminho do arquivo de cache do mapa uniprot→gene."""
    cache_dir = os.path.join(
        settings.BASE_DIR, 'diagnostics', 'cache'
    )
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, UNIPROT_CACHE_FILENAME)


def _cache_is_fresh(path: str) -> bool:
    """Retorna True se o cache existe e tem menos de UNIPROT_CACHE_MAX_AGE_DAYS."""
    if not os.path.isfile(path):
        return False
    age_days = (time.time() - os.path.getmtime(path)) / 86400
    return age_days < UNIPROT_CACHE_MAX_AGE_DAYS


def _fetch_uniprot_batch(gene_symbols: list[str]) -> dict[str, str]:
    """
    Consulta o UniProt REST para um lote de símbolos de gene.

    Retorna dict {uniprot_accession → gene_symbol} para o lote.
    Usa gene_exact + organism_id:9606 + reviewed:true para pegar apenas
    o canonical Swiss-Prot humano.

    Em caso de falha (timeout, HTTP error), retorna {} para o lote.
    Resiliência: não propaga exceção; o chamador contabiliza genes perdidos.
    """
    # Constrói a query OR: "gene_exact:TP53 OR gene_exact:BRCA1 OR ..."
    query_parts = [f'gene_exact:{sym}' for sym in gene_symbols]
    query = f'({" OR ".join(query_parts)}) AND organism_id:9606 AND reviewed:true'
    params = urlencode({
        'query': query,
        'fields': 'accession,gene_names',
        'format': 'tsv',
        'size': len(gene_symbols) * 3,  # margem para variantes de splice/isoforma
    })
    url = f'{UNIPROT_API_BASE}?{params}'

    result: dict[str, str] = {}

    for attempt in range(1, UNIPROT_MAX_RETRIES + 1):
        try:
            req = Request(url, headers={'Accept': 'text/plain'})
            with urlopen(req, timeout=UNIPROT_TIMEOUT_S) as resp:
                body = resp.read().decode('utf-8')

            # Formato TSV: "Entry\tGene Names\n" — pegar o primeiro gene primário
            lines = body.strip().split('\n')
            if len(lines) < 2:
                # Nenhum resultado para o lote
                break

            for line in lines[1:]:  # pula cabeçalho
                parts = line.split('\t')
                if len(parts) < 2:
                    continue
                accession = parts[0].strip()
                gene_names_raw = parts[1].strip()
                if not accession or not gene_names_raw:
                    continue

                # gene_names pode ser "TP53 TP53_HUMAN" — pegar o primeiro
                primary_gene = gene_names_raw.split()[0].upper()

                # Só adicionar se o primary_gene é um dos genes do lote
                # (evita mapear genes externos ao lote de allowlist)
                if primary_gene in {s.upper() for s in gene_symbols}:
                    result[accession] = primary_gene

            break  # sucesso — sai do loop de retry

        except (URLError, HTTPError, TimeoutError) as exc:
            if attempt < UNIPROT_MAX_RETRIES:
                wait = UNIPROT_BACKOFF_BASE_S ** attempt
                logger.warning(
                    'UniProt batch falhou (tentativa %d/%d): %s — '
                    'aguardando %.1fs antes de retry',
                    attempt,
                    UNIPROT_MAX_RETRIES,
                    exc,
                    wait,
                )
                time.sleep(wait)
            else:
                logger.warning(
                    'UniProt batch falhou após %d tentativas: %s — '
                    'genes do lote ficam sem uniprot_id (fallback gracioso)',
                    UNIPROT_MAX_RETRIES,
                    exc,
                )

    return result


def build_uniprot_gene_map(
    gene_symbols: list[str],
    *,
    use_cache: bool = True,
) -> tuple[dict[str, str], int]:
    """
    Constrói o mapa {uniprot_id → gene_symbol} para os genes do GeneRole.

    Estratégia:
      1. Se cache em disco existir e for fresco (< 7 dias), usa o cache
         filtrado pelos gene_symbols atuais.
      2. Caso contrário, consulta UniProt REST em lotes de UNIPROT_BATCH_SIZE.
      3. Grava resultado em diagnostics/cache/uniprot_gene_map.json.
      4. Genes sem hit UniProt → não entram no mapa (AM não filtra esses genes;
         variantes associadas são descartadas pelo filtro allowlist do Rust).

    Args:
        gene_symbols: lista de gene_symbols de GeneRole (source='oncokb').
        use_cache: se True, tenta usar cache em disco; útil desabilitar em testes.

    Returns:
        (uniprot_to_gene, n_genes_without_uniprot):
          - uniprot_to_gene: {uniprot_accession → gene_symbol}
          - n_genes_without_uniprot: quantos genes do allowlist ficaram sem hit
    """
    cache_path = _cache_path()
    symbols_set = {s.upper() for s in gene_symbols}

    # ── Tenta cache ───────────────────────────────────────────────────────────
    if use_cache and _cache_is_fresh(cache_path):
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                cached: dict[str, str] = json.load(f)

            # Filtra o cache pelos genes da allowlist atual
            filtered = {
                acc: sym
                for acc, sym in cached.items()
                if sym.upper() in symbols_set
            }
            # Genes do allowlist que não têm nenhum accession no cache
            genes_in_cache = {sym.upper() for sym in filtered.values()}
            n_without = sum(
                1 for s in symbols_set if s not in genes_in_cache
            )

            logger.info(
                'Mapa uniprot→gene carregado do cache (%s): '
                '%d mappings, %d genes sem uniprot_id (de %d no allowlist)',
                cache_path,
                len(filtered),
                n_without,
                len(symbols_set),
            )
            return filtered, n_without
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                'Cache uniprot→gene inválido (%s): %s — re-fetching',
                cache_path,
                exc,
            )

    # ── Fetch da UniProt REST por lote ────────────────────────────────────────
    symbols_list = sorted(symbols_set)  # ordem determinística
    n_total = len(symbols_list)
    full_map: dict[str, str] = {}

    logger.info(
        'Consultando UniProt REST para %d gene_symbols (lotes de %d)',
        n_total,
        UNIPROT_BATCH_SIZE,
    )

    for i in range(0, n_total, UNIPROT_BATCH_SIZE):
        batch = symbols_list[i: i + UNIPROT_BATCH_SIZE]
        batch_result = _fetch_uniprot_batch(batch)
        full_map.update(batch_result)

        # Log de progresso a cada 10 lotes (~200 genes)
        if (i // UNIPROT_BATCH_SIZE) % 10 == 0:
            logger.debug(
                'UniProt fetch progresso: %d/%d genes processados, '
                '%d mappings acumulados',
                min(i + UNIPROT_BATCH_SIZE, n_total),
                n_total,
                len(full_map),
            )

        # Pequena pausa entre lotes para não sobrecarregar a API
        if i + UNIPROT_BATCH_SIZE < n_total:
            time.sleep(0.2)

    # ── Contabiliza genes sem hit ──────────────────────────────────────────────
    genes_with_map = {sym.upper() for sym in full_map.values()}
    n_without = sum(1 for s in symbols_set if s not in genes_with_map)

    logger.info(
        'Mapa uniprot→gene construído: %d mappings para %d genes '
        '(%d genes sem uniprot_id — AM não filtrará esses genes)',
        len(full_map),
        n_total,
        n_without,
    )

    if n_without > 0:
        missing = sorted(s for s in symbols_set if s not in genes_with_map)
        logger.warning(
            '%d genes sem uniprot_id no mapa AM (serão descartados pelo '
            'filtro allowlist do Rust): %s%s',
            n_without,
            ', '.join(missing[:20]),
            ' ...' if len(missing) > 20 else '',
        )

    # ── Persiste cache em disco ───────────────────────────────────────────────
    if use_cache:
        try:
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump(full_map, f, ensure_ascii=False, indent=2)
            logger.info('Cache uniprot→gene gravado: %s', cache_path)
        except OSError as exc:
            logger.warning(
                'Falha ao gravar cache uniprot→gene (%s): %s — '
                'mapa apenas em memória',
                cache_path,
                exc,
            )

    return full_map, n_without


# ── Service principal ─────────────────────────────────────────────────────────

class VariantEffectRawService:
    """
    Serviço de carga do catálogo raw de efeito de variante (ClinVar + AlphaMissense).

    Uso síncrono (management command / bancada de prova):
        service = VariantEffectRawService(project)
        result = service.run()

    Uso assíncrono (Celery):
        job = VariantEffectRawService.dispatch(project)
        # task run_variant_effect_raw_load(job.id, str(project.id)) prossegue

    Flags:
        skip_alphamissense=True — executa apenas ClinVar (útil para testes ou
        quando o AlphaMissense ~643MB não é necessário na primeira carga).
    """

    def __init__(
        self,
        project: DaVinciProject,
        *,
        skip_alphamissense: bool = False,
    ):
        self.project = project
        self.skip_alphamissense = skip_alphamissense

    # ── API pública ────────────────────────────────────────────────────────────

    @classmethod
    def dispatch(
        cls,
        project: DaVinciProject,
        *,
        skip_alphamissense: bool = False,
    ) -> IngestionJob:
        """
        Gate de idempotência + criação do IngestionJob.

        Não executa a carga — apenas verifica pré-condições e enfileira
        a task Celery (via run_variant_effect_raw_load).

        Returns:
            IngestionJob recém-criado (status=PENDING).

        Raises:
            GeneRoleNotPopulatedError: GeneRole vazio.
            VariantEffectRawJobActiveError: job ativo encontrado.
        """
        from apps.core.tasks.ingestion_tasks import run_variant_effect_raw_load

        service = cls(project, skip_alphamissense=skip_alphamissense)
        gene_allowlist = service._check_gene_role_populated()
        service._check_idempotency()

        job = IngestionJob.objects.create(
            project=project,
            job_type=IngestionJob.JobType.VARIANT_EFFECT_RAW_LOAD,
            status=IngestionJob.JobStatus.PENDING,
            parameters={
                'clinvar_url': CLINVAR_VCF_URL,
                'alphamissense_url': ALPHAMISSENSE_URL,
                'n_genes_in_allowlist': len(gene_allowlist),
                'skip_alphamissense': skip_alphamissense,
                'loader_version': LOADER_VERSION,
                # db_url NÃO é armazenada (sensitive-data-handling)
            },
        )

        try:
            run_variant_effect_raw_load.delay(
                str(job.id),
                str(project.id),
                skip_alphamissense=skip_alphamissense,
            )
            logger.info(
                'VARIANT_EFFECT_RAW_LOAD disparado (job %s) para projeto %s, '
                '%d genes no allowlist, skip_am=%s',
                job.id,
                project.id,
                len(gene_allowlist),
                skip_alphamissense,
            )
        except Exception as exc:
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=f'Failed to dispatch Celery task: {exc}',
            )
            raise

        return job

    def run(
        self,
        job: Optional[IngestionJob] = None,
        *,
        use_uniprot_cache: bool = True,
    ) -> dict:
        """
        Executa a carga de forma síncrona.

        Fluxo:
          1. Pré-check GeneRole populado → gene_allowlist.
          2. Gate de idempotência (job ativo).
          3. Cria/atualiza IngestionJob para RUNNING.
          4. Constrói mapa uniprot→gene (UniProt REST, com cache).
          5. Rust: load_clinvar_effects → ClinVarLoadManifest.
          6. Rust: load_alphamissense_effects → AlphaMissenseLoadManifest
             (ou marcado como pulado se skip_alphamissense=True ou
              mapa vazio / handoff_required).
          7. Marca job COMPLETED com contadores combinados.

        Args:
            job: IngestionJob existente (criado pelo dispatch); se None,
                 cria internamente (modo síncrono direto do command).
            use_uniprot_cache: se True, usa/grava cache em disco (padrão).
                 Desabilitar em testes para evitar I/O de disco.

        Returns:
            dict com contadores ClinVar, AlphaMissense e mapa.
        """
        import rust_engine

        gene_allowlist = self._check_gene_role_populated()

        if job is None:
            self._check_idempotency()

        if job is None:
            job = IngestionJob.objects.create(
                project=self.project,
                job_type=IngestionJob.JobType.VARIANT_EFFECT_RAW_LOAD,
                status=IngestionJob.JobStatus.RUNNING,
                parameters={
                    'clinvar_url': CLINVAR_VCF_URL,
                    'alphamissense_url': ALPHAMISSENSE_URL,
                    'n_genes_in_allowlist': len(gene_allowlist),
                    'skip_alphamissense': self.skip_alphamissense,
                    'loader_version': LOADER_VERSION,
                },
            )
        else:
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.RUNNING,
                started_at=timezone.now(),
            )

        try:
            result = self._execute(job, gene_allowlist, rust_engine, use_uniprot_cache)
            return result
        except Exception as exc:
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=str(exc),
                completed_at=timezone.now(),
            )
            raise

    # ── Implementação interna ──────────────────────────────────────────────────

    def _check_gene_role_populated(self) -> list[str]:
        """
        Verifica se GeneRole está populado (source='oncokb') e retorna a allowlist.

        Raises:
            GeneRoleNotPopulatedError: se nenhum GeneRole com source='oncokb'.

        Returns:
            Lista de gene_symbols únicos (UPPERCASE) da fonte 'oncokb'.
        """
        allowlist = list(
            GeneRole.objects.filter(source='oncokb')
            .values_list('gene_symbol', flat=True)
            .order_by('gene_symbol')
        )
        if not allowlist:
            raise GeneRoleNotPopulatedError()
        return allowlist

    def _check_idempotency(self) -> None:
        """
        Verifica se há um VARIANT_EFFECT_RAW_LOAD ativo (pending ou running).

        Raises:
            VariantEffectRawJobActiveError: job ativo encontrado.
        """
        active_job = IngestionJob.objects.filter(
            project=self.project,
            job_type=IngestionJob.JobType.VARIANT_EFFECT_RAW_LOAD,
            status__in=[
                IngestionJob.JobStatus.PENDING,
                IngestionJob.JobStatus.RUNNING,
            ],
        ).first()
        if active_job:
            raise VariantEffectRawJobActiveError(active_job)

    def _build_db_url(self) -> str:
        """
        Constrói a db_url a partir de settings.DATABASES['default'].

        NUNCA logar o retorno desta função (contém credenciais).
        """
        db = settings.DATABASES['default']
        return (
            f"postgresql://{db['USER']}:{db['PASSWORD']}"
            f"@{db['HOST']}:{db['PORT']}/{db['NAME']}"
        )

    def _execute(
        self,
        job: IngestionJob,
        gene_allowlist: list[str],
        rust_engine,
        use_uniprot_cache: bool,
    ) -> dict:
        """
        Núcleo da carga: constrói mapa → ClinVar → AlphaMissense → COMPLETED.

        rust_engine passado explicitamente para facilitar mock em testes.
        """
        with tempfile.TemporaryDirectory(prefix='davinci_vareff_') as dest_dir:
            # ── db_url — NUNCA logada ──────────────────────────────────────────
            db_url = self._build_db_url()

            # ── Passo 1: mapa uniprot→gene para AlphaMissense ─────────────────
            uniprot_to_gene: dict[str, str] = {}
            n_genes_without_uniprot = 0
            am_skipped = False
            am_skip_reason = ''

            if self.skip_alphamissense:
                am_skipped = True
                am_skip_reason = '--skip-alphamissense passado pelo caller'
                logger.info(
                    'AlphaMissense: pulado por flag --skip-alphamissense (job %s)',
                    job.id,
                )
            else:
                logger.info(
                    'Construindo mapa uniprot→gene para %d genes '
                    '(job %s, use_cache=%s)',
                    len(gene_allowlist),
                    job.id,
                    use_uniprot_cache,
                )
                uniprot_to_gene, n_genes_without_uniprot = build_uniprot_gene_map(
                    gene_allowlist,
                    use_cache=use_uniprot_cache,
                )
                logger.info(
                    'Mapa uniprot→gene: %d entries, %d genes sem uniprot_id '
                    '(job %s)',
                    len(uniprot_to_gene),
                    n_genes_without_uniprot,
                    job.id,
                )

            # ── Passo 2: ClinVar (sempre) ──────────────────────────────────────
            logger.info(
                'Chamando rust_engine.load_clinvar_effects (job %s, '
                'url=%s, dest_dir omitido, %d genes no allowlist)',
                job.id,
                CLINVAR_VCF_URL,
                len(gene_allowlist),
            )

            clinvar_manifest = rust_engine.load_clinvar_effects(
                url=CLINVAR_VCF_URL,
                dest_dir=dest_dir,
                db_url=db_url,
                gene_allowlist=gene_allowlist,
            )

            logger.info(
                'load_clinvar_effects concluído (job %s): '
                'n_variants_processed=%d, n_kept=%d, n_skipped_offlist=%d, '
                'n_skipped_no_gene=%d, n_upserted=%d, source_version=%s',
                job.id,
                clinvar_manifest.n_variants_processed,
                clinvar_manifest.n_kept,
                clinvar_manifest.n_skipped_offlist,
                clinvar_manifest.n_skipped_no_gene,
                clinvar_manifest.n_upserted,
                clinvar_manifest.source_version,
            )

            # Reporta erros do ClinVar (não para a execução — reporta e segue)
            if clinvar_manifest.errors:
                for err in clinvar_manifest.errors:
                    logger.warning('ClinVar loader erro (job %s): %s', job.id, err)

            # ── Passo 3: AlphaMissense (com mapa; ou pulado) ───────────────────
            am_manifest = None

            if not am_skipped:
                logger.info(
                    'Chamando rust_engine.load_alphamissense_effects (job %s, '
                    'url=%s, dest_dir omitido, %d entries no mapa, '
                    '%d genes no allowlist)',
                    job.id,
                    ALPHAMISSENSE_URL,
                    len(uniprot_to_gene),
                    len(gene_allowlist),
                )

                am_manifest = rust_engine.load_alphamissense_effects(
                    url=ALPHAMISSENSE_URL,
                    dest_dir=dest_dir,
                    db_url=db_url,
                    uniprot_to_gene=uniprot_to_gene,
                    gene_allowlist=gene_allowlist,
                )

                # Verifica handoff_required (mapa enviado vazio ao Rust)
                if am_manifest.handoff_required:
                    am_skipped = True
                    am_skip_reason = (
                        'AlphaMissense: handoff_required=True retornado pelo Rust '
                        '(mapa uniprot→gene vazio)'
                    )
                    logger.warning(
                        'AlphaMissense pulado: handoff_required=True (job %s). '
                        'Nenhuma variante AM carregada.',
                        job.id,
                    )
                else:
                    logger.info(
                        'load_alphamissense_effects concluído (job %s): '
                        'n_variants_processed=%d, n_kept=%d, '
                        'n_skipped_no_map=%d, n_upserted=%d, source_version=%s',
                        job.id,
                        am_manifest.n_variants_processed,
                        am_manifest.n_kept,
                        am_manifest.n_skipped_no_map,
                        am_manifest.n_upserted,
                        am_manifest.source_version,
                    )
                    if am_manifest.errors:
                        for err in am_manifest.errors:
                            logger.warning(
                                'AlphaMissense loader erro (job %s): %s',
                                job.id,
                                err,
                            )

        # db_url saiu de escopo ao sair do TemporaryDirectory — não referenciada abaixo

        # ── Monta resultado combinado ──────────────────────────────────────────
        n_total_upserted = clinvar_manifest.n_upserted
        if am_manifest and not am_skipped:
            n_total_upserted += am_manifest.n_upserted

        n_total_processed = clinvar_manifest.n_variants_processed
        if am_manifest and not am_skipped:
            n_total_processed += am_manifest.n_variants_processed

        # ── Marca job COMPLETED ────────────────────────────────────────────────
        IngestionJob.objects.filter(id=job.id).update(
            status=IngestionJob.JobStatus.COMPLETED,
            records_processed=n_total_processed,
            records_inserted=n_total_upserted,
            completed_at=timezone.now(),
        )

        result = {
            'job_id': str(job.id),
            'n_genes_in_allowlist': len(gene_allowlist),
            # ClinVar
            'clinvar_n_variants_processed': clinvar_manifest.n_variants_processed,
            'clinvar_n_kept': clinvar_manifest.n_kept,
            'clinvar_n_skipped_offlist': clinvar_manifest.n_skipped_offlist,
            'clinvar_n_skipped_no_gene': clinvar_manifest.n_skipped_no_gene,
            'clinvar_n_upserted': clinvar_manifest.n_upserted,
            'clinvar_source_version': clinvar_manifest.source_version,
            'clinvar_errors': list(clinvar_manifest.errors),
            # AlphaMissense
            'am_skipped': am_skipped,
            'am_skip_reason': am_skip_reason,
            'am_n_variants_processed': (
                am_manifest.n_variants_processed
                if am_manifest and not am_skipped else 0
            ),
            'am_n_kept': (
                am_manifest.n_kept
                if am_manifest and not am_skipped else 0
            ),
            'am_n_skipped_no_map': (
                am_manifest.n_skipped_no_map
                if am_manifest and not am_skipped else 0
            ),
            'am_n_upserted': (
                am_manifest.n_upserted
                if am_manifest and not am_skipped else 0
            ),
            'am_source_version': (
                am_manifest.source_version
                if am_manifest and not am_skipped else ''
            ),
            'am_errors': (
                list(am_manifest.errors)
                if am_manifest and not am_skipped else []
            ),
            # Mapa uniprot→gene
            'n_genes_with_uniprot': len(uniprot_to_gene),
            'n_genes_without_uniprot': n_genes_without_uniprot,
        }

        logger.info(
            'VARIANT_EFFECT_RAW_LOAD concluído (job %s): '
            'clinvar_n_upserted=%d, am_n_upserted=%d, am_skipped=%s, '
            'n_genes_without_uniprot=%d',
            job.id,
            clinvar_manifest.n_upserted,
            result['am_n_upserted'],
            am_skipped,
            n_genes_without_uniprot,
        )

        return result
