"""
Management command — load_phospho_matrix

Carrega a matriz de fosfoproteoma CPTAC CCRCC (gene-level phospho × amostra,
intensidades MS) como OmicMatrix(proteomic/phospho_site/intensities).

OmnisPathway Objetivo 2, Fase 3, Passo 3.1.

Fonte (acesso público, sem credencial):
  LinkedOmics CPTAC-CCRCC (tumor + normal)
  https://www.linkedomics.org/data_download/CPTAC-CCRCC/
  HS_CPTAC_CCRCC_phosphoproteome_gene_Tumor.cct
  HS_CPTAC_CCRCC_phosphoproteome_gene_Normal.cct

Schema resultante:
  OmicMatrix.omics_layer='proteomic'
  OmicMatrix.feature_axis='phospho_site'     (distingue da proteoma gene-level)
  OmicMatrix.data_format_level='intensities'
  OmicDataset.access_type='public' → Parquet no namespace _shared
  OmicSample: get_or_create (reutiliza amostras CCRCC já carregadas pela Fase 0)

Analogia com load_cnv_matrix:
  - --project obrigatório (isolamento por usuário / Regra #3).
  - --async enfileira via Celery; padrão síncrono para bancada de prova.
  - Reporta n_features, n_samples, storage_key (sem vazar caminho físico
    nem credencial — não há credencial neste fluxo).

Idempotência:
    Rodar duas vezes com o mesmo --project não duplica OmicMatrix nem
    IngestionJob. O segundo run levanta PhosphoMatrixAlreadyLoadedError ou
    PhosphoMatrixJobActiveError e o command reporta claramente o estado.

Pré-requisito:
    Este command é pré-condição do passo 3.4 (map_readouts Regra 1).
    A matriz de fosfoproteoma deve existir antes de rodar o mapeamento
    readout→feature de fosforilação.

Sensitive-data-handling:
    Nenhuma credencial necessária (dados LinkedOmics públicos). db_url nunca
    logada. storage_key reportada é chave lógica, não caminho físico absoluto.
"""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        'Carrega a matriz de fosfoproteoma CPTAC CCRCC '
        '(proteomic/phospho_site/intensities) como OmicMatrix. '
        'OmnisPathway Obj 2, Fase 3, Passo 3.1.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--project',
            metavar='UUID',
            required=True,
            help='UUID do DaVinciProject alvo (obrigatório — isolamento por projeto).',
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
        from apps.core.services.phospho_matrix_load_service import (
            LOADER_VERSION,
            PHOSPHO_CCRCC_ACCESSION,
            PHOSPHO_NORMAL_URL,
            PHOSPHO_TUMOR_URL,
            PhosphoMatrixAlreadyLoadedError,
            PhosphoMatrixJobActiveError,
            PhosphoMatrixLoadService,
        )

        project_uuid = options['project']
        use_async = options['use_async']

        # ── Resolve projeto ───────────────────────────────────────────────────
        try:
            project = DaVinciProject.objects.select_related('user').get(pk=project_uuid)
        except DaVinciProject.DoesNotExist:
            raise CommandError(f'Projeto não encontrado: {project_uuid}')

        self.stdout.write(f'Projeto: {project.title} (id={project.id})')
        self.stdout.write(f'Estudo alvo: {PHOSPHO_CCRCC_ACCESSION}')
        self.stdout.write(f'loader_version: {LOADER_VERSION}')
        self.stdout.write(f'Fonte tumor: {PHOSPHO_TUMOR_URL}')
        self.stdout.write(f'Fonte normal: {PHOSPHO_NORMAL_URL}')
        self.stdout.write(
            'Schema: omics_layer=proteomic | feature_axis=phospho_site | '
            'data_format_level=intensities | access_type=public (namespace _shared)'
        )
        self.stdout.write(
            'Nota: feature = símbolo de gene UPPERCASE (fosfoproteoma gene-level). '
            'OmicSample: reutiliza amostras CCRCC já carregadas (get_or_create).'
        )

        if use_async:
            # ── Modo assíncrono: apenas enfileira ─────────────────────────────
            self.stdout.write('Modo: assíncrono (Celery)')
            try:
                job = PhosphoMatrixLoadService.dispatch(project)
                self.stdout.write(self.style.SUCCESS(
                    f'Job enfileirado: {job.id} (status={job.status})\n'
                    f'Acompanhe o progresso via IngestionJob.id={job.id}'
                ))
            except PhosphoMatrixAlreadyLoadedError as exc:
                self.stdout.write(self.style.WARNING(
                    f'Idempotência: OmicMatrix de fosfo já existe — {exc}'
                ))
            except PhosphoMatrixJobActiveError as exc:
                self.stdout.write(self.style.WARNING(
                    f'Idempotência: job ativo encontrado — {exc}'
                ))
            return

        # ── Modo síncrono: executa end-to-end ─────────────────────────────────
        self.stdout.write('Modo: síncrono (aguarde — download pode demorar)')
        self.stdout.write(
            'Atenção: rust_engine deve estar compilado '
            '(`maturin develop --release`)'
        )

        try:
            service = PhosphoMatrixLoadService(project)
            result = service.run()
        except PhosphoMatrixAlreadyLoadedError as exc:
            self.stdout.write(self.style.WARNING(
                f'Idempotência: OmicMatrix de fosfo já existe para esta natural key.\n'
                f'  {exc}\n'
                f'  Nenhuma ação necessária.'
            ))
            return
        except PhosphoMatrixJobActiveError as exc:
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
            raise CommandError(f'Carga do fosfoproteoma falhou: {exc}') from exc

        # ── Relatório final (sem vazar caminho físico nem credencial) ─────────
        self.stdout.write(self.style.SUCCESS(
            'Carga do fosfoproteoma CPTAC CCRCC concluída com sucesso.'
        ))
        self.stdout.write(f'  job_id:          {result["job_id"]}')
        self.stdout.write(f'  storage_key:     {result["storage_key"]}')
        self.stdout.write(f'  n_features:      {result["n_features"]}')
        self.stdout.write(f'  n_samples:       {result["n_samples"]}')
        self.stdout.write(f'  checksum_md5:    {result["checksum_md5"]}')
        self.stdout.write(f'  genes_discarded: {result["genes_discarded"]}')
        self.stdout.write(f'  roles:           {result["roles"]}')
        self.stdout.write(
            f'\nOmicMatrix (proteomic/phospho_site/intensities) e OmicMatrixSample '
            f'populados no banco.\n'
            f'Parquet disponível via default_storage[{result["storage_key"]}].\n'
            f'\nPróximo passo: carregar topologia KEGG (load_pathway_topology) e '
            f'regulons (load_regulons), depois mapear readouts (map_readouts Regra 1).'
        )
