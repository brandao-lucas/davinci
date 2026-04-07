from django.core.management.base import BaseCommand

from apps.core.models import ClinicalCategory, OmicCategory


class Command(BaseCommand):
    help = 'Popula ClinicalCategory e OmicCategory com dados padrão'

    def handle(self, *args, **options):
        self._seed_clinical_categories()
        self._seed_omic_categories()

    def _seed_clinical_categories(self):
        categories = [
            {
                'slug': 'diagnosis',
                'name': 'Diagnóstico',
                'description': 'Papers relacionados a diagnóstico, biomarcadores, detecção e screening',
                'keywords': [
                    'diagnosis', 'diagnostic', 'biomarker', 'detection', 'screening',
                    'sensitivity', 'specificity', 'predictive value', 'ROC curve',
                    'early detection', 'prognosis', 'prognostic', 'staging',
                    'imaging', 'biopsy', 'assay', 'marker', 'indicator',
                ],
                'is_default': True,
                'priority': 1,
            },
            {
                'slug': 'treatment',
                'name': 'Tratamento',
                'description': 'Papers sobre tratamentos, terapias, intervenções e ensaios clínicos',
                'keywords': [
                    'treatment', 'therapy', 'therapeutic', 'drug', 'intervention',
                    'clinical trial', 'randomized', 'placebo', 'efficacy', 'dose',
                    'response', 'remission', 'surgery', 'surgical', 'chemotherapy',
                    'immunotherapy', 'radiation', 'transplant', 'pharmacological',
                ],
                'is_default': True,
                'priority': 2,
            },
            {
                'slug': 'epidemiology',
                'name': 'Epidemiologia',
                'description': 'Papers sobre prevalência, incidência, fatores de risco e saúde pública',
                'keywords': [
                    'epidemiology', 'prevalence', 'incidence', 'risk factor',
                    'cohort', 'case-control', 'population', 'mortality', 'morbidity',
                    'survival', 'odds ratio', 'hazard ratio', 'relative risk',
                    'cross-sectional', 'longitudinal', 'public health', 'burden',
                ],
                'is_default': True,
                'priority': 3,
            },
            {
                'slug': 'mechanism',
                'name': 'Mecanismo',
                'description': 'Papers sobre mecanismos moleculares, patogênese e biologia',
                'keywords': [
                    'mechanism', 'pathway', 'signaling', 'molecular', 'cellular',
                    'pathogenesis', 'pathophysiology', 'gene expression', 'regulation',
                    'transcription', 'translation', 'mutation', 'polymorphism',
                    'protein', 'receptor', 'ligand', 'kinase', 'apoptosis',
                    'inflammation', 'immune response', 'oxidative stress',
                ],
                'is_default': True,
                'priority': 4,
            },
            {
                'slug': 'signs_symptoms',
                'name': 'Sinais e Sintomas',
                'description': 'Papers sobre manifestações clínicas, fenótipos e apresentação',
                'keywords': [
                    'signs', 'symptoms', 'clinical presentation', 'manifestation',
                    'phenotype', 'complication', 'comorbidity', 'outcome',
                    'clinical features', 'severity', 'classification',
                    'differential diagnosis', 'case report', 'clinical case',
                ],
                'is_default': True,
                'priority': 5,
            },
        ]

        created = 0
        for cat_data in categories:
            _, was_created = ClinicalCategory.objects.update_or_create(
                slug=cat_data['slug'],
                defaults=cat_data,
            )
            if was_created:
                created += 1

        self.stdout.write(self.style.SUCCESS(
            f'ClinicalCategory: {created} criadas, {len(categories) - created} atualizadas'
        ))

    def _seed_omic_categories(self):
        categories = [
            {
                'omic_type': 'microbiome',
                'keywords': ['16S', 'microbiome', 'metagenom', 'gut microbiota', 'microbiota', 'ITS', 'shotgun metagenom'],
                'priority': 1,
                'is_active': True,
            },
            {
                'omic_type': 'epigenomic',
                'keywords': ['ChIP-seq', 'ATAC-seq', 'methylat', 'histone', 'bisulfite', 'RRBS', 'WGBS', 'MeDIP', 'epigenom'],
                'priority': 2,
                'is_active': True,
            },
            {
                'omic_type': 'transcriptomic',
                'keywords': ['RNA-seq', 'mRNA', 'transcriptom', 'gene expression', 'RNA-Seq', 'scRNA', 'single-cell RNA', 'microarray', 'GeneChip'],
                'priority': 3,
                'is_active': True,
            },
            {
                'omic_type': 'genomic',
                'keywords': ['WGS', 'whole genome', 'SNP', 'variant', 'exome', 'WES', 'genotyp', 'GWAS', 'genome-wide'],
                'priority': 4,
                'is_active': True,
            },
            {
                'omic_type': 'proteomic',
                'keywords': ['proteom', 'mass spectrometry', 'iTRAQ', 'TMT', 'LC-MS', 'protein expression', '2D-gel', 'SILAC'],
                'priority': 5,
                'is_active': True,
            },
            {
                'omic_type': 'metabolomic',
                'keywords': ['metabolom', 'metabolite', 'NMR', 'LC-MS', 'GC-MS', 'lipidom', 'metabolic profil'],
                'priority': 6,
                'is_active': True,
            },
            {
                'omic_type': 'multi_omic',
                'keywords': ['multi-om', 'integrat', 'multi-modal', 'pan-om'],
                'priority': 7,
                'is_active': True,
            },
            {
                'omic_type': 'metagenomic',
                'keywords': ['metagenomic', 'environmental sequencing', 'functional metagenom'],
                'priority': 8,
                'is_active': True,
            },
        ]

        created = 0
        for cat_data in categories:
            _, was_created = OmicCategory.objects.update_or_create(
                omic_type=cat_data['omic_type'],
                defaults=cat_data,
            )
            if was_created:
                created += 1

        self.stdout.write(self.style.SUCCESS(
            f'OmicCategory: {created} criadas, {len(categories) - created} atualizadas'
        ))
