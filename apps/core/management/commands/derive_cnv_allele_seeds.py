"""
Management command: derive_cnv_allele_seeds

Refino CNV×alelo (Fase 2, Objetivo 2, slice 2.7). Cruza os seeds CNV v1
(dosagem×papel) com os seeds SNV (alelo) por (amostra, gene) e grava seeds
CNV v2 (fase2-cnv-v2) que coexistem com o v1.

Pipeline (em ordem):
    manage.py load_gene_roles      --project <uuid>
    manage.py load_cnv_matrix      --project <uuid>
    manage.py derive_cnv_seeds     --project <uuid>   # cnv-v1
    manage.py load_variant_effects --project <uuid>   # VariantEffectRaw
    manage.py resolve_variant_effects                 # VariantEffectResolved
    manage.py load_somatic_maf     --project <uuid>   # snv seeds
    manage.py derive_cnv_allele_seeds --project <uuid>  # cnv-v2 (este)

Derivação set-based rápida (sem IngestionJob, como resolve_variant_effects).
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.core.models import DaVinciProject
from apps.core.services.cnv_allele_service import (
    CnvAlleleMatrixNotFoundError,
    CnvAllelePrerequisiteError,
    CnvAlleleService,
)


class Command(BaseCommand):
    help = (
        'Refina o seed CNV v1 (dosagem×papel) para o estado conjunto CNV×alelo '
        '(fase2-cnv-v2), cruzando com o seed SNV. Requer cnv-v1 + snv seeds.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--project',
            required=True,
            help='UUID do DaVinciProject (isolamento).',
        )
        parser.add_argument(
            '--matrix',
            type=int,
            default=None,
            help='ID específico da OmicMatrix copy_number. Default: auto-detecta.',
        )

    def handle(self, *args, **options):
        project_id = options['project']
        matrix_id = options['matrix']

        try:
            project = DaVinciProject.objects.get(id=project_id)
        except (DaVinciProject.DoesNotExist, ValueError, ValidationError):
            raise CommandError(f'DaVinciProject id={project_id} não encontrado.')

        service = CnvAlleleService(project)
        try:
            result = service.run(matrix_id=matrix_id)
        except CnvAlleleMatrixNotFoundError as exc:
            raise CommandError(str(exc))
        except CnvAllelePrerequisiteError as exc:
            raise CommandError(str(exc))

        self.stdout.write(self.style.SUCCESS('\nCNV×alelo v2 (fase2-cnv-v2) concluido'))
        self.stdout.write(f'  matrix_id                       : {result["matrix_id"]}')
        self.stdout.write(f'  method_version                  : {result["method_version"]}')
        self.stdout.write(f'  cnv-v1 seeds (entrada)          : {result["n_cnv_v1_seeds"]}')
        self.stdout.write(f'  v2 seeds gravados               : {result["n_v2_created"]}')
        self.stdout.write('  --- distribuicao por caso ---')
        self.stdout.write(f'  inativacao bialelica (reforco)  : {result["n_biallelic_reinforced"]}')
        self.stdout.write(f'  amplif. de alelo danoso (neutro): {result["n_amplified_damaged_neutralized"]}')
        self.stdout.write(f'  perda monoalelica               : {result["n_monoallelic"]}')
        self.stdout.write(f'  amplificacao limpa (herdado)    : {result["n_activator_inherited"]}')
        self.stdout.write('  --- direction final ---')
        self.stdout.write(f'  activator                       : {result["n_activator"]}')
        self.stdout.write(f'  inactivator                     : {result["n_inactivator"]}')
        self.stdout.write(f'  neutral                         : {result["n_neutral"]}')
