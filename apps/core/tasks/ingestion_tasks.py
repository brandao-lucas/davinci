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
def run_omics_download(self, project_id: str, dataset_id: int, file_kind: str = 'geo_supplementary'):
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
