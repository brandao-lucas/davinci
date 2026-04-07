# DaVinci — Roadmap Checklist

## Fase 1 — Fundação

- [x] `apps/core/models.py` contém TODOS os models: DaVinciProject, Paper, PaperAuthor, PaperKeyword, PaperMeSHTerm, PaperGene, PaperDrug, PaperVariant, VariantAnnotation, EntityContext, OmicDataset, DatasetPaperLink, OmicCategory, ClinicalCategory, UserCategory, ProjectPaper, ProjectPaperClinicalCategory, ProjectDataset, ProjectPaperDataset, ProjectStats, IngestionJob
- [x] `apps/accounts/models.py` contém UserProfile com campos: firebase_uid, auth_provider, orcid_id, institution, research_area, avatar_url, ncbi_api_key, last_firebase_sync
- [x] Migration RunSQL com triggers FTS para Paper e OmicDataset (0002_fts_triggers.py)
- [x] Migration RunSQL com índices GIN em search_vector (0002_fts_triggers.py)
- [x] `config/celery.py` existe e está configurado
- [x] `docker-compose.yml` com Postgres 16 + Redis
- [ ] ❌ Management command `seed_categories.py` — NÃO EXISTE (sem diretório management/commands/)
- [x] DRF configurado com FirebaseAuthentication, paginação, filtros

## Fase 2 — Rust Engine Literatura

- [x] `rust_engine/src/lib.rs` — entry point PyO3 com `search_and_ingest_pubmed`
- [x] `rust_engine/src/ncbi/client.rs` — HTTP client com rate limiting, backoff exponencial
- [x] `rust_engine/src/ncbi/parser.rs` — quick-xml parser completo (PMID, título, abstract, autores, etc.)
- [x] `rust_engine/src/ncbi/models.rs` — structs PaperData, AuthorData, MeSHTerm
- [x] `rust_engine/src/categorization/clinical.rs` — regex para 5 eixos clínicos
- [x] ⚠️ `rust_engine/src/categorization/gene_ner.rs` — APENAS 6 genes hardcoded (BRCA1, TP53, EGFR, TNF, IL6, BRAF)
- [x] ⚠️ `rust_engine/src/categorization/drug_ner.rs` — APENAS 6 drogas hardcoded
- [x] ⚠️ `rust_engine/src/categorization/context_extractor.rs` — implementação naive (sentence split)
- [x] `rust_engine/src/db/copy_writer.rs` — COPY para todas as tabelas (771 linhas, 10 funções)
- [x] `rust_engine/src/db/job_tracker.rs` — update_job_status
- [x] `rust_engine/src/db/connection.rs` — tokio-postgres connection
- [x] `apps/core/tasks/ingestion_tasks.py` — Celery task run_pubmed_ingestion
- [x] `apps/core/services/search_service.py` — dispatch_pubmed_search

## Fase 3 — Rust Engine Ômicas

- [x] `rust_engine/src/omics/geo_parser.rs` — parser de metadados GEO (esearch → esummary)
- [x] `rust_engine/src/omics/sra_parser.rs` — parser SRA
- [x] `rust_engine/src/omics/bioproject_parser.rs` — parser BioProject
- [x] `rust_engine/src/omics/gwas_parser.rs` — parser GWAS Catalog (EBI REST API)
- [x] `rust_engine/src/omics/type_classifier.rs` — classificação de omic_type + subcategory
- [x] `rust_engine/src/omics/elink.rs` — discovery de links dataset↔paper via elink
- [x] `rust_engine/src/lib.rs` expõe `search_and_ingest_omics` via PyO3
- [x] `apps/core/tasks/ingestion_tasks.py` — Celery task run_omics_ingestion
- [x] `apps/core/services/search_service.py` — dispatch_omics_search

## Fase 4 — API Curadoria e Análise

- [x] `apps/core/views/paper_views.py` — ProjectPaperViewSet com list, retrieve, partial_update, search, categorize, bulk_curate
- [x] `apps/core/views/dataset_views.py` — ProjectDatasetViewSet com list, retrieve, partial_update, search, bulk_curate
- [x] `apps/core/views/project_views.py` — DaVinciProjectViewSet com search, omics_search, stats, export
- [x] Filtros: curation_status, pub_year, journal, pub_type, omic_type, organism, source_db, clinical_category
- [x] `apps/core/serializers/paper.py` — ProjectPaperListSerializer, ProjectPaperDetailSerializer, ProjectPaperCurateSerializer
- [x] `apps/core/serializers/dataset.py` — serializers de dataset
- [x] `apps/core/services/stats_service.py` — compute_and_save com agregações
- [ ] ❌ `apps/core/services/export_service.py` — NÃO EXISTE (export inline em project_views.py)
- [x] ProjectPaperDataset endpoints (links, confirm, reject) — link_views.py
- [x] UserCategory CRUD endpoints — category_views.py
- [x] ⚠️ Celery beat configurado — MAS path incorreto (referencia stats_tasks, deveria ser stats_tasks existente)

## Firebase Auth

- [x] `apps/accounts/authentication.py` — FirebaseAuthentication backend
- [x] `apps/accounts/services/user_service.py` — get_or_create_from_firebase (via UserService, sem Signals)
- [x] `apps/accounts/views.py` — endpoints me/ e verify/
- [x] `apps/accounts/urls.py`
- [x] CORS configurado (CORS_ALLOW_ALL_ORIGINS = True em local.py)

## Resumo

| Fase | Total | ✅ | ⚠️ | ❌ |
|------|-------|----|----|-----|
| 1 - Fundação | 8 | 7 | 0 | 1 |
| 2 - Rust Literatura | 13 | 10 | 3 | 0 |
| 3 - Rust Ômicas | 9 | 9 | 0 | 0 |
| 4 - API | 11 | 9 | 1 | 1 |
| Auth | 5 | 5 | 0 | 0 |
| **TOTAL** | **46** | **40** | **4** | **2** |

### Items Faltantes (❌)
1. Management command `seed_categories.py`
2. `export_service.py` dedicado

### Items Incompletos (⚠️)
1. Gene NER com apenas 6 genes hardcoded
2. Drug NER com apenas 6 drogas hardcoded
3. Context extractor naive
4. Celery beat path possivelmente incorreto
