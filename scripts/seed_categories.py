import os
import sys
import django

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.local')
django.setup()

from apps.core.models import ClinicalCategory, OmicCategory

def seed():
    clinical_eixos = [
        {'slug': 'diagnosis', 'name': 'Diagnosis', 'description': 'Diagnostic methods and markers.', 'keywords': ['diagnos', 'biomarker', 'detect'], 'priority': 1},
        {'slug': 'treatment', 'name': 'Treatment', 'description': 'Therapeutic approaches.', 'keywords': ['therap', 'treat', 'drug'], 'priority': 2},
        {'slug': 'epidemiology', 'name': 'Epidemiology', 'description': 'Incidence and prevalence.', 'keywords': ['prevalence', 'incidence', 'epidemiolog'], 'priority': 3},
        {'slug': 'mechanism', 'name': 'Mechanism', 'description': 'Pathophysiological mechanisms.', 'keywords': ['pathophysiolog', 'mechanism', 'pathway'], 'priority': 4},
        {'slug': 'signs_symptoms', 'name': 'Signs & Symptoms', 'description': 'Clinical manifestations.', 'keywords': ['symptom', 'sign', 'manifestation'], 'priority': 5},
    ]

    for eixo in clinical_eixos:
        ClinicalCategory.objects.get_or_create(
            slug=eixo['slug'],
            defaults={
                'name': eixo['name'],
                'description': eixo['description'],
                'keywords': eixo['keywords'],
                'is_default': True,
                'priority': eixo['priority']
            }
        )

    omic_types = [
        {'omic_type': 'genomic', 'keywords': ['wgs', 'wes', 'gwas']},
        {'omic_type': 'transcriptomic', 'keywords': ['rna-seq', 'microarray']},
    ]

    for ot in omic_types:
        OmicCategory.objects.get_or_create(
            omic_type=ot['omic_type'],
            defaults={'keywords': ot['keywords'], 'priority': 1}
        )

    print("Seed completed.")

if __name__ == '__main__':
    seed()
