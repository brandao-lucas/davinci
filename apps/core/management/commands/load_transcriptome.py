"""
Management command — load_transcriptome

Carrega a matriz de transcriptoma CPTAC CCRCC (RNA-seq FPKM log2, gene ×
amostra) como OmicMatrix(transcriptomic/gene/fpkm_log2).

OmnisPathway Objetivo 2 — camada ômica faltante (mRNA). Insumo da Regra 2
do motor PFS (footprint de fator de transcrição) e da análise comparativa
mRNA×proteína por gene.

Fonte (acesso público, sem credencial):
  LinkedOmics CPTAC-CCRCC (tumor + normal)
  https://www.linkedomics.org/data_download/CPTAC-CCRCC/
  HS_CPTAC_CCRCC_RNAseq_fpkm_log2_Tumor.cct
  HS_CPTAC_CCRCC_RNAseq_fpkm_log2_Normal.cct

Schema resultante:
  OmicMatrix.omics_layer='transcriptomic'
  OmicMatrix.feature_axis='gene'
  OmicMatrix.data_format_level='fpkm_log2'
  OmicDataset.access_type='public' → Parquet no namespace _shared
  OmicSample: get_or_create (reutiliza amostras CCRCC já carregadas pelas
  demais camadas — proteoma, fosfoproteoma, CNV, MAF somático)

Reúso do loader Rust:
  Chama rust_engine.load_cptac_matrix (o mesmo usado pelo proteoma da
  Fase 0) — totalmente parametrizado por URL, sem qualquer mudança em
  rust_src/.

Analogia com load_phospho_matrix / load_cnv_matrix:
  - --project obrigatório (isolamento por usuário / Regra #3).
  - --async enfileira via Celery; padrão síncrono para bancada de prova.
  - Reporta n_features, n_samples, storage_key (sem vazar caminho físico
    nem credencial — não há credencial neste fluxo).

Idempotência:
    Rodar duas vezes com o mesmo --project não duplica OmicMatrix nem
    IngestionJob. O segundo run levanta TranscriptomeMatrixAlreadyLoadedError
    ou TranscriptomeMatrixJobActiveError e o command reporta claramente o
    estado.

Próximos passos após a carga (NÃO executados por este command):
    1. pair_samples nesta matriz — a natural key de OmicSamplePairing é
       (matrix, case_id); pareamento é por matriz.
    2. backfill_matrix_features nesta matriz — pré-condição para qualquer
       mapeamento de readout contra ela.

Sensitive-data-handling:
    Nenhuma credencial necessária (dados LinkedOmics públicos). db_url nunca
    logada. storage_key reportada é chave lógica, não caminho físico absoluto.
"""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        'Carrega a matriz de transcriptoma CPTAC CCRCC '
        '(transcriptomic/gene/fpkm_log2) como OmicMatrix. '
        'OmnisPathway Obj 2 — camada mRNA.'
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
        from apps.core.services.transcriptome_load_service import (
            LOADER_VERSION,
            TRANSCRIPTOME_CCRCC_ACCESSION,
            TRANSCRIPTOME_NORMAL_URL,
            TRANSCRIPTOME_TUMOR_URL,
            TranscriptomeLoadService,
            TranscriptomeMatrixAlreadyLoadedError,
            TranscriptomeMatrixJobActiveError,
        )

        project_uuid = options['project']
        use_async = options['use_async']

        # ── Resolve projeto ───────────────────────────────────────────────────
        try:
            project = DaVinciProject.objects.select_related('user').get(pk=project_uuid)
        except DaVinciProject.DoesNotExist:
            raise CommandError(f'Projeto não encontrado: {project_uuid}')

        self.stdout.write(f'Projeto: {project.title} (id={project.id})')
        self.stdout.write(f'Estudo alvo: {TRANSCRIPTOME_CCRCC_ACCESSION}')
        self.stdout.write(f'loader_version: {LOADER_VERSION}')
        self.stdout.write(f'Fonte tumor: {TRANSCRIPTOME_TUMOR_URL}')
        self.stdout.write(f'Fonte normal: {TRANSCRIPTOME_NORMAL_URL}')
        self.stdout.write(
            'Schema: omics_layer=transcriptomic | feature_axis=gene | '
            'data_format_level=fpkm_log2 | access_type=public (namespace _shared)'
        )
        self.stdout.write(
            'Nota: OmicSample reutiliza amostras CCRCC já carregadas pelas '
            'demais camadas (get_or_create).'
        )

        if use_async:
            # ── Modo assíncrono: apenas enfileira ─────────────────────────────
            self.stdout.write('Modo: assíncrono (Celery)')
            try:
                job = TranscriptomeLoadService.dispatch(project)
                self.stdout.write(self.style.SUCCESS(
                    f'Job enfileirado: {job.id} (status={job.status})\n'
                    f'Acompanhe o progresso via IngestionJob.id={job.id}'
                ))
            except TranscriptomeMatrixAlreadyLoadedError as exc:
                self.stdout.write(self.style.WARNING(
                    f'Idempotência: OmicMatrix de transcriptoma já existe — {exc}'
                ))
            except TranscriptomeMatrixJobActiveError as exc:
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
            service = TranscriptomeLoadService(project)
            result = service.run()
        except TranscriptomeMatrixAlreadyLoadedError as exc:
            self.stdout.write(self.style.WARNING(
                f'Idempotência: OmicMatrix de transcriptoma já existe para esta '
                f'natural key.\n'
                f'  {exc}\n'
                f'  Nenhuma ação necessária.'
            ))
            return
        except TranscriptomeMatrixJobActiveError as exc:
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
            raise CommandError(f'Carga do transcriptoma falhou: {exc}') from exc

        # ── Relatório final (sem vazar caminho físico nem credencial) ─────────
        self.stdout.write(self.style.SUCCESS(
            'Carga do transcriptoma CPTAC CCRCC concluída com sucesso.'
        ))
        self.stdout.write(f'  job_id:          {result["job_id"]}')
        self.stdout.write(f'  storage_key:     {result["storage_key"]}')
        self.stdout.write(f'  n_features:      {result["n_features"]}')
        self.stdout.write(f'  n_samples:       {result["n_samples"]}')
        self.stdout.write(f'  checksum_md5:    {result["checksum_md5"]}')
        self.stdout.write(f'  genes_discarded: {result["genes_discarded"]}')
        self.stdout.write(f'  roles:           {result["roles"]}')
        self.stdout.write(
            f'\nOmicMatrix (transcriptomic/gene/fpkm_log2) e OmicMatrixSample '
            f'populados no banco.\n'
            f'Parquet disponível via default_storage[{result["storage_key"]}].\n'
            f'\nPróximos passos (NÃO executados por este command):\n'
            f'  1. pair_samples nesta matriz (NK de OmicSamplePairing é '
            f'(matrix, case_id) — pareamento é por matriz).\n'
            f'  2. backfill_matrix_features nesta matriz (pré-condição para '
            f'mapeamento de readout contra o transcriptoma).'
        )
