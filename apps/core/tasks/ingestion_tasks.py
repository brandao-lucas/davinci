import logging
import os
import tempfile

from celery import shared_task
from django.conf import settings
from django.core.files import File
from django.core.files.storage import default_storage
from django.utils import timezone
from apps.core.models import DatasetFile, IngestionJob, OmicDataset, OmicSample, ProjectDataset, ProjectSample
from apps.core.storage_utils import omics_storage_key

logger = logging.getLogger(__name__)


def _dispatch_omics_after_pubmed(job: IngestionJob) -> None:
    """
    Dispara ingestão GEO_SEARCH para o projeto do job PubMed concluído.

    Guarda de idempotência: só cria o job omics se não existe nenhum
    GEO_SEARCH em status pending ou running para o mesmo projeto.
    Isso evita duplo disparo em caso de retry da task de papers.

    Importações locais (evitam importação circular: tasks → service → tasks).
    """
    from apps.core.services.search_service import SearchService

    project = job.project

    already_active = IngestionJob.objects.filter(
        project=project,
        job_type=IngestionJob.JobType.GEO_SEARCH,
        status__in=[IngestionJob.JobStatus.PENDING, IngestionJob.JobStatus.RUNNING],
    ).exists()

    if already_active:
        logger.info(
            'PubMed job %s concluído; GEO_SEARCH já ativo para projeto %s — disparo omics ignorado (idempotência)',
            job.id,
            project.id,
        )
        return

    user = project.user
    logger.info(
        'PubMed job %s concluído; disparando GEO_SEARCH automático para projeto %s',
        job.id,
        project.id,
    )
    SearchService.dispatch_omics_search(project, user=user)


@shared_task(bind=True, max_retries=3)
def run_pubmed_ingestion(self, job_id: str):
    """
    Chama o Rust engine via PyO3.
    Ao concluir com sucesso, dispara automaticamente a ingestão GEO_SEARCH
    para o mesmo projeto (Op 1.1 — encadeamento automático).
    """
    try:
        import rust_engine

        try:
            job = IngestionJob.objects.select_related('project__user').get(id=job_id)
        except IngestionJob.DoesNotExist:
            logger.warning('IngestionJob %s not found — task aborted', job_id)
            return {'processed': 0, 'inserted': 0}

        # Build DB URL from settings
        db = settings.DATABASES['default']
        db_url = f"postgresql://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}"

        result = rust_engine.search_and_ingest_pubmed(
            job_id=str(job.id),
            query=job.parameters['query'],
            project_id=str(job.project_id),
            date_from=job.parameters.get('date_from'),
            date_to=job.parameters.get('date_to'),
            db_url=db_url,
            ncbi_api_key=job.parameters.get('ncbi_api_key') or getattr(settings, 'NCBI_API_KEY', None),
        )

        # Defense in depth: se o Rust não marcou o job, a task garante o estado final.
        # O filter em status__in garante idempotência com o Rust real.
        IngestionJob.objects.filter(
            id=job_id,
            status__in=[IngestionJob.JobStatus.PENDING, IngestionJob.JobStatus.RUNNING],
        ).update(
            status=IngestionJob.JobStatus.COMPLETED,
            records_processed=result.records_processed,
            records_inserted=result.records_inserted,
        )

        # Resolve any pending dataset-paper links from prior omics runs
        try:
            resolved = rust_engine.resolve_pending_links(db_url)
            if resolved > 0:
                logger.info('Resolved %d pending dataset-paper links after PubMed ingestion', resolved)
        except Exception as e:
            logger.warning('resolve_pending_links warning: %s', e)

        # Materializa vínculos project-scoped (ProjectPaperDataset, Nível 1).
        # Idempotente via ON CONFLICT DO NOTHING. Falha não derruba o job de papers.
        try:
            from apps.core.services.link_service import materialize_project_links
            inserted = materialize_project_links(job.project_id)
            if inserted > 0:
                logger.info(
                    'PubMed job %s: %d vínculos ProjectPaperDataset materializados para projeto %s',
                    job_id, inserted, job.project_id,
                )
        except Exception as e:
            logger.error(
                'materialize_project_links falhou após PubMed job %s (projeto %s): %s',
                job_id, job.project_id, e,
            )

        # Encadeamento automático: dispara GEO_SEARCH após PubMed concluído (Op 1.1).
        # Protegido por guarda de idempotência em _dispatch_omics_after_pubmed.
        try:
            # Recarrega para garantir estado atualizado antes de checar o status.
            job.refresh_from_db()
            if job.status == IngestionJob.JobStatus.COMPLETED:
                _dispatch_omics_after_pubmed(job)
        except Exception as e:
            # Falha no encadeamento não deve derrubar o job de papers já concluído.
            logger.error(
                'Falha ao disparar GEO_SEARCH automático após PubMed job %s: %s',
                job_id, e,
            )

        # Tenta avançar para curating se não há mais jobs de busca ativos.
        # Chamado APÓS o encadeamento para que o GEO job já exista antes de checar.
        try:
            from apps.core.services.project_status import advance_to_curating_if_done
            advance_to_curating_if_done(job.project)
        except Exception as e:
            logger.error(
                'advance_to_curating_if_done falhou após PubMed job %s (projeto %s): %s',
                job_id, job.project_id, e,
            )

        return {
            'processed': result.records_processed,
            'inserted': result.records_inserted,
        }
    except ImportError:
        # rust_engine não compilado: marca FAILED com mensagem clara.
        try:
            job = IngestionJob.objects.get(id=job_id)
            job.status = IngestionJob.JobStatus.FAILED
            job.error_message = (
                'rust_engine not installed — compile with '
                '`maturin develop --release`'
            )
            job.save(update_fields=['status', 'error_message'])
        except IngestionJob.DoesNotExist:
            pass
        return {'processed': 0, 'inserted': 0}
    except Exception as exc:
        try:
            job = IngestionJob.objects.get(id=job_id)
            job.status = IngestionJob.JobStatus.FAILED
            job.error_message = str(exc)
            job.save(update_fields=['status', 'error_message'])
        except IngestionJob.DoesNotExist:
            logger.warning('IngestionJob %s not found — task aborted', job_id)

        raise self.retry(exc=exc, countdown=60)


@shared_task(bind=True, max_retries=3)
def run_omics_ingestion(self, job_id: str):
    """
    Calls the Rust engine to ingest omics metadata from GEO, SRA, BioProject,
    and/or GWAS Catalog via PyO3.

    Job parameters expected:
        query         (str)        — search term
        sources       (list[str])  — subset of ["geo", "sra", "bioproject", "gwas"]
        max_per_source (int)       — max datasets per source (default: 500)
    """
    try:
        import rust_engine

        try:
            job = IngestionJob.objects.get(id=job_id)
        except IngestionJob.DoesNotExist:
            logger.warning('IngestionJob %s not found — task aborted', job_id)
            return {'datasets_processed': 0, 'datasets_inserted': 0, 'links_inserted': 0, 'errors': []}

        db = settings.DATABASES['default']
        db_url = f"postgresql://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}"

        sources = job.parameters.get('sources', ['geo', 'sra', 'bioproject', 'gwas'])
        max_per_source = job.parameters.get('max_per_source', 500)

        result = rust_engine.search_and_ingest_omics(
            job_id=str(job.id),
            query=job.parameters['query'],
            db_url=db_url,
            project_id=str(job.project_id),
            sources=sources,
            max_per_source=max_per_source,
            ncbi_api_key=job.parameters.get('ncbi_api_key') or getattr(settings, 'NCBI_API_KEY', None),
            synonyms=job.parameters.get('synonyms') or [],
        )

        # Defense in depth: se o Rust não marcou o job, a task garante o estado final.
        # O filter em status__in garante idempotência com o Rust real.
        IngestionJob.objects.filter(
            id=job_id,
            status__in=[IngestionJob.JobStatus.PENDING, IngestionJob.JobStatus.RUNNING],
        ).update(
            status=IngestionJob.JobStatus.COMPLETED,
            records_processed=result.datasets_processed,
            records_inserted=result.datasets_inserted,
        )

        # Surface any non-fatal errors into the job record
        if result.errors:
            try:
                job = IngestionJob.objects.get(id=job_id)
                job.records_processed = result.datasets_processed
                job.error_message = '; '.join(result.errors)
                job.save(update_fields=['records_processed', 'error_message'])
            except IngestionJob.DoesNotExist:
                pass

        # Materializa vínculos project-scoped (ProjectPaperDataset, Nível 1).
        # Executado após resolve_pending_links do Rust (já ocorreu dentro de search_and_ingest_omics).
        # Idempotente via ON CONFLICT DO NOTHING. Falha não derruba o job de ômicas.
        try:
            from apps.core.services.link_service import materialize_project_links
            # Recarrega o job para obter project_id caso não esteja hydratado.
            _project_id = job.project_id
            inserted = materialize_project_links(_project_id)
            if inserted > 0:
                logger.info(
                    'Omics job %s: %d vínculos ProjectPaperDataset materializados para projeto %s',
                    job_id, inserted, _project_id,
                )
        except Exception as e:
            logger.error(
                'materialize_project_links falhou após omics job %s (projeto %s): %s',
                job_id, job.project_id if 'job' in dir() else '?', e,
            )

        # Tenta avançar para curating se não há mais jobs de busca ativos.
        try:
            from apps.core.services.project_status import advance_to_curating_if_done
            advance_to_curating_if_done(job.project)
        except Exception as e:
            logger.error(
                'advance_to_curating_if_done falhou após omics job %s (projeto %s): %s',
                job_id, job.project_id, e,
            )

        # Recompute pós-ingestão: reconstrói disease_axis + monogenic_gene_hit para
        # os datasets tocados por este job (fecha gap R4 — COPY writer apaga o campo).
        # Assíncrono e tolerante a falha: se a task de classificação falhar, o job
        # de ingestão já está COMPLETED e o erro fica apenas no log.
        try:
            from apps.core.tasks.disease_axis_tasks import recompute_disease_axis_for_job
            recompute_disease_axis_for_job.delay(str(job_id))
            logger.info(
                'recompute_disease_axis_for_job enfileirado para omics job %s', job_id
            )
        except Exception as e:
            logger.error(
                'Falha ao enfileirar recompute_disease_axis_for_job após omics job %s: %s',
                job_id, e,
            )

        return {
            'datasets_processed': result.datasets_processed,
            'datasets_inserted': result.datasets_inserted,
            'links_inserted': result.links_inserted,
            'errors': result.errors,
        }
    except ImportError:
        # rust_engine não compilado: marca FAILED com mensagem clara.
        try:
            job = IngestionJob.objects.get(id=job_id)
            job.status = IngestionJob.JobStatus.FAILED
            job.error_message = (
                'rust_engine not installed — compile with '
                '`maturin develop --release`'
            )
            job.save(update_fields=['status', 'error_message'])
        except IngestionJob.DoesNotExist:
            pass
        return {'datasets_processed': 0, 'datasets_inserted': 0, 'links_inserted': 0, 'errors': []}
    except Exception as exc:
        try:
            job = IngestionJob.objects.get(id=job_id)
            job.status = IngestionJob.JobStatus.FAILED
            job.error_message = str(exc)
            job.save(update_fields=['status', 'error_message'])
        except IngestionJob.DoesNotExist:
            logger.warning('IngestionJob %s not found — task aborted', job_id)
        raise self.retry(exc=exc, countdown=60)


@shared_task(bind=True, max_retries=3)
def run_sample_ingestion(self, project_id: str, dataset_id: int):
    """
    Ingestão de amostras (OmicSample) sob demanda para um dataset já curado.

    Fluxo:
      1. Cria/usa IngestionJob SAMPLE_FETCH com guarda de idempotência.
      2. Chama rust_engine.ingest_samples_for_dataset — popula core_omicsample.
      3. Cria vínculos ProjectSample(project, sample, status='pending') para
         todos os samples do dataset ainda não vinculados ao projeto.
         Usa bulk_create(..., ignore_conflicts=True) respeitando
         unique_together=(project, sample).

    Regra #1: a task apenas orquestra — não faz HTTP nem parse.
    """
    from apps.core.models import DaVinciProject, OmicDataset

    try:
        import rust_engine

        try:
            project = DaVinciProject.objects.select_related('user').get(id=project_id)
        except DaVinciProject.DoesNotExist:
            logger.warning('DaVinciProject %s not found — sample ingestion aborted', project_id)
            return {'samples_fetched': 0, 'samples_written': 0, 'errors': []}

        try:
            dataset = OmicDataset.objects.get(id=dataset_id)
        except OmicDataset.DoesNotExist:
            logger.warning('OmicDataset %s not found — sample ingestion aborted', dataset_id)
            return {'samples_fetched': 0, 'samples_written': 0, 'errors': []}

        # Idempotência: não duplicar job se já há um SAMPLE_FETCH ativo para este dataset+projeto.
        already_active = IngestionJob.objects.filter(
            project=project,
            job_type=IngestionJob.JobType.SAMPLE_FETCH,
            status__in=[IngestionJob.JobStatus.PENDING, IngestionJob.JobStatus.RUNNING],
            parameters__dataset_id=dataset_id,
        ).exists()
        if already_active:
            logger.info(
                'SAMPLE_FETCH já ativo para projeto %s / dataset %s — disparo ignorado (idempotência)',
                project_id,
                dataset_id,
            )
            return {'samples_fetched': 0, 'samples_written': 0, 'errors': []}

        db = settings.DATABASES['default']
        db_url = f"postgresql://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}"

        # Obtém ncbi_api_key pelo mesmo padrão de run_omics_ingestion
        user = project.user
        ncbi_api_key = getattr(settings, 'NCBI_API_KEY', None)
        try:
            ncbi_api_key = user.profile.ncbi_api_key or ncbi_api_key
        except Exception:
            pass

        # Deriva o accession correto para cada fonte.
        # GEO: o campo `accession` guarda o BioProject (PRJNA…). O accession real
        # para buscar samples no acc.cgi é a Série GEO (GSE…), armazenada em
        # extra_metadata['gse'] apenas como número (ex: '249027' → 'GSE249027').
        # Se extra_metadata não tiver 'gse', o job é abortado com erro claro.
        if dataset.source_db == 'geo':
            gse_raw = (dataset.extra_metadata or {}).get('gse')
            if not gse_raw:
                error_msg = (
                    f"GEO dataset {dataset.accession} sem GSE em extra_metadata — "
                    "não é possível buscar samples sem o accession GSE*"
                )
                logger.error(
                    'run_sample_ingestion abortado para dataset %s: %s',
                    dataset_id,
                    error_msg,
                )
                IngestionJob.objects.create(
                    project=project,
                    job_type=IngestionJob.JobType.SAMPLE_FETCH,
                    status=IngestionJob.JobStatus.FAILED,
                    parameters={
                        'dataset_id': dataset_id,
                        'dataset_accession': dataset.accession,
                        'source_db': dataset.source_db,
                    },
                    error_message=error_msg,
                )
                return {'samples_fetched': 0, 'samples_written': 0, 'errors': [error_msg]}

            gse_str = str(gse_raw).strip()
            # Normaliza: se o valor já vier prefixado (ex: 'GSE249027'), usa como está;
            # se for apenas o número ('249027'), adiciona o prefixo.
            if gse_str.upper().startswith('GSE'):
                dataset_accession = gse_str
            else:
                dataset_accession = f"GSE{gse_str}"
        else:
            # SRA: accession (SRP…) está correto.
            # BioProject/GWAS: mantém o accession original.
            dataset_accession = dataset.accession

        job = IngestionJob.objects.create(
            project=project,
            job_type=IngestionJob.JobType.SAMPLE_FETCH,
            status=IngestionJob.JobStatus.RUNNING,
            parameters={
                'dataset_id': dataset_id,
                'dataset_accession': dataset_accession,
                'source_db': dataset.source_db,
            },
        )

        result = rust_engine.ingest_samples_for_dataset(
            dataset_id=dataset.id,
            dataset_accession=dataset_accession,
            source_db=dataset.source_db,
            db_url=db_url,
            ncbi_api_key=ncbi_api_key,
        )

        # Atualiza o job com os resultados
        error_msg = '; '.join(result.errors) if result.errors else ''
        final_status = (
            IngestionJob.JobStatus.FAILED
            if error_msg and result.samples_written == 0
            else IngestionJob.JobStatus.COMPLETED
        )
        IngestionJob.objects.filter(id=job.id).update(
            status=final_status,
            records_processed=result.samples_fetched,
            records_inserted=result.samples_written,
            error_message=error_msg,
        )

        # Cria vínculos ProjectSample para os samples ingeridos que ainda não estão no projeto.
        # O Rust já populou core_omicsample; agora vincula ao projeto com status 'pending'.
        new_samples = OmicSample.objects.filter(dataset=dataset)
        existing_sample_ids = set(
            ProjectSample.objects.filter(project=project, sample__dataset=dataset)
            .values_list('sample_id', flat=True)
        )
        to_create = [
            ProjectSample(
                project=project,
                sample=s,
                curation_status=ProjectSample.CurationStatus.PENDING,
            )
            for s in new_samples
            if s.id not in existing_sample_ids
        ]
        if to_create:
            ProjectSample.objects.bulk_create(to_create, ignore_conflicts=True)
            logger.info(
                'Criados %d vínculos ProjectSample para projeto %s / dataset %s',
                len(to_create),
                project_id,
                dataset.accession,
            )

        return {
            'samples_fetched': result.samples_fetched,
            'samples_written': result.samples_written,
            'project_samples_linked': len(to_create),
            'errors': result.errors,
        }

    except ImportError:
        logger.error(
            'rust_engine não instalado — compile com `maturin develop --release`'
        )
        return {'samples_fetched': 0, 'samples_written': 0, 'errors': ['rust_engine not installed']}
    except Exception as exc:
        logger.error(
            'run_sample_ingestion falhou para projeto %s / dataset %s: %s',
            project_id,
            dataset_id,
            exc,
        )
        raise self.retry(exc=exc, countdown=60)


@shared_task(bind=True, max_retries=3)
def run_pride_ingestion(self, job_id: str):
    """
    Chama o Rust engine para ingerir datasets de proteômica do PRIDE/EBI via PyO3.

    Job parameters esperados:
        query      (str) — termo de busca PRIDE
        max_results (int) — limite de datasets (default: 500)

    Nota: PRIDE usa a API REST do EBI — não requer NCBI API key.
    """
    try:
        import rust_engine

        try:
            job = IngestionJob.objects.select_related('project').get(id=job_id)
        except IngestionJob.DoesNotExist:
            logger.warning('IngestionJob %s not found — task aborted', job_id)
            return {'datasets_processed': 0, 'datasets_inserted': 0, 'links_inserted': 0, 'errors': []}

        db = settings.DATABASES['default']
        db_url = f"postgresql://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}"

        max_results = job.parameters.get('max_results', 500)

        result = rust_engine.search_and_ingest_pride(
            job_id=str(job.id),
            query=job.parameters['query'],
            db_url=db_url,
            project_id=str(job.project_id),
            max_results=max_results,
        )

        # Defense in depth: se o Rust não marcou o job, a task garante o estado final.
        # O filter em status__in garante idempotência com o Rust real.
        IngestionJob.objects.filter(
            id=job_id,
            status__in=[IngestionJob.JobStatus.PENDING, IngestionJob.JobStatus.RUNNING],
        ).update(
            status=IngestionJob.JobStatus.COMPLETED,
            records_processed=result.datasets_processed,
            records_inserted=result.datasets_inserted,
        )

        # Surface any non-fatal errors into the job record
        if result.errors:
            try:
                job = IngestionJob.objects.get(id=job_id)
                job.records_processed = result.datasets_processed
                job.error_message = '; '.join(result.errors)
                job.save(update_fields=['records_processed', 'error_message'])
            except IngestionJob.DoesNotExist:
                pass

        # Materializa vínculos project-scoped (ProjectPaperDataset, Nível 1).
        # Idempotente via ON CONFLICT DO NOTHING. Falha não derruba o job de PRIDE.
        try:
            from apps.core.services.link_service import materialize_project_links
            _project_id = job.project_id
            inserted = materialize_project_links(_project_id)
            if inserted > 0:
                logger.info(
                    'PRIDE job %s: %d vínculos ProjectPaperDataset materializados para projeto %s',
                    job_id, inserted, _project_id,
                )
        except Exception as e:
            logger.error(
                'materialize_project_links falhou após PRIDE job %s (projeto %s): %s',
                job_id, job.project_id, e,
            )

        # Tenta avançar para curating se não há mais jobs de busca ativos.
        try:
            from apps.core.services.project_status import advance_to_curating_if_done
            advance_to_curating_if_done(job.project)
        except Exception as e:
            logger.error(
                'advance_to_curating_if_done falhou após PRIDE job %s (projeto %s): %s',
                job_id, job.project_id, e,
            )

        # Recompute pós-ingestão: reconstrói disease_axis + monogenic_gene_hit para
        # os datasets PRIDE tocados por este job (fecha gap R4 — COPY writer apaga
        # extra_metadata['contract']['monogenic_gene_hit'] ao re-ingerir o dataset).
        # Assíncrono e tolerante a falha: se a task de classificação falhar, o job
        # de ingestão já está COMPLETED e o erro fica apenas no log.
        try:
            from apps.core.tasks.disease_axis_tasks import recompute_disease_axis_for_job
            recompute_disease_axis_for_job.delay(str(job_id))
            logger.info(
                'recompute_disease_axis_for_job enfileirado para PRIDE job %s', job_id
            )
        except Exception as e:
            logger.error(
                'Falha ao enfileirar recompute_disease_axis_for_job após PRIDE job %s: %s',
                job_id, e,
            )

        return {
            'datasets_processed': result.datasets_processed,
            'datasets_inserted': result.datasets_inserted,
            'links_inserted': result.links_inserted,
            'errors': result.errors,
        }
    except ImportError:
        # rust_engine não compilado: marca FAILED com mensagem clara.
        try:
            job = IngestionJob.objects.get(id=job_id)
            job.status = IngestionJob.JobStatus.FAILED
            job.error_message = (
                'rust_engine not installed — compile with '
                '`maturin develop --release`'
            )
            job.save(update_fields=['status', 'error_message'])
        except IngestionJob.DoesNotExist:
            pass
        return {'datasets_processed': 0, 'datasets_inserted': 0, 'links_inserted': 0, 'errors': []}
    except Exception as exc:
        try:
            job = IngestionJob.objects.get(id=job_id)
            job.status = IngestionJob.JobStatus.FAILED
            job.error_message = str(exc)
            job.save(update_fields=['status', 'error_message'])
        except IngestionJob.DoesNotExist:
            logger.warning('IngestionJob %s not found — task aborted', job_id)
        raise self.retry(exc=exc, countdown=60)


@shared_task(
    bind=True,
    max_retries=3,
    # Jobs longos (F2 — FASTQ GB–TB): time limits generosos.
    # soft_time_limit dispara SoftTimeLimitExceeded antes do hard limit,
    # permitindo que a task faça cleanup antes de morrer.
    # Para GEO supplementary (F1, MB) esses limites são folgados e não impactam.
    time_limit=72 * 3600,        # 72 horas: hard kill
    soft_time_limit=70 * 3600,   # 70 horas: sinal suave para cleanup
    # acks_late=True: a mensagem só é confirmada após a task concluir (ou falhar
    # definitivamente). Garante que jobs FASTQ longos não são perdidos em caso de
    # crash do worker — o broker reenfileira a mensagem para retry.
    acks_late=True,
)
def run_omics_download(self, project_id: str, dataset_id: int, file_kind: str = 'geo_supplementary', sample_ids: list[int] | None = None):
    """
    Orquestra o download de arquivos ômicos para um dataset já curado.

    Fluxo (F1 — GEO supplementary / F2 — FASTQ):
      1. Guarda de idempotência: aborta se job do tipo correspondente já
         ativo para o mesmo dataset+projeto (o DownloadService já cria o job
         antes de enfileirar esta task, então apenas confirma que o job existe).
      2. Monta db_url; obtém ncbi_api_key de user.profile ou settings —
         NUNCA logado (skill sensitive-data-handling).
      3. Deriva dataset_accession (GSE* para GEO; SRP*/accession original para SRA).
      4. Chama rust_engine.download_dataset_files — Rust faz HTTP, streaming
         para dest_dir local, popula core_datasetfile via COPY.
      5. Upload pós-job (decisão D3): para cada DatasetFile, abre em modo
         streaming (File(f) em chunks — não carrega tudo em memória) e faz
         upload via default_storage.save().  Remove arquivo local após upload.
         - F1: itera sobre dataset.files (DatasetFile com dataset=dataset).
         - F2 (FASTQ): itera também sobre sample.files de todos os OmicSample
           do dataset (DatasetFile com sample=sample, pois o Rust grava por
           sample SRR*).  O storage_key inclui o accession do sample para
           identificador estável: omics/{user_id}/{project_id}/{srr_accession}/{filename}.
      6. Quando TODOS os DatasetFile (dataset + samples) estiverem 'downloaded',
         seta ProjectDataset.curation_status='downloaded'.
      7. Atualiza IngestionJob com status/contadores finais.

    Abordagem de upload streaming (F2, arquivos GB–TB):
      - default_storage.save(key, File(f)) com f aberto em 'rb'.
      - django-storages S3Boto3 usa multipart upload automaticamente para
        arquivos > 5 MB (padrão AWS_S3_MULTIPART_THRESHOLD = 8 MB).
      - File(f) não bufferiza em memória: o boto3 lê em chunks e faz upload
        parte a parte, mantendo footprint de memória constante.
      - O arquivo local temporário é removido apenas após upload bem-sucedido.

    Seleção de amostras (MVP-A):
      - sample_ids=None → Rust baixa todas as runs do dataset (scope='all').
      - sample_ids=[...] → Rust filtra por OmicSample.id (scope='included'/'manual').
        A validação de isolamento cross-project já foi feita na view antes do dispatch.

    Regra #1: a task apenas orquestra — não faz HTTP nem parse de dados.
    Upload via default_storage é I/O de orquestração aceito no Django.
    Auditoria (curation-audit-trail): download NÃO é curadoria;
    curated_at/exclusion_reason/notes não são tocados.
    Falha por arquivo vira download_status='failed' + error_message —
    NUNCA DELETE de DatasetFile baixado.
    """
    from apps.core.models import DaVinciProject

    try:
        import rust_engine

        try:
            project = DaVinciProject.objects.select_related('user').get(id=project_id)
        except DaVinciProject.DoesNotExist:
            logger.warning('DaVinciProject %s not found — download aborted', project_id)
            return {'files_downloaded': 0, 'bytes_total': 0, 'errors': []}

        try:
            dataset = OmicDataset.objects.get(id=dataset_id)
        except OmicDataset.DoesNotExist:
            logger.warning('OmicDataset %s not found — download aborted', dataset_id)
            return {'files_downloaded': 0, 'bytes_total': 0, 'errors': []}

        # Resolve o job criado pelo DownloadService (já deve existir em PENDING).
        # Se não existir (chamada direta sem service), cria um novo.
        from apps.core.services.download_service import _file_kind_to_job_type
        job_type = _file_kind_to_job_type(file_kind)

        job = IngestionJob.objects.filter(
            project=project,
            job_type=job_type,
            parameters__dataset_id=dataset_id,
            status__in=[IngestionJob.JobStatus.PENDING, IngestionJob.JobStatus.RUNNING],
        ).order_by('-created_at').first()

        if job is None:
            # Fallback: cria job se a task for chamada sem service (testes / retries)
            job = IngestionJob.objects.create(
                project=project,
                job_type=job_type,
                status=IngestionJob.JobStatus.RUNNING,
                parameters={
                    'dataset_id': dataset_id,
                    'dataset_accession': dataset.accession,
                    'source_db': dataset.source_db,
                    'file_kind': file_kind,
                },
            )
        else:
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.RUNNING,
            )

        # Monta db_url
        db = settings.DATABASES['default']
        db_url = f"postgresql://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}"

        # Obtém ncbi_api_key — NUNCA logar (skill sensitive-data-handling)
        user = project.user
        ncbi_api_key = getattr(settings, 'NCBI_API_KEY', None)
        try:
            ncbi_api_key = user.profile.ncbi_api_key or ncbi_api_key
        except Exception:
            pass

        # Deriva dataset_accession (GSE*) — mesma normalização de run_sample_ingestion
        if dataset.source_db == 'geo':
            gse_raw = (dataset.extra_metadata or {}).get('gse')
            if not gse_raw:
                error_msg = (
                    f"GEO dataset {dataset.accession} sem GSE em extra_metadata — "
                    "não é possível baixar supplementary sem o accession GSE*"
                )
                logger.error('run_omics_download abortado para dataset %s: %s', dataset_id, error_msg)
                IngestionJob.objects.filter(id=job.id).update(
                    status=IngestionJob.JobStatus.FAILED,
                    error_message=error_msg,
                )
                return {'files_downloaded': 0, 'bytes_total': 0, 'errors': [error_msg]}

            gse_str = str(gse_raw).strip()
            if gse_str.upper().startswith('GSE'):
                dataset_accession = gse_str
            else:
                dataset_accession = f"GSE{gse_str}"
        else:
            dataset_accession = dataset.accession

        # Cria diretório temporário para o Rust escrever os arquivos locais.
        # O Rust popula core_datasetfile com storage_key = caminho local absoluto;
        # o Django faz upload para object storage e sobrescreve storage_key.
        with tempfile.TemporaryDirectory(prefix='davinci_omics_') as dest_dir:
            result = rust_engine.download_dataset_files(
                job_id=str(job.id),
                dataset_id=dataset.id,
                dataset_accession=dataset_accession,
                source_db=dataset.source_db,
                file_kind=file_kind,
                dest_dir=dest_dir,
                db_url=db_url,
                ncbi_api_key=ncbi_api_key,
                # sample_ids=None → Rust baixa todas as runs (scope='all')
                # sample_ids=[...] → Rust filtra por OmicSample.id (scope='included'/'manual')
                sample_ids=sample_ids,
            )

            upload_errors = []
            uploaded_count = 0

            # ── Upload pós-job (decisão D3) ───────────────────────────────────
            # F1 (GEO): DatasetFile.dataset = dataset
            # F2 (FASTQ): DatasetFile.sample = sample (o Rust grava por SRR*)
            # Ambos os casos: storage_key local → object storage, depois remove local.
            #
            # Abordagem streaming: File(f) com f aberto em 'rb'.
            # django-storages S3Boto3 usa multipart upload automaticamente
            # (AWS_S3_MULTIPART_THRESHOLD padrão 8 MB), sem bufferizar em memória.

            def _upload_datasetfile(df: DatasetFile, accession_for_path: str) -> None:
                """Faz upload de um DatasetFile do caminho local para o object storage."""
                nonlocal uploaded_count

                local_path = df.storage_key  # Rust gravou o caminho local aqui
                if not local_path or not os.path.isfile(local_path):
                    # Arquivo ausente: ou já carregado (key de object storage)
                    # ou o Rust marcou failed — não toca.
                    return

                filename = os.path.basename(local_path)
                object_key = omics_storage_key(
                    user_id=user.id,
                    project_id=project.id,
                    dataset_accession=accession_for_path,
                    filename=filename,
                )

                try:
                    with open(local_path, 'rb') as f:
                        saved_key = default_storage.save(object_key, File(f))

                    DatasetFile.objects.filter(id=df.id).update(
                        storage_key=saved_key,
                        download_status=DatasetFile.DownloadStatus.DOWNLOADED,
                        downloaded_at=timezone.now(),
                        # size_bytes e checksum_md5 já foram gravados pelo Rust via COPY
                    )
                    uploaded_count += 1

                    # Remove arquivo local apenas após upload bem-sucedido
                    try:
                        os.remove(local_path)
                    except OSError as rm_err:
                        logger.warning(
                            'Falha ao remover arquivo local %s após upload: %s',
                            local_path,
                            rm_err,
                        )

                except Exception as upload_err:
                    # Falha de upload: marca failed, NUNCA deleta o registro
                    logger.error(
                        'Falha ao fazer upload de %s para object storage (accession=%s): %s',
                        filename,
                        accession_for_path,
                        upload_err,
                    )
                    DatasetFile.objects.filter(id=df.id).update(
                        download_status=DatasetFile.DownloadStatus.FAILED,
                        error_message=str(upload_err),
                    )
                    upload_errors.append(str(upload_err))

            # F1: arquivos vinculados diretamente ao dataset
            for df in DatasetFile.objects.filter(dataset=dataset).iterator():
                _upload_datasetfile(df, accession_for_path=dataset_accession)

            # F2 (FASTQ): arquivos vinculados aos OmicSamples do dataset.
            # O Rust grava DatasetFile(sample=sample, ...) por SRR* — o path
            # usa o accession do sample para identificador estável e isolamento.
            if file_kind == 'fastq':
                for sample in OmicSample.objects.filter(dataset=dataset).iterator():
                    sample_accession = sample.accession  # ex. 'SRR123456'
                    for df in DatasetFile.objects.filter(sample=sample).iterator():
                        _upload_datasetfile(df, accession_for_path=sample_accession)

        # Verifica se todos os arquivos do dataset (F1) e dos samples (F2) estão
        # 'downloaded' para promover o status agregado do ProjectDataset.
        if file_kind == 'fastq':
            # Conta arquivos de todos os samples do dataset
            total_files = DatasetFile.objects.filter(
                sample__dataset=dataset,
            ).count()
            downloaded_files = DatasetFile.objects.filter(
                sample__dataset=dataset,
                download_status=DatasetFile.DownloadStatus.DOWNLOADED,
            ).count()
        else:
            total_files = DatasetFile.objects.filter(dataset=dataset).count()
            downloaded_files = DatasetFile.objects.filter(
                dataset=dataset,
                download_status=DatasetFile.DownloadStatus.DOWNLOADED,
            ).count()

        if total_files > 0 and downloaded_files == total_files:
            ProjectDataset.objects.filter(
                project=project,
                dataset=dataset,
            ).update(curation_status=ProjectDataset.CurationStatus.DOWNLOADED)
            logger.info(
                'Todos os %d arquivo(s) do dataset %s baixados — ProjectDataset marcado como downloaded',
                total_files,
                dataset_accession,
            )

        # Consolida erros: do Rust + do upload Django
        all_errors = list(result.errors or []) + upload_errors
        error_msg = '; '.join(all_errors) if all_errors else ''

        final_status = (
            IngestionJob.JobStatus.FAILED
            if error_msg and uploaded_count == 0
            else IngestionJob.JobStatus.COMPLETED
        )

        IngestionJob.objects.filter(id=job.id).update(
            status=final_status,
            records_processed=result.files_downloaded,
            records_inserted=uploaded_count,
            error_message=error_msg,
        )

        return {
            'files_downloaded': result.files_downloaded,
            'bytes_total': result.bytes_total,
            'uploaded': uploaded_count,
            'errors': all_errors,
        }

    except ImportError:
        logger.error('rust_engine não instalado — compile com `maturin develop --release`')
        try:
            job = IngestionJob.objects.filter(
                project_id=project_id,
                parameters__dataset_id=dataset_id,
                status=IngestionJob.JobStatus.RUNNING,
            ).first()
            if job:
                job.status = IngestionJob.JobStatus.FAILED
                job.error_message = 'rust_engine not installed — compile with `maturin develop --release`'
                job.save(update_fields=['status', 'error_message'])
        except Exception:
            pass
        return {'files_downloaded': 0, 'bytes_total': 0, 'errors': ['rust_engine not installed']}

    except Exception as exc:
        logger.error(
            'run_omics_download falhou para projeto %s / dataset %s: %s',
            project_id,
            dataset_id,
            exc,
        )
        try:
            job = IngestionJob.objects.filter(
                project_id=project_id,
                parameters__dataset_id=dataset_id,
                status__in=[IngestionJob.JobStatus.PENDING, IngestionJob.JobStatus.RUNNING],
            ).order_by('-created_at').first()
            if job:
                IngestionJob.objects.filter(id=job.id).update(
                    status=IngestionJob.JobStatus.FAILED,
                    error_message=str(exc),
                )
        except Exception:
            pass
        raise self.retry(exc=exc, countdown=60)


@shared_task(
    bind=True,
    max_retries=2,
    # Carga de matriz CPTAC: download de dois arquivos CCT (~100–200 MB cada)
    # + parse streaming + escrita de Parquet local + upload via default_storage.
    # Estimativa conservadora: 2 horas para redes lentas / storage remoto.
    time_limit=4 * 3600,
    soft_time_limit=3 * 3600 + 50 * 60,
    acks_late=True,
)
def run_matrix_load(self, job_id: str, project_id: str):
    """
    Orquestra a carga de matriz CPTAC (OmnisPathway Obj 2, Fase 0).

    Wrapper fino que delega ao MatrixLoadService.run().  A task resolve
    o projeto e o job pelo ID antes de processar, garantindo isolamento
    (o job foi criado pelo MatrixLoadService.dispatch, que verificou que
    o projeto pertence ao usuário autenticado).

    Fluxo:
      1. Resolve DaVinciProject e IngestionJob pelos IDs recebidos.
      2. Resolve OmicDataset via MatrixLoadService._get_or_create_dataset().
      3. Delega o corpo da carga a MatrixLoadService.run(job=job).
         (Rust → manifest → upload → ORM set-based → job COMPLETED)

    Regra #1: task apenas orquestra — não faz HTTP nem parse de dados.
    Sensitive-data-handling: db_url nunca logada nem gravada em parameters.
    """
    from apps.core.models import DaVinciProject
    from apps.core.services.matrix_load_service import MatrixLoadService

    try:
        import rust_engine  # noqa: F401 — valida disponibilidade antes de iniciar
    except ImportError:
        try:
            job = IngestionJob.objects.get(id=job_id)
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=(
                    'rust_engine not installed — compile with '
                    '`maturin develop --release`'
                ),
            )
        except IngestionJob.DoesNotExist:
            pass
        return {'n_features': 0, 'n_samples': 0, 'errors': ['rust_engine not installed']}

    try:
        try:
            project = DaVinciProject.objects.select_related('user').get(id=project_id)
        except DaVinciProject.DoesNotExist:
            logger.warning(
                'run_matrix_load: DaVinciProject %s não encontrado — task abortada',
                project_id,
            )
            return {'n_features': 0, 'n_samples': 0, 'errors': ['project not found']}

        try:
            job = IngestionJob.objects.get(id=job_id)
        except IngestionJob.DoesNotExist:
            logger.warning(
                'run_matrix_load: IngestionJob %s não encontrado — task abortada',
                job_id,
            )
            return {'n_features': 0, 'n_samples': 0, 'errors': ['job not found']}

        # Isolamento: confirma que o job pertence ao projeto recebido
        if str(job.project_id) != str(project_id):
            logger.error(
                'run_matrix_load: job %s não pertence ao projeto %s — task abortada (isolamento)',
                job_id,
                project_id,
            )
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message='Isolamento: job não pertence ao projeto informado.',
            )
            return {'n_features': 0, 'n_samples': 0, 'errors': ['isolation violation']}

        service = MatrixLoadService(project)
        result = service.run(job=job)

        return {
            'n_features': result['n_features'],
            'n_samples': result['n_samples'],
            'storage_key': result['storage_key'],
            'roles': result['roles'],
            'errors': [],
        }

    except Exception as exc:
        logger.error(
            'run_matrix_load falhou para projeto %s / job %s: %s',
            project_id,
            job_id,
            exc,
        )
        try:
            IngestionJob.objects.filter(id=job_id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=str(exc),
            )
        except Exception:
            pass
        raise self.retry(exc=exc, countdown=120)


@shared_task(
    bind=True,
    max_retries=2,
    # Carga do cancerGeneList.txt (TOKEN-FREE): arquivo pequeno (~300KB TSV),
    # parse rápido, UPSERT. Estimativa: alguns minutos. 30 min é folgado.
    time_limit=30 * 60,
    soft_time_limit=25 * 60,
    acks_late=True,
)
def run_gene_role_load(self, job_id: str, project_id: str):
    """
    Orquestra a carga do catálogo GeneRole via OncoKB cancerGeneList.txt
    (OmnisPathway Obj 2, Fase 2, Slice 2A).

    Wrapper fino que delega ao GeneRoleLoadService.run(). A task resolve o
    projeto e o job pelos IDs antes de processar.

    Fonte TOKEN-FREE: https://www.oncokb.org/api/v1/utils/cancerGeneList.txt
    O Rust faz COPY/UPSERT direto em GeneRole — Django apenas orquestra.

    Regra #1: task apenas orquestra — não faz HTTP nem parse.
    Sensitive-data-handling: db_url nunca logada nem gravada em parameters.
    """
    from apps.core.models import DaVinciProject
    from apps.core.services.gene_role_load_service import GeneRoleLoadService

    try:
        import rust_engine  # noqa: F401 — valida disponibilidade antes de iniciar
    except ImportError:
        try:
            IngestionJob.objects.filter(id=job_id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=(
                    'rust_engine not installed — compile with '
                    '`maturin develop --release`'
                ),
            )
        except Exception:
            pass
        return {'n_genes': 0, 'n_upserted': 0, 'errors': ['rust_engine not installed']}

    try:
        try:
            project = DaVinciProject.objects.select_related('user').get(id=project_id)
        except DaVinciProject.DoesNotExist:
            logger.warning(
                'run_gene_role_load: DaVinciProject %s não encontrado — task abortada',
                project_id,
            )
            return {'n_genes': 0, 'n_upserted': 0, 'errors': ['project not found']}

        try:
            job = IngestionJob.objects.get(id=job_id)
        except IngestionJob.DoesNotExist:
            logger.warning(
                'run_gene_role_load: IngestionJob %s não encontrado — task abortada',
                job_id,
            )
            return {'n_genes': 0, 'n_upserted': 0, 'errors': ['job not found']}

        # Isolamento: confirma que o job pertence ao projeto recebido
        if str(job.project_id) != str(project_id):
            logger.error(
                'run_gene_role_load: job %s não pertence ao projeto %s — '
                'task abortada (isolamento)',
                job_id,
                project_id,
            )
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message='Isolamento: job não pertence ao projeto informado.',
            )
            return {'n_genes': 0, 'n_upserted': 0, 'errors': ['isolation violation']}

        service = GeneRoleLoadService(project)
        result = service.run(job=job)

        return {
            'n_genes': result['n_genes'],
            'n_oncogene': result['n_oncogene'],
            'n_tsg': result['n_tsg'],
            'n_dual': result['n_dual'],
            'n_neither': result['n_neither'],
            'n_unknown': result['n_unknown'],
            'source_version': result['source_version'],
            'n_upserted': result['n_upserted'],
            'errors': [],
        }

    except Exception as exc:
        logger.error(
            'run_gene_role_load falhou para projeto %s / job %s: %s',
            project_id,
            job_id,
            exc,
        )
        try:
            IngestionJob.objects.filter(id=job_id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=str(exc),
            )
        except Exception:
            pass
        raise self.retry(exc=exc, countdown=120)


@shared_task(
    bind=True,
    max_retries=2,
    # Carga da matriz CNV: download do .cct (~100 MB) + parse streaming +
    # escrita de Parquet local + upload via default_storage.
    # Estimativa conservadora: 2 horas para redes lentas / storage remoto.
    time_limit=4 * 3600,
    soft_time_limit=3 * 3600 + 50 * 60,
    acks_late=True,
)
def run_cnv_matrix_load(self, job_id: str, project_id: str):
    """
    Orquestra a carga da matriz CNV CPTAC CCRCC
    (OmnisPathway Obj 2, Fase 2, Slice CNV — materialização da matriz).

    Wrapper fino que delega ao CnvMatrixLoadService.run(). A task resolve
    o projeto e o job pelo ID antes de processar.

    Fonte pública (sem credencial): LinkedOmics HS_CPTAC_CCRCC_CNV_gene_Tumor.cct
    O Rust NÃO toca PG, NÃO faz upload — apenas escrita de Parquet local.
    Django faz upload via shared_omics_storage_key (dado público).

    Regra #1: task apenas orquestra — não faz HTTP nem parse de dados.
    Sensitive-data-handling: db_url nunca logada nem gravada em parameters.
    """
    from apps.core.models import DaVinciProject
    from apps.core.services.cnv_matrix_load_service import CnvMatrixLoadService

    try:
        import rust_engine  # noqa: F401 — valida disponibilidade antes de iniciar
    except ImportError:
        try:
            IngestionJob.objects.filter(id=job_id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=(
                    'rust_engine not installed — compile with '
                    '`maturin develop --release`'
                ),
            )
        except Exception:
            pass
        return {'n_features': 0, 'n_samples': 0, 'errors': ['rust_engine not installed']}

    try:
        try:
            project = DaVinciProject.objects.select_related('user').get(id=project_id)
        except DaVinciProject.DoesNotExist:
            logger.warning(
                'run_cnv_matrix_load: DaVinciProject %s não encontrado — task abortada',
                project_id,
            )
            return {'n_features': 0, 'n_samples': 0, 'errors': ['project not found']}

        try:
            job = IngestionJob.objects.get(id=job_id)
        except IngestionJob.DoesNotExist:
            logger.warning(
                'run_cnv_matrix_load: IngestionJob %s não encontrado — task abortada',
                job_id,
            )
            return {'n_features': 0, 'n_samples': 0, 'errors': ['job not found']}

        # Isolamento: confirma que o job pertence ao projeto recebido
        if str(job.project_id) != str(project_id):
            logger.error(
                'run_cnv_matrix_load: job %s não pertence ao projeto %s — '
                'task abortada (isolamento)',
                job_id,
                project_id,
            )
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message='Isolamento: job não pertence ao projeto informado.',
            )
            return {'n_features': 0, 'n_samples': 0, 'errors': ['isolation violation']}

        service = CnvMatrixLoadService(project)
        result = service.run(job=job)

        return {
            'n_features': result['n_features'],
            'n_samples': result['n_samples'],
            'storage_key': result['storage_key'],
            'errors': [],
        }

    except Exception as exc:
        logger.error(
            'run_cnv_matrix_load falhou para projeto %s / job %s: %s',
            project_id,
            job_id,
            exc,
        )
        try:
            IngestionJob.objects.filter(id=job_id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=str(exc),
            )
        except Exception:
            pass
        raise self.retry(exc=exc, countdown=120)


@shared_task(
    bind=True,
    max_retries=2,
    # Derivação de seed CNV: download do Parquet (dezenas de MB) + leitura
    # no Rust + COPY UPSERT em VariantEffectSeed. Estimativa: até 30 minutos
    # para redes lentas / storage remoto. 2 horas é margem folgada.
    time_limit=2 * 3600,
    soft_time_limit=1 * 3600 + 50 * 60,
    acks_late=True,
)
def run_cnv_seed_load(self, job_id: str, project_id: str):
    """
    Orquestra a derivação de VariantEffectSeed a partir da matriz CNV
    (OmnisPathway Obj 2, Fase 2, Slice CNV-seed).

    Wrapper fino que delega ao CnvSeedService.run(). A task resolve o projeto
    e o job pelos IDs antes de processar, garantindo isolamento (o job foi
    criado pelo CnvSeedService.dispatch, que verificou que o projeto pertence
    ao usuário autenticado).

    Fluxo:
      1. Resolve DaVinciProject e IngestionJob pelos IDs recebidos.
      2. Valida isolamento: job.project_id == project_id.
      3. Delega a CnvSeedService.run(job=job), que:
           - Resolve OmicMatrix CNV via ProjectDataset (isolamento).
           - Pré-checks (GeneRole populado?).
           - Download do Parquet via default_storage → tempfile local.
           - rust_engine.seed_cnv_from_parquet(parquet_path, matrix_id, db_url, ...).
           - Marca job COMPLETED com o manifesto.

    Regra #1: task apenas orquestra — não faz HTTP nem parse de dados.
    Sensitive-data-handling: db_url nunca logada nem gravada em parameters.
    """
    from apps.core.models import DaVinciProject
    from apps.core.services.cnv_seed_service import CnvSeedService

    try:
        import rust_engine  # noqa: F401 — valida disponibilidade antes de iniciar
    except ImportError:
        try:
            IngestionJob.objects.filter(id=job_id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=(
                    'rust_engine not installed — compile with '
                    '`maturin develop --release`'
                ),
            )
        except Exception:
            pass
        return {'n_seeds_written': 0, 'errors': ['rust_engine not installed']}

    try:
        try:
            project = DaVinciProject.objects.select_related('user').get(id=project_id)
        except DaVinciProject.DoesNotExist:
            logger.warning(
                'run_cnv_seed_load: DaVinciProject %s não encontrado — task abortada',
                project_id,
            )
            return {'n_seeds_written': 0, 'errors': ['project not found']}

        try:
            job = IngestionJob.objects.get(id=job_id)
        except IngestionJob.DoesNotExist:
            logger.warning(
                'run_cnv_seed_load: IngestionJob %s não encontrado — task abortada',
                job_id,
            )
            return {'n_seeds_written': 0, 'errors': ['job not found']}

        # Isolamento: confirma que o job pertence ao projeto recebido
        if str(job.project_id) != str(project_id):
            logger.error(
                'run_cnv_seed_load: job %s não pertence ao projeto %s — '
                'task abortada (isolamento)',
                job_id,
                project_id,
            )
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message='Isolamento: job não pertence ao projeto informado.',
            )
            return {'n_seeds_written': 0, 'errors': ['isolation violation']}

        service = CnvSeedService(project)
        result = service.run(job=job)

        return {
            'n_seeds_written': result['n_seeds_written'],
            'n_activator': result['n_activator'],
            'n_inactivator': result['n_inactivator'],
            'n_neutral_skipped': result['n_neutral_skipped'],
            'n_genes_with_role': result['n_genes_with_role'],
            'n_genes_skipped_no_role': result['n_genes_skipped_no_role'],
            'errors': [],
        }

    except Exception as exc:
        logger.error(
            'run_cnv_seed_load falhou para projeto %s / job %s: %s',
            project_id,
            job_id,
            exc,
        )
        try:
            IngestionJob.objects.filter(id=job_id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=str(exc),
            )
        except Exception:
            pass
        raise self.retry(exc=exc, countdown=120)


@shared_task(
    bind=True,
    max_retries=1,
    # Carga ClinVar (~1 GB) + AlphaMissense (~643 MB): download streaming +
    # parse + COPY UPSERT em VariantEffectRaw. Estimativa: 4-6h em redes lentas.
    # time_limit conservador de 8h; soft a 7h50m para flush limpo.
    time_limit=8 * 3600,
    soft_time_limit=7 * 3600 + 50 * 60,
    acks_late=True,
)
def run_variant_effect_raw_load(
    self,
    job_id: str,
    project_id: str,
    skip_alphamissense: bool = False,
):
    """
    Orquestra a carga de VariantEffectRaw via ClinVar + AlphaMissense
    (OmnisPathway Obj 2, Fase 2, Slice 2B).

    Wrapper fino que delega ao VariantEffectRawService.run(). A task resolve
    o projeto e o job pelos IDs antes de processar, garantindo isolamento
    (o job foi criado pelo VariantEffectRawService.dispatch, que verificou que
    o projeto pertence ao usuário autenticado).

    Fluxo:
      1. Resolve DaVinciProject e IngestionJob pelos IDs recebidos.
      2. Valida isolamento: job.project_id == project_id.
      3. Delega a VariantEffectRawService.run(job=job), que:
           - Pré-check: GeneRole populado.
           - Constrói gene_allowlist de GeneRole(source='oncokb').
           - Mapa uniprot→gene via UniProt REST (com cache).
           - rust_engine.load_clinvar_effects(url, dest_dir, db_url, allowlist).
           - rust_engine.load_alphamissense_effects(...) se não skip_alphamissense.
           - Marca job COMPLETED com contadores combinados.

    Regra #1: task apenas orquestra — não faz HTTP de dados grandes nem parse.
    Sensitive-data-handling: db_url nunca logada nem gravada em parameters.
    """
    from apps.core.models import DaVinciProject
    from apps.core.services.variant_effect_raw_service import (
        GeneRoleNotPopulatedError,
        VariantEffectRawService,
    )

    try:
        import rust_engine  # noqa: F401 — valida disponibilidade antes de iniciar
    except ImportError:
        try:
            IngestionJob.objects.filter(id=job_id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=(
                    'rust_engine not installed — compile with '
                    '`maturin develop --release`'
                ),
            )
        except Exception:
            pass
        return {
            'clinvar_n_upserted': 0,
            'am_n_upserted': 0,
            'errors': ['rust_engine not installed'],
        }

    try:
        try:
            project = DaVinciProject.objects.select_related('user').get(
                id=project_id
            )
        except DaVinciProject.DoesNotExist:
            logger.warning(
                'run_variant_effect_raw_load: DaVinciProject %s não encontrado '
                '— task abortada',
                project_id,
            )
            return {
                'clinvar_n_upserted': 0,
                'am_n_upserted': 0,
                'errors': ['project not found'],
            }

        try:
            job = IngestionJob.objects.get(id=job_id)
        except IngestionJob.DoesNotExist:
            logger.warning(
                'run_variant_effect_raw_load: IngestionJob %s não encontrado '
                '— task abortada',
                job_id,
            )
            return {
                'clinvar_n_upserted': 0,
                'am_n_upserted': 0,
                'errors': ['job not found'],
            }

        # Isolamento: confirma que o job pertence ao projeto recebido
        if str(job.project_id) != str(project_id):
            logger.error(
                'run_variant_effect_raw_load: job %s não pertence ao projeto %s '
                '— task abortada (isolamento)',
                job_id,
                project_id,
            )
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message='Isolamento: job não pertence ao projeto informado.',
            )
            return {
                'clinvar_n_upserted': 0,
                'am_n_upserted': 0,
                'errors': ['isolation violation'],
            }

        service = VariantEffectRawService(
            project,
            skip_alphamissense=skip_alphamissense,
        )
        result = service.run(job=job)

        return {
            'clinvar_n_kept': result['clinvar_n_kept'],
            'clinvar_n_upserted': result['clinvar_n_upserted'],
            'clinvar_source_version': result['clinvar_source_version'],
            'am_skipped': result['am_skipped'],
            'am_n_kept': result['am_n_kept'],
            'am_n_upserted': result['am_n_upserted'],
            'n_genes_in_allowlist': result['n_genes_in_allowlist'],
            'n_genes_with_uniprot': result['n_genes_with_uniprot'],
            'n_genes_without_uniprot': result['n_genes_without_uniprot'],
            'errors': [],
        }

    except GeneRoleNotPopulatedError as exc:
        logger.error(
            'run_variant_effect_raw_load: GeneRole não populado (projeto %s / '
            'job %s) — task abortada sem retry: %s',
            project_id,
            job_id,
            exc,
        )
        try:
            IngestionJob.objects.filter(id=job_id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=str(exc),
            )
        except Exception:
            pass
        # GeneRole vazio é erro de configuração, não transitório — não retry
        return {
            'clinvar_n_upserted': 0,
            'am_n_upserted': 0,
            'errors': [str(exc)],
        }

    except Exception as exc:
        logger.error(
            'run_variant_effect_raw_load falhou para projeto %s / job %s: %s',
            project_id,
            job_id,
            exc,
        )
        try:
            IngestionJob.objects.filter(id=job_id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=str(exc),
            )
        except Exception:
            pass
        raise self.retry(exc=exc, countdown=300)


@shared_task(
    bind=True,
    max_retries=3,
    # Resolução SRX→SRR é HTTP + parse — tipicamente segundos a poucos minutos.
    # Limites generosos para datasets grandes com muitos GSM.
    time_limit=2 * 3600,
    soft_time_limit=1 * 3600 + 50 * 60,
    acks_late=True,
)
def run_sra_resolution(self, project_id: str, dataset_id: int):
    """
    Resolve GSM→SRR para um dataset GEO via rust_engine.resolve_sra_runs_for_dataset.

    Fluxo (MVP-B — passo 4):
      1. Localiza o IngestionJob SRA_RESOLUTION criado pelo SraResolutionService.
      2. Monta db_url; obtém ncbi_api_key — NUNCA logado (sensitive-data-handling).
      3. Chama rust_engine.resolve_sra_runs_for_dataset(dataset_id, db_url, ncbi_api_key).
         O Rust:
           a. Lê OmicSample.extra_metadata['relation'] de cada GSM do dataset.
           b. Extrai SRX* da string de relation GEO SOFT.
           c. Resolve SRX*→SRR* via ENA filereport API.
           d. Grava extra_metadata['sra_runs'] = ["SRR..."] no GSM (UPDATE sem migration).
      4. Atualiza IngestionJob com contadores e status final.

    Regra #1: task apenas orquestra — Rust faz HTTP e UPDATE.
    Auditoria (curation-audit-trail): resolução não é curadoria —
    curated_at/exclusion_reason/notes não são tocados.
    Sensitive-data-handling: ncbi_api_key NUNCA em log nem em IngestionJob.parameters.
    """
    from apps.core.models import DaVinciProject

    try:
        import rust_engine

        try:
            project = DaVinciProject.objects.select_related('user').get(id=project_id)
        except DaVinciProject.DoesNotExist:
            logger.warning('DaVinciProject %s not found — SRA resolution aborted', project_id)
            return {'samples_updated': 0, 'errors': []}

        try:
            dataset = OmicDataset.objects.get(id=dataset_id)
        except OmicDataset.DoesNotExist:
            logger.warning('OmicDataset %s not found — SRA resolution aborted', dataset_id)
            return {'samples_updated': 0, 'errors': []}

        job_type = IngestionJob.JobType.SRA_RESOLUTION

        job = IngestionJob.objects.filter(
            project=project,
            job_type=job_type,
            parameters__dataset_id=dataset_id,
            status__in=[IngestionJob.JobStatus.PENDING, IngestionJob.JobStatus.RUNNING],
        ).order_by('-created_at').first()

        if job is None:
            # Fallback: cria job se chamado sem service (testes / retries)
            job = IngestionJob.objects.create(
                project=project,
                job_type=job_type,
                status=IngestionJob.JobStatus.RUNNING,
                parameters={
                    'dataset_id': dataset_id,
                    'dataset_accession': dataset.accession,
                    'source_db': dataset.source_db,
                },
            )
        else:
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.RUNNING,
            )

        # Monta db_url — NUNCA logar (sensitive-data-handling)
        db = settings.DATABASES['default']
        db_url = f"postgresql://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}"

        # Obtém ncbi_api_key — NUNCA logar (sensitive-data-handling)
        user = project.user
        ncbi_api_key = getattr(settings, 'NCBI_API_KEY', None)
        try:
            ncbi_api_key = user.profile.ncbi_api_key or ncbi_api_key
        except Exception:
            pass

        result = rust_engine.resolve_sra_runs_for_dataset(
            dataset_id=dataset.id,
            db_url=db_url,
            ncbi_api_key=ncbi_api_key,
        )

        error_msg = '; '.join(result.errors) if result.errors else ''
        final_status = (
            IngestionJob.JobStatus.FAILED
            if error_msg and result.samples_updated == 0
            else IngestionJob.JobStatus.COMPLETED
        )

        IngestionJob.objects.filter(id=job.id).update(
            status=final_status,
            records_processed=result.samples_updated,
            records_inserted=result.samples_updated,
            error_message=error_msg,
        )

        logger.info(
            'SRA resolution concluída para dataset %s: %d amostras atualizadas, %d erro(s)',
            dataset.accession,
            result.samples_updated,
            len(result.errors or []),
        )

        return {
            'samples_updated': result.samples_updated,
            'errors': list(result.errors or []),
        }

    except ImportError:
        logger.error('rust_engine não instalado — compile com `maturin develop --release`')
        try:
            job = IngestionJob.objects.filter(
                project_id=project_id,
                parameters__dataset_id=dataset_id,
                status__in=[IngestionJob.JobStatus.PENDING, IngestionJob.JobStatus.RUNNING],
            ).order_by('-created_at').first()
            if job:
                IngestionJob.objects.filter(id=job.id).update(
                    status=IngestionJob.JobStatus.FAILED,
                    error_message='rust_engine not installed — compile with `maturin develop --release`',
                )
        except Exception:
            pass
        return {'samples_updated': 0, 'errors': ['rust_engine not installed']}

    except Exception as exc:
        logger.error(
            'run_sra_resolution falhou para projeto %s / dataset %s: %s',
            project_id,
            dataset_id,
            exc,
        )
        try:
            job = IngestionJob.objects.filter(
                project_id=project_id,
                parameters__dataset_id=dataset_id,
                job_type=IngestionJob.JobType.SRA_RESOLUTION,
                status__in=[IngestionJob.JobStatus.PENDING, IngestionJob.JobStatus.RUNNING],
            ).order_by('-created_at').first()
            if job:
                IngestionJob.objects.filter(id=job.id).update(
                    status=IngestionJob.JobStatus.FAILED,
                    error_message=str(exc),
                )
        except Exception:
            pass
        raise self.retry(exc=exc, countdown=60)


@shared_task(
    bind=True,
    max_retries=1,
    # Carga do MAF somático GDC: fetch em lote de ~353 arquivos + parse streaming +
    # Parquet gene×amostra + TSV de ocorrências + seed SNV set-based.
    # Estimativa conservadora: 4-6h em redes lentas. time_limit de 8h; soft a 7h50m.
    time_limit=8 * 3600,
    soft_time_limit=7 * 3600 + 50 * 60,
    acks_late=True,
)
def run_somatic_maf_load(self, job_id: str, project_id: str):
    """
    Orquestra a carga do MAF somático GDC CPTAC-3 Kidney + derivação de seeds SNV
    (OmnisPathway Obj 2, Fase 2, Passo 2.6 — Slice 2D).

    Wrapper fino que delega ao SomaticMafService.run(). A task resolve o projeto
    e o job pelos IDs antes de processar, garantindo isolamento.

    Fluxo único (matriz + seed na mesma execução):
      1. Resolve file_map via GDC API pública (sem credencial).
      2. Chama rust_engine.load_somatic_maf (Parquet burden + TSV ocorrências).
      3. Upload Parquet + ORM (OmicMatrix/OmicSample/OmicMatrixSample).
      4. Seed SNV set-based: lê TSV × VariantEffectResolved → VariantEffectSeed.

    Regra #1: task apenas orquestra — não faz parse de dados.
    Sensitive-data-handling: db_url nunca logada nem gravada em parameters.
    """
    from apps.core.models import DaVinciProject
    from apps.core.services.somatic_maf_service import SomaticMafService

    try:
        import rust_engine  # noqa: F401 — valida disponibilidade antes de iniciar
    except ImportError:
        try:
            IngestionJob.objects.filter(id=job_id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=(
                    'rust_engine not installed — compile with '
                    '`maturin develop --release`'
                ),
            )
        except Exception:
            pass
        return {'n_seeds_created': 0, 'errors': ['rust_engine not installed']}

    try:
        try:
            project = DaVinciProject.objects.select_related('user').get(id=project_id)
        except DaVinciProject.DoesNotExist:
            logger.warning(
                'run_somatic_maf_load: DaVinciProject %s não encontrado — task abortada',
                project_id,
            )
            return {'n_seeds_created': 0, 'errors': ['project not found']}

        try:
            job = IngestionJob.objects.get(id=job_id)
        except IngestionJob.DoesNotExist:
            logger.warning(
                'run_somatic_maf_load: IngestionJob %s não encontrado — task abortada',
                job_id,
            )
            return {'n_seeds_created': 0, 'errors': ['job not found']}

        # Isolamento: confirma que o job pertence ao projeto recebido
        if str(job.project_id) != str(project_id):
            logger.error(
                'run_somatic_maf_load: job %s não pertence ao projeto %s — '
                'task abortada (isolamento)',
                job_id,
                project_id,
            )
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message='Isolamento: job não pertence ao projeto informado.',
            )
            return {'n_seeds_created': 0, 'errors': ['isolation violation']}

        service = SomaticMafService(project)
        result = service.run(job=job)

        return {
            'n_files_processed': result['n_files_processed'],
            'n_samples': result['n_samples'],
            'n_occurrences': result['n_occurrences'],
            'n_seeds_created': result['n_seeds_created'],
            'n_inactivator': result['n_inactivator'],
            'n_activator': result['n_activator'],
            'n_neutral': result['n_neutral'],
            'n_truncating_lof': result['n_truncating_lof'],
            'n_missense_resolved': result['n_missense_resolved'],
            'n_unannotated_skipped': result['n_unannotated_skipped'],
            'storage_key': result['storage_key'],
            'errors': result.get('errors', []),
        }

    except Exception as exc:
        logger.error(
            'run_somatic_maf_load falhou para projeto %s / job %s: %s',
            project_id,
            job_id,
            exc,
        )
        try:
            IngestionJob.objects.filter(id=job_id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=str(exc),
                completed_at=timezone.now(),
            )
        except Exception:
            pass
        raise self.retry(exc=exc, countdown=120)


@shared_task(
    bind=True,
    max_retries=2,
    # Carga do fosfoproteoma CPTAC: download de dois arquivos CCT (~100–200 MB
    # cada) + parse streaming + escrita de Parquet local + upload via
    # default_storage. Estimativa conservadora: 2 horas. 4 horas é margem folgada.
    time_limit=4 * 3600,
    soft_time_limit=3 * 3600 + 50 * 60,
    acks_late=True,
)
def run_phospho_matrix_load(self, job_id: str, project_id: str):
    """
    Orquestra a carga da matriz de fosfoproteoma CPTAC CCRCC
    (OmnisPathway Obj 2, Fase 3, Passo 3.1).

    Wrapper fino que delega ao PhosphoMatrixLoadService.run(). A task resolve
    o projeto e o job pelo ID antes de processar, garantindo isolamento (o job
    foi criado pelo PhosphoMatrixLoadService.dispatch, que verificou que o
    projeto pertence ao usuário autenticado).

    Fluxo:
      1. Resolve DaVinciProject e IngestionJob pelos IDs recebidos.
      2. Valida isolamento: job.project_id == project_id.
      3. Delega a PhosphoMatrixLoadService.run(job=job), que:
           - Resolve/cria OmicDataset CPTAC-CCRCC-PHOSPHO.
           - Gate de idempotência (OmicMatrix/job ativo).
           - rust_engine.load_cptac_phospho_matrix -> manifest.
           - Upload do Parquet via shared_omics_storage_key.
           - Cria OmicMatrix + OmicSample (get_or_create) + OmicMatrixSample.
           - Marca job COMPLETED.

    Regra #1: task apenas orquestra - nao faz HTTP nem parse de dados.
    Sensitive-data-handling: db_url nunca logada nem gravada em parameters.
    """
    from apps.core.models import DaVinciProject
    from apps.core.services.phospho_matrix_load_service import (
        PhosphoMatrixAlreadyLoadedError,
        PhosphoMatrixLoadService,
    )

    try:
        import rust_engine  # noqa: F401 — valida disponibilidade antes de iniciar
    except ImportError:
        try:
            IngestionJob.objects.filter(id=job_id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=(
                    'rust_engine not installed — compile with '
                    '`maturin develop --release`'
                ),
            )
        except Exception:
            pass
        return {'n_features': 0, 'n_samples': 0, 'errors': ['rust_engine not installed']}

    try:
        try:
            project = DaVinciProject.objects.select_related('user').get(id=project_id)
        except DaVinciProject.DoesNotExist:
            logger.warning(
                'run_phospho_matrix_load: DaVinciProject %s nao encontrado '
                '— task abortada',
                project_id,
            )
            return {'n_features': 0, 'n_samples': 0, 'errors': ['project not found']}

        try:
            job = IngestionJob.objects.get(id=job_id)
        except IngestionJob.DoesNotExist:
            logger.warning(
                'run_phospho_matrix_load: IngestionJob %s nao encontrado '
                '— task abortada',
                job_id,
            )
            return {'n_features': 0, 'n_samples': 0, 'errors': ['job not found']}

        if str(job.project_id) != str(project_id):
            logger.error(
                'run_phospho_matrix_load: job %s nao pertence ao projeto %s '
                '— task abortada (isolamento)',
                job_id,
                project_id,
            )
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message='Isolamento: job nao pertence ao projeto informado.',
            )
            return {'n_features': 0, 'n_samples': 0, 'errors': ['isolation violation']}

        service = PhosphoMatrixLoadService(project)
        result = service.run(job=job)

        return {
            'n_features': result['n_features'],
            'n_samples': result['n_samples'],
            'storage_key': result['storage_key'],
            'genes_discarded': result['genes_discarded'],
            'roles': result['roles'],
            'errors': [],
        }

    except PhosphoMatrixAlreadyLoadedError as exc:
        logger.info(
            'run_phospho_matrix_load: idempotencia (job %s / projeto %s): %s',
            job_id,
            project_id,
            exc,
        )
        try:
            IngestionJob.objects.filter(id=job_id).update(
                status=IngestionJob.JobStatus.COMPLETED,
                error_message=f'Idempotencia: {exc}',
            )
        except Exception:
            pass
        return {'n_features': 0, 'n_samples': 0, 'errors': []}

    except Exception as exc:
        logger.error(
            'run_phospho_matrix_load falhou para projeto %s / job %s: %s',
            project_id,
            job_id,
            exc,
        )
        try:
            IngestionJob.objects.filter(id=job_id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=str(exc),
            )
        except Exception:
            pass
        raise self.retry(exc=exc, countdown=120)


@shared_task(
    bind=True,
    max_retries=2,
    # Carga de topologia KEGG: fetch de 3 KGMLs (pequenos, rest.kegg.jp) +
    # parse XML + COPY Pathway/PathwayNode/PathwayEdge em PG via Rust.
    # Estimativa: alguns minutos (3 vias, KGMLs pequenos). 1 hora e folgado.
    time_limit=1 * 3600,
    soft_time_limit=55 * 60,
    acks_late=True,
)
def run_pathway_topology_load(self, job_id: str, project_id: str):
    """
    Orquestra a carga de topologia KEGG das vias de sinalizacao
    (OmnisPathway Obj 2, Fase 3, Passo 3.2).

    Wrapper fino que delega ao PathwayTopologyLoadService.run(). A task
    resolve o projeto e o job pelo ID antes de processar.

    Catalogo global: Pathway/PathwayNode/PathwayEdge sao catalogo global de
    referencia (sem FK de projeto). O job e de tracking — a carga afeta as
    tabelas globais.

    Fluxo:
      1. Resolve DaVinciProject e IngestionJob pelos IDs recebidos.
      2. Valida isolamento do job (job.project_id == project_id).
      3. Delega a PathwayTopologyLoadService.run(job=job), que:
           - rust_engine.load_kegg_topology -> manifest (COPY em PG via Rust).
           - Marca job COMPLETED com contadores (n_pathways, n_nodes, n_edges).

    Regra #1: task apenas orquestra — Rust faz fetch KGML, parse XML e COPY.
    Sensitive-data-handling: db_url nunca logada nem gravada em parameters.
    """
    from apps.core.models import DaVinciProject
    from apps.core.services.pathway_topology_load_service import (
        PathwayTopologyJobActiveError,
        PathwayTopologyLoadService,
    )

    try:
        import rust_engine  # noqa: F401 — valida disponibilidade antes de iniciar
    except ImportError:
        try:
            IngestionJob.objects.filter(id=job_id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=(
                    'rust_engine not installed — compile with '
                    '`maturin develop --release`'
                ),
            )
        except Exception:
            pass
        return {
            'n_pathways': 0,
            'n_nodes': 0,
            'n_edges': 0,
            'errors': ['rust_engine not installed'],
        }

    try:
        try:
            project = DaVinciProject.objects.select_related('user').get(id=project_id)
        except DaVinciProject.DoesNotExist:
            logger.warning(
                'run_pathway_topology_load: DaVinciProject %s nao encontrado '
                '— task abortada',
                project_id,
            )
            return {'n_pathways': 0, 'n_nodes': 0, 'n_edges': 0, 'errors': ['project not found']}

        try:
            job = IngestionJob.objects.get(id=job_id)
        except IngestionJob.DoesNotExist:
            logger.warning(
                'run_pathway_topology_load: IngestionJob %s nao encontrado '
                '— task abortada',
                job_id,
            )
            return {'n_pathways': 0, 'n_nodes': 0, 'n_edges': 0, 'errors': ['job not found']}

        if str(job.project_id) != str(project_id):
            logger.error(
                'run_pathway_topology_load: job %s nao pertence ao projeto %s '
                '— task abortada (isolamento)',
                job_id,
                project_id,
            )
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message='Isolamento: job nao pertence ao projeto informado.',
            )
            return {'n_pathways': 0, 'n_nodes': 0, 'n_edges': 0, 'errors': ['isolation violation']}

        service = PathwayTopologyLoadService(project)
        result = service.run(job=job)

        return {
            'n_pathways': result['n_pathways'],
            'n_nodes': result['n_nodes'],
            'n_edges': result['n_edges'],
            'n_signed': result['n_signed'],
            'n_unsigned': result['n_unsigned'],
            'n_orphan_symbols': result['n_orphan_symbols'],
            'source_version': result['source_version'],
            'errors': [],
        }

    except PathwayTopologyJobActiveError as exc:
        logger.info(
            'run_pathway_topology_load: idempotencia (job %s / projeto %s): %s',
            job_id,
            project_id,
            exc,
        )
        return {'n_pathways': 0, 'n_nodes': 0, 'n_edges': 0, 'errors': []}

    except Exception as exc:
        logger.error(
            'run_pathway_topology_load falhou para projeto %s / job %s: %s',
            project_id,
            job_id,
            exc,
        )
        try:
            IngestionJob.objects.filter(id=job_id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=str(exc),
            )
        except Exception:
            pass
        raise self.retry(exc=exc, countdown=120)


@shared_task(
    bind=True,
    max_retries=2,
    # Carga de regulons CollecTRI via OmniPath: fetch HTTP + filtro por
    # tf_allowlist em streaming + escrita JSONL local. Estimativa: minutos
    # (dados filtrados, volume modesto). 30 min e folgado.
    # RISCO: OmniPath estava com 502 em 2026-07-15 — confirmar na 1a execucao.
    time_limit=30 * 60,
    soft_time_limit=25 * 60,
    acks_late=True,
)
def run_regulon_load(self, job_id: str, project_id: str):
    """
    Orquestra a carga de regulons CollecTRI (OmniPath) para os TFs das vias
    (OmnisPathway Obj 2, Fase 3, Passo 3.3).

    Wrapper fino que delega ao RegulonLoadService.run(). A task resolve o
    projeto e o job pelo ID antes de processar.

    tf_allowlist: derivada automaticamente dos PathwayNode(node_type='gene')
    do grafo ja carregado em PG (passo 3.2). O Rust baixa e filtra regulons
    em streaming; grava JSONL local em diagnostics/cache/omnipath/.

    Pre-condicao: passo 3.2 (load_kegg_topology) deve ter sido executado.
    Se nao existirem PathwayNode nas vias, RegulonGraphNotLoadedError e
    levantado sem retry (erro de configuracao).

    Fluxo:
      1. Resolve DaVinciProject e IngestionJob pelos IDs recebidos.
      2. Valida isolamento do job.
      3. Delega a RegulonLoadService.run(job=job), que:
           - Deriva tf_allowlist via ORM set-based.
           - rust_engine.load_collectri_regulons -> manifest + JSONL local.
           - Persiste regulon_path em IngestionJob.parameters.
           - Marca job COMPLETED.

    Regra #1: task apenas orquestra — Rust faz fetch HTTP e parse.
    Sensitive-data-handling: db_url nao necessaria (sem COPY em PG).
    RISCO: OmniPath pode retornar 502 — confirmar endpoint na 1a execucao.
    """
    from apps.core.models import DaVinciProject
    from apps.core.services.regulon_load_service import (
        RegulonGraphNotLoadedError,
        RegulonJobActiveError,
        RegulonLoadService,
    )

    try:
        import rust_engine  # noqa: F401 — valida disponibilidade antes de iniciar
    except ImportError:
        try:
            IngestionJob.objects.filter(id=job_id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=(
                    'rust_engine not installed — compile with '
                    '`maturin develop --release`'
                ),
            )
        except Exception:
            pass
        return {'n_tfs': 0, 'n_edges': 0, 'errors': ['rust_engine not installed']}

    try:
        try:
            project = DaVinciProject.objects.select_related('user').get(id=project_id)
        except DaVinciProject.DoesNotExist:
            logger.warning(
                'run_regulon_load: DaVinciProject %s nao encontrado — task abortada',
                project_id,
            )
            return {'n_tfs': 0, 'n_edges': 0, 'errors': ['project not found']}

        try:
            job = IngestionJob.objects.get(id=job_id)
        except IngestionJob.DoesNotExist:
            logger.warning(
                'run_regulon_load: IngestionJob %s nao encontrado — task abortada',
                job_id,
            )
            return {'n_tfs': 0, 'n_edges': 0, 'errors': ['job not found']}

        if str(job.project_id) != str(project_id):
            logger.error(
                'run_regulon_load: job %s nao pertence ao projeto %s '
                '— task abortada (isolamento)',
                job_id,
                project_id,
            )
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message='Isolamento: job nao pertence ao projeto informado.',
            )
            return {'n_tfs': 0, 'n_edges': 0, 'errors': ['isolation violation']}

        service = RegulonLoadService(project)
        result = service.run(job=job)

        return {
            'n_tfs': result['n_tfs'],
            'n_targets': result['n_targets'],
            'n_edges': result['n_edges'],
            'n_positive': result['n_positive'],
            'n_negative': result['n_negative'],
            'n_neutral': result['n_neutral'],
            'source_version': result['source_version'],
            'tf_allowlist_size': result['tf_allowlist_size'],
            'errors': [],
        }

    except RegulonGraphNotLoadedError as exc:
        logger.error(
            'run_regulon_load: grafo nao carregado (projeto %s / job %s) '
            '— task abortada sem retry: %s',
            project_id,
            job_id,
            exc,
        )
        try:
            IngestionJob.objects.filter(id=job_id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=str(exc),
            )
        except Exception:
            pass
        return {'n_tfs': 0, 'n_edges': 0, 'errors': [str(exc)]}

    except RegulonJobActiveError as exc:
        logger.info(
            'run_regulon_load: idempotencia (job %s / projeto %s): %s',
            job_id,
            project_id,
            exc,
        )
        return {'n_tfs': 0, 'n_edges': 0, 'errors': []}

    except Exception as exc:
        logger.error(
            'run_regulon_load falhou para projeto %s / job %s: %s',
            project_id,
            job_id,
            exc,
        )
        try:
            IngestionJob.objects.filter(id=job_id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=str(exc),
            )
        except Exception:
            pass
        raise self.retry(exc=exc, countdown=120)


@shared_task(
    bind=True,
    max_retries=1,
    # Motor PFS v1: download de DOIS Parquets (fosfo + proteoma, dezenas de
    # MB cada) + RWR assinado/ULM/permutação em Rust (algoritmicamente
    # segundos, pela linearidade do RWR — D7 do plano) + COPY UPSERT em
    # PathwayActivityScore. O tempo dominante é I/O de download, não CPU.
    # max_retries=1 (não 2/3): o gate de reprodutibilidade (D3) e os
    # pré-checks já falham alto de forma determinística — re-tentar um erro
    # de configuração não ajuda; falhas de rede/I/O justificam 1 retry.
    time_limit=2 * 3600,
    soft_time_limit=1 * 3600 + 50 * 60,
    acks_late=True,
)
def run_pathway_scoring(self, job_id: str, project_id: str):
    """
    Orquestra o run do motor PFS v1 (OmnisPathway Obj 2, Fase 4).

    Wrapper fino que delega ao PathwayScoringService.run(). A task resolve
    o projeto e o job pelos IDs antes de processar, garantindo isolamento
    (o job foi criado por PathwayScoringService.dispatch, que já validou
    que o projeto pertence ao usuário autenticado e checou todos os
    pré-checks/gates).

    Fluxo:
      1. Resolve DaVinciProject e IngestionJob pelos IDs recebidos.
      2. Valida isolamento: job.project_id == project_id.
      3. Delega a PathwayScoringService.run(job=job), que:
           - Resolve as duas OmicMatrix (fosfo + proteoma) via
             ProjectDataset (isolamento).
           - Download dos dois Parquets via default_storage → tempfile
             local.
           - rust_engine.run_pfs_scoring(...) → PfsRunManifest.
           - Marca job COMPLETED com os contadores do manifesto.

    Regra #1: task apenas orquestra — não faz nenhuma conta numérica.
    Sensitive-data-handling: db_url nunca logada nem gravada em parameters.
    """
    from apps.core.models import DaVinciProject
    from apps.core.services.pathway_scoring_service import PathwayScoringService

    empty_result = {
        'n_rows_written': 0,
        'n_cases_scored': 0,
        'n_seeds_on_graph': 0,
        'n_seeds_off_graph': 0,
        'n_readouts_mapped': 0,
        'n_readouts_with_value': 0,
        'errors': [],
    }

    try:
        import rust_engine  # noqa: F401 — valida disponibilidade antes de iniciar
    except ImportError:
        try:
            IngestionJob.objects.filter(id=job_id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=(
                    'rust_engine not installed — compile with '
                    '`maturin develop --release`'
                ),
            )
        except Exception:
            pass
        return {**empty_result, 'errors': ['rust_engine not installed']}

    try:
        try:
            project = DaVinciProject.objects.select_related('user').get(id=project_id)
        except DaVinciProject.DoesNotExist:
            logger.warning(
                'run_pathway_scoring: DaVinciProject %s não encontrado — task abortada',
                project_id,
            )
            return {**empty_result, 'errors': ['project not found']}

        try:
            job = IngestionJob.objects.get(id=job_id)
        except IngestionJob.DoesNotExist:
            logger.warning(
                'run_pathway_scoring: IngestionJob %s não encontrado — task abortada',
                job_id,
            )
            return {**empty_result, 'errors': ['job not found']}

        # Isolamento: confirma que o job pertence ao projeto recebido
        if str(job.project_id) != str(project_id):
            logger.error(
                'run_pathway_scoring: job %s não pertence ao projeto %s — '
                'task abortada (isolamento)',
                job_id,
                project_id,
            )
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message='Isolamento: job não pertence ao projeto informado.',
            )
            return {**empty_result, 'errors': ['isolation violation']}

        service = PathwayScoringService(project)
        result = service.run(job=job)

        return {
            'n_rows_written': result['n_rows_written'],
            'n_cases_scored': result['n_cases_scored'],
            'n_seeds_on_graph': result['n_seeds_on_graph'],
            'n_seeds_off_graph': result['n_seeds_off_graph'],
            'n_readouts_mapped': result['n_readouts_mapped'],
            'n_readouts_with_value': result['n_readouts_with_value'],
            'zero_seeds_on_graph': result['zero_seeds_on_graph'],
            'errors': [],
        }

    except Exception as exc:
        logger.error(
            'run_pathway_scoring falhou para projeto %s / job %s: %s',
            project_id,
            job_id,
            exc,
        )
        try:
            IngestionJob.objects.filter(id=job_id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=str(exc),
            )
        except Exception:
            pass
        raise self.retry(exc=exc, countdown=120)
