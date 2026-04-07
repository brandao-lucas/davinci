# DaVinci — Prompt de Correção (Pós-Diagnóstico)

**Contexto:** O diagnóstico completo foi executado. O projeto está 87% completo (40/46 itens do roadmap). Os problemas são localizados e corrigíveis. Este prompt lista TODAS as correções necessárias em ordem de prioridade. Execute cada uma sequencialmente, testando após cada correção.

---

## CORREÇÃO 1 — Shadow de `rust_engine` (CRÍTICO)

**Problema:** O diretório `rust_engine/` no root do projeto faz shadow do módulo PyO3 compilado. Quando o CWD está no sys.path (ex: `manage.py shell`, Celery worker), `import rust_engine` importa o diretório como namespace package vazio em vez do `.so` compilado pelo maturin.

**Diagnóstico confirmado:** `search_and_ingest_pubmed` e `search_and_ingest_omics` não ficam acessíveis via Django/Celery.

**Correção (escolha UMA):**

### Opção A — Renomear o diretório (recomendada)
```bash
# 1. Renomear o diretório de código-fonte Rust
mv rust_engine rust_src

# 2. Atualizar Cargo.toml do workspace (se houver no root)
# Mudar o path do membro do workspace de "rust_engine" para "rust_src"

# 3. Atualizar rust_src/Cargo.toml
# O [lib] name DEVE continuar sendo "rust_engine" (é o nome do módulo Python)
# [lib]
# name = "rust_engine"
# crate-type = ["cdylib"]

# 4. Atualizar maturin — recompilar
cd rust_src
maturin develop --release
cd ..

# 5. Verificar
python -c "import rust_engine; print(dir(rust_engine)); print(hasattr(rust_engine, 'search_and_ingest_pubmed'))"
# Deve imprimir True

# 6. Atualizar referências no código (se houver imports de caminho do diretório)
grep -rn "rust_engine/" --include="*.py" --include="*.toml" --include="*.yml" --include="*.yaml" --include="*.sh" .
# Substituir "rust_engine/" por "rust_src/" em cada ocorrência encontrada
```

### Opção B — Adicionar `__init__.py` ao diretório (alternativa rápida)
```python
# rust_engine/__init__.py
"""
Bridge: re-exporta o módulo PyO3 compilado para evitar shadow.
"""
import importlib
import sys

# Remove este diretório do namespace para importar o .so real
_self = sys.modules.pop(__name__)
try:
    _compiled = importlib.import_module(__name__)
    sys.modules[__name__] = _compiled
    # Re-exporta tudo
    globals().update({k: getattr(_compiled, k) for k in dir(_compiled) if not k.startswith('_')})
except ImportError:
    # Rust não compilado — modo fallback
    sys.modules[__name__] = _self
```

**Teste após correção:**
```bash
cd /caminho/do/projeto   # IMPORTANTE: estar no root do projeto
python -c "
import rust_engine
print('Tipo:', type(rust_engine))
print('search_and_ingest_pubmed:', hasattr(rust_engine, 'search_and_ingest_pubmed'))
print('search_and_ingest_omics:', hasattr(rust_engine, 'search_and_ingest_omics'))
"
# Todos devem ser True

# Testar via manage.py também
python manage.py shell -c "
import rust_engine
print('OK:', hasattr(rust_engine, 'search_and_ingest_pubmed'))
"
```

---

## CORREÇÃO 2 — SRA retorna 0 datasets (ALTO)

**Problema:** A busca SRA retornou 0 datasets para "cardiovascular disease" enquanto a API NCBI tem resultados. O parser provavelmente não está extraindo corretamente os accessions do formato esummary do SRA.

**Investigação — Execute isso primeiro:**
```bash
# Testar o que o NCBI retorna para SRA
python3 -c "
import requests, json

# 1. esearch — verificar se retorna IDs
r = requests.get('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi', params={
    'db': 'sra', 'term': 'cardiovascular disease', 'retmax': 5, 'retmode': 'json'
}, timeout=30)
data = r.json()
ids = data['esearchresult']['idlist']
print(f'esearch IDs: {ids}')
print(f'Total count: {data[\"esearchresult\"][\"count\"]}')

if ids:
    # 2. esummary — verificar formato de resposta
    r2 = requests.get('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi', params={
        'db': 'sra', 'id': ','.join(ids[:3]), 'retmode': 'json'
    }, timeout=30)
    summary = r2.json()
    print(f'\nesummary keys: {list(summary.get(\"result\", {}).keys())}')

    # Imprimir o primeiro resultado completo para ver a estrutura
    for uid in ids[:1]:
        item = summary.get('result', {}).get(uid, {})
        print(f'\nUID {uid}:')
        for k, v in item.items():
            val_str = str(v)[:200]
            print(f'  {k}: {val_str}')
"
```

**O problema mais comum é:** O SRA esummary retorna os dados dentro de um campo XML embutido em JSON (campo `expxml` ou `runs`). O parser precisa fazer um parse adicional desse XML interno para extrair o accession (SRP/SRX/SRR).

**Correção em `rust_src/src/omics/sra_parser.rs` (ou `rust_engine/src/omics/sra_parser.rs`):**

Verifique se o parser está:
1. Chamando `esearch` com `db=sra` ✓
2. Chamando `esummary` (não `efetch`) com os IDs retornados
3. Parseando o campo `expxml` do JSON de esummary — este campo contém XML embutido como string
4. Extraindo `<Study acc="SRP..." />` ou `<Experiment acc="SRX..." />` de dentro do `expxml`
5. Construindo o `OmicDatasetData` com `source_db = "sra"` e `accession` correto

```rust
// Pseudocódigo da correção esperada em sra_parser.rs:
//
// 1. esearch(db=sra, term=query) → Vec<String> de UIDs numéricos
// 2. esummary(db=sra, id=uids.join(",")) → JSON
// 3. Para cada UID no JSON result:
//    a. Extrair campo "expxml" (é uma string contendo XML)
//    b. Parsear esse XML interno com quick-xml
//    c. Extrair <Study acc="SRPxxxxxx" name="..." />
//    d. Extrair <Summary>
//         <Title>...</Title>
//         <Platform>...</Platform>
//         <Statistics total_runs="N" total_spots="M" />
//       </Summary>
//    e. Extrair <Organism taxid="9606" ScientificName="Homo sapiens" />
//    f. Construir OmicDatasetData { accession: study_acc, source_db: "sra", ... }
//
// ATENÇÃO: O campo "runs" também contém XML embutido com info dos runs individuais
// mas para o DaVinci queremos o Study-level accession (SRP), não runs (SRR)
```

**Teste após correção:**
```bash
cd rust_src  # ou rust_engine
cargo test sra  # rodar testes específicos do SRA
maturin develop --release
cd ..

python manage.py shell -c "
import rust_engine
from django.conf import settings
db = settings.DATABASES['default']
db_url = f\"postgresql://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}\"

from apps.core.models import DaVinciProject, IngestionJob
project = DaVinciProject.objects.first()
job = IngestionJob.objects.create(project=project, job_type='geo_search', parameters={})

result = rust_engine.search_and_ingest_omics(
    job_id=str(job.id),
    query='cardiovascular disease',
    db_url=db_url,
    project_id=str(project.id),
    sources=['sra'],
    max_per_source=10,
    ncbi_api_key=getattr(settings, 'NCBI_API_KEY', None),
    synonyms=[],
)
print(f'SRA result: {result}')

from apps.core.models import OmicDataset
sra_count = OmicDataset.objects.filter(source_db='sra').count()
print(f'SRA datasets no banco: {sra_count}')
# Deve ser > 0
"
```

---

## CORREÇÃO 3 — DatasetPaperLinks sempre 0 (ALTO)

**Problema:** Nenhum link dataset↔paper foi criado. O elink não está encontrando relações ou os resultados não estão sendo persistidos.

**Investigação:**
```bash
# 1. Verificar se o elink funciona manualmente
python3 -c "
import requests

# Pegar um GSE ID que sabemos que existe
# Primeiro buscar um
r = requests.get('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi', params={
    'db': 'gds', 'term': 'cardiovascular disease', 'retmax': 3, 'retmode': 'json'
}, timeout=30)
gds_ids = r.json()['esearchresult']['idlist']
print(f'GDS IDs: {gds_ids}')

# elink: gds → pubmed
if gds_ids:
    r2 = requests.get('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi', params={
        'dbfrom': 'gds',
        'db': 'pubmed',
        'id': ','.join(gds_ids[:3]),
        'retmode': 'json',
    }, timeout=30)
    data = r2.json()
    print(f'\nelink response keys: {list(data.keys())}')

    # Navegar na estrutura para encontrar os links
    linksets = data.get('linksets', [])
    for ls in linksets:
        print(f'  LinkSet from ID {ls.get(\"ids\", \"?\")}: ')
        for lsdb in ls.get('linksetdbs', []):
            print(f'    → {lsdb.get(\"dbto\", \"?\")}: {lsdb.get(\"links\", [])[:5]}')
"
```

**Causas prováveis e correções:**

1. **elink não está sendo chamado no fluxo de ingestão:** Verificar se `search_and_ingest_omics` chama a função de elink após inserir os datasets.

2. **FK resolution falha:** O `copy_dataset_paper_links` precisa resolver `accession → dataset.id` E `pmid → paper.id`. Se os PMIDs retornados pelo elink não existem na tabela `Paper`, o link não pode ser criado. **Correção:** Após o elink retornar PMIDs, verificar quais já existem no banco. Os que não existem: ou ignorar, ou buscar via efetch primeiro.

3. **IDs do GDS vs accessions GSE:** O NCBI retorna GDS IDs numéricos no esearch, mas os accessions são GSE. O elink precisa usar os IDs corretos (numéricos do GDS, não o string GSE).

**Verificar no Rust:**
```bash
# Verificar se elink está implementado e é chamado
grep -rn "elink" rust_src/src/ --include="*.rs"
# Deve aparecer em omics/ e possivelmente em lib.rs

# Verificar se copy_dataset_paper_links existe e está sendo chamada
grep -rn "copy_dataset_paper_links\|link.*paper" rust_src/src/db/copy_writer.rs
grep -rn "copy_dataset_paper_links\|link.*paper" rust_src/src/lib.rs
```

**Se elink não está sendo chamado, adicionar ao fluxo em `lib.rs`:**
```rust
// Após copy_omic_datasets() e copy_papers():
// 1. Coletar todos os dataset IDs numéricos (do esearch)
// 2. Chamar elink(dbfrom=gds, db=pubmed, ids=gds_ids)
// 3. Para cada par (gds_id → pmid):
//    a. Resolver gds_id → dataset.id no Postgres
//    b. Resolver pmid → paper.id no Postgres
//    c. Se ambos existem: inserir DatasetPaperLink
// 4. copy_dataset_paper_links(links, conn)
```

---

## CORREÇÃO 4 — POST /projects/ slug duplicado → 500 (ALTO)

**Arquivo:** `apps/core/views/project_views.py` (linha ~26, `perform_create`)

**Correção:**
```python
# apps/core/views/project_views.py

from django.utils.text import slugify
from django.db import IntegrityError
import uuid

class DaVinciProjectViewSet(viewsets.ModelViewSet):
    # ... existing code ...

    def perform_create(self, serializer):
        title = serializer.validated_data.get('title', '')
        slug = slugify(title)

        # Garantir unicidade do slug
        base_slug = slug
        attempt = 0
        while True:
            try:
                if attempt > 0:
                    slug = f"{base_slug}-{uuid.uuid4().hex[:6]}"
                serializer.save(user=self.request.user, slug=slug)
                break
            except IntegrityError:
                attempt += 1
                if attempt > 5:
                    raise
```

**Teste:**
```bash
python manage.py shell -c "
from rest_framework.test import APIClient
from django.contrib.auth import get_user_model
User = get_user_model()
user = User.objects.first()
client = APIClient()
client.force_authenticate(user=user)

# Criar dois projetos com mesmo título
for i in range(3):
    r = client.post('/api/v1/projects/', {
        'title': 'Duplicate Test',
        'query_term': 'test',
    }, format='json')
    print(f'Tentativa {i+1}: status={r.status_code}, slug={r.data.get(\"slug\", \"ERRO\")}')
    if r.status_code >= 400:
        print(f'  Erro: {r.data}')
# Todas devem retornar 201 com slugs diferentes
"
```

---

## CORREÇÃO 5 — GET /auth/me/ retorna 500 (ALTO)

**Arquivo:** `apps/accounts/views.py` (linha ~20)

**Problema:** `UserProfile.objects.get(user=request.user)` falha com `DoesNotExist` se o user não tem profile.

**Correção:**
```python
# apps/accounts/views.py

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def me(request):
    """Retorna perfil do usuário autenticado."""
    profile, created = UserProfile.objects.get_or_create(
        user=request.user,
        defaults={
            'firebase_uid': request.user.username,  # fallback
            'auth_provider': 'password',
        }
    )
    serializer = UserProfileSerializer(profile)
    return Response(serializer.data)
```

**Se `UserProfile` usa `firebase_uid` como required unique, ajustar o default para não conflitar:**
```python
    defaults={
        'firebase_uid': getattr(request.auth, 'uid', None) or request.user.username,
        'auth_provider': 'password',
    }
```

**Teste:**
```bash
python manage.py shell -c "
from rest_framework.test import APIClient
from django.contrib.auth import get_user_model
User = get_user_model()
user = User.objects.first()
client = APIClient()
client.force_authenticate(user=user)
r = client.get('/api/v1/auth/me/')
print(f'Status: {r.status_code}')
print(f'Data: {r.data}')
# Deve ser 200
"
```

---

## CORREÇÃO 6 — Teste `test_list_papers_filter_by_status` falha (MÉDIO)

**Arquivo:** `apps/core/tests/test_api.py` (linha ~102)

**Problema:** Espera 1 resultado mas recebe 2. Provavelmente dados de testes anteriores não estão sendo limpos ou o setUp cria registros extras.

**Investigação:**
```bash
# Ver o teste
grep -A 30 "test_list_papers_filter_by_status" apps/core/tests/test_api.py
```

**Correções possíveis:**

1. **Se o teste não limpa dados:** Adicionar `setUp` que limpa `ProjectPaper`:
```python
def setUp(self):
    super().setUp()
    ProjectPaper.objects.all().delete()
```

2. **Se outro teste cria dados que persistem:** Usar `TransactionTestCase` ou verificar que cada teste é isolado.

3. **Se o filtro está incorreto:** Verificar no viewset se `filterset_fields` inclui `curation_status` e se o nome do campo no queryset corresponde ao parâmetro da query string.

```python
# Verificar no ViewSet:
# O filtro deve ser sobre ProjectPaper.curation_status, não Paper.status
filterset_fields = {
    'curation_status': ['exact'],  # ou filterset_class
}
```

---

## CORREÇÃO 7 — Teste `efetch failed: error decoding response body` (MÉDIO)

**Arquivo:** `apps/core/tests/test_ingestion.py`

**Problema:** O teste faz chamada REAL ao NCBI e falha no decode. Testes de integração que dependem de APIs externas são frágeis.

**Correção:** Mockar a chamada ao NCBI nos testes unitários. Manter um teste de integração separado que pode ser skipped:

```python
# apps/core/tests/test_ingestion.py

from unittest.mock import patch, MagicMock
from django.test import TestCase, tag

class PubMedIngestionUnitTest(TestCase):
    """Testes com mock — sempre rodam."""

    @patch('apps.core.tasks.ingestion_tasks.rust_engine')
    def test_pubmed_task_calls_rust(self, mock_rust):
        mock_rust.search_and_ingest_pubmed.return_value = MagicMock(
            records_processed=10,
            records_inserted=8,
            records_updated=2,
            errors=[],
        )
        from apps.core.tasks.ingestion_tasks import run_pubmed_ingestion
        # ... setup job ...
        # Verificar que a task chama o rust com os parâmetros corretos


@tag('integration')  # Rodar com: manage.py test --tag=integration
class PubMedIngestionIntegrationTest(TestCase):
    """Testes com NCBI real — podem falhar por rate limit."""

    def test_real_ingestion(self):
        # ... teste existente ...
        pass
```

---

## CORREÇÃO 8 — Criar management command `seed_categories` (MÉDIO)

**Criar:** `apps/core/management/commands/seed_categories.py`

```python
# apps/core/management/__init__.py  (criar se não existe)
# apps/core/management/commands/__init__.py  (criar se não existe)

# apps/core/management/commands/seed_categories.py
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
                "slug": "diagnosis",
                "name": "Diagnóstico",
                "description": "Papers relacionados a diagnóstico, biomarcadores, detecção e screening",
                "keywords": [
                    "diagnosis", "diagnostic", "biomarker", "detection", "screening",
                    "sensitivity", "specificity", "predictive value", "ROC curve",
                    "early detection", "prognosis", "prognostic", "staging",
                    "imaging", "biopsy", "assay", "marker", "indicator",
                ],
                "is_default": True,
                "priority": 1,
            },
            {
                "slug": "treatment",
                "name": "Tratamento",
                "description": "Papers sobre tratamentos, terapias, intervenções e ensaios clínicos",
                "keywords": [
                    "treatment", "therapy", "therapeutic", "drug", "intervention",
                    "clinical trial", "randomized", "placebo", "efficacy", "dose",
                    "response", "remission", "surgery", "surgical", "chemotherapy",
                    "immunotherapy", "radiation", "transplant", "pharmacological",
                ],
                "is_default": True,
                "priority": 2,
            },
            {
                "slug": "epidemiology",
                "name": "Epidemiologia",
                "description": "Papers sobre prevalência, incidência, fatores de risco e saúde pública",
                "keywords": [
                    "epidemiology", "prevalence", "incidence", "risk factor",
                    "cohort", "case-control", "population", "mortality", "morbidity",
                    "survival", "odds ratio", "hazard ratio", "relative risk",
                    "cross-sectional", "longitudinal", "public health", "burden",
                ],
                "is_default": True,
                "priority": 3,
            },
            {
                "slug": "mechanism",
                "name": "Mecanismo",
                "description": "Papers sobre mecanismos moleculares, patogênese e biologia",
                "keywords": [
                    "mechanism", "pathway", "signaling", "molecular", "cellular",
                    "pathogenesis", "pathophysiology", "gene expression", "regulation",
                    "transcription", "translation", "mutation", "polymorphism",
                    "protein", "receptor", "ligand", "kinase", "apoptosis",
                    "inflammation", "immune response", "oxidative stress",
                ],
                "is_default": True,
                "priority": 4,
            },
            {
                "slug": "signs_symptoms",
                "name": "Sinais e Sintomas",
                "description": "Papers sobre manifestações clínicas, fenótipos e apresentação",
                "keywords": [
                    "signs", "symptoms", "clinical presentation", "manifestation",
                    "phenotype", "complication", "comorbidity", "outcome",
                    "clinical features", "severity", "classification",
                    "differential diagnosis", "case report", "clinical case",
                ],
                "is_default": True,
                "priority": 5,
            },
        ]

        created = 0
        for cat_data in categories:
            _, was_created = ClinicalCategory.objects.update_or_create(
                slug=cat_data["slug"],
                defaults=cat_data,
            )
            if was_created:
                created += 1

        self.stdout.write(
            self.style.SUCCESS(
                f'ClinicalCategory: {created} criadas, {len(categories) - created} atualizadas'
            )
        )

    def _seed_omic_categories(self):
        categories = [
            {"omic_type": "microbiome", "display_name": "Microbioma",
             "keywords": ["16S", "microbiome", "metagenom", "gut microbiota", "microbiota", "ITS", "shotgun metagenom"],
             "priority": 1, "is_active": True},
            {"omic_type": "epigenomic", "display_name": "Epigenômica",
             "keywords": ["ChIP-seq", "ATAC-seq", "methylat", "histone", "bisulfite", "RRBS", "WGBS", "MeDIP", "epigenom"],
             "priority": 2, "is_active": True},
            {"omic_type": "transcriptomic", "display_name": "Transcriptômica",
             "keywords": ["RNA-seq", "mRNA", "transcriptom", "gene expression", "RNA-Seq", "scRNA", "single-cell RNA", "microarray", "GeneChip"],
             "priority": 3, "is_active": True},
            {"omic_type": "genomic", "display_name": "Genômica",
             "keywords": ["WGS", "whole genome", "SNP", "variant", "exome", "WES", "genotyp", "GWAS", "genome-wide"],
             "priority": 4, "is_active": True},
            {"omic_type": "proteomic", "display_name": "Proteômica",
             "keywords": ["proteom", "mass spectrometry", "iTRAQ", "TMT", "LC-MS", "protein expression", "2D-gel", "SILAC"],
             "priority": 5, "is_active": True},
            {"omic_type": "metabolomic", "display_name": "Metabolômica",
             "keywords": ["metabolom", "metabolite", "NMR", "LC-MS", "GC-MS", "lipidom", "metabolic profil"],
             "priority": 6, "is_active": True},
            {"omic_type": "multi_omic", "display_name": "Multi-ômica",
             "keywords": ["multi-om", "integrat", "multi-modal", "pan-om"],
             "priority": 7, "is_active": True},
            {"omic_type": "metagenomic", "display_name": "Metagenômica",
             "keywords": ["metagenomic", "environmental sequencing", "functional metagenom"],
             "priority": 8, "is_active": True},
        ]

        created = 0
        for cat_data in categories:
            _, was_created = OmicCategory.objects.update_or_create(
                omic_type=cat_data["omic_type"],
                defaults=cat_data,
            )
            if was_created:
                created += 1

        self.stdout.write(
            self.style.SUCCESS(
                f'OmicCategory: {created} criadas, {len(categories) - created} atualizadas'
            )
        )
```

```bash
# Criar diretórios necessários
mkdir -p apps/core/management/commands
touch apps/core/management/__init__.py
touch apps/core/management/commands/__init__.py

# Executar
python manage.py seed_categories
```

---

## CORREÇÃO 9 — Extrair `export_service.py` (BAIXO)

**Criar:** `apps/core/services/export_service.py`

```python
# apps/core/services/export_service.py

import csv
import io
import json
from django.http import HttpResponse
from apps.core.models import (
    ProjectPaper, ProjectDataset, ProjectPaperDataset,
)
from apps.core.serializers.paper import ProjectPaperDetailSerializer
from apps.core.serializers.dataset import ProjectDatasetListSerializer


class ExportService:
    """Exportação de dados curados do projeto."""

    @staticmethod
    def export_json(project):
        """Exporta papers e datasets incluídos em JSON estruturado."""
        papers = ProjectPaper.objects.filter(
            project=project, curation_status='included'
        ).select_related('paper').prefetch_related(
            'paper__authors', 'paper__keywords', 'paper__mesh_terms',
            'paper__genes', 'paper__drugs', 'paper__variants',
            'paper__contexts', 'clinical_categories', 'user_categories',
        )

        datasets = ProjectDataset.objects.filter(
            project=project, curation_status='included'
        ).select_related('dataset')

        links = ProjectPaperDataset.objects.filter(
            project=project
        ).select_related('project_paper__paper', 'project_dataset__dataset')

        return {
            'project': {
                'id': str(project.id),
                'title': project.title,
                'query_term': project.query_term,
                'query_synonyms': project.query_synonyms,
            },
            'papers': ProjectPaperDetailSerializer(papers, many=True).data,
            'datasets': ProjectDatasetListSerializer(datasets, many=True).data,
            'links': [
                {
                    'paper_pmid': link.project_paper.paper.pmid,
                    'dataset_accession': link.project_dataset.dataset.accession,
                    'confidence': link.link_confidence,
                }
                for link in links
            ],
            'stats': {
                'total_papers': papers.count(),
                'total_datasets': datasets.count(),
                'total_links': links.count(),
            },
        }

    @staticmethod
    def export_csv(project):
        """Exporta papers incluídos em CSV."""
        papers = ProjectPaper.objects.filter(
            project=project, curation_status='included'
        ).select_related('paper')

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            'PMID', 'Title', 'Journal', 'Year', 'DOI',
            'Curation Status', 'Relevance Score', 'Notes',
        ])
        for pp in papers:
            writer.writerow([
                pp.paper.pmid, pp.paper.title, pp.paper.journal,
                pp.paper.pub_year, pp.paper.doi,
                pp.curation_status, pp.relevance_score, pp.notes or '',
            ])

        return output.getvalue()
```

**Atualizar o view para usar o service:**
```python
# apps/core/views/project_views.py — action export

from apps.core.services.export_service import ExportService

@action(detail=True, methods=['get'])
def export(self, request, pk=None):
    project = self.get_object()
    fmt = request.query_params.get('format', 'json')

    if fmt == 'csv':
        csv_data = ExportService.export_csv(project)
        response = HttpResponse(csv_data, content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="{project.slug}_export.csv"'
        return response
    else:
        data = ExportService.export_json(project)
        return Response(data)
```

---

## CORREÇÃO 10 — Expandir NER dictionaries (BAIXO, mas impactante)

**Problema:** Gene NER tem apenas 6 genes hardcoded (BRCA1, TP53, EGFR, TNF, IL6, BRAF). Drug NER idem. Para o DaVinci ser útil, precisa de listas muito maiores.

**Abordagem recomendada:**

1. **Gene symbols:** Baixar a lista oficial do HGNC (Hugo Gene Nomenclature Committee). São ~42.000 gene symbols aprovados. Armazenar como arquivo JSON embutido no binário Rust via `include_str!`.

2. **Drug names:** Baixar do DrugBank (versão open) ou ChEMBL. São ~10.000 nomes aprovados.

3. **Implementação no Rust:**
```rust
// rust_src/src/categorization/gene_ner.rs

use std::collections::HashSet;
use once_cell::sync::Lazy;

// Arquivo gerado: contém um gene symbol por linha
static GENE_SYMBOLS: Lazy<HashSet<String>> = Lazy::new(|| {
    include_str!("../../data/hgnc_gene_symbols.txt")
        .lines()
        .filter(|l| !l.is_empty() && l.len() >= 2)
        .map(|l| l.trim().to_uppercase())
        .collect()
});

pub fn extract_genes(abstract_text: &str) -> Vec<GeneData> {
    let mut found = Vec::new();
    // Tokenizar por espaço e pontuação
    let words: Vec<&str> = abstract_text.split(|c: char| !c.is_alphanumeric() && c != '-')
        .filter(|w| w.len() >= 2)
        .collect();

    for word in &words {
        let upper = word.to_uppercase();
        if GENE_SYMBOLS.contains(&upper) {
            // Contar menções
            let count = words.iter().filter(|w| w.to_uppercase() == upper).count();
            if !found.iter().any(|g: &GeneData| g.gene_symbol == upper) {
                found.push(GeneData {
                    gene_symbol: upper,
                    entrez_id: None,
                    mention_count: count as i32,
                });
            }
        }
    }
    found
}
```

**Para gerar o arquivo de gene symbols:**
```bash
# Baixar do HGNC (gratuito)
curl -o data/hgnc_complete.txt \
  "https://ftp.ebi.ac.uk/pub/databases/genenames/hgnc/tsv/hgnc_complete_set.txt"

# Extrair apenas os symbols aprovados
cut -f2 data/hgnc_complete.txt | tail -n +2 | sort -u > rust_src/data/hgnc_gene_symbols.txt
```

---

## CHECKLIST DE VALIDAÇÃO FINAL

Após todas as correções, rode este script para confirmar que tudo funciona:

```bash
echo "=== VALIDAÇÃO FINAL ===" | tee diagnostics/VALIDATION.log

echo -e "\n1. rust_engine importa corretamente" | tee -a diagnostics/VALIDATION.log
python manage.py shell -c "
import rust_engine
assert hasattr(rust_engine, 'search_and_ingest_pubmed'), 'FALHA: search_and_ingest_pubmed'
assert hasattr(rust_engine, 'search_and_ingest_omics'), 'FALHA: search_and_ingest_omics'
print('  ✅ rust_engine OK')
" 2>&1 | tee -a diagnostics/VALIDATION.log

echo -e "\n2. Ingestão PubMed funciona" | tee -a diagnostics/VALIDATION.log
python manage.py shell -c "
import rust_engine
from django.conf import settings
from apps.core.models import *
db = settings.DATABASES['default']
db_url = f\"postgresql://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}\"
project = DaVinciProject.objects.first()
job = IngestionJob.objects.create(project=project, job_type='pubmed_search', parameters={})
result = rust_engine.search_and_ingest_pubmed(
    job_id=str(job.id), query='hidradenitis AND cancer',
    db_url=db_url, project_id=str(project.id),
    date_from=2024, date_to=2025, ncbi_api_key=getattr(settings, 'NCBI_API_KEY', None),
)
print(f'  ✅ PubMed: {result.records_inserted} papers inseridos')
" 2>&1 | tee -a diagnostics/VALIDATION.log

echo -e "\n3. Ingestão Ômica - GEO" | tee -a diagnostics/VALIDATION.log
python manage.py shell -c "
import rust_engine
from django.conf import settings
from apps.core.models import *
db = settings.DATABASES['default']
db_url = f\"postgresql://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}\"
project = DaVinciProject.objects.first()
job = IngestionJob.objects.create(project=project, job_type='geo_search', parameters={})
result = rust_engine.search_and_ingest_omics(
    job_id=str(job.id), query='cardiovascular disease',
    db_url=db_url, project_id=str(project.id),
    sources=['geo'], max_per_source=5,
    ncbi_api_key=getattr(settings, 'NCBI_API_KEY', None), synonyms=[],
)
print(f'  ✅ GEO: {result.datasets_inserted if hasattr(result, \"datasets_inserted\") else result} datasets')
" 2>&1 | tee -a diagnostics/VALIDATION.log

echo -e "\n4. Ingestão Ômica - SRA" | tee -a diagnostics/VALIDATION.log
python manage.py shell -c "
import rust_engine
from django.conf import settings
from apps.core.models import *
db = settings.DATABASES['default']
db_url = f\"postgresql://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}\"
project = DaVinciProject.objects.first()
job = IngestionJob.objects.create(project=project, job_type='geo_search', parameters={})
result = rust_engine.search_and_ingest_omics(
    job_id=str(job.id), query='cardiovascular disease',
    db_url=db_url, project_id=str(project.id),
    sources=['sra'], max_per_source=5,
    ncbi_api_key=getattr(settings, 'NCBI_API_KEY', None), synonyms=[],
)
count = OmicDataset.objects.filter(source_db='sra').count()
print(f'  {\"✅\" if count > 0 else \"❌\"} SRA: {count} datasets no banco')
" 2>&1 | tee -a diagnostics/VALIDATION.log

echo -e "\n5. DatasetPaperLinks" | tee -a diagnostics/VALIDATION.log
python manage.py shell -c "
from apps.core.models import DatasetPaperLink
count = DatasetPaperLink.objects.count()
print(f'  {\"✅\" if count > 0 else \"❌\"} DatasetPaperLinks: {count}')
" 2>&1 | tee -a diagnostics/VALIDATION.log

echo -e "\n6. API /auth/me/" | tee -a diagnostics/VALIDATION.log
python manage.py shell -c "
from rest_framework.test import APIClient
from django.contrib.auth import get_user_model
user = get_user_model().objects.first()
c = APIClient(); c.force_authenticate(user=user)
r = c.get('/api/v1/auth/me/')
print(f'  {\"✅\" if r.status_code == 200 else \"❌\"} /auth/me/: {r.status_code}')
" 2>&1 | tee -a diagnostics/VALIDATION.log

echo -e "\n7. API /projects/ slug duplicado" | tee -a diagnostics/VALIDATION.log
python manage.py shell -c "
from rest_framework.test import APIClient
from django.contrib.auth import get_user_model
user = get_user_model().objects.first()
c = APIClient(); c.force_authenticate(user=user)
r1 = c.post('/api/v1/projects/', {'title': 'Validation Test', 'query_term': 'test'}, format='json')
r2 = c.post('/api/v1/projects/', {'title': 'Validation Test', 'query_term': 'test'}, format='json')
ok = r1.status_code == 201 and r2.status_code == 201
print(f'  {\"✅\" if ok else \"❌\"} Slug duplicado: {r1.status_code}, {r2.status_code}')
" 2>&1 | tee -a diagnostics/VALIDATION.log

echo -e "\n8. Django tests" | tee -a diagnostics/VALIDATION.log
python manage.py test apps/ -v 2 --no-input 2>&1 | tail -5 | tee -a diagnostics/VALIDATION.log

echo -e "\n9. seed_categories command" | tee -a diagnostics/VALIDATION.log
python manage.py seed_categories 2>&1 | tee -a diagnostics/VALIDATION.log

echo -e "\n=== VALIDAÇÃO COMPLETA ===" | tee -a diagnostics/VALIDATION.log
```

---

## Ordem de Execução Recomendada

1. **CORREÇÃO 1** (shadow rust_engine) — desbloqueia tudo
2. **CORREÇÃO 5** (/auth/me/) — fix rápido, 2 linhas
3. **CORREÇÃO 4** (slug duplicado) — fix rápido, ~10 linhas
4. **CORREÇÃO 2** (SRA parser) — requer investigação do formato de resposta
5. **CORREÇÃO 3** (DatasetPaperLinks) — requer investigação do elink
6. **CORREÇÃO 6** (teste filter_by_status) — investigar setUp
7. **CORREÇÃO 7** (teste efetch) — adicionar mocks
8. **CORREÇÃO 8** (seed_categories command) — copiar e colar
9. **CORREÇÃO 9** (export_service) — refatoração
10. **CORREÇÃO 10** (NER expansion) — quando tiver tempo

Após cada correção, rode o teste específico indicado. No final, rode o checklist de validação completo.
