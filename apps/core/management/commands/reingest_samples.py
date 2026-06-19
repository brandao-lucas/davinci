"""
reingest_samples — Re-dispara ingestão de amostras para datasets GEO curados.

Uso:
    python manage.py reingest_samples <project_id> [--force] [--sync]

Contexto:
    Datasets GEO guardam em `accession` o BioProject (PRJNA…), mas o accession
    correto para buscar samples no NCBI acc.cgi é a Série GEO (GSE…), armazenada
    em extra_metadata['gse']. O fix em run_sample_ingestion corrigiu a derivação;
    este command re-dispara os datasets já marcados como `included` que foram
    ingeridos com o accession errado (0 samples).

    A guarda de idempotência em run_sample_ingestion verifica SAMPLE_FETCH ativos
    (pending/running), não a existência de OmicSample — portanto re-disparar
    diretamente é seguro desde que não haja job ativo. Com --force, samples
    não-curados são limpos antes do re-disparo para liberar espaço de idempotência
    de OmicSample (inserção via ON CONFLICT DO UPDATE no Rust — ou seja, amostras
    reais que retornam serão upserted de qualquer forma; limpar só evita órfãos
    de uma corrida anterior com accession errado).

Regras curation-audit-trail:
    - Nunca deleta OmicSample vinculado a ProjectSample com status != 'pending'.
    - Se qualquer sample do dataset já estiver curado (included/excluded/maybe),
      o dataset é pulado e o usuário é avisado.
"""

import logging

from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Re-dispara a ingestão de amostras para todos os datasets com "
        "curation_status='included' de um projeto. "
        "Use --force para limpar samples stale (não-curados) antes do re-disparo. "
        "Use --sync para rodar inline (sem Celery) e facilitar debug."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            'project_id',
            type=str,
            help='UUID do DaVinciProject cujos datasets devem ser re-ingeridos.',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            default=False,
            help=(
                'Limpa OmicSample/ProjectSample stale (curation_status=pending) '
                'do dataset antes de re-ingerir. '
                'Datasets com algum sample já curado (included/excluded/maybe) '
                'NÃO são limpos — são pulados com aviso.'
            ),
        )
        parser.add_argument(
            '--sync',
            action='store_true',
            default=False,
            help=(
                'Executa run_sample_ingestion inline (sem .delay()), '
                'útil para debug local sem Celery rodando.'
            ),
        )

    def handle(self, *args, **options):
        from apps.core.models import DaVinciProject, OmicSample, ProjectDataset, ProjectSample
        from apps.core.tasks.ingestion_tasks import run_sample_ingestion

        project_id = options['project_id']
        force = options['force']
        sync = options['sync']

        # Valida projeto
        try:
            project = DaVinciProject.objects.select_related('user').get(id=project_id)
        except DaVinciProject.DoesNotExist:
            raise CommandError(f"DaVinciProject '{project_id}' não encontrado.")

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"Projeto: {project.title} ({project.id})"
            )
        )
        self.stdout.write(f"Modo: {'sync (inline)' if sync else 'async (Celery .delay())'}")
        self.stdout.write(f"Force: {'sim — samples pending serao limpos' if force else 'nao'}")
        self.stdout.write("")

        # Datasets incluídos no projeto
        project_datasets = (
            ProjectDataset.objects
            .filter(project=project, curation_status=ProjectDataset.CurationStatus.INCLUDED)
            .select_related('dataset')
        )

        if not project_datasets.exists():
            self.stdout.write(self.style.WARNING("Nenhum dataset com status 'included' encontrado."))
            return

        dispatched = 0
        skipped = 0

        for pd in project_datasets:
            dataset = pd.dataset
            source_db = dataset.source_db

            # Deriva o accession que será usado (espelha a lógica de run_sample_ingestion)
            if source_db == 'geo':
                gse_raw = (dataset.extra_metadata or {}).get('gse')
                if not gse_raw:
                    self.stdout.write(
                        self.style.WARNING(
                            f"  [PULADO] {dataset.accession} (geo) — "
                            "sem 'gse' em extra_metadata; run_sample_ingestion marcaria FAILED."
                        )
                    )
                    skipped += 1
                    continue

                gse_str = str(gse_raw).strip()
                derived_accession = gse_str if gse_str.upper().startswith('GSE') else f"GSE{gse_str}"
            else:
                derived_accession = dataset.accession

            self.stdout.write(
                f"  Dataset id={dataset.id}  accession={dataset.accession}"
                f"  source_db={source_db}  accession_efetivo={derived_accession}"
            )

            # --force: limpa samples stale deste dataset para este projeto
            if force:
                curated_count = ProjectSample.objects.filter(
                    project=project,
                    sample__dataset=dataset,
                ).exclude(
                    curation_status=ProjectSample.CurationStatus.PENDING,
                ).count()

                if curated_count > 0:
                    self.stdout.write(
                        self.style.WARNING(
                            f"    [FORCE PULADO] {curated_count} sample(s) ja curado(s) "
                            "neste dataset — limpeza ignorada para preservar auditoria."
                        )
                    )
                else:
                    # Sem samples curados: pode limpar os pending com segurança
                    stale_ps = ProjectSample.objects.filter(
                        project=project,
                        sample__dataset=dataset,
                        curation_status=ProjectSample.CurationStatus.PENDING,
                    )
                    stale_ps_count = stale_ps.count()

                    # Coleta os IDs de OmicSample antes de deletar o vínculo
                    stale_sample_ids = list(stale_ps.values_list('sample_id', flat=True))

                    if stale_ps_count > 0:
                        stale_ps.delete()
                        # Deleta OmicSample que não tem mais nenhum vínculo ProjectSample
                        # (outros projetos podem referenciar o mesmo sample — preserve-os)
                        orphan_samples = OmicSample.objects.filter(
                            id__in=stale_sample_ids,
                        ).exclude(
                            in_projects__isnull=False,
                        )
                        orphan_count = orphan_samples.count()
                        orphan_samples.delete()
                        self.stdout.write(
                            f"    [FORCE] {stale_ps_count} ProjectSample(pending) removidos, "
                            f"{orphan_count} OmicSample orfaos deletados."
                        )
                    else:
                        self.stdout.write(
                            "    [FORCE] Nenhum sample pending para limpar."
                        )

            # Dispara a ingestão
            if sync:
                self.stdout.write("    Executando inline...")
                result = run_sample_ingestion(str(project.id), dataset.id)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"    Concluido: fetched={result.get('samples_fetched', '?')} "
                        f"written={result.get('samples_written', '?')} "
                        f"linked={result.get('project_samples_linked', '?')}"
                    )
                )
            else:
                run_sample_ingestion.delay(str(project.id), dataset.id)
                self.stdout.write(
                    self.style.SUCCESS("    Tarefa Celery despachada.")
                )

            dispatched += 1

        self.stdout.write("")
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"Resumo: {dispatched} dataset(s) despachados, {skipped} pulados."
            )
        )
