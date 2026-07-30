"""
Management command — load_pathway_topology

Carrega a topologia KEGG das 3 vias de sinalização (PI3K-Akt, MAPK, p53)
como catálogo global de grafos assinados (Pathway/PathwayNode/PathwayEdge).

OmnisPathway Objetivo 2, Fase 3, Passo 3.2.

Fonte (acesso público, licença acadêmica aceita, sem credencial):
  KEGG REST — https://rest.kegg.jp/get/<via>/kgml
  Vias padrão: hsa04151 (PI3K-Akt), hsa04010 (MAPK), hsa04115 (p53)

Schema resultante (catálogo global):
  Pathway: kegg_id, name, source='kegg', source_version
  PathwayNode: kegg_entry_id, node_type, gene_symbol (do graphics name),
               kegg_ids (Entrez cru), readout_role='none' (marcado em map_readouts)
  PathwayEdge: source_node, target_node, sign (+1/-1/0), relation_type, interaction

Nota sobre catálogo global:
  Pathway/PathwayNode/PathwayEdge são catálogo global de referência (sem FK
  de projeto, como GeneRole/VariantEffectRaw). --project é usado apenas como
  FK de tracking para o IngestionJob (constraint do modelo). A carga afeta
  as tabelas globais, não apenas o projeto.

Analogia com load_cnv_matrix:
  - --project obrigatório (tracking do IngestionJob — constraint do modelo).
  - --async enfileira via Celery; padrão síncrono para bancada de prova.
  - --pathways aceita lista de IDs KEGG separados por vírgula.

Idempotência:
    O Rust usa COPY ON CONFLICT DO UPDATE — re-rodar atualiza sem duplicar.
    O gate de idempotência Django verifica job ativo (PENDING/RUNNING).

Licença:
    KEGG via REST para uso acadêmico. Cache local em diagnostics/cache/kegg/
    (gitignored). O grafo derivado persiste em PG — o KGML bruto não é
    commitado nem redistribuído.

Sensitive-data-handling:
    db_url construída de settings.DATABASES — NUNCA logada nem exibida.
    Nenhuma credencial neste fluxo (KEGG público sem autenticação no v1).
"""

from django.core.management.base import BaseCommand, CommandError

_DEFAULT_PATHWAYS = 'hsa04151,hsa04010,hsa04115'


class Command(BaseCommand):
    help = (
        'Carrega a topologia KEGG das 3 vias de sinalização como catálogo '
        'global de grafos assinados (Pathway/PathwayNode/PathwayEdge). '
        'OmnisPathway Obj 2, Fase 3, Passo 3.2.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--project',
            metavar='UUID',
            required=True,
            help=(
                'UUID do DaVinciProject de tracking (obrigatório — '
                'FK do IngestionJob; não restringe a carga ao projeto).'
            ),
        )
        parser.add_argument(
            '--pathways',
            metavar='IDS',
            default=_DEFAULT_PATHWAYS,
            help=(
                f'IDs KEGG separados por vírgula '
                f'(padrão: {_DEFAULT_PATHWAYS}).'
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
        from apps.core.services.pathway_topology_load_service import (
            DEFAULT_PATHWAY_IDS,
            LOADER_VERSION,
            PathwayTopologyJobActiveError,
            PathwayTopologyLoadService,
        )

        project_uuid = options['project']
        use_async = options['use_async']

        # Parseia lista de IDs KEGG
        raw_pathways = options['pathways']
        pathway_ids = [p.strip() for p in raw_pathways.split(',') if p.strip()]
        if not pathway_ids:
            pathway_ids = DEFAULT_PATHWAY_IDS

        # ── Resolve projeto ───────────────────────────────────────────────────
        try:
            project = DaVinciProject.objects.select_related('user').get(pk=project_uuid)
        except DaVinciProject.DoesNotExist:
            raise CommandError(f'Projeto não encontrado: {project_uuid}')

        self.stdout.write(f'Projeto (tracking): {project.title} (id={project.id})')
        self.stdout.write(f'loader_version: {LOADER_VERSION}')
        self.stdout.write(f'Vias KEGG: {pathway_ids}')
        self.stdout.write(
            'Catálogo global: Pathway/PathwayNode/PathwayEdge não são '
            'isolados por projeto.'
        )
        self.stdout.write(
            'Licença: KEGG via REST (acadêmico). Cache local gitignored. '
            'KGML bruto não persistido no produto.'
        )

        if use_async:
            # ── Modo assíncrono: apenas enfileira ─────────────────────────────
            self.stdout.write('Modo: assíncrono (Celery)')
            try:
                job = PathwayTopologyLoadService.dispatch(
                    project, pathway_ids=pathway_ids
                )
                self.stdout.write(self.style.SUCCESS(
                    f'Job enfileirado: {job.id} (status={job.status})\n'
                    f'Acompanhe o progresso via IngestionJob.id={job.id}'
                ))
            except PathwayTopologyJobActiveError as exc:
                self.stdout.write(self.style.WARNING(
                    f'Idempotência: job ativo encontrado — {exc}'
                ))
            return

        # ── Modo síncrono: executa end-to-end ─────────────────────────────────
        self.stdout.write('Modo: síncrono (aguarde — fetch KEGG REST pode demorar)')
        self.stdout.write(
            'Atenção: rust_engine deve estar compilado '
            '(`maturin develop --release`)'
        )

        try:
            service = PathwayTopologyLoadService(project)
            result = service.run(pathway_ids=pathway_ids)
        except PathwayTopologyJobActiveError as exc:
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
            raise CommandError(f'Carga de topologia KEGG falhou: {exc}') from exc

        # ── Relatório final ────────────────────────────────────────────────────
        self.stdout.write(self.style.SUCCESS(
            'Carga de topologia KEGG concluída com sucesso.'
        ))
        self.stdout.write(f'  job_id:           {result["job_id"]}')
        self.stdout.write(f'  n_pathways:       {result["n_pathways"]}')
        self.stdout.write(f'  n_nodes:          {result["n_nodes"]}')
        self.stdout.write(f'  n_edges:          {result["n_edges"]}')
        self.stdout.write(f'  n_signed:         {result["n_signed"]}')
        self.stdout.write(f'  n_unsigned:       {result["n_unsigned"]}')
        self.stdout.write(f'  n_orphan_symbols: {result["n_orphan_symbols"]}')
        self.stdout.write(f'  source_version:   {result["source_version"]}')
        self.stdout.write(
            f'\nPathway/PathwayNode/PathwayEdge populados no catálogo global.\n'
            f'readout_role permanece "none" — será marcado pelo map_readouts (passo 3.4).\n'
            f'\nPróximo passo: carregar regulons (load_regulons) — precisa dos nós '
            f'do grafo para derivar tf_allowlist.'
        )
