"""
Management command — load_regulons

Carrega regulons CollecTRI (via OmniPath) para os TFs das 3 vias de
sinalização KEGG, gerando o arquivo JSONL intermediário consumido pelo
mapeamento readout→feature (map_readouts, passo 3.4).

OmnisPathway Objetivo 2, Fase 3, Passo 3.3.

Fonte (acesso público, licença GPL/acadêmica, sem credencial no v1):
  OmniPath — https://omnipathdb.org/
  Endpoint CollecTRI: interações TF→alvo assinadas (mode of regulation ±1)
  RISCO: OmniPath estava com 502 na discovery (2026-07-15). Confirmar
  endpoint real e colunas antes da 1ª ingestão live.

tf_allowlist:
  Derivada automaticamente dos PathwayNode(node_type='gene') das vias já
  carregadas em PG (passo 3.2). Apenas regulons de TFs que aparecem como
  nós no grafo são baixados (escopo v1 — Decisão 4 do plano).

Arquivo intermediário:
  O Rust grava um JSONL local (em diagnostics/cache/omnipath/) e guarda o
  caminho em IngestionJob.parameters['regulon_path']. O passo 3.4 usa esse
  caminho. Se 3.3 e 3.4 rodarem em workers Celery distintos sem volume
  compartilhado, o JSONL não estará acessível — rodar ambos no mesmo worker.

Pré-condição:
  load_pathway_topology (passo 3.2) deve ter sido executado. Os PathwayNode
  das 3 vias precisam existir em PG para derivar o tf_allowlist.

Sensitive-data-handling:
    Nenhuma credencial necessária (OmniPath público sem token no v1).
    regulon_path gravado em IngestionJob.parameters é caminho LOCAL — não
    exposto ao cliente nem em logs de produção.
"""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        'Carrega regulons CollecTRI (OmniPath) para os TFs das vias KEGG. '
        'tf_allowlist derivada automaticamente dos PathwayNode do grafo. '
        'OmnisPathway Obj 2, Fase 3, Passo 3.3.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--project',
            metavar='UUID',
            required=True,
            help=(
                'UUID do DaVinciProject de tracking (obrigatório — '
                'FK do IngestionJob; não restringe o regulon ao projeto).'
            ),
        )
        parser.add_argument(
            '--async',
            action='store_true',
            default=False,
            dest='use_async',
            help=(
                'Enfileira a carga via Celery (worker deve estar ativo). '
                'Padrão: execução síncrona (bancada de prova).'
            ),
        )

    def handle(self, *args, **options):
        from apps.core.models import DaVinciProject
        from apps.core.services.regulon_load_service import (
            DEFAULT_PATHWAY_IDS,
            LOADER_VERSION,
            RegulonGraphNotLoadedError,
            RegulonJobActiveError,
            RegulonLoadService,
        )

        project_uuid = options['project']
        use_async = options['use_async']

        # ── Resolve projeto ───────────────────────────────────────────────────
        try:
            project = DaVinciProject.objects.select_related('user').get(pk=project_uuid)
        except DaVinciProject.DoesNotExist:
            raise CommandError(f'Projeto não encontrado: {project_uuid}')

        self.stdout.write(f'Projeto (tracking): {project.title} (id={project.id})')
        self.stdout.write(f'loader_version: {LOADER_VERSION}')
        self.stdout.write(f'Vias para derivar tf_allowlist: {DEFAULT_PATHWAY_IDS}')
        self.stdout.write(
            'tf_allowlist: derivada automaticamente dos PathwayNode(node_type=gene) '
            'já carregados em PG.'
        )
        self.stdout.write(
            'Licença: CollecTRI via OmniPath (GPL/acadêmico). Cache local gitignored. '
            'Não redistribuir dado bruto.'
        )
        self.stdout.write(
            'RISCO: OmniPath estava com 502 em 2026-07-15. Confirmar endpoint '
            'e colunas na 1ª ingestão live.'
        )
        self.stdout.write(
            'Pré-condição: load_pathway_topology deve ter sido executado antes.'
        )

        if use_async:
            # ── Modo assíncrono: apenas enfileira ─────────────────────────────
            self.stdout.write('Modo: assíncrono (Celery)')
            try:
                job = RegulonLoadService.dispatch(project)
                self.stdout.write(self.style.SUCCESS(
                    f'Job enfileirado: {job.id} (status={job.status})\n'
                    f'Acompanhe o progresso via IngestionJob.id={job.id}'
                ))
            except RegulonGraphNotLoadedError as exc:
                raise CommandError(
                    f'Pré-condição não satisfeita: {exc}\n'
                    f'Execute load_pathway_topology antes de carregar regulons.'
                )
            except RegulonJobActiveError as exc:
                self.stdout.write(self.style.WARNING(
                    f'Idempotência: job ativo encontrado — {exc}'
                ))
            return

        # ── Modo síncrono: executa end-to-end ─────────────────────────────────
        self.stdout.write('Modo: síncrono (aguarde — fetch OmniPath pode demorar)')
        self.stdout.write(
            'Atenção: rust_engine deve estar compilado '
            '(`maturin develop --release`)'
        )

        try:
            service = RegulonLoadService(project)
            result = service.run()
        except RegulonGraphNotLoadedError as exc:
            raise CommandError(
                f'Pré-condição não satisfeita: {exc}\n'
                f'Execute load_pathway_topology antes de carregar regulons.'
            )
        except RegulonJobActiveError as exc:
            self.stdout.write(self.style.WARNING(
                f'Idempotência: job ativo encontrado.\n'
                f'  {exc}\n'
                f'  Aguarde o job concluir ou cancele-o manualmente.'
            ))
            return
        except ImportError:
            raise CommandError(
                'rust_engine não encontrado. Compile com: maturin develop --release'
            )
        except Exception as exc:
            raise CommandError(f'Carga de regulons falhou: {exc}') from exc

        # ── Relatório final ────────────────────────────────────────────────────
        self.stdout.write(self.style.SUCCESS(
            'Carga de regulons CollecTRI concluída com sucesso.'
        ))
        self.stdout.write(f'  job_id:            {result["job_id"]}')
        self.stdout.write(f'  n_tfs:             {result["n_tfs"]}')
        self.stdout.write(f'  n_targets:         {result["n_targets"]}')
        self.stdout.write(f'  n_edges:           {result["n_edges"]}')
        self.stdout.write(f'  n_positive:        {result["n_positive"]}')
        self.stdout.write(f'  n_negative:        {result["n_negative"]}')
        self.stdout.write(f'  n_neutral:         {result["n_neutral"]}')
        self.stdout.write(f'  source_version:    {result["source_version"]}')
        self.stdout.write(f'  tf_allowlist_size: {result["tf_allowlist_size"]}')
        # regulon_path não impresso (caminho local — não expor ao cliente)
        self.stdout.write(
            f'\nArquivo JSONL de regulon gravado localmente '
            f'(caminho em IngestionJob.parameters["regulon_path"]).\n'
            f'\nPróximo passo: mapear readouts (map_readouts Regra 2) — '
            f'usa o JSONL de regulon para mapear TFs × features da matriz proteoma.'
        )
