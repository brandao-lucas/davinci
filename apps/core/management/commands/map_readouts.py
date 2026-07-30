"""
Management command — map_readouts

Executa o mapeamento readout→feature (Fase 3, Passo 3.4):
  Regra 1 (fosfo): PathwayNode(node_type='gene') das vias hsa04151/hsa04010
                   × matriz de fosfoproteoma (phospho_site).
  Regra 2 (TF):   TFs do regulon CollecTRI × matriz proteoma (gene).

Materializa PathwayReadoutFeature (bulk_create ignore_conflicts) e atualiza
PathwayNode.readout_role para 'phospho' / 'tf_target'.

OmnisPathway Objetivo 2, Fase 3, Passo 3.4.

Pré-condições (em ordem de execução):
  1. load_phospho_matrix (passo 3.1) — matriz de fosfoproteoma em PG.
  2. load_cptac_matrix (Fase 0) — matriz proteoma gene-level em PG.
  3. load_pathway_topology (passo 3.2) — PathwayNode das 3 vias em PG.
  4. load_regulons (passo 3.3) — JSONL de regulon em
     IngestionJob.parameters['regulon_path'].

Sem endpoint HTTP (só management command).

Estratégia de validação de existência (A→C concluída — migration 0036):
  feature_key validada contra `OmicMatrixFeature` da MATRIZ ALVO (fosfo para
  a Regra 1, proteoma para a Regra 2), não mais contra o grafo KEGG. Ver
  readout_mapping_service.py para a justificativa completa.

  Pré-condição adicional: a matriz alvo precisa ter `OmicMatrixFeature`
  catalogado (rode `backfill_matrix_features` antes). Se estiver vazio, o
  mapeamento falha alto (OmicMatrixFeatureNotCataloguedError) em vez de
  degradar silenciosamente para a validação intra-grafo antiga.

Idempotência:
  bulk_create(ignore_conflicts=True) — seguro re-rodar. PathwayNode.readout_role
  é atualizado (sobrescreve) — também idempotente.

Sensitive-data-handling:
    Nenhuma credencial. regulon_path é caminho local (nunca exposto ao cliente
    nem em stdout — só os contadores são impressos).
"""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        'Mapeia readouts do grafo KEGG a features das matrizes ômicas '
        '(Regra 1 fosfo + Regra 2 TF). OmnisPathway Obj 2, Fase 3, Passo 3.4.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--pathway',
            metavar='KEGG_ID',
            default=None,
            help=(
                'Restringir a uma via KEGG (ex: hsa04151). '
                'Padrão: todas as vias configuradas no service.'
            ),
        )
        parser.add_argument(
            '--mapping-version',
            metavar='VERSION',
            default='fase3-readout-v1',
            dest='mapping_version',
            help=(
                'Versão do mapeamento (integra a natural key — versões '
                'coexistem). Padrão: fase3-readout-v1.'
            ),
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            dest='dry_run',
            help=(
                'Calcula o mapeamento sem persistir (mostra contadores). '
                'Útil para verificação sem alterar o banco.'
            ),
        )

    def handle(self, *args, **options):
        from apps.core.services.readout_mapping_service import (
            DEFAULT_MAPPING_VERSION,
            PHOSPHO_PATHWAY_IDS,
            TF_PATHWAY_IDS,
            ReadoutMappingError,
            ReadoutMappingService,
        )

        pathway_filter = options['pathway']
        mapping_version = options['mapping_version'] or DEFAULT_MAPPING_VERSION
        dry_run = options['dry_run']

        # Filtra vias se --pathway foi especificado
        phospho_ids = PHOSPHO_PATHWAY_IDS
        tf_ids = TF_PATHWAY_IDS
        if pathway_filter:
            phospho_ids = [p for p in PHOSPHO_PATHWAY_IDS if p == pathway_filter]
            tf_ids = [p for p in TF_PATHWAY_IDS if p == pathway_filter]
            if not phospho_ids and not tf_ids:
                raise CommandError(
                    f'Via {pathway_filter!r} nao esta na configuracao '
                    f'(fosfo: {PHOSPHO_PATHWAY_IDS}, tf: {TF_PATHWAY_IDS}).'
                )

        self.stdout.write(
            f'Mapeamento readout->feature (versao={mapping_version})'
        )
        self.stdout.write(f'  Vias fosfo (Regra 1): {phospho_ids}')
        self.stdout.write(f'  Vias TF (Regra 2):    {tf_ids}')
        self.stdout.write(f'  dry_run: {dry_run}')
        self.stdout.write('')
        self.stdout.write('Pre-condicoes necessarias:')
        self.stdout.write('  1. load_phospho_matrix (Regra 1)')
        self.stdout.write('  2. load_cptac_matrix Fase 0 (Regra 2)')
        self.stdout.write('  3. load_pathway_topology (ambas as regras)')
        self.stdout.write('  4. load_regulons (Regra 2)')
        self.stdout.write(
            '\nEstrategia de validacao: OmicMatrixFeature da matriz alvo '
            '(fosfo/proteoma). Requer backfill_matrix_features previamente '
            'executado para as matrizes envolvidas.'
        )
        self.stdout.write('')

        try:
            service = ReadoutMappingService(
                pathway_ids_phospho=phospho_ids,
                pathway_ids_tf=tf_ids,
                mapping_version=mapping_version,
                dry_run=dry_run,
            )
            report = service.run()
        except ReadoutMappingError as exc:
            raise CommandError(
                f'Pre-condicao nao satisfeita: {exc}\n'
                f'Se o erro mencionar OmicMatrixFeature vazio, rode '
                f'`manage.py backfill_matrix_features` para a matriz '
                f'indicada antes de tentar novamente.'
            ) from exc
        except Exception as exc:
            raise CommandError(f'Mapeamento falhou: {exc}') from exc

        # ── Relatório final ────────────────────────────────────────────────────
        if dry_run:
            self.stdout.write(self.style.WARNING(
                'DRY RUN — nada foi persistido no banco.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                'Mapeamento readout->feature concluido com sucesso.'
            ))

        self.stdout.write(f'  n_phospho_mapped:    {report["n_phospho_mapped"]}')
        self.stdout.write(f'  n_tf_mapped:         {report["n_tf_mapped"]}')
        self.stdout.write(f'  n_nodes_readout:     {report["n_nodes_readout"]}')
        self.stdout.write(f'  n_unmapped_nodes:    {report["n_unmapped_nodes"]}')
        self.stdout.write(f'  n_features_not_found:{report["n_features_not_found"]}')
        self.stdout.write(f'  mapping_version:     {report["mapping_version"]}')
        self.stdout.write(f'  duracao:             {report["duration_s"]}s')
        self.stdout.write(f'  phospho_matrix_id:   {report["phospho_matrix_id"]}')
        self.stdout.write(f'  proteome_matrix_id:  {report["proteome_matrix_id"]}')
        self.stdout.write(f'  regulon_path_found:  {report["regulon_path_found"]}')
        self.stdout.write('')
        self.stdout.write(f'  estrategia: {report["validation_strategy"]}')

        if not dry_run:
            total = report['n_phospho_mapped'] + report['n_tf_mapped']
            self.stdout.write(
                f'\nPathwayReadoutFeature: {total} objetos materializados '
                f'(bulk_create ignore_conflicts).\n'
                f'PathwayNode.readout_role atualizado para '
                f'{report["n_nodes_readout"]} nos.\n'
                f'\nFase 3 concluida. Proximo passo: Fase 4 (leitura numerica '
                f'dos readouts e score de atividade de via).'
            )
