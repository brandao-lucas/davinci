"""
DownloadService — despacho de jobs de download de dados ômicos.

Responsabilidade única: validar estado, checar quota (FASTQ) e enfileirar a
Celery task `run_omics_download`. Não processa dados, não faz HTTP, não faz parse.

Gate de quota (passo 7 — F2):
  - Apenas downloads FASTQ (file_kind='fastq') estão sujeitos à quota.
  - A soma de DatasetFile.size_bytes já baixados (status='downloaded') do
    projeto é comparada com settings.DOWNLOAD_QUOTA_BYTES.
  - Se exceder (ou for indeterminado e o risco for alto), dispatch é rejeitado
    com QuotaExceededError.
  - Downloads GEO supplementary (F1, MB) são completamente isentos.

Confirmação explícita (passo 7 — F2):
  - FASTQ exige confirm=True no corpo da chamada a dispatch().
  - Sem confirm, dispatch levanta FastqConfirmRequiredError com prévia de uso.
  - GEO não precisa de confirm.

Isolamento (firebase-auth-guard / Regra #3):
  - A soma de quota é calculada sobre DatasetFile do projeto do usuário.
  - Jamais cruzar projetos de usuários distintos.
  - NUNCA logar os valores de credenciais (sensitive-data-handling).
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction
from django.db.models import Q, Sum

from apps.core.models import DatasetFile, DaVinciProject, IngestionJob, OmicDataset, ProjectDataset

logger = logging.getLogger(__name__)


class QuotaExceededError(Exception):
    """
    Levantado quando a soma de bytes já baixados (status='downloaded') do
    projeto excede DOWNLOAD_QUOTA_BYTES.  O dispatch é rejeitado.
    """

    def __init__(self, used_bytes: int, quota_bytes: int):
        self.used_bytes = used_bytes
        self.quota_bytes = quota_bytes
        super().__init__(
            f"Quota de download excedida: {used_bytes} bytes usados de "
            f"{quota_bytes} bytes permitidos."
        )


class FastqConfirmRequiredError(Exception):
    """
    Levantado quando file_kind='fastq' é solicitado sem confirm=True.
    Carrega a prévia de uso atual para que o endpoint informe o cliente.
    """

    def __init__(self, used_bytes: int, quota_bytes: int):
        self.used_bytes = used_bytes
        self.quota_bytes = quota_bytes
        super().__init__(
            "Download FASTQ requer confirmação explícita (confirm=true). "
            f"Uso atual do projeto: {used_bytes} bytes / {quota_bytes} bytes permitidos."
        )


class DownloadService:
    """
    Serviço de dispatch para download de arquivos ômicos.

    Padrão idêntico a SearchService: cria IngestionJob e enfileira task.
    Toda lógica de download (HTTP, parse, checksum) reside no Rust.
    Upload para object storage é orquestrado pela task, não aqui.

    Para FASTQ (file_kind='fastq'):
      - Requer confirm=True para prosseguir.
      - Verifica quota de bytes por projeto (DOWNLOAD_QUOTA_BYTES).
      - Lança FastqConfirmRequiredError ou QuotaExceededError conforme o caso.

    Para GEO supplementary (file_kind='geo_supplementary'):
      - Sem gate de confirm ou quota — arquivos MB, fluxo F1 simples.
    """

    @staticmethod
    def dispatch(
        project: DaVinciProject,
        dataset: OmicDataset,
        file_kind: str = 'geo_supplementary',
        user=None,
        confirm: bool = False,
    ) -> IngestionJob:
        """
        Enfileira download de arquivos para um dataset ômico.

        Guarda de idempotência: não cria job se já houver job do tipo
        correspondente em status pending/running para o mesmo dataset+projeto.

        Args:
            project: DaVinciProject do usuário autenticado.
            dataset: OmicDataset alvo.
            file_kind: Tipo de arquivo a baixar ('geo_supplementary' para F1,
                       'fastq' para F2). Mapeado para IngestionJob.JobType.
            user: User autenticado (usado para logar; ncbi_api_key é obtido
                  pela task para evitar armazená-la nos parâmetros do job).
            confirm: Confirmação explícita obrigatória para file_kind='fastq'.
                     Sem confirm=True, lança FastqConfirmRequiredError com
                     prévia de uso da quota.  Ignorado para GEO.

        Returns:
            IngestionJob criado (ou job ativo existente, se idempotência ativa).

        Raises:
            FastqConfirmRequiredError: FASTQ solicitado sem confirm=True.
            QuotaExceededError: quota de bytes do projeto excedida.
            ValueError: file_kind inválido.
            Exception: se o dispatch para o Celery falhar; job marcado FAILED.
        """
        from apps.core.tasks.ingestion_tasks import run_omics_download

        job_type = _file_kind_to_job_type(file_kind)

        # ── Gate de confirmação e quota (apenas FASTQ) ────────────────────────
        if file_kind == 'fastq':
            used_bytes = _project_used_bytes(project)
            quota_bytes = getattr(settings, 'DOWNLOAD_QUOTA_BYTES', 200 * 1024 ** 3)

            if not confirm:
                # Retorna prévia ao cliente — ele deve reenviar com confirm=true
                raise FastqConfirmRequiredError(
                    used_bytes=used_bytes,
                    quota_bytes=quota_bytes,
                )

            if used_bytes >= quota_bytes:
                # NUNCA logar used_bytes junto de identificadores pessoais do usuário
                logger.warning(
                    'QuotaExceededError: projeto %s atingiu/excedeu quota de download FASTQ',
                    project.id,
                )
                raise QuotaExceededError(
                    used_bytes=used_bytes,
                    quota_bytes=quota_bytes,
                )

        # ── Idempotência: não duplicar job ativo para mesmo dataset+projeto+kind ──
        existing = IngestionJob.objects.filter(
            project=project,
            job_type=job_type,
            status__in=[IngestionJob.JobStatus.PENDING, IngestionJob.JobStatus.RUNNING],
            parameters__dataset_id=dataset.id,
        ).first()
        if existing:
            logger.info(
                '%s já ativo (job %s) para projeto %s / dataset %s — dispatch ignorado (idempotência)',
                job_type,
                existing.id,
                project.id,
                dataset.accession,
            )
            return existing

        with transaction.atomic():
            job = IngestionJob.objects.create(
                project=project,
                job_type=job_type,
                status=IngestionJob.JobStatus.PENDING,
                parameters={
                    'dataset_id': dataset.id,
                    'dataset_accession': dataset.accession,
                    'source_db': dataset.source_db,
                    'file_kind': file_kind,
                    # ncbi_api_key NÃO é armazenado aqui — obtido pela task
                    # a partir de user.profile e settings (sensitive-data-handling)
                },
            )

        try:
            run_omics_download.delay(str(project.id), dataset.id, file_kind)
            logger.info(
                '%s disparado (job %s) para projeto %s / dataset %s',
                job_type,
                job.id,
                project.id,
                dataset.accession,
            )
        except Exception as exc:
            IngestionJob.objects.filter(id=job.id).update(
                status=IngestionJob.JobStatus.FAILED,
                error_message=f'Failed to dispatch Celery task: {exc}',
            )
            raise

        return job


def _project_used_bytes(project: DaVinciProject) -> int:
    """
    Soma os bytes de DatasetFile com download_status='downloaded' do projeto.

    Isolamento (firebase-auth-guard / Regra #3): filtra por projeto do usuário
    — o projeto já pertence ao request.user (garantido pelo _get_project() na
    view).  Jamais cruza projetos de usuários distintos.

    Não loga o valor retornado junto de dados pessoais (sensitive-data-handling).
    """
    # Cobre os dois vínculos possíveis (CheckConstraint XOR garante que cada
    # DatasetFile tem exatamente um deles preenchido — sem dupla contagem):
    #   - via dataset: GEO supplementary (DatasetFile.dataset → OmicDataset
    #                  → OmicDataset.in_projects → ProjectDataset.project)
    #   - via sample:  FASTQ (DatasetFile.sample → OmicSample.dataset →
    #                  OmicDataset.in_projects → ProjectDataset.project)
    result = DatasetFile.objects.filter(
        Q(dataset__in_projects__project=project)
        | Q(sample__dataset__in_projects__project=project),
        download_status=DatasetFile.DownloadStatus.DOWNLOADED,
    ).aggregate(total=Sum('size_bytes'))
    return result['total'] or 0


def _file_kind_to_job_type(file_kind: str) -> str:
    """Mapeia file_kind para IngestionJob.JobType."""
    mapping = {
        'geo_supplementary': IngestionJob.JobType.GEO_SUPPLEMENTARY_DOWNLOAD,
        'fastq': IngestionJob.JobType.FASTQ_DOWNLOAD,
    }
    if file_kind not in mapping:
        raise ValueError(
            f"file_kind inválido: {file_kind!r}. Valores suportados: {list(mapping)}"
        )
    return mapping[file_kind]
