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


class DownloadDispatchRequestSerializer(serializers.Serializer):
    """
    Body do POST .../download/.

    Campos obrigatórios/opcionais:
    - `file_kind`: se omitido, derivado de source_db do dataset.
    - `confirm`: obrigatório apenas para file_kind='fastq' (F2, GB–TB).
    - `scope`: escopo de amostras a baixar (MVP-A):
        - 'all' (padrão): todas as runs do dataset (comportamento original).
        - 'included': apenas amostras com ProjectSample.curation_status='included'
          no projeto do usuário autenticado.
        - 'manual': exatamente os OmicSample ids informados em sample_ids.
        ('filter' é Inc-2 — não implementado neste MVP.)
    - `sample_ids`: lista de OmicSample.id; obrigatório se scope='manual'.
      Cada id é validado contra ProjectSample do projeto do user
      (firebase-auth-guard / Regra #3 — ids de outro projeto → 403/404).
    - `destination`: destino do download; 'server' (padrão) já implementado;
      'client' será implementado no Inc-1 — retorna 400 neste MVP.

    Campos NUNCA expostos: nenhum dado sensível — só intenção do usuário.
    """

    FILE_KIND_CHOICES = ['geo_supplementary', 'fastq']
    SCOPE_CHOICES = ['all', 'included', 'manual']
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
            "'manual' = exatamente os ids em sample_ids."
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
    destination = serializers.ChoiceField(
        choices=DESTINATION_CHOICES,
        required=False,
        default='server',
        help_text=(
            "Destino do download: "
            "'server' (padrão) = enfileira job Celery, salva no object storage. "
            "'client' = Inc-1, não implementado neste MVP (retorna HTTP 400)."
        ),
    )

    def validate(self, attrs):
        """Validações cross-field do body de download."""
        scope = attrs.get('scope', 'all')
        sample_ids = attrs.get('sample_ids')
        destination = attrs.get('destination', 'server')

        if scope == 'manual' and not sample_ids:
            raise serializers.ValidationError(
                {'sample_ids': "sample_ids é obrigatório quando scope='manual'."}
            )

        if destination == 'client':
            raise serializers.ValidationError(
                {'destination': "destination='client' não está implementado neste MVP (Inc-1)."}
            )

        return attrs


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
