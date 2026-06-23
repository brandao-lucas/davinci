"""
Serializers para o fluxo de download de arquivos ômicos.

Regra de segurança (sensitive-data-handling):
- `storage_key` NUNCA é exposto ao cliente.
- `download_url` é uma URL de proxy autenticado gerada por reverse()
  (decisão D6 = proxy autenticado Django). O cliente usa essa URL para
  obter o conteúdo; o Django valida isolamento por usuário antes de servir.
"""

from __future__ import annotations

from rest_framework import serializers
from rest_framework.reverse import reverse
from drf_spectacular.utils import extend_schema_field

from apps.core.models import DatasetFile, IngestionJob


class DatasetFileSerializer(serializers.ModelSerializer):
    """
    Serializer read-only de DatasetFile.

    Campos expostos:
        id, file_type, source, size_bytes, checksum_md5, checksum_algo,
        download_status, bytes_downloaded, downloaded_at, accession,
        download_url (URL de proxy autenticado — nunca storage_key cru).

    Campos NUNCA expostos: storage_key, remote_url, dataset_id, sample_id.
    """

    @extend_schema_field({'type': 'string', 'format': 'uri', 'nullable': True})
    def get_download_url(self, obj: DatasetFile) -> str | None:
        """
        Retorna a URL do endpoint de proxy autenticado para este arquivo.

        Somente arquivos com download_status='downloaded' têm URL válida;
        os demais retornam None para indicar que o conteúdo ainda não está
        disponível.

        A URL aponta para:
          GET /projects/{project_pk}/datasets/{dataset_pk}/files/{file_id}/content/

        O project_pk e dataset_pk são obtidos do contexto da view (kwargs),
        garantindo que o endpoint de proxy valida isolamento antes de servir.
        Nunca inclui storage_key nem URL pública do MinIO.
        """
        if obj.download_status != DatasetFile.DownloadStatus.DOWNLOADED:
            return None

        request = self.context.get('request')
        view = self.context.get('view')
        if not request or not view:
            return None

        project_pk = view.kwargs.get('project_pk')
        dataset_pk = view.kwargs.get('pk')
        if not project_pk or not dataset_pk:
            return None

        return reverse(
            'project-dataset-file-content',
            kwargs={'project_pk': project_pk, 'pk': dataset_pk, 'file_id': obj.id},
            request=request,
        )

    download_url = serializers.SerializerMethodField()

    class Meta:
        model = DatasetFile
        fields = [
            'id',
            'accession',
            'file_type',
            'source',
            'size_bytes',
            'checksum_md5',
            'checksum_algo',
            'download_status',
            'bytes_downloaded',
            'downloaded_at',
            'download_url',
            # storage_key e remote_url são explicitamente excluídos
        ]
        read_only_fields = fields


class SampleFilterSerializer(serializers.Serializer):
    """
    Filtros de amostra para scope='filter' em DownloadDispatchRequestSerializer.

    Todos os campos são opcionais e combinados com AND. Pelo menos um campo
    deve ser fornecido (validado na view / apply_sample_filters).

    Filtros suportados:
    - curation_status (list[str]): valores de ProjectSample.CurationStatus
      (pending/included/excluded/maybe). Filtra amostras cujo ProjectSample
      no projeto do usuário tem o status indicado (IN list).
    - organism (str): icontains no campo OmicSample.organism.
    - platform (str): icontains no campo OmicSample.platform.
    """

    curation_status = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        allow_null=True,
        default=None,
        help_text=(
            "Lista de status de curadoria de amostra (in). "
            "Valores: 'pending', 'included', 'excluded', 'maybe'. "
            "Ex.: [\"included\", \"maybe\"]."
        ),
    )
    organism = serializers.CharField(
        required=False,
        allow_null=True,
        default=None,
        help_text="Filtro parcial (icontains) no campo OmicSample.organism.",
    )
    platform = serializers.CharField(
        required=False,
        allow_null=True,
        default=None,
        help_text="Filtro parcial (icontains) no campo OmicSample.platform.",
    )


class DownloadDispatchRequestSerializer(serializers.Serializer):
    """
    Body do POST .../download/.

    Campos obrigatórios/opcionais:
    - `file_kind`: se omitido, derivado de source_db do dataset.
    - `confirm`: obrigatório apenas para file_kind='fastq' (F2, GB–TB).
    - `scope`: escopo de amostras a baixar:
        - 'all' (padrão): todas as runs do dataset (comportamento original).
        - 'included': apenas amostras com ProjectSample.curation_status='included'
          no projeto do usuário autenticado.
        - 'manual': exatamente os OmicSample ids informados em sample_ids.
        - 'filter' (Inc-2): amostras que casam o objeto `filters` (curation_status,
          organism, platform). Requer campo `filters` preenchido.
    - `sample_ids`: lista de ProjectSample.id; obrigatório se scope='manual'.
      Cada id é validado contra ProjectSample do projeto do user
      (firebase-auth-guard / Regra #3 — ids de outro projeto → 403/404).
    - `filters`: objeto SampleFilterSerializer; obrigatório se scope='filter'.
    - `destination`: destino do download; 'server' (padrão) enfileira job Celery;
      'client' resolve URLs FASTQ públicas de forma síncrona e retorna 200 com a lista.

    Campos NUNCA expostos: nenhum dado sensível — só intenção do usuário.
    """

    FILE_KIND_CHOICES = ['geo_supplementary', 'fastq']
    SCOPE_CHOICES = ['all', 'included', 'manual', 'filter']
    DESTINATION_CHOICES = ['server', 'client']

    file_kind = serializers.ChoiceField(
        choices=FILE_KIND_CHOICES,
        required=False,
        allow_null=True,
        default=None,
        help_text=(
            "Tipo de arquivo a baixar. Se omitido, derivado de source_db do dataset: "
            "'geo' → 'geo_supplementary'; 'sra' → 'fastq'."
        ),
    )
    confirm = serializers.BooleanField(
        required=False,
        default=False,
        help_text=(
            "Confirmação explícita obrigatória para file_kind='fastq' (F2). "
            "Sem confirm=true, retorna HTTP 400 com prévia de uso da quota. "
            "Ignorado para GEO supplementary."
        ),
    )
    scope = serializers.ChoiceField(
        choices=SCOPE_CHOICES,
        required=False,
        default='all',
        help_text=(
            "Escopo de amostras a baixar: "
            "'all' (padrão) = todas as runs do dataset; "
            "'included' = só amostras com curation_status='included' no projeto; "
            "'manual' = exatamente os ids em sample_ids; "
            "'filter' = amostras que casam o objeto filters (curation_status, organism, platform)."
        ),
    )
    sample_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        allow_null=True,
        default=None,
        help_text=(
            "Lista de ProjectSample.id para scope='manual' — mesmo padrão de id "
            "exposto por ProjectSampleListSerializer e usado em bulk_curate de samples. "
            "Obrigatório quando scope='manual'. "
            "A view valida pertencimento ao projeto/dataset do usuário autenticado e "
            "resolve internamente para OmicSample.id antes de repassar ao Rust — "
            "ids de outro projeto resultam em 404 (firebase-auth-guard, Regra #3)."
        ),
    )
    filters = SampleFilterSerializer(
        required=False,
        allow_null=True,
        default=None,
        help_text=(
            "Filtros de amostra para scope='filter'. Obrigatório quando scope='filter'. "
            "Campos: curation_status (list, in), organism (icontains), platform (icontains). "
            "Todos os campos são opcionais e combinados com AND."
        ),
    )
    destination = serializers.ChoiceField(
        choices=DESTINATION_CHOICES,
        required=False,
        default='server',
        help_text=(
            "Destino do download: "
            "'server' (padrão) = enfileira job Celery, salva no object storage (202). "
            "'client' (Inc-1) = resolve URLs públicas ENA/NCBI e retorna a lista na "
            "resposta (200); sem criação de job, sem quota, sem DatasetFile."
        ),
    )

    def validate(self, attrs):
        """Validações cross-field do body de download."""
        scope = attrs.get('scope', 'all')
        sample_ids = attrs.get('sample_ids')
        filters = attrs.get('filters')

        if scope == 'manual' and not sample_ids:
            raise serializers.ValidationError(
                {'sample_ids': "sample_ids é obrigatório quando scope='manual'."}
            )

        if scope == 'filter' and not filters:
            raise serializers.ValidationError(
                {'filters': "filters é obrigatório quando scope='filter'."}
            )

        return attrs


# ── Inc-1: destination='client' — URLs públicas direto pro browser ───────────


class FastqUrlItemSerializer(serializers.Serializer):
    """
    Um item da lista de URLs FASTQ retornada por destination='client'.

    Campos:
    - `run_accession`: accession SRA do run (SRR*/ERR*/DRR*).
    - `fastq_url`: URL(s) públicas FASTQ na ENA (HTTPS, separadas por ';' se multi-arquivo,
      ex. R1+R2). Vazio se o run é BAM-only na ENA (sem FASTQ publicado).
    - `file_name`: nome(s) de arquivo correspondente(s), separados por ';'.
    - `size_bytes`: tamanho total declarado pela ENA (soma de todos os arquivos do run);
      null se não disponível.
    - `checksum_md5`: MD5(s) declarados pela ENA, separados por ';'; null se não disponível.
    - `has_public_fastq`: true se fastq_url não vazio — indica que o browser pode baixar
      diretamente. false indica run BAM-only (sem FASTQ na ENA); use destination='server'
      com sra-tools fallback para esses runs.

    Campos NUNCA expostos: db_url, ncbi_api_key, storage_key (sensitive-data-handling).
    """

    run_accession = serializers.CharField(
        read_only=True,
        help_text="Accession SRA do run (SRR*/ERR*/DRR*).",
    )
    fastq_url = serializers.CharField(
        read_only=True,
        allow_blank=True,
        help_text=(
            "URL(s) FASTQ públicas na ENA (HTTPS). Separadas por ';' para runs "
            "multi-arquivo (R1/R2). Vazio se o run não tem FASTQ publicado na ENA (BAM-only)."
        ),
    )
    file_name = serializers.CharField(
        read_only=True,
        allow_blank=True,
        help_text="Nome(s) de arquivo correspondente(s), separados por ';'.",
    )
    size_bytes = serializers.IntegerField(
        read_only=True,
        allow_null=True,
        help_text="Tamanho total declarado pela ENA em bytes; null se não disponível.",
    )
    checksum_md5 = serializers.CharField(
        read_only=True,
        allow_null=True,
        allow_blank=True,
        help_text="MD5(s) declarados pela ENA, separados por ';'; null se não disponível.",
    )
    has_public_fastq = serializers.BooleanField(
        read_only=True,
        help_text=(
            "true se fastq_url não vazio (browser pode baixar diretamente). "
            "false indica run BAM-only sem FASTQ na ENA."
        ),
    )


class FastqUrlListResponseSerializer(serializers.Serializer):
    """
    Resposta HTTP 200 de destination='client' (single dataset).

    Campos:
    - `dataset_id`: OmicDataset.id.
    - `dataset_accession`: accession do dataset (GSE*, SRP*, etc.).
    - `runs`: lista de FastqUrlItem.
    - `total_runs`: número total de runs na resposta.
    - `bytes_total`: soma de size_bytes dos runs (null para runs sem tamanho declarado).
    - `bam_only_count`: nº de runs sem FASTQ publicado na ENA (has_public_fastq=false).

    Teto de runs por resposta: CLIENT_DOWNLOAD_MAX_RUNS (padrão: 200).
    Se a seleção exceder o teto, retorna HTTP 400 com mensagem orientativa.
    """

    dataset_id = serializers.IntegerField(read_only=True)
    dataset_accession = serializers.CharField(read_only=True)
    runs = FastqUrlItemSerializer(many=True, read_only=True)
    total_runs = serializers.IntegerField(read_only=True)
    bytes_total = serializers.IntegerField(
        read_only=True,
        allow_null=True,
        help_text="Soma de size_bytes de todos os runs; null se algum run não tem tamanho.",
    )
    bam_only_count = serializers.IntegerField(
        read_only=True,
        help_text="Número de runs sem FASTQ publicado na ENA (has_public_fastq=false).",
    )


class BatchFastqUrlListResponseSerializer(serializers.Serializer):
    """
    Resposta HTTP 200 de destination='client' para download-batch.

    Agrega FastqUrlListResponseSerializer por dataset.

    Campos:
    - `datasets`: lista de FastqUrlListResponse (um por dataset alvo).
    - `total_datasets`: número de datasets na resposta.
    - `total_runs`: soma dos total_runs de cada dataset.
    - `bytes_total`: soma agregada de size_bytes (null se algum run não tem tamanho).
    - `skipped_datasets`: accessions ignorados (source não suportada, sem amostras, etc.).
    """

    datasets = FastqUrlListResponseSerializer(many=True, read_only=True)
    total_datasets = serializers.IntegerField(read_only=True)
    total_runs = serializers.IntegerField(read_only=True)
    bytes_total = serializers.IntegerField(
        read_only=True,
        allow_null=True,
        help_text="Soma agregada de size_bytes; null se algum run não tem tamanho declarado.",
    )
    skipped_datasets = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
        help_text="Accessions ignorados (fonte não suportada, sem amostras, erro de resolução).",
    )


class DownloadQuotaPreviewSerializer(serializers.Serializer):
    """
    Resposta de erro de quota (HTTP 400 — confirm ausente; HTTP 409 — quota excedida).

    Campos:
    - `detail`: mensagem legível explicando o motivo do bloqueio.
    - `file_kind`: tipo de arquivo solicitado.
    - `used_bytes`: bytes já baixados no projeto (status='downloaded').
    - `quota_bytes`: limite configurado (DOWNLOAD_QUOTA_BYTES).
    - `confirm_required`: true se o bloqueio é por falta de confirm (400);
      false se é por quota esgotada (409).

    Não expõe storage_key nem credenciais (sensitive-data-handling).
    """

    detail = serializers.CharField(read_only=True)
    file_kind = serializers.CharField(read_only=True)
    used_bytes = serializers.IntegerField(read_only=True)
    quota_bytes = serializers.IntegerField(read_only=True)
    confirm_required = serializers.BooleanField(read_only=True)


class DownloadDispatchResponseSerializer(serializers.ModelSerializer):
    """Resposta do POST .../download/ — retorna o IngestionJob criado/ativo."""

    class Meta:
        model = IngestionJob
        fields = [
            'id',
            'job_type',
            'status',
            'records_processed',
            'records_inserted',
            'error_message',
            'created_at',
        ]
        read_only_fields = fields


class SraResolutionResponseSerializer(serializers.ModelSerializer):
    """
    Resposta do POST .../resolve-sra/ — retorna o IngestionJob de resolução GSM→SRR.

    O job pode ser monitorado via GET /projects/{project_pk}/jobs/{id}/.
    Campos NUNCA expostos: db_url, ncbi_api_key (sensitive-data-handling).
    """

    class Meta:
        model = IngestionJob
        fields = [
            'id',
            'job_type',
            'status',
            'records_processed',
            'records_inserted',
            'error_message',
            'created_at',
        ]
        read_only_fields = fields


# ── Inc-5: batch download por projeto ────────────────────────────────────────


class BatchDownloadRequestSerializer(serializers.Serializer):
    """
    Body do POST .../datasets/download-batch/.

    Campos:
    - `dataset_ids`: lista de OmicDataset.id a incluir no lote. Opcional —
      quando ausente/null, todos os datasets do projeto com amostras incluídas
      são usados (conforme `scope`).
    - `scope`: escopo de amostras por dataset:
        - 'included' (padrão): amostras com ProjectSample.curation_status='included'.
        - 'all': todas as amostras de cada dataset.
    - `confirm`: obrigatório para downloads FASTQ (gate de quota, mesmo que single).
    - `destination`: 'server' (padrão) enfileira jobs Celery; 'client' resolve URLs
      FASTQ públicas de forma síncrona por dataset e retorna 200 com lista agrupada.

    Campos NUNCA expostos: nenhum dado sensível — só intenção do usuário.
    """

    SCOPE_CHOICES = ['included', 'all']
    DESTINATION_CHOICES = ['server', 'client']

    dataset_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        allow_null=True,
        default=None,
        help_text=(
            "Lista de OmicDataset.id a incluir no lote. "
            "Quando omitido, todos os datasets do projeto com amostras incluídas são usados."
        ),
    )
    scope = serializers.ChoiceField(
        choices=SCOPE_CHOICES,
        required=False,
        default='included',
        help_text=(
            "'included' (padrão) = só amostras com curation_status='included' em cada dataset; "
            "'all' = todas as amostras de cada dataset."
        ),
    )
    confirm = serializers.BooleanField(
        required=False,
        default=False,
        help_text=(
            "Confirmação explícita obrigatória para downloads FASTQ (F2, GB–TB). "
            "Sem confirm=true, retorna HTTP 400 com prévia agregada de quota. "
            "Ignorado para GEO supplementary."
        ),
    )
    destination = serializers.ChoiceField(
        choices=DESTINATION_CHOICES,
        required=False,
        default='server',
        help_text=(
            "'server' (padrão) = enfileira jobs Celery, salva no object storage. "
            "'client' = resolve URLs FASTQ de forma síncrona, retorna 200 com lista agrupada."
        ),
    )

    def validate(self, attrs):
        dataset_ids = attrs.get('dataset_ids')
        if dataset_ids is not None and len(dataset_ids) == 0:
            raise serializers.ValidationError(
                {'dataset_ids': "dataset_ids não pode ser lista vazia."}
            )
        return attrs


class BatchDownloadJobSerializer(serializers.ModelSerializer):
    """Resumo de um IngestionJob criado no lote — para o frontend monitorar."""

    dataset_id = serializers.SerializerMethodField()
    dataset_accession = serializers.SerializerMethodField()

    @extend_schema_field({'type': 'integer', 'nullable': True})
    def get_dataset_id(self, obj) -> int | None:
        return obj.parameters.get('dataset_id')

    @extend_schema_field({'type': 'string', 'nullable': True})
    def get_dataset_accession(self, obj) -> str | None:
        return obj.parameters.get('dataset_accession')

    class Meta:
        model = IngestionJob
        fields = [
            'id',
            'job_type',
            'status',
            'dataset_id',
            'dataset_accession',
            'created_at',
        ]
        read_only_fields = fields


class BatchDownloadResponseSerializer(serializers.Serializer):
    """
    Resposta 202 do POST .../datasets/download-batch/.

    Campos:
    - `jobs`: lista de jobs criados (um por dataset alvo).
    - `total_jobs`: número total de jobs despachados.
    - `skipped_datasets`: datasets ignorados (sem amostras resolvíveis no scope).
    """

    jobs = BatchDownloadJobSerializer(many=True, read_only=True)
    total_jobs = serializers.IntegerField(read_only=True)
    skipped_datasets = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
        help_text="Accessions de datasets ignorados (sem amostras no scope ou job já ativo).",
    )


class BatchDownloadQuotaPreviewSerializer(serializers.Serializer):
    """
    Prévia de quota para o lote (HTTP 400 sem confirm; HTTP 409 quota excedida).

    Campos:
    - `detail`: mensagem legível.
    - `used_bytes`: bytes já baixados no projeto.
    - `quota_bytes`: limite configurado (DOWNLOAD_QUOTA_BYTES).
    - `estimated_bytes`: soma estimada do lote (SampleSraRun.size_bytes disponíveis).
    - `confirm_required`: true se bloqueio por falta de confirm (400).
    - `datasets_preview`: lista de {dataset_accession, sample_count} para UX.
    """

    detail = serializers.CharField(read_only=True)
    used_bytes = serializers.IntegerField(read_only=True)
    quota_bytes = serializers.IntegerField(read_only=True)
    estimated_bytes = serializers.IntegerField(read_only=True)
    confirm_required = serializers.BooleanField(read_only=True)
    datasets_preview = serializers.ListField(
        child=serializers.DictField(),
        read_only=True,
        help_text="[{dataset_accession, sample_count}] — prévia dos datasets afetados.",
    )
