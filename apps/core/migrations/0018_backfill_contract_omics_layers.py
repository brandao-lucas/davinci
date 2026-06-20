# Backfill dos campos de contrato derivados de omic_type (OmnisPathway Fase 0).
# Idempotente e reversível. Apenas deriva omics_layers / omics_count;
# os demais eixos (access_type, disease_axis, has_control_group,
# sample_join_key, etc.) ficam nos defaults — serão refinados nas Fases 2/3.
from django.db import migrations


# Mapa de tokens omic_type -> camada canônica (minúscula), alinhado ao
# OMICS_LAYER_VOCAB do model. MULTI_OMIC e OTHER são propositalmente omitidos:
# não há contagem confiável de camadas para eles.
_OMIC_TYPE_TO_LAYER = {
    'genomic': 'genomic',
    'transcriptomic': 'transcriptomic',
    'proteomic': 'proteomic',
    'metabolomic': 'metabolomic',
    'epigenomic': 'epigenomic',
    'metagenomic': 'metagenomic',
    'microbiome': 'microbiome',
}


def _derive_layers(omic_type: str):
    """Normaliza uma string omic_type (possivelmente comma-separated, ex.
    'transcriptomic,genomic') em camadas canônicas distintas, preservando a
    ordem de primeira aparição."""
    if not omic_type:
        return []
    layers = []
    for raw in omic_type.split(','):
        token = raw.strip().lower()
        layer = _OMIC_TYPE_TO_LAYER.get(token)
        if layer and layer not in layers:
            layers.append(layer)
    return layers


def backfill_contract(apps, schema_editor):
    OmicDataset = apps.get_model('core', 'OmicDataset')
    for ds in OmicDataset.objects.all().iterator():
        layers = _derive_layers(ds.omic_type or '')
        if not layers:
            # MULTI_OMIC, OTHER, vazio ou não mapeável: mantém defaults
            # (omics_layers=[], omics_count=NULL). Idempotente.
            continue
        ds.omics_layers = layers
        ds.omics_count = len(layers)
        ds.save(update_fields=['omics_layers', 'omics_count'])


def reverse_backfill(apps, schema_editor):
    """Zera os campos derivados (volta aos defaults: [] e NULL)."""
    OmicDataset = apps.get_model('core', 'OmicDataset')
    OmicDataset.objects.update(omics_layers=[], omics_count=None)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0017_omicdataset_contract_fields'),
    ]

    operations = [
        migrations.RunPython(backfill_contract, reverse_backfill),
    ]
