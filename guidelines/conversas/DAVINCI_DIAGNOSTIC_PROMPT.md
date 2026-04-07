# DaVinci — Prompt de Diagnóstico Completo para Claude Code

## Contexto do Projeto

O **DaVinci** é um módulo do ecossistema **PlatOmics** para download, processamento e análise integrada de literatura científica (PubMed/PMC) e metadados de bases ômicas (GEO, SRA, BioProject, GWAS Catalog). A stack é **Django 6.0 + Rust (PyO3/Maturin) + PostgreSQL + Celery/Redis + Firebase Auth**. O frontend é **Next.js 15 + TypeScript**.

O projeto está com **erros recorrentes na ingestão de dados de bancos ômicos** e possivelmente outros bugs não identificados. Preciso que você execute uma auditoria completa, rode testes, gere logs estruturados e sugira correções.

---

## TAREFA 1 — Inventário do Código Existente

Antes de qualquer teste, faça um inventário completo do que existe vs. o que deveria existir. Execute os comandos abaixo e registre os resultados em um arquivo `diagnostics/01_inventory.log`:

```bash
mkdir -p diagnostics

echo "=== INVENTÁRIO DO PROJETO DAVINCI ===" > diagnostics/01_inventory.log
echo "Data: $(date)" >> diagnostics/01_inventory.log

echo -e "\n=== 1. ESTRUTURA DE DIRETÓRIOS ===" >> diagnostics/01_inventory.log
find . -type f -name "*.py" -o -name "*.rs" -o -name "*.toml" -o -name "*.tsx" -o -name "*.ts" | grep -v node_modules | grep -v __pycache__ | grep -v .venv | sort >> diagnostics/01_inventory.log

echo -e "\n=== 2. DJANGO APPS REGISTRADAS ===" >> diagnostics/01_inventory.log
grep -r "INSTALLED_APPS" config/settings/ >> diagnostics/01_inventory.log 2>&1

echo -e "\n=== 3. MODELS EXISTENTES ===" >> diagnostics/01_inventory.log
grep -n "class.*models.Model" apps/*/models.py >> diagnostics/01_inventory.log 2>&1

echo -e "\n=== 4. MIGRATIONS ===" >> diagnostics/01_inventory.log
find apps/ -path "*/migrations/*.py" -not -name "__init__.py" | sort >> diagnostics/01_inventory.log 2>&1

echo -e "\n=== 5. RUST ENGINE — MÓDULOS ===" >> diagnostics/01_inventory.log
find rust_engine/src -name "*.rs" | sort >> diagnostics/01_inventory.log 2>&1

echo -e "\n=== 6. CARGO.TOML ===" >> diagnostics/01_inventory.log
cat rust_engine/Cargo.toml >> diagnostics/01_inventory.log 2>&1

echo -e "\n=== 7. CELERY TASKS ===" >> diagnostics/01_inventory.log
grep -rn "@shared_task\|@app.task" apps/ >> diagnostics/01_inventory.log 2>&1

echo -e "\n=== 8. SERIALIZERS ===" >> diagnostics/01_inventory.log
grep -rn "class.*Serializer" apps/*/serializers/ apps/*/serializers.py >> diagnostics/01_inventory.log 2>&1

echo -e "\n=== 9. VIEWS / VIEWSETS ===" >> diagnostics/01_inventory.log
grep -rn "class.*ViewSet\|class.*APIView\|@api_view" apps/*/views/ apps/*/views.py >> diagnostics/01_inventory.log 2>&1

echo -e "\n=== 10. URLS ===" >> diagnostics/01_inventory.log
cat apps/*/urls.py config/urls.py >> diagnostics/01_inventory.log 2>&1

echo -e "\n=== 11. SERVICES ===" >> diagnostics/01_inventory.log
find apps/ -path "*/services/*.py" -not -name "__init__.py" | sort >> diagnostics/01_inventory.log 2>&1

echo -e "\n=== 12. DOCKER COMPOSE ===" >> diagnostics/01_inventory.log
cat docker/docker-compose.yml docker-compose.yml 2>/dev/null >> diagnostics/01_inventory.log

echo -e "\n=== 13. REQUIREMENTS / DEPENDÊNCIAS ===" >> diagnostics/01_inventory.log
cat requirements.txt pyproject.toml 2>/dev/null >> diagnostics/01_inventory.log

echo -e "\n=== 14. TESTES EXISTENTES ===" >> diagnostics/01_inventory.log
find . -name "test_*.py" -o -name "*_test.py" -o -name "test_*.rs" | grep -v node_modules | grep -v .venv | sort >> diagnostics/01_inventory.log

echo -e "\n=== 15. MANAGEMENT COMMANDS ===" >> diagnostics/01_inventory.log
find apps/ -path "*/management/commands/*.py" -not -name "__init__.py" | sort >> diagnostics/01_inventory.log 2>&1

echo -e "\n=== 16. FRONTEND EXISTENTE ===" >> diagnostics/01_inventory.log
find davinci-frontend/src -name "*.tsx" -o -name "*.ts" 2>/dev/null | grep -v node_modules | sort >> diagnostics/01_inventory.log 2>&1
```

### Checklist de Inventário — Verificar Contra o Roadmap

Compare o inventário com a lista abaixo. Para cada item, marque ✅ (existe e parece completo), ⚠️ (existe mas incompleto/suspeito), ou ❌ (não existe). Salve em `diagnostics/02_roadmap_checklist.md`:

#### Fase 1 — Fundação
- [ ] `apps/core/models.py` contém TODOS os models: `DaVinciProject`, `Paper`, `PaperAuthor`, `PaperKeyword`, `PaperMeSHTerm`, `PaperGene`, `PaperDrug`, `PaperVariant`, `VariantAnnotation`, `EntityContext`, `OmicDataset`, `DatasetPaperLink`, `OmicCategory`, `ClinicalCategory`, `UserCategory`, `ProjectPaper`, `ProjectPaperClinicalCategory`, `ProjectDataset`, `ProjectPaperDataset`, `ProjectStats`, `IngestionJob`
- [ ] `apps/accounts/models.py` contém `UserProfile` com campos: `firebase_uid`, `auth_provider`, `orcid_id`, `institution`, `research_area`, `avatar_url`, `ncbi_api_key`, `last_firebase_sync`
- [ ] Migration RunSQL com triggers FTS para `Paper` e `OmicDataset` (search_vector)
- [ ] Migration RunSQL com índices GIN em `search_vector`
- [ ] `config/celery.py` existe e está configurado
- [ ] `docker-compose.yml` com Postgres 16 + Redis
- [ ] Management command `seed_categories.py` com 5 `ClinicalCategory` + `OmicCategory`
- [ ] DRF configurado com `FirebaseAuthentication`, paginação, filtros

#### Fase 2 — Rust Engine Literatura
- [ ] `rust_engine/src/lib.rs` — entry point PyO3 com `search_and_ingest_pubmed`
- [ ] `rust_engine/src/ncbi/client.rs` — HTTP client com rate limiting (3 ou 10 req/s), backoff exponencial, respeita Retry-After
- [ ] `rust_engine/src/ncbi/parser.rs` — quick-xml parser que extrai PMID, título, abstract, autores, afiliações, keywords, MeSH, DOI, PMC, journal, pub_year, pub_month, pub_type em UMA passada
- [ ] `rust_engine/src/ncbi/models.rs` — structs `PaperData`, `AuthorData`, `MeSHTerm`, `GeneData`, `DrugData`, `EntityContext`
- [ ] `rust_engine/src/categorization/clinical.rs` — regex compilados para 5 eixos clínicos
- [ ] `rust_engine/src/categorization/gene_ner.rs` — extração de genes
- [ ] `rust_engine/src/categorization/drug_ner.rs` — extração de drogas
- [ ] `rust_engine/src/categorization/context_extractor.rs` — extração de sentenças-contexto
- [ ] `rust_engine/src/db/copy_writer.rs` — COPY para todas as tabelas (paper, author, keyword, mesh, gene, drug, variant, context, project_paper, clinical_category)
- [ ] `rust_engine/src/db/job_tracker.rs` — mark_running, mark_completed, mark_failed
- [ ] `rust_engine/src/db/connection.rs` — pool de conexões tokio-postgres
- [ ] `apps/core/tasks/ingestion_tasks.py` — Celery task `run_pubmed_ingestion` que chama Rust via PyO3
- [ ] `apps/core/services/search_service.py` — `dispatch_pubmed_search`

#### Fase 3 — Rust Engine Ômicas
- [ ] `rust_engine/src/omics/geo_parser.rs` — parser de metadados GEO (esearch db=gds → esummary)
- [ ] `rust_engine/src/omics/sra_parser.rs` — parser SRA
- [ ] `rust_engine/src/omics/bioproject_parser.rs` — parser BioProject
- [ ] `rust_engine/src/omics/gwas_parser.rs` — parser GWAS Catalog (NHGRI API, não NCBI)
- [ ] `rust_engine/src/omics/type_classifier.rs` — classificação de omic_type + omic_subcategory
- [ ] `rust_engine/src/omics/elink.rs` ou similar — discovery de links dataset↔paper via elink
- [ ] `rust_engine/src/lib.rs` expõe `search_and_ingest_omics` via PyO3
- [ ] `apps/core/tasks/ingestion_tasks.py` — Celery task `run_omics_ingestion`
- [ ] `apps/core/services/search_service.py` — `dispatch_omics_search`

#### Fase 4 — API Curadoria e Análise
- [ ] `apps/core/views/paper_views.py` — `ProjectPaperViewSet` com list, retrieve, partial_update, search (FTS), categorize, bulk_curate
- [ ] `apps/core/views/dataset_views.py` — `ProjectDatasetViewSet` com list, retrieve, partial_update, search (FTS), bulk_curate
- [ ] `apps/core/views/project_views.py` — `DaVinciProjectViewSet` com search, omics_search, stats, export
- [ ] Filtros: curation_status, pub_year, journal, pub_type, omic_type, organism, source_db, clinical_category
- [ ] `apps/core/serializers/paper.py` — `ProjectPaperListSerializer`, `ProjectPaperDetailSerializer`, `ProjectPaperCurateSerializer`
- [ ] `apps/core/serializers/dataset.py` — serializers de dataset
- [ ] `apps/core/services/stats_service.py` — `compute_and_save` com todas as agregações
- [ ] `apps/core/services/export_service.py` — exportação JSON/CSV
- [ ] `ProjectPaperDataset` endpoints (links, confirm, reject)
- [ ] `UserCategory` CRUD endpoints
- [ ] Celery beat configurado para refresh periódico de `ProjectStats`

#### Firebase Auth
- [ ] `apps/accounts/authentication.py` — `FirebaseAuthentication` backend
- [ ] `apps/accounts/services/auth_service.py` — `get_or_create_researcher` (sem Signals)
- [ ] `apps/accounts/views.py` — endpoints `me/` e `update_profile/`
- [ ] `apps/accounts/urls.py`
- [ ] CORS configurado

---

## TAREFA 2 — Verificação de Integridade dos Models e Banco

Execute e salve em `diagnostics/03_db_integrity.log`:

```bash
echo "=== VERIFICAÇÃO DE INTEGRIDADE DO BANCO ===" > diagnostics/03_db_integrity.log

# 2.1 Verificar se migrations estão em dia
echo -e "\n=== MIGRATIONS PENDENTES ===" >> diagnostics/03_db_integrity.log
python manage.py showmigrations 2>&1 >> diagnostics/03_db_integrity.log
python manage.py migrate --check 2>&1 >> diagnostics/03_db_integrity.log

# 2.2 Verificar consistência do schema
echo -e "\n=== MAKEMIGRATIONS (detectar mudanças não migradas) ===" >> diagnostics/03_db_integrity.log
python manage.py makemigrations --dry-run --check 2>&1 >> diagnostics/03_db_integrity.log

# 2.3 Verificar triggers FTS
echo -e "\n=== TRIGGERS NO POSTGRES ===" >> diagnostics/03_db_integrity.log
python manage.py dbshell <<'EOF' 2>&1 >> diagnostics/03_db_integrity.log
SELECT tgname, tgrelid::regclass, tgfoid::regproc
FROM pg_trigger
WHERE tgrelid IN ('core_paper'::regclass, 'core_omicdataset'::regclass)
AND NOT tgisinternal;
EOF

# 2.4 Verificar índices GIN
echo -e "\n=== ÍNDICES GIN ===" >> diagnostics/03_db_integrity.log
python manage.py dbshell <<'EOF' 2>&1 >> diagnostics/03_db_integrity.log
SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename IN ('core_paper', 'core_omicdataset')
AND indexdef LIKE '%gin%';
EOF

# 2.5 Verificar tabelas existentes vs esperadas
echo -e "\n=== TABELAS NO BANCO ===" >> diagnostics/03_db_integrity.log
python manage.py dbshell <<'EOF' 2>&1 >> diagnostics/03_db_integrity.log
SELECT tablename FROM pg_tables
WHERE schemaname = 'public'
AND tablename LIKE 'core_%' OR tablename LIKE 'accounts_%'
ORDER BY tablename;
EOF

# 2.6 Verificar constraints UNIQUE
echo -e "\n=== CONSTRAINTS UNIQUE ===" >> diagnostics/03_db_integrity.log
python manage.py dbshell <<'EOF' 2>&1 >> diagnostics/03_db_integrity.log
SELECT conname, conrelid::regclass, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE contype = 'u'
AND conrelid::regclass::text LIKE 'core_%'
ORDER BY conrelid::regclass, conname;
EOF

# 2.7 Verificar seeds de categorias
echo -e "\n=== CLINICAL CATEGORIES ===" >> diagnostics/03_db_integrity.log
python manage.py shell -c "
from apps.core.models import ClinicalCategory, OmicCategory
print('ClinicalCategory count:', ClinicalCategory.objects.count())
for c in ClinicalCategory.objects.all():
    print(f'  {c.slug}: {c.name} (keywords: {len(c.keywords)} items)')
print()
print('OmicCategory count:', OmicCategory.objects.count())
for c in OmicCategory.objects.all():
    print(f'  {c.omic_type}: priority={c.priority}, keywords={len(c.keywords)} items')
" 2>&1 >> diagnostics/03_db_integrity.log
```

---

## TAREFA 3 — Verificação do Rust Engine

Execute e salve em `diagnostics/04_rust_engine.log`:

```bash
echo "=== VERIFICAÇÃO DO RUST ENGINE ===" > diagnostics/04_rust_engine.log

# 3.1 Verificar compilação
echo -e "\n=== COMPILAÇÃO RUST ===" >> diagnostics/04_rust_engine.log
cd rust_engine && cargo check 2>&1 >> ../diagnostics/04_rust_engine.log
cd ..

# 3.2 Testes unitários Rust
echo -e "\n=== TESTES UNITÁRIOS RUST ===" >> diagnostics/04_rust_engine.log
cd rust_engine && cargo test 2>&1 >> ../diagnostics/04_rust_engine.log
cd ..

# 3.3 Verificar se maturin compila
echo -e "\n=== MATURIN BUILD ===" >> diagnostics/04_rust_engine.log
cd rust_engine && maturin develop --release 2>&1 >> ../diagnostics/04_rust_engine.log
cd ..

# 3.4 Verificar importação do módulo Python
echo -e "\n=== IMPORTAÇÃO PYTHON DO RUST ENGINE ===" >> diagnostics/04_rust_engine.log
python -c "
import rust_engine
print('rust_engine importado com sucesso')
print('Funções disponíveis:', dir(rust_engine))

# Verificar assinaturas
import inspect
for name in dir(rust_engine):
    if not name.startswith('_'):
        obj = getattr(rust_engine, name)
        print(f'  {name}: {type(obj).__name__}')
" 2>&1 >> diagnostics/04_rust_engine.log

# 3.5 Verificar módulos Rust existentes vs esperados
echo -e "\n=== MÓDULOS RUST — CONTEÚDO ===" >> diagnostics/04_rust_engine.log
for f in $(find rust_engine/src -name "*.rs" | sort); do
    echo -e "\n--- $f ---" >> diagnostics/04_rust_engine.log
    head -50 "$f" >> diagnostics/04_rust_engine.log
    echo "..." >> diagnostics/04_rust_engine.log
done

# 3.6 Verificar funções PyO3 expostas no lib.rs
echo -e "\n=== FUNÇÕES PYFUNCTION NO LIB.RS ===" >> diagnostics/04_rust_engine.log
grep -n "#\[pyfunction\]\|#\[pyclass\]\|#\[pymethods\]\|fn .*PyResult" rust_engine/src/lib.rs >> diagnostics/04_rust_engine.log 2>&1
```

---

## TAREFA 4 — Testes de Ingestão de Literatura (PubMed)

Este é o teste mais crítico. Execute e salve em `diagnostics/05_pubmed_ingestion.log`:

```bash
echo "=== TESTE DE INGESTÃO PUBMED ===" > diagnostics/05_pubmed_ingestion.log

python manage.py shell <<'PYEOF' 2>&1 >> diagnostics/05_pubmed_ingestion.log
import traceback
import json
from datetime import datetime

print(f"=== Início: {datetime.now()} ===\n")

# --- PASSO 1: Verificar infraestrutura ---
print("--- PASSO 1: Verificar infraestrutura ---")
try:
    from django.conf import settings
    print(f"DATABASE: {settings.DATABASES['default']['NAME']}")
    print(f"CELERY_BROKER: {getattr(settings, 'CELERY_BROKER_URL', 'NÃO CONFIGURADO')}")
    print(f"NCBI_API_KEY: {'CONFIGURADO' if getattr(settings, 'NCBI_API_KEY', None) else 'NÃO CONFIGURADO'}")
except Exception as e:
    print(f"ERRO infraestrutura: {e}")
    traceback.print_exc()

# --- PASSO 2: Verificar importação do Rust engine ---
print("\n--- PASSO 2: Importar rust_engine ---")
rust_available = False
try:
    import rust_engine
    rust_available = True
    print(f"rust_engine importado: {dir(rust_engine)}")
    
    # Verificar se as funções ômicas existem
    has_pubmed = hasattr(rust_engine, 'search_and_ingest_pubmed')
    has_omics = hasattr(rust_engine, 'search_and_ingest_omics')
    has_variants = hasattr(rust_engine, 'annotate_variants')
    has_genes = hasattr(rust_engine, 'extract_genes_from_abstracts')
    print(f"  search_and_ingest_pubmed: {has_pubmed}")
    print(f"  search_and_ingest_omics: {has_omics}")
    print(f"  annotate_variants: {has_variants}")
    print(f"  extract_genes_from_abstracts: {has_genes}")
except ImportError as e:
    print(f"AVISO: rust_engine não disponível: {e}")
    print("  → O sistema deve funcionar em modo fallback (stub)")

# --- PASSO 3: Verificar models ---
print("\n--- PASSO 3: Verificar models ---")
from apps.core.models import (
    DaVinciProject, Paper, PaperAuthor, PaperKeyword, PaperMeSHTerm,
    PaperGene, PaperVariant, OmicDataset, DatasetPaperLink,
    ProjectPaper, ProjectDataset, ProjectPaperDataset,
    ProjectStats, IngestionJob, ClinicalCategory, OmicCategory,
)
try:
    from apps.core.models import PaperDrug
    print("  PaperDrug: EXISTE")
except ImportError:
    print("  PaperDrug: NÃO EXISTE — FALTANDO NO MODELS")

try:
    from apps.core.models import EntityContext
    print("  EntityContext: EXISTE")
except ImportError:
    print("  EntityContext: NÃO EXISTE — FALTANDO NO MODELS")

try:
    from apps.core.models import UserCategory
    print("  UserCategory: EXISTE")
except ImportError:
    print("  UserCategory: NÃO EXISTE — FALTANDO NO MODELS")

try:
    from apps.core.models import ProjectPaperClinicalCategory
    print("  ProjectPaperClinicalCategory: EXISTE")
except ImportError:
    print("  ProjectPaperClinicalCategory: NÃO EXISTE — FALTANDO NO MODELS")

# --- PASSO 4: Verificar SearchService ---
print("\n--- PASSO 4: Verificar SearchService ---")
try:
    from apps.core.services.search_service import SearchService
    print(f"  SearchService importado")
    print(f"  dispatch_pubmed_search: {hasattr(SearchService, 'dispatch_pubmed_search')}")
    print(f"  dispatch_omics_search: {hasattr(SearchService, 'dispatch_omics_search')}")
except Exception as e:
    print(f"  ERRO: {e}")
    traceback.print_exc()

# --- PASSO 5: Verificar Celery tasks ---
print("\n--- PASSO 5: Verificar Celery tasks ---")
try:
    from apps.core.tasks.ingestion_tasks import run_pubmed_ingestion
    print(f"  run_pubmed_ingestion: OK")
except Exception as e:
    print(f"  ERRO run_pubmed_ingestion: {e}")
try:
    from apps.core.tasks.ingestion_tasks import run_omics_ingestion
    print(f"  run_omics_ingestion: OK")
except Exception as e:
    print(f"  ERRO run_omics_ingestion: {e}")

# --- PASSO 6: Criar projeto de teste ---
print("\n--- PASSO 6: Criar projeto de teste ---")
try:
    from django.contrib.auth import get_user_model
    User = get_user_model()
    user, _ = User.objects.get_or_create(
        username='diagnostic_test_user',
        defaults={'email': 'diag@test.com'}
    )
    project, created = DaVinciProject.objects.get_or_create(
        user=user,
        slug='diag-test-cvd',
        defaults={
            'title': 'Diagnostic Test - CVD',
            'query_term': 'cardiovascular disease',
            'query_synonyms': ['CVD', 'heart disease'],
            'date_from': 2024,
            'date_to': 2025,
            'target_organisms': ['Homo sapiens'],
            'status': 'DRAFT',
        }
    )
    print(f"  Projeto: {project.id} ({'criado' if created else 'existente'})")
    print(f"  query_term: {project.query_term}")
except Exception as e:
    print(f"  ERRO: {e}")
    traceback.print_exc()

# --- PASSO 7: Testar dispatch_pubmed_search (sem executar Celery) ---
print("\n--- PASSO 7: Testar dispatch de busca PubMed ---")
try:
    job = IngestionJob.objects.create(
        project=project,
        job_type='pubmed_search',
        parameters={
            'query': 'cardiovascular disease',
            'date_from': 2024,
            'date_to': 2025,
        }
    )
    print(f"  IngestionJob criado: {job.id}, status={job.status}")
    
    # Testar construção da URL do banco
    from django.conf import settings
    db = settings.DATABASES['default']
    db_url = f"postgresql://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}"
    print(f"  DB URL construída: postgresql://{db['USER']}:***@{db['HOST']}:{db['PORT']}/{db['NAME']}")
    
    # Se Rust disponível, testar chamada direta (com query pequena)
    if rust_available and has_pubmed:
        print("\n  Tentando chamada direta ao Rust (query pequena)...")
        try:
            result = rust_engine.search_and_ingest_pubmed(
                job_id=str(job.id),
                query='hidradenitis AND cancer',  # Query pequena para teste
                db_url=db_url,
                project_id=str(project.id),
                date_from=2024,
                date_to=2025,
                ncbi_api_key=getattr(settings, 'NCBI_API_KEY', None),
            )
            print(f"  RESULTADO: {result}")
            print(f"  records_processed: {result.records_processed if hasattr(result, 'records_processed') else result.get('records_processed', '?')}")
            print(f"  records_inserted: {result.records_inserted if hasattr(result, 'records_inserted') else result.get('records_inserted', '?')}")
        except Exception as e:
            print(f"  ERRO na chamada Rust: {e}")
            traceback.print_exc()
    
except Exception as e:
    print(f"  ERRO: {e}")
    traceback.print_exc()

# --- PASSO 8: Verificar dados no banco após ingestão ---
print("\n--- PASSO 8: Verificar dados no banco ---")
try:
    print(f"  Papers no banco: {Paper.objects.count()}")
    print(f"  PaperAuthors: {PaperAuthor.objects.count()}")
    print(f"  PaperKeywords: {PaperKeyword.objects.count()}")
    print(f"  PaperMeSHTerms: {PaperMeSHTerm.objects.count()}")
    print(f"  PaperGenes: {PaperGene.objects.count()}")
    print(f"  PaperVariants: {PaperVariant.objects.count()}")
    print(f"  OmicDatasets: {OmicDataset.objects.count()}")
    print(f"  DatasetPaperLinks: {DatasetPaperLink.objects.count()}")
    print(f"  ProjectPapers: {ProjectPaper.objects.count()}")
    print(f"  ProjectDatasets: {ProjectDataset.objects.count()}")
    print(f"  IngestionJobs: {IngestionJob.objects.count()}")
    
    # Verificar FTS
    from django.contrib.postgres.search import SearchQuery
    fts_results = Paper.objects.filter(search_vector=SearchQuery('cardiovascular')).count()
    print(f"  FTS 'cardiovascular': {fts_results} resultados")
    
    # Verificar jobs com erro
    failed_jobs = IngestionJob.objects.filter(status='failed')
    if failed_jobs.exists():
        print(f"\n  JOBS COM ERRO ({failed_jobs.count()}):")
        for j in failed_jobs[:5]:
            print(f"    {j.id} | type={j.job_type} | error={j.error_message[:200] if j.error_message else 'None'}")
    
except Exception as e:
    print(f"  ERRO: {e}")
    traceback.print_exc()

print(f"\n=== Fim: {datetime.now()} ===")
PYEOF
```

---

## TAREFA 5 — Testes de Ingestão Ômica (GEO, SRA, BioProject, GWAS)

**Esta é a área com mais erros reportados.** Execute e salve em `diagnostics/06_omics_ingestion.log`:

```bash
echo "=== TESTE DE INGESTÃO ÔMICA ===" > diagnostics/06_omics_ingestion.log

python manage.py shell <<'PYEOF' 2>&1 >> diagnostics/06_omics_ingestion.log
import traceback
import json
from datetime import datetime

print(f"=== Início: {datetime.now()} ===\n")

# --- Verificar se search_and_ingest_omics existe ---
print("--- PASSO 1: Verificar função ômica no Rust ---")
try:
    import rust_engine
    has_omics = hasattr(rust_engine, 'search_and_ingest_omics')
    print(f"  search_and_ingest_omics: {has_omics}")
    
    if has_omics:
        # Inspecionar assinatura
        import inspect
        sig = inspect.signature(rust_engine.search_and_ingest_omics) if callable(rust_engine.search_and_ingest_omics) else "não é callable"
        print(f"  Assinatura: {sig}")
except ImportError:
    print("  rust_engine não disponível")
    has_omics = False

# --- Verificar parsers ômicos no Rust ---
print("\n--- PASSO 2: Verificar módulos ômicos Rust ---")
import os
omics_dir = 'rust_engine/src/omics'
if os.path.exists(omics_dir):
    for f in sorted(os.listdir(omics_dir)):
        filepath = os.path.join(omics_dir, f)
        if f.endswith('.rs'):
            size = os.path.getsize(filepath)
            with open(filepath, 'r') as fh:
                lines = len(fh.readlines())
            print(f"  {f}: {lines} linhas, {size} bytes")
else:
    print(f"  ERRO: Diretório {omics_dir} NÃO EXISTE")

# --- Testar cada fonte ômica individualmente ---
print("\n--- PASSO 3: Testar ingestão ômica ---")

from django.contrib.auth import get_user_model
from apps.core.models import DaVinciProject, IngestionJob, OmicDataset, DatasetPaperLink, ProjectDataset

User = get_user_model()
user = User.objects.get(username='diagnostic_test_user')
project = DaVinciProject.objects.get(slug='diag-test-cvd')

from django.conf import settings
db = settings.DATABASES['default']
db_url = f"postgresql://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}"

if has_omics:
    # Testar com cada fonte separadamente para isolar erros
    sources_to_test = ['geo', 'sra', 'bioproject', 'gwas']
    
    for source in sources_to_test:
        print(f"\n  --- Testando fonte: {source} ---")
        job = IngestionJob.objects.create(
            project=project,
            job_type='geo_search',
            parameters={
                'query': 'cardiovascular disease',
                'sources': [source],
                'max_per_source': 10,  # Limite baixo para teste
                'ncbi_api_key': getattr(settings, 'NCBI_API_KEY', None),
            }
        )
        
        try:
            result = rust_engine.search_and_ingest_omics(
                job_id=str(job.id),
                query='cardiovascular disease',
                db_url=db_url,
                project_id=str(project.id),
                sources=[source],
                max_per_source=10,
                ncbi_api_key=getattr(settings, 'NCBI_API_KEY', None),
                synonyms=['CVD', 'heart disease'],
            )
            print(f"    SUCESSO: {result}")
        except TypeError as e:
            print(f"    ERRO DE ASSINATURA (TypeError): {e}")
            print(f"    → A função Rust provavelmente espera parâmetros diferentes")
            print(f"    → Verificar lib.rs: parâmetros de search_and_ingest_omics")
            traceback.print_exc()
        except ConnectionError as e:
            print(f"    ERRO DE CONEXÃO: {e}")
            print(f"    → Verificar db_url e conexão com Postgres")
        except Exception as e:
            print(f"    ERRO: {type(e).__name__}: {e}")
            traceback.print_exc()
        
        # Verificar job status no banco
        job.refresh_from_db()
        print(f"    Job status: {job.status}")
        if job.error_message:
            print(f"    Job error: {job.error_message[:500]}")
    
    # Resultados após todos os testes
    print(f"\n  --- Resultado acumulado ---")
    print(f"  OmicDatasets total: {OmicDataset.objects.count()}")
    for source in sources_to_test:
        count = OmicDataset.objects.filter(source_db=source).count()
        if count > 0:
            print(f"    {source}: {count} datasets")
            sample = OmicDataset.objects.filter(source_db=source).first()
            print(f"      Exemplo: {sample.accession} | {sample.omic_type} | {sample.organism}")
    print(f"  DatasetPaperLinks: {DatasetPaperLink.objects.count()}")
    print(f"  ProjectDatasets: {ProjectDataset.objects.filter(project=project).count()}")

else:
    print("  SKIP: rust_engine.search_and_ingest_omics não disponível")

# --- Verificar Celery task de ômicas ---
print("\n--- PASSO 4: Verificar Celery task de ômicas ---")
try:
    from apps.core.tasks.ingestion_tasks import run_omics_ingestion
    print("  run_omics_ingestion: importado com sucesso")
    # Verificar código fonte da task
    import inspect
    source = inspect.getsource(run_omics_ingestion)
    print(f"  Código da task ({len(source)} chars):")
    # Verificar se passa os parâmetros corretos para o Rust
    if 'search_and_ingest_omics' in source:
        print("    → Chama rust_engine.search_and_ingest_omics: SIM")
    else:
        print("    → Chama rust_engine.search_and_ingest_omics: NÃO — PROBLEMA!")
    if 'sources' in source:
        print("    → Passa 'sources': SIM")
    else:
        print("    → Passa 'sources': NÃO — POSSÍVEL PROBLEMA")
    if 'max_per_source' in source:
        print("    → Passa 'max_per_source': SIM")
    else:
        print("    → Passa 'max_per_source': NÃO — POSSÍVEL PROBLEMA")
    if 'synonyms' in source:
        print("    → Passa 'synonyms': SIM")
    else:
        print("    → Passa 'synonyms': NÃO — POSSÍVEL PROBLEMA")
except Exception as e:
    print(f"  ERRO: {e}")
    traceback.print_exc()

# --- Verificar SearchService.dispatch_omics_search ---
print("\n--- PASSO 5: Verificar dispatch_omics_search ---")
try:
    from apps.core.services.search_service import SearchService
    if hasattr(SearchService, 'dispatch_omics_search'):
        import inspect
        source = inspect.getsource(SearchService.dispatch_omics_search)
        print(f"  dispatch_omics_search existe ({len(source)} chars)")
        if 'run_omics_ingestion' in source:
            print("    → Despacha para run_omics_ingestion: SIM")
        else:
            print("    → Despacha para run_omics_ingestion: NÃO — PROBLEMA!")
    else:
        print("  dispatch_omics_search: NÃO EXISTE — FALTANDO!")
except Exception as e:
    print(f"  ERRO: {e}")
    traceback.print_exc()

print(f"\n=== Fim: {datetime.now()} ===")
PYEOF
```

---

## TAREFA 6 — Testes da API REST (DRF)

Execute e salve em `diagnostics/07_api_tests.log`:

```bash
echo "=== TESTES DA API DRF ===" > diagnostics/07_api_tests.log

python manage.py shell <<'PYEOF' 2>&1 >> diagnostics/07_api_tests.log
import traceback
from datetime import datetime
from django.test import RequestFactory
from rest_framework.test import APIClient, force_authenticate
from django.contrib.auth import get_user_model

print(f"=== Início: {datetime.now()} ===\n")

User = get_user_model()
user = User.objects.get(username='diagnostic_test_user')
client = APIClient()
client.force_authenticate(user=user)

# --- PASSO 1: Testar CRUD de projetos ---
print("--- PASSO 1: CRUD de projetos ---")
try:
    # List
    r = client.get('/api/v1/projects/')
    print(f"  GET /projects/: {r.status_code}")
    if r.status_code != 200:
        print(f"    ERRO: {r.data}")
    else:
        print(f"    Projetos: {r.data.get('count', len(r.data.get('results', r.data)))}")
    
    # Create
    r = client.post('/api/v1/projects/', {
        'title': 'API Test Project',
        'query_term': 'diabetes',
        'query_synonyms': ['DM', 'diabetes mellitus'],
        'date_from': 2023,
        'date_to': 2025,
    }, format='json')
    print(f"  POST /projects/: {r.status_code}")
    if r.status_code in (200, 201):
        project_id = r.data.get('id')
        print(f"    Projeto criado: {project_id}")
    else:
        print(f"    ERRO: {r.data}")
        project_id = None
except Exception as e:
    print(f"  ERRO: {e}")
    traceback.print_exc()
    project_id = None

# --- PASSO 2: Testar endpoint de busca ---
print("\n--- PASSO 2: Endpoints de busca ---")
if project_id:
    try:
        r = client.post(f'/api/v1/projects/{project_id}/search/')
        print(f"  POST /projects/{project_id}/search/: {r.status_code}")
        if r.status_code in (200, 201, 202):
            print(f"    Job: {r.data}")
        else:
            print(f"    ERRO: {r.data}")
        
        r = client.post(f'/api/v1/projects/{project_id}/omics_search/', {
            'sources': ['geo', 'sra'],
            'max_per_source': 5,
        }, format='json')
        print(f"  POST /projects/{project_id}/omics_search/: {r.status_code}")
        if r.status_code in (200, 201, 202):
            print(f"    Job: {r.data}")
        else:
            print(f"    ERRO: {r.data}")
    except Exception as e:
        print(f"  ERRO: {e}")
        traceback.print_exc()

# --- PASSO 3: Testar endpoints de papers ---
print("\n--- PASSO 3: Endpoints de papers ---")
from apps.core.models import DaVinciProject
project_with_papers = DaVinciProject.objects.filter(
    user=user, projectpaper__isnull=False
).first()

if project_with_papers:
    pid = project_with_papers.id
    try:
        r = client.get(f'/api/v1/projects/{pid}/papers/')
        print(f"  GET /papers/: {r.status_code}")
        if r.status_code == 200:
            count = r.data.get('count', len(r.data.get('results', r.data)))
            print(f"    Papers: {count}")
        else:
            print(f"    ERRO: {r.data}")
        
        # FTS
        r = client.get(f'/api/v1/projects/{pid}/papers/search/?q=cardiovascular')
        print(f"  GET /papers/search/?q=cardiovascular: {r.status_code}")
        if r.status_code == 200:
            count = r.data.get('count', len(r.data.get('results', r.data)))
            print(f"    Resultados FTS: {count}")
        else:
            print(f"    ERRO: {r.data}")
        
        # Filtros
        r = client.get(f'/api/v1/projects/{pid}/papers/?curation_status=pending')
        print(f"  GET /papers/?curation_status=pending: {r.status_code}")
        
        # Bulk curate
        r = client.get(f'/api/v1/projects/{pid}/papers/')
        if r.status_code == 200:
            results = r.data.get('results', r.data)
            if results and len(results) >= 2:
                ids = [results[0]['id'], results[1]['id']]
                r = client.post(f'/api/v1/projects/{pid}/papers/bulk_curate/', {
                    'paper_ids': ids,
                    'curation_status': 'included',
                }, format='json')
                print(f"  POST /papers/bulk_curate/: {r.status_code}")
                if r.status_code not in (200, 204):
                    print(f"    ERRO: {r.data}")
    except Exception as e:
        print(f"  ERRO: {e}")
        traceback.print_exc()
else:
    print("  SKIP: Nenhum projeto com papers encontrado")

# --- PASSO 4: Testar endpoints de datasets ---
print("\n--- PASSO 4: Endpoints de datasets ---")
project_with_datasets = DaVinciProject.objects.filter(
    user=user, projectdataset__isnull=False
).first()

if project_with_datasets:
    pid = project_with_datasets.id
    try:
        r = client.get(f'/api/v1/projects/{pid}/datasets/')
        print(f"  GET /datasets/: {r.status_code}")
        if r.status_code == 200:
            count = r.data.get('count', len(r.data.get('results', r.data)))
            print(f"    Datasets: {count}")
        else:
            print(f"    ERRO: {r.data}")
        
        # FTS
        r = client.get(f'/api/v1/projects/{pid}/datasets/search/?q=cardiovascular')
        print(f"  GET /datasets/search/?q=cardiovascular: {r.status_code}")
        
        # Filtros
        r = client.get(f'/api/v1/projects/{pid}/datasets/?omic_type=transcriptomic')
        print(f"  GET /datasets/?omic_type=transcriptomic: {r.status_code}")
    except Exception as e:
        print(f"  ERRO: {e}")
        traceback.print_exc()
else:
    print("  SKIP: Nenhum projeto com datasets encontrado")

# --- PASSO 5: Testar stats ---
print("\n--- PASSO 5: Endpoints de stats ---")
if project_with_papers:
    try:
        r = client.get(f'/api/v1/projects/{project_with_papers.id}/stats/')
        print(f"  GET /stats/: {r.status_code}")
        if r.status_code == 200:
            for key in ['total_papers', 'included_papers', 'total_datasets', 'top_genes']:
                print(f"    {key}: {r.data.get(key, 'MISSING')}")
        else:
            print(f"    ERRO: {r.data}")
    except Exception as e:
        print(f"  ERRO: {e}")
        traceback.print_exc()

# --- PASSO 6: Testar export ---
print("\n--- PASSO 6: Endpoint de export ---")
if project_with_papers:
    try:
        r = client.get(f'/api/v1/projects/{project_with_papers.id}/export/?format=json')
        print(f"  GET /export/?format=json: {r.status_code}")
        if r.status_code == 200:
            print(f"    Tipo de resposta: {type(r.data)}")
        else:
            print(f"    ERRO: {r.data}")
    except Exception as e:
        print(f"  ERRO: {e}")
        traceback.print_exc()

# --- PASSO 7: Testar links (ProjectPaperDataset) ---
print("\n--- PASSO 7: Endpoints de links ---")
if project_with_papers:
    try:
        r = client.get(f'/api/v1/projects/{project_with_papers.id}/links/')
        print(f"  GET /links/: {r.status_code}")
        if r.status_code == 200:
            count = r.data.get('count', len(r.data.get('results', r.data)))
            print(f"    Links: {count}")
        elif r.status_code == 404:
            print(f"    ENDPOINT NÃO EXISTE (404) — FALTANDO IMPLEMENTAR")
        else:
            print(f"    ERRO: {r.data}")
    except Exception as e:
        print(f"  ERRO: {e}")
        traceback.print_exc()

# --- PASSO 8: Testar jobs ---
print("\n--- PASSO 8: Endpoints de jobs ---")
if project_with_papers:
    try:
        r = client.get(f'/api/v1/projects/{project_with_papers.id}/jobs/')
        print(f"  GET /jobs/: {r.status_code}")
        if r.status_code == 200:
            count = r.data.get('count', len(r.data.get('results', r.data)))
            print(f"    Jobs: {count}")
        else:
            print(f"    ERRO: {r.data}")
    except Exception as e:
        print(f"  ERRO: {e}")
        traceback.print_exc()

# --- PASSO 9: Testar auth ---
print("\n--- PASSO 9: Endpoints de auth ---")
try:
    r = client.get('/api/v1/auth/me/')
    print(f"  GET /auth/me/: {r.status_code}")
    if r.status_code == 200:
        print(f"    User data: {list(r.data.keys())}")
    else:
        print(f"    ERRO: {r.data}")
except Exception as e:
    print(f"  ERRO: {e}")
    traceback.print_exc()

# --- PASSO 10: Testar categories ---
print("\n--- PASSO 10: Endpoints de categorias ---")
try:
    r = client.get('/api/v1/clinical-categories/')
    print(f"  GET /clinical-categories/: {r.status_code}")
    if r.status_code == 200:
        count = r.data.get('count', len(r.data.get('results', r.data)))
        print(f"    ClinicalCategories: {count}")
    elif r.status_code == 404:
        print(f"    ENDPOINT NÃO EXISTE (404) — FALTANDO IMPLEMENTAR")
    else:
        print(f"    ERRO: {r.data}")
except Exception as e:
    print(f"  ERRO: {e}")
    traceback.print_exc()

if project_with_papers:
    try:
        r = client.get(f'/api/v1/projects/{project_with_papers.id}/categories/')
        print(f"  GET /categories/ (UserCategory): {r.status_code}")
        if r.status_code == 404:
            print(f"    ENDPOINT NÃO EXISTE (404) — FALTANDO IMPLEMENTAR")
    except Exception as e:
        print(f"  ERRO: {e}")

print(f"\n=== Fim: {datetime.now()} ===")
PYEOF
```

---

## TAREFA 7 — Testes Unitários Django (pytest ou manage.py test)

Execute os testes existentes e salve em `diagnostics/08_django_tests.log`:

```bash
echo "=== TESTES UNITÁRIOS DJANGO ===" > diagnostics/08_django_tests.log

# Rodar testes com verbosidade máxima
python manage.py test apps/ -v 3 --traceback 2>&1 >> diagnostics/08_django_tests.log

# Se usar pytest:
# pytest apps/ -v --tb=long 2>&1 >> diagnostics/08_django_tests.log
```

---

## TAREFA 8 — Verificação de Consistência entre Rust e Django

Salve em `diagnostics/09_rust_django_consistency.log`:

```bash
echo "=== CONSISTÊNCIA RUST ↔ DJANGO ===" > diagnostics/09_rust_django_consistency.log

python manage.py shell <<'PYEOF' 2>&1 >> diagnostics/09_rust_django_consistency.log
import traceback

print("=== Verificação de consistência entre Rust structs e Django models ===\n")

# Verificar se os nomes das tabelas no Rust correspondem aos do Django
from django.apps import apps

print("--- Tabelas Django (db_table) ---")
expected_tables = {}
for model in apps.get_app_config('core').get_models():
    table = model._meta.db_table
    fields = [f.column for f in model._meta.get_fields() if hasattr(f, 'column')]
    expected_tables[table] = fields
    print(f"  {table}: {fields}")

print("\n--- Tabelas accounts ---")
try:
    for model in apps.get_app_config('accounts').get_models():
        table = model._meta.db_table
        fields = [f.column for f in model._meta.get_fields() if hasattr(f, 'column')]
        expected_tables[table] = fields
        print(f"  {table}: {fields}")
except Exception as e:
    print(f"  ERRO: {e}")

# Verificar que as tabelas críticas existem
print("\n--- Tabelas críticas para COPY do Rust ---")
critical_tables = [
    'core_paper',
    'core_paperauthor',
    'core_paperkeyword',
    'core_papermeshterm',
    'core_papergene',
    'core_paperdrug',
    'core_papervariant',
    'core_entitycontext',
    'core_omicdataset',
    'core_datasetpaperlink',
    'core_projectpaper',
    'core_projectdataset',
    'core_projectpaperclinicalcategory',
    'core_ingestionjob',
]

from django.db import connection
with connection.cursor() as cursor:
    cursor.execute("""
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public'
        ORDER BY tablename
    """)
    existing = {row[0] for row in cursor.fetchall()}

for table in critical_tables:
    exists = table in existing
    status = "✅" if exists else "❌ MISSING"
    print(f"  {status} {table}")
    
    if exists:
        # Verificar colunas
        with connection.cursor() as cursor:
            cursor.execute(f"""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_name = '{table}'
                ORDER BY ordinal_position
            """)
            cols = cursor.fetchall()
            print(f"       Colunas: {[c[0] for c in cols]}")

# Verificar campos que o Rust precisa escrever
print("\n--- Verificação de campos críticos para COPY ---")

# Paper: o Rust precisa de search_vector como SearchVectorField
try:
    from apps.core.models import Paper
    sv = Paper._meta.get_field('search_vector')
    print(f"  Paper.search_vector: {sv.__class__.__name__} ✅")
except Exception as e:
    print(f"  Paper.search_vector: ERRO - {e}")

# OmicDataset: mesmo
try:
    from apps.core.models import OmicDataset
    sv = OmicDataset._meta.get_field('search_vector')
    print(f"  OmicDataset.search_vector: {sv.__class__.__name__} ✅")
except Exception as e:
    print(f"  OmicDataset.search_vector: ERRO - {e}")

# Verificar ON CONFLICT constraints
print("\n--- Constraints UNIQUE necessárias para ON CONFLICT ---")
required_unique = {
    'core_paper': 'pmid',
    'core_omicdataset': 'accession',
    'core_paperauthor': '(paper_id, position)',
    'core_paperkeyword': '(paper_id, keyword_lower)',
    'core_papermeshterm': '(paper_id, descriptor, qualifier)',
    'core_papergene': '(paper_id, gene_symbol)',
    'core_paperdrug': '(paper_id, drug_name_lower)',
    'core_papervariant': '(paper_id, rs_number)',
    'core_datasetpaperlink': '(dataset_id, paper_id)',
    'core_projectpaper': '(project_id, paper_id)',
    'core_projectdataset': '(project_id, dataset_id)',
    'core_projectpaperclinicalcategory': '(project_paper_id, category_id)',
}

with connection.cursor() as cursor:
    for table, expected_cols in required_unique.items():
        cursor.execute(f"""
            SELECT conname, pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE contype = 'u' AND conrelid = '{table}'::regclass
        """)
        constraints = cursor.fetchall()
        if constraints:
            print(f"  ✅ {table} ({expected_cols}): {constraints[0][0]}")
        else:
            # Verificar se é unique_together no model
            print(f"  ⚠️  {table} ({expected_cols}): verificar se UNIQUE constraint existe no banco")

print("\n=== Fim ===")
PYEOF
```

---

## TAREFA 9 — Diagnóstico Específico dos Erros de Ômicas

Este é o foco principal do problema. Salve em `diagnostics/10_omics_deep_debug.log`:

```bash
echo "=== DIAGNÓSTICO PROFUNDO — ERROS ÔMICAS ===" > diagnostics/10_omics_deep_debug.log

# 9.1 Verificar se o Rust compila os módulos ômicos sem erros
echo -e "\n=== COMPILAÇÃO MÓDULOS ÔMICOS ===" >> diagnostics/10_omics_deep_debug.log
cd rust_engine
cargo check 2>&1 | grep -E "error|warning|omics" >> ../diagnostics/10_omics_deep_debug.log
cd ..

# 9.2 Verificar conteúdo completo dos parsers ômicos
echo -e "\n=== CONTEÚDO DOS PARSERS ÔMICOS ===" >> diagnostics/10_omics_deep_debug.log
for f in rust_engine/src/omics/*.rs; do
    echo -e "\n========== $f ==========" >> diagnostics/10_omics_deep_debug.log
    cat "$f" >> diagnostics/10_omics_deep_debug.log 2>&1
done

# 9.3 Verificar lib.rs — assinatura da função ômica
echo -e "\n=== LIB.RS (search_and_ingest_omics) ===" >> diagnostics/10_omics_deep_debug.log
cat rust_engine/src/lib.rs >> diagnostics/10_omics_deep_debug.log

# 9.4 Verificar copy_writer para datasets
echo -e "\n=== COPY WRITER (datasets) ===" >> diagnostics/10_omics_deep_debug.log
cat rust_engine/src/db/copy_writer.rs >> diagnostics/10_omics_deep_debug.log 2>&1

# 9.5 Verificar Celery task de ômicas
echo -e "\n=== CELERY TASK (omics) ===" >> diagnostics/10_omics_deep_debug.log
cat apps/core/tasks/ingestion_tasks.py >> diagnostics/10_omics_deep_debug.log 2>&1

# 9.6 Verificar SearchService
echo -e "\n=== SEARCH SERVICE ===" >> diagnostics/10_omics_deep_debug.log
cat apps/core/services/search_service.py >> diagnostics/10_omics_deep_debug.log 2>&1

# 9.7 Verificar logs de erro de jobs anteriores
echo -e "\n=== JOBS COM ERRO (histórico) ===" >> diagnostics/10_omics_deep_debug.log
python manage.py shell -c "
from apps.core.models import IngestionJob
failed = IngestionJob.objects.filter(status='failed').order_by('-created_at')[:20]
for j in failed:
    print(f'Job {j.id}')
    print(f'  Type: {j.job_type}')
    print(f'  Created: {j.created_at}')
    print(f'  Params: {j.parameters}')
    print(f'  Error: {j.error_message}')
    print(f'  Processed: {j.records_processed}')
    print()
" 2>&1 >> diagnostics/10_omics_deep_debug.log

# 9.8 Verificar se a comunicação Rust ↔ NCBI funciona para ômicas
echo -e "\n=== TESTE MANUAL NCBI API (ômicas) ===" >> diagnostics/10_omics_deep_debug.log
python -c "
import requests
import json

# Testar esearch no db=gds (GEO)
print('--- GEO (db=gds) ---')
try:
    r = requests.get('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi', params={
        'db': 'gds',
        'term': 'cardiovascular disease',
        'retmax': 3,
        'retmode': 'json'
    }, timeout=30)
    print(f'  Status: {r.status_code}')
    data = r.json()
    print(f'  Count: {data.get(\"esearchresult\", {}).get(\"count\", \"?\")}')
    print(f'  IDs: {data.get(\"esearchresult\", {}).get(\"idlist\", [])[:3]}')
except Exception as e:
    print(f'  ERRO: {e}')

# Testar esearch no db=sra
print('--- SRA (db=sra) ---')
try:
    r = requests.get('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi', params={
        'db': 'sra',
        'term': 'cardiovascular disease',
        'retmax': 3,
        'retmode': 'json'
    }, timeout=30)
    print(f'  Status: {r.status_code}')
    data = r.json()
    print(f'  Count: {data.get(\"esearchresult\", {}).get(\"count\", \"?\")}')
except Exception as e:
    print(f'  ERRO: {e}')

# Testar esearch no db=bioproject
print('--- BioProject ---')
try:
    r = requests.get('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi', params={
        'db': 'bioproject',
        'term': 'cardiovascular disease',
        'retmax': 3,
        'retmode': 'json'
    }, timeout=30)
    print(f'  Status: {r.status_code}')
    data = r.json()
    print(f'  Count: {data.get(\"esearchresult\", {}).get(\"count\", \"?\")}')
except Exception as e:
    print(f'  ERRO: {e}')

# Testar GWAS Catalog
print('--- GWAS Catalog ---')
try:
    r = requests.get('https://www.ebi.ac.uk/gwas/rest/api/search/associations', params={
        'q': 'cardiovascular disease',
        'max': 3,
    }, timeout=30)
    print(f'  Status: {r.status_code}')
    if r.status_code == 200:
        data = r.json()
        print(f'  Response keys: {list(data.keys())[:5]}')
except Exception as e:
    print(f'  ERRO: {e}')
" 2>&1 >> diagnostics/10_omics_deep_debug.log
```

---

## TAREFA 10 — Gerar Relatório Final de Diagnóstico

Após executar todas as tarefas acima, gere um relatório consolidado em `diagnostics/FINAL_REPORT.md`:

```bash
python manage.py shell <<'PYEOF' > diagnostics/FINAL_REPORT.md
print("# DaVinci — Relatório Final de Diagnóstico")
print()
print("## Arquivos de Log Gerados")
print()

import os
for f in sorted(os.listdir('diagnostics')):
    if f.endswith('.log') or f.endswith('.md'):
        size = os.path.getsize(f'diagnostics/{f}')
        print(f"- `diagnostics/{f}` ({size} bytes)")

print()
print("## Instruções para o Desenvolvedor")
print()
print("1. Leia TODOS os arquivos de log na pasta `diagnostics/`")
print("2. Para cada erro encontrado, o log indica:")
print("   - Arquivo e linha onde o problema está")
print("   - Tipo de erro (falta de implementação, bug, inconsistência)")
print("   - Sugestão de correção")
print("3. Priorize os erros na seguinte ordem:")
print("   a. Erros de compilação Rust (bloqueiam tudo)")
print("   b. Erros de ingestão ômica (problema principal reportado)")
print("   c. Erros de ingestão PubMed")
print("   d. Erros de API/endpoints")
print("   e. Features faltantes do roadmap")
print("4. Após corrigir, rode novamente este prompt de diagnóstico")
PYEOF
```

---

## TAREFA 11 — Implementação de Features Faltantes

Com base no inventário (Tarefa 1) e no checklist do roadmap, **implemente TUDO o que estiver faltando**. As prioridades são:

### Prioridade 1 — Corrigir bugs de ingestão ômica
Os erros mais comuns neste tipo de sistema são:
1. **Mismatch de assinatura PyO3:** A função Rust espera parâmetros que o Python não está passando (ou vice-versa). Compare `lib.rs` com `ingestion_tasks.py` linha por linha.
2. **URLs/endpoints NCBI errados:** O `db=gds` (GEO DataSets) usa `esummary` com `retmode=json`, não `efetch` com XML. O SRA usa `esummary` com XML. Verificar cada parser.
3. **Parsing XML/JSON incorreto:** O formato de resposta do NCBI varia por database. GEO retorna JSON via esummary, SRA retorna XML.
4. **COPY com colunas erradas:** Se o Rust tenta fazer COPY com colunas que não existem no schema, falha silenciosamente ou com erro genérico.
5. **GWAS Catalog API:** A API do GWAS Catalog (EBI) é REST, não NCBI E-utilities. A URL base e o formato de resposta são completamente diferentes.
6. **Connection pool exausto:** Se o Rust abre muitas conexões ao Postgres sem fechar, jobs subsequentes falham.

### Prioridade 2 — Features faltantes do roadmap
Verificar e implementar tudo que estiver marcado como ❌ no checklist da Tarefa 1.

### Prioridade 3 — Testes
Criar testes para cada feature implementada, seguindo o padrão TDD descrito no documento:
1. Mock/Fixture
2. Rust unit test
3. Integration test (Postgres)
4. API test (DRF)

---

## RESUMO: O Que Eu Espero de Volta

Após executar este prompt, me traga:

1. **`diagnostics/01_inventory.log`** — O que existe no projeto
2. **`diagnostics/02_roadmap_checklist.md`** — Checklist do roadmap (✅/⚠️/❌)
3. **`diagnostics/03_db_integrity.log`** — Estado do banco de dados
4. **`diagnostics/04_rust_engine.log`** — Compilação e testes Rust
5. **`diagnostics/05_pubmed_ingestion.log`** — Teste de ingestão PubMed
6. **`diagnostics/06_omics_ingestion.log`** — Teste de ingestão ômica (foco principal)
7. **`diagnostics/07_api_tests.log`** — Testes da API REST
8. **`diagnostics/08_django_tests.log`** — Testes unitários Django
9. **`diagnostics/09_rust_django_consistency.log`** — Consistência Rust ↔ Django
10. **`diagnostics/10_omics_deep_debug.log`** — Debug profundo dos erros ômicos
11. **`diagnostics/FINAL_REPORT.md`** — Relatório consolidado
12. **Lista de correções sugeridas** com arquivo, linha e o que mudar
13. **Lista de features faltantes** do roadmap com status de implementação

Esses logs serão usados para diagnosticar e corrigir todos os problemas do DaVinci de forma sistemática.
