"""
Management command — export_catalog

Snapshot versionado e reprodutível do catálogo de datasets OmnisPathway.

Entrega ponteiros + metadados + filtros do contrato §2 (nunca matriz numérica).
Acesso controlado (access_type=controlled) segue marcado via access_controlled=True
e matrix_pointer é sempre ponteiro — nunca conteúdo.

Determinismo:
  - Corpo ordenado por accession (ordem estável entre execuções).
  - Timestamp UTC fica SOMENTE no bloco de proveniência, fora do corpo ordenado.
  - Mesma entrada → corpo idêntico byte-a-byte.

Reusa apply_dataset_filters e DatasetExportItemSerializer (sem lógica duplicada).

Uso:
    manage.py export_catalog --snapshot-version v1 [opções]

Opções obrigatórias:
    --snapshot-version vN   Versão do snapshot (ex: v1, v2). Gravada na proveniência.

Opções de formato:
    --format {json,csv}     Formato de saída. Default: json.
                            CSV: .meta.json lado a lado com as contagens de proveniência.

Escopo:
    --project <uuid>        UUID do projeto. Sem este argumento: catálogo global
                            (todos os datasets ativos). Com ele: escopa ao projeto.

Filtros de descoberta (mesmos de apply_dataset_filters):
    --disease-axis <valor>                  (monogenic/multifactorial/mixed/indeterminate)
    --has-control-group <valor>             (yes/no/unknown)
    --is-single-cell <valor>               (single_cell/bulk/unknown)
    --access-type <valor>                   (public/controlled/unknown)
    --omics-layer <valor>                   containment (ex: proteomic)
    --omics-count-min <N>
    --omics-count-max <N>
    --has-sample-join-key                   boolean flag
    --disease-axis-confidence-min <float>
    --has-control-group-confidence-min <float>
    --is-single-cell-confidence-min <float>
    --sample-join-key-confidence-min <float>
    --gene <símbolo>                        iexact em DatasetGene.gene_symbol
    --monogenic-gene-hit                    boolean flag

Saída:
    --output <path>         Arquivo de saída. Default: diagnostics/exports/<nome_auto>.

Exemplos:
    # Snapshot global JSON v1
    manage.py export_catalog --snapshot-version v1

    # Snapshot filtrado por proteoma+genoma com controle, CSV
    manage.py export_catalog --snapshot-version v1 --format csv \\
        --omics-layer proteomic --has-control-group yes

    # Snapshot de projeto específico
    manage.py export_catalog --snapshot-version v1 --project 550e8400-e29b-41d4-a716-446655440000
"""

import json
import os
import logging
from datetime import datetime as _datetime, timezone as _timezone

from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)

# Diretório de saída default (criado com exist_ok=True se não existir)
_DEFAULT_EXPORT_DIR = os.path.join(
    os.path.dirname(  # commands/
        os.path.dirname(  # management/
            os.path.dirname(  # core/
                os.path.dirname(  # apps/
                    os.path.dirname(  # davinci/
                        os.path.abspath(__file__)
                    )
                )
            )
        )
    ),
    'diagnostics', 'exports',
)


class Command(BaseCommand):
    help = 'Snapshot versionado e reprodutível do catálogo de datasets OmnisPathway.'

    def add_arguments(self, parser):
        # Nota: --version é reservado pelo BaseCommand do Django (exibe versão do Django).
        # Usamos --snapshot-version para a versão do snapshot do catálogo.
        parser.add_argument(
            '--snapshot-version',
            required=True,
            metavar='VN',
            dest='snapshot_version',
            help='Versão do snapshot (ex: v1, v2). Gravada na proveniência.',
        )
        parser.add_argument(
            '--format',
            choices=['json', 'csv'],
            default='json',
            dest='output_format',
            help='Formato de saída: json (default) ou csv.',
        )
        parser.add_argument(
            '--project',
            metavar='UUID',
            default=None,
            help='UUID do projeto. Sem este argumento: catálogo global.',
        )
        parser.add_argument(
            '--output',
            metavar='PATH',
            default=None,
            help=(
                f'Arquivo de saída. Default: {_DEFAULT_EXPORT_DIR}/<nome_auto>. '
                'Para CSV, um arquivo .meta.json com proveniência é gerado lado a lado.'
            ),
        )
        # ── Filtros de descoberta ─────────────────────────────────────────────
        parser.add_argument('--disease-axis', metavar='VALOR', default=None)
        parser.add_argument('--has-control-group', metavar='VALOR', default=None)
        parser.add_argument('--is-single-cell', metavar='VALOR', default=None)
        parser.add_argument('--access-type', metavar='VALOR', default=None)
        parser.add_argument('--omics-layer', metavar='VALOR', default=None)
        parser.add_argument('--omics-count-min', type=int, metavar='N', default=None)
        parser.add_argument('--omics-count-max', type=int, metavar='N', default=None)
        parser.add_argument('--has-sample-join-key', action='store_true', default=False)
        parser.add_argument('--disease-axis-confidence-min', type=float, metavar='F', default=None)
        parser.add_argument('--has-control-group-confidence-min', type=float, metavar='F', default=None)
        parser.add_argument('--is-single-cell-confidence-min', type=float, metavar='F', default=None)
        parser.add_argument('--sample-join-key-confidence-min', type=float, metavar='F', default=None)
        parser.add_argument('--gene', metavar='SÍMBOLO', default=None)
        parser.add_argument('--monogenic-gene-hit', action='store_true', default=False)

    def handle(self, *args, **options):
        from apps.core.models import OmicDataset, ProjectDataset, DaVinciProject
        from apps.core.views.dataset_views import apply_dataset_filters
        from apps.core.serializers.dataset_export import DatasetExportItemSerializer

        snapshot_version = options['snapshot_version']
        output_format = options['output_format']
        project_uuid = options['project']
        output_path = options['output']

        # ── Constrói dict de filtros compatível com apply_dataset_filters ─────
        # apply_dataset_filters espera dict-like com .get(); dict nativo funciona.
        filter_params = {}
        if options['disease_axis']:
            filter_params['disease_axis'] = options['disease_axis']
        if options['has_control_group']:
            filter_params['has_control_group'] = options['has_control_group']
        if options['is_single_cell']:
            filter_params['is_single_cell'] = options['is_single_cell']
        if options['access_type']:
            filter_params['access_type'] = options['access_type']
        if options['omics_layer']:
            filter_params['omics_layer'] = options['omics_layer']
        if options['omics_count_min'] is not None:
            filter_params['omics_count_min'] = options['omics_count_min']
        if options['omics_count_max'] is not None:
            filter_params['omics_count_max'] = options['omics_count_max']
        if options['has_sample_join_key']:
            filter_params['has_sample_join_key'] = 'true'
        if options['disease_axis_confidence_min'] is not None:
            filter_params['disease_axis_confidence_min'] = options['disease_axis_confidence_min']
        if options['has_control_group_confidence_min'] is not None:
            filter_params['has_control_group_confidence_min'] = options['has_control_group_confidence_min']
        if options['is_single_cell_confidence_min'] is not None:
            filter_params['is_single_cell_confidence_min'] = options['is_single_cell_confidence_min']
        if options['sample_join_key_confidence_min'] is not None:
            filter_params['sample_join_key_confidence_min'] = options['sample_join_key_confidence_min']
        if options['gene']:
            filter_params['gene'] = options['gene']
        if options['monogenic_gene_hit']:
            filter_params['monogenic_gene_hit'] = 'true'

        # ── Queryset base ─────────────────────────────────────────────────────
        if project_uuid:
            try:
                project = DaVinciProject.objects.get(pk=project_uuid)
            except DaVinciProject.DoesNotExist:
                raise CommandError(f'Projeto não encontrado: {project_uuid}')
            qs = (
                ProjectDataset.objects.filter(project=project)
                .select_related('dataset')
                .prefetch_related('dataset__genes')
            )
            scope_label = f'projeto:{project_uuid}'
        else:
            # Catálogo global: todos os ProjectDatasets de todos os projetos ativos.
            # Acesso controlado segue marcado (access_controlled=True no serializer).
            # Nenhuma matriz entra — matrix_pointer é ponteiro.
            qs = (
                ProjectDataset.objects.filter(dataset__is_active=True)
                .select_related('dataset')
                .prefetch_related('dataset__genes')
            )
            scope_label = 'global'

        # ── Aplica filtros (motor único — sem lógica duplicada) ───────────────
        qs = apply_dataset_filters(qs, filter_params)

        # ── Ordenação determinística por accession (R4) ───────────────────────
        # Timestamp UTC fica SOMENTE no bloco de proveniência, fora do corpo.
        qs = qs.order_by('dataset__accession')

        serializer_context = {'snapshot_version': snapshot_version}

        # ── Materializa e serializa ───────────────────────────────────────────
        # chunk_size obrigatório com prefetch_related + iterator() no Django 4+.
        items = []
        for pd in qs.iterator(chunk_size=200):
            s = DatasetExportItemSerializer(pd, context=serializer_context)
            items.append(s.data)

        total_count = len(items)

        # ── Contagens de proveniência ─────────────────────────────────────────
        by_disease_axis: dict[str, int] = {}
        by_access_type: dict[str, int] = {}
        with_sample_join_key = 0

        for item in items:
            axis = item.get('disease_axis') or 'indeterminate'
            by_disease_axis[axis] = by_disease_axis.get(axis, 0) + 1

            at = item.get('access_type') or 'unknown'
            by_access_type[at] = by_access_type.get(at, 0) + 1

            sjk = item.get('sample_join_key')
            if sjk:
                with_sample_join_key += 1

        # Timestamp UTC — só na proveniência, nunca embutido no corpo ordenado
        timestamp_utc = _datetime.now(_timezone.utc).isoformat()

        provenance = {
            'timestamp_utc': timestamp_utc,
            'snapshot_version': snapshot_version,
            'scope': scope_label,
            'filters_applied': filter_params,
            'classifier_rules_version': 'omnispathway-fase2-fase3',
            'counts': {
                'total': total_count,
                'by_disease_axis': by_disease_axis,
                'by_access_type': by_access_type,
                'with_sample_join_key': with_sample_join_key,
            },
        }

        # ── Resolve path de saída ─────────────────────────────────────────────
        os.makedirs(_DEFAULT_EXPORT_DIR, exist_ok=True)

        if output_path is None:
            scope_slug = project_uuid[:8] if project_uuid else 'global'
            ext = output_format
            filename = f'catalog_{snapshot_version}_{scope_slug}.{ext}'
            output_path = os.path.join(_DEFAULT_EXPORT_DIR, filename)

        # ── Serializa para o formato escolhido ───────────────────────────────
        if output_format == 'json':
            payload = {
                'provenance': provenance,
                'results': [dict(item) for item in items],
            }
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, default=str)

            self.stdout.write(self.style.SUCCESS(
                f'Export JSON gravado em: {output_path} '
                f'({total_count} datasets, versão {snapshot_version})'
            ))

        else:  # csv
            import csv as _csv
            import io as _io

            COLUMNS = [
                'accession', 'source_db', 'omics_layers', 'omics_count',
                'is_single_cell', 'has_control_group', 'disease_axis',
                'data_format', 'access_type', 'access_controlled',
                'sample_join_key',
                'contract_confidence',
                'contract.matrix_pointer', 'contract.proteomics_modality',
                'contract.tissue_raw', 'contract.disease_raw',
                'contract.ref_pmids', 'contract.ref_dois',
                'contract.monogenic_gene_hit',
                'snapshot_version',
            ]

            def _join(val):
                if val is None:
                    return ''
                if isinstance(val, list):
                    return ';'.join(str(v) for v in val)
                if isinstance(val, dict):
                    return ';'.join(f'{k}={v}' for k, v in val.items())
                return str(val)

            with open(output_path, 'w', newline='', encoding='utf-8') as f:
                writer = _csv.writer(f)
                writer.writerow(COLUMNS)
                for item in items:
                    contract = item.get('contract') or {}
                    cc = item.get('contract_confidence') or {}
                    writer.writerow([
                        item.get('accession', ''),
                        item.get('source_db', ''),
                        _join(item.get('omics_layers')),
                        item.get('omics_count', ''),
                        item.get('is_single_cell', ''),
                        item.get('has_control_group', ''),
                        item.get('disease_axis', ''),
                        item.get('data_format', ''),
                        item.get('access_type', ''),
                        item.get('access_controlled', ''),
                        _join(item.get('sample_join_key')),
                        _join(cc),
                        contract.get('matrix_pointer', ''),
                        contract.get('proteomics_modality', ''),
                        _join(contract.get('tissue_raw')),
                        _join(contract.get('disease_raw')),
                        _join(contract.get('ref_pmids')),
                        _join(contract.get('ref_dois')),
                        _join(contract.get('monogenic_gene_hit')),
                        item.get('snapshot_version', ''),
                    ])

            # Para CSV limpo: proveniência em arquivo .meta.json lado a lado
            meta_path = output_path.rsplit('.', 1)[0] + '.meta.json'
            with open(meta_path, 'w', encoding='utf-8') as f:
                json.dump(provenance, f, ensure_ascii=False, indent=2, default=str)

            self.stdout.write(self.style.SUCCESS(
                f'Export CSV gravado em: {output_path} '
                f'({total_count} datasets, versão {snapshot_version})'
            ))
            self.stdout.write(f'Proveniência: {meta_path}')

        # ── Sumário na stdout ─────────────────────────────────────────────────
        self.stdout.write(f'Proveniência:')
        self.stdout.write(f'  timestamp_utc:             {timestamp_utc}')
        self.stdout.write(f'  snapshot_version:          {snapshot_version}')
        self.stdout.write(f'  scope:                     {scope_label}')
        self.stdout.write(f'  total:                     {total_count}')
        self.stdout.write(f'  por disease_axis:          {by_disease_axis}')
        self.stdout.write(f'  por access_type:           {by_access_type}')
        self.stdout.write(f'  com sample_join_key:       {with_sample_join_key}')
        self.stdout.write(f'  regras_classificador:      omnispathway-fase2-fase3')
