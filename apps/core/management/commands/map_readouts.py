"""
Management command — map_readouts

Executa o mapeamento readout→feature (Fase 3, Passo 3.4):
  Regra 1 (fosfo): PathwayNode(node_type='gene') das vias do universo desta
                   execução × matriz de fosfoproteoma (phospho_site).
  Regra 2 (TF):   TFs do regulon CollecTRI, dentre as vias do universo desta
                   execução, × matriz proteoma (gene).

Materializa PathwayReadoutFeature (bulk_create ignore_conflicts) e atualiza
PathwayNode.readout_role para 'phospho' / 'tf_target'.

OmnisPathway Objetivo 2, Fase 3, Passo 3.4.

Universo de vias (generalizado — deixou de ser 3 fixas):
  Sem `--pathway`, o comando processa TODAS as `Pathway` carregadas em PG
  (resolvido em tempo de execução por `ReadoutMappingService`, não fixado
  numa constante). Com `--pathway`, restringe a uma lista explícita — a
  MESMA lista vale para as DUAS regras (fosfo e TF); não há como restringir
  cada regra a um universo diferente via este comando, de propósito: vias
  pontuadas com composições de readout diferentes (ex.: X com fosfo+TF, Y só
  com TF) produzem z-scores não comparáveis entre si na Fase 4 — ver
  docstring de `ReadoutMappingService` (apps/core/services/
  readout_mapping_service.py) para a justificativa completa. Se algum dia for
  necessário universos distintos por regra, isso é uma chamada direta ao
  service (não exposta aqui).

Pré-condições (em ordem de execução):
  1. load_phospho_matrix (passo 3.1) — matriz de fosfoproteoma em PG.
  2. load_cptac_matrix (Fase 0) — matriz proteoma gene-level em PG.
  3. load_pathway_topology (passo 3.2) — PathwayNode das vias em PG.
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

Escala (universo expandido para as vias humanas completas do KEGG — 372
vias, ~5.520 símbolos-gene no grafo): o número de PathwayReadoutFeature
materializados sobe de milhares (v1, 3 vias) para a ordem de centenas de
milhares. `ReadoutMappingService` já é set-based (sem query por via em laço)
e `bulk_create` já é feito em lotes — ver docstring de
`ReadoutMappingService._persist`. Rode com `--dry-run` primeiro para
conferir os contadores (incluindo a distribuição por via) antes de
persistir.

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
        '(Regra 1 fosfo + Regra 2 TF), para todas as vias carregadas por '
        'padrão. OmnisPathway Obj 2, Fase 3, Passo 3.4.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--pathway',
            metavar='KEGG_ID',
            nargs='+',
            default=None,
            help=(
                'Restringe a uma ou mais vias KEGG. Aceita múltiplos '
                'argumentos (--pathway hsa04151 hsa04010), lista separada '
                'por vírgula (--pathway hsa04151,hsa04010) ou uma mistura '
                'dos dois. Vale para as DUAS regras (fosfo e TF) — mesmo '
                'universo, ver docstring do módulo. Padrão (sem esta flag): '
                'TODAS as vias carregadas em Pathway (resolvido em tempo de '
                'execução, reflete o catálogo no momento do run).'
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
        from apps.core.models import Pathway
        from apps.core.services.readout_mapping_service import (
            DEFAULT_MAPPING_VERSION,
            ReadoutMappingError,
            ReadoutMappingService,
        )

        pathway_tokens = options['pathway']
        mapping_version = options['mapping_version'] or DEFAULT_MAPPING_VERSION
        dry_run = options['dry_run']

        # ── Resolve --pathway: aceita múltiplos args e/ou CSV, dedup ────────
        pathway_ids = None
        if pathway_tokens:
            raw_ids = []
            for token in pathway_tokens:
                raw_ids.extend(p.strip() for p in token.split(',') if p.strip())
            seen = set()
            pathway_ids = []
            for p in raw_ids:
                if p not in seen:
                    seen.add(p)
                    pathway_ids.append(p)

            existing = set(
                Pathway.objects.filter(kegg_id__in=pathway_ids).values_list(
                    'kegg_id', flat=True
                )
            )
            if not existing:
                raise CommandError(
                    f'Nenhuma das vias informadas existe em Pathway: '
                    f'{pathway_ids}. Execute load_pathway_topology para '
                    f'carrega-las antes.'
                )
            missing = [p for p in pathway_ids if p not in existing]
            if missing:
                self.stdout.write(self.style.WARNING(
                    f'Aviso: vias nao encontradas em Pathway (nenhum '
                    f'PathwayNode sera considerado para elas): {missing}'
                ))

        self.stdout.write(
            f'Mapeamento readout->feature (versao={mapping_version})'
        )
        if pathway_ids is None:
            self.stdout.write(
                '  Vias: TODAS as carregadas em Pathway (resolvido em '
                'tempo de execucao) — mesmo universo p/ Regra 1 e Regra 2.'
            )
        else:
            self.stdout.write(
                f'  Vias (restrito via --pathway): {pathway_ids} — mesmo '
                f'universo p/ Regra 1 e Regra 2.'
            )
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
                pathway_ids_phospho=pathway_ids,
                pathway_ids_tf=pathway_ids,
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
        self.stdout.write('Distribuicao por via:')
        self.stdout.write(f'  n_pathways_total:          {report["n_pathways_total"]}')
        self.stdout.write(f'  n_pathways_with_phospho:   {report["n_pathways_with_phospho"]}')
        self.stdout.write(f'  n_pathways_with_tf:        {report["n_pathways_with_tf"]}')
        self.stdout.write(f'  n_pathways_without_readout:{report["n_pathways_without_readout"]}'
                           ' (escore degenerado na Fase 4)')
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
