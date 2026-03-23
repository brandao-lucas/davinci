# DaVinci — Prompt de Desenvolvimento Completo

## Resumo Executivo

O **DaVinci** é um módulo do ecossistema **PlatOmics** para download, processamento e análise integrada de literatura científica (PubMed/PMC) e metadados de bases ômicas (GEO, SRA, BioProject). O pesquisador informa um termo de busca (ex: "Doenças Cardiovasculares") e o sistema varre automaticamente as bases públicas, organiza os resultados, permite curadoria manual (inclusão/exclusão com auditoria), realiza análises cruzadas entre literatura e dados ômicos, e exporta outputs estruturados que servirão como base de conhecimento para uma futura camada de IA generativa.

A stack é **Python (Django) + Rust + PostgreSQL**, com o Django atuando como orquestrador e API, o Rust como engine de ingestão e parsing de alta performance, e o PostgreSQL como o centro de gravidade dos dados com Full-Text Search nativo.

---

## 1. Visão Geral da Arquitetura

### 1.1 Princípio Fundamental

O Django **nunca** processa dados brutos. Ele gerencia metadados, orquestra tarefas e expõe resultados via API. Todo processamento pesado — fetch HTTP das APIs do NCBI, parsing de XML, categorização heurística, injeção em massa no banco — é responsabilidade do Rust engine. A comunicação entre Django e Rust acontece via PyO3 (chamada direta ao binário Rust como módulo Python) e via tabela de controle `IngestionJob` no PostgreSQL.

### 1.2 Fluxo Principal

```
Pesquisador → Django (cria projeto + define query)
    ↓
Django → cria IngestionJob no Postgres
    ↓
Django → dispara Rust engine via PyO3
    ↓
Rust → faz UM ÚNICO esearch + efetch ao NCBI
Rust → parseia XML completo com quick-xml (todos os campos em uma passada)
Rust → categorização clínica (regex → ClinicalCategory) + NER (genes, drogas, variantes)
Rust → acumula em memória via IndexMap (dedup)
Rust → injeta no Postgres via COPY (bypassa ORM do Django)
Rust → atualiza IngestionJob.status = 'completed'
    ↓
Django → detecta job completo (polling ou webhook)
Django → expõe dados via DRF (consulta Views Materializadas)
    ↓
Pesquisador → curadoria (inclui/exclui papers e datasets)
Pesquisador → categorização clínica + categorias customizadas
Pesquisador → análise integrada (literatura ↔ ômicas)
Pesquisador → exportação para IA generativa (incluindo contextos semânticos)
```

### 1.3 Diagrama de Responsabilidades

| Camada | Responsabilidade | Tecnologia | Detalhes |
|--------|-----------------|------------|----------|
| **Orquestração** | Gestão de projetos, filas, parâmetros de busca | Django + Celery + Redis | Django cria jobs e chama o Rust via PyO3. Celery gerencia filas assíncronas. |
| **Ingestão Literatura** | Fetch HTTP + parsing XML do PubMed/PMC | Rust (`reqwest` + `tokio` + `quick-xml`) | UMA chamada por query. Extrai: PMID, título, abstract, autores, afiliações, keywords, MeSH, DOI, journal, data. NER extrai genes, drogas e variantes dos abstracts. |
| **Ingestão Metadados Ômicos** | Fetch + parsing de metadados GEO/SRA/BioProject/GWAS | Rust (`reqwest` + `tokio` + `quick-xml`) | Busca via elink/esearch nas bases ômicas e GWAS Catalog. Normaliza accessions e classifica por tipo ômico. |
| **Categorização** | Classificação heurística de papers e datasets | Rust (regex compilados + scoring) | Categoriza papers por eixo clínico (diagnosis, treatment, epidemiology, mechanism, signs_symptoms) e datasets por tipo de ômica. Injeta categorias junto com os dados via COPY. |
| **Extração Semântica** | NER + contextos de entidades | Rust (regex + sentence splitting) | Extrai genes, drogas, variantes dos abstracts. Persiste sentenças-contexto em `EntityContext` para a camada de IA generativa. |
| **Persistência** | Armazenamento e busca textual | PostgreSQL 16+ | FTS via `tsvector` com triggers. Views Materializadas para stats. COPY para ingestão em massa. |
| **Distribuição** | APIs REST para o frontend | Django Rest Framework (DRF) | Services consultam Views Materializadas. Sem Signals, sem lógica pesada nos Views. |
| **Frontend** | Interface do pesquisador | Desenvolvido separadamente (Gemini Pro) | Consome a API DRF. Não faz parte deste prompt. |

---

## 2. Regras Arquiteturais Invioláveis

Estas regras são **absolutas** e devem ser seguidas em todo o código do DaVinci:

### 2.1 Django

1. **PROIBIDO usar Django Signals.** Toda lógica de pós-processamento deve ser encapsulada em Services ou Actions dentro de um diretório `services/` no app Django.
2. **PROIBIDO processar dados brutos no Django.** O Django lê dados já processados do Postgres. O ORM é usado apenas para consultas e para a camada de administração.
3. **A ingestão via Rust ignora o ORM.** O Rust faz INSERT/COPY diretamente no Postgres. Após a carga, o Rust atualiza a tabela `IngestionJob` que o Django monitora.
4. **Services over Views.** Os ViewSets do DRF devem ser finos — a lógica de negócio fica nos Services. Um ViewSet chama um Service, que consulta o banco (preferencialmente uma View Materializada) e retorna o resultado.
5. **Celery + Redis para tarefas assíncronas.** O Django despacha tarefas longas (busca NCBI, anotação de variantes) para o Celery. O Celery chama o Rust engine. O Django nunca bloqueia esperando o Rust terminar.

### 2.2 Rust

1. **UM ÚNICO fetch por query.** O Rust faz UMA chamada `esearch` + `efetch`, armazena o XML completo em memória, e extrai TODOS os campos (PMID, título, abstract, autores, afiliações, keywords, MeSH, DOI, journal, data) em uma única passada com `quick-xml`.
2. **`tokio` para I/O, `rayon` para CPU.** Chamadas HTTP ao NCBI são I/O-bound (usar `tokio` + `reqwest`). Parsing de XML e processamento de arquivos multi-ômicos grandes são CPU-bound (usar `rayon` para paralelismo).
3. **`memmap2` para arquivos grandes.** Metadados gigantes de bases como o TCGA devem ser mapeados em memória sem carregar tudo na RAM.
4. **`IndexMap` como buffer de dedup.** Antes de injetar no Postgres, o Rust acumula os dados processados em um `IndexMap<PMID, PaperData>` para evitar duplicatas.
5. **Injeção via COPY.** O Rust prepara os dados no formato CSV/binary e usa `COPY FROM STDIN` para inserção em massa. Nada de INSERT row-by-row.
6. **Respeitar rate limits do NCBI.** Máximo 3 req/s sem API key, 10 req/s com API key. O Rust deve implementar backoff exponencial e respeitar o header `Retry-After`.

### 2.3 PostgreSQL

1. **Triggers de FTS no banco, não no Django.** Os campos `search_vector` são atualizados por triggers Postgres (`tsvector_update_trigger`) que disparam automaticamente no INSERT/UPDATE — inclusive no COPY do Rust.
2. **Views Materializadas para consultas pesadas.** Stats por projeto, agregações por ano/journal/omic_type devem ser pre-computadas em Materialized Views com `REFRESH MATERIALIZED VIEW CONCURRENTLY` via Celery beat.
3. **Índices GIN para FTS.** Todas as tabelas com `search_vector` devem ter índices GIN.
4. **UPSERT via ON CONFLICT.** O Rust faz `INSERT ... ON CONFLICT (pmid) DO UPDATE` para papers e `ON CONFLICT (accession) DO UPDATE` para datasets, evitando duplicatas sem precisar de locks.

---

## 3. Modelo de Dados (Schema Postgres / Django Models)

O schema segue dois princípios de separação: **dados globais** (um paper ou dataset existe uma única vez no banco, compartilhado entre todos os projetos) e **dados por projeto** (curadoria, notas, categorias pertencem à relação entre o dado e o projeto do usuário).

### 3.1 Tabelas Globais (Dados Compartilhados)

**`Paper`** — Paper do PubMed/PMC. PK auto-increment, `pmid` como UNIQUE + indexed. Campos: `pmid`, `pmc_id`, `doi`, `title`, `abstract`, `journal`, `pub_year`, `pub_month`, `search_vector` (tsvector), `raw_xml_hash` (SHA-256 para detectar atualizações).

**`PaperAuthor`** — N autores por paper. Campos: `paper_id` (FK), `position`, `last_name`, `initials`, `affiliation`, `country` (extraído via regex no Rust). UNIQUE(`paper`, `position`).

**`PaperKeyword`** — Keywords do autor (não MeSH). Campos: `paper_id` (FK), `keyword`, `keyword_lower` (normalizado para dedup). UNIQUE(`paper`, `keyword_lower`).

**`PaperMeSHTerm`** — MeSH descriptors. Campos: `paper_id` (FK), `descriptor`, `qualifier`, `is_major_topic`. UNIQUE(`paper`, `descriptor`, `qualifier`).

**`PaperGene`** — Genes mencionados no abstract (NER via Rust). Campos: `paper_id` (FK), `gene_symbol`, `entrez_id`, `mention_count`. UNIQUE(`paper`, `gene_symbol`).

**`PaperDrug`** — Drogas/fármacos mencionados no abstract (NER via Rust). Campos: `paper_id` (FK), `drug_name`, `drug_name_lower` (normalizado), `mention_count`, `drugbank_id` (opcional). UNIQUE(`paper`, `drug_name_lower`).

**`PaperVariant`** — Variantes RS mencionadas no paper. Campos: `paper_id` (FK), `rs_number`. UNIQUE(`paper`, `rs_number`).

**`VariantAnnotation`** — Anotações externas de variantes (dbSNP, ClinVar). PK = `rs_number`. Campos: `gene_symbol`, `gene_name`, `entrez_id`, `chromosome`, `position`, `alleles`, `maf`, `clinical_significance`. Populada assincronamente após ingestão dos papers.

**`EntityContext`** — Contextos semânticos extraídos dos abstracts. Persiste as sentenças ao redor de entidades (genes, drogas, variantes, doenças, pathways) para análise contextual e como input para a camada de IA generativa. Campos: `paper_id` (FK), `entity_type` (gene/drug/variant/disease/pathway), `entity_name`, `sentence`, `sentence_position`.

**`OmicDataset`** — Dataset ômico de repositório público. `accession` como UNIQUE (GSE, SRP, PRJNA, E-MTAB). Campos: `source_db` (GEO/SRA/BioProject/ArrayExpress/TCGA/GWAS Catalog), `bioproject_id`, `title`, `summary`, `omic_type` (genomic/transcriptomic/proteomic/metabolomic/epigenomic/metagenomic/microbiome/multi_omic), `omic_subcategory`,`tissue` (tecido(s) alvo(s) do estudo), `organism`, `tax_id`, `n_samples`, `platform`, `extra_metadata` (JSONField para campos específicos de cada fonte, incluindo traits/associations/p-values do GWAS Catalog), `search_vector`, `is_active`.

**`DatasetPaperLink`** — Relação entre dataset e paper (descoberta via `elink` ou XML do GEO). Campos: `dataset_id` (FK), `paper_id` (FK), `link_source` (elink/geo_xml/manual).

**`OmicCategory`** — Definições de categorias ômicas com keywords para classificação automática pelo Rust. Campos: `omic_type`, `keywords` (JSONField), `priority`.

**`ClinicalCategory`** — Definições de categorias clínicas para classificação de papers. 5 eixos padrão pré-populados: diagnosis, treatment, epidemiology, mechanism, signs_symptoms. Extensível. Campos: `slug` (UNIQUE), `name`, `description`, `keywords` (JSONField para regex do Rust), `is_default`, `priority`. O Rust usa os keywords para scoring automático.

**`UserCategory`** — Categorias customizadas criadas pelo pesquisador para um projeto específico. Campos: `project_id` (FK), `name`, `keywords` (JSONField), `color` (hex para frontend). UNIQUE(`project`, `name`).

### 3.2 Tabelas por Projeto (Curadoria do Usuário)

**`DaVinciProject`** — Projeto de investigação. PK = UUID. Campos: `user` (FK), `slug` (UNIQUE, gerado automaticamente), `title`, `description`, `query_term`, `query_synonyms` (JSONField — sinônimos para normalização entre bases), `date_from`, `date_to`, `target_organisms` (JSONField), `target_tissues` (JSONField), `status` (draft/searching/curating/analyzing/complete).

**`ProjectPaper`** — Relação Paper ↔ Projeto. É aqui que a curadoria acontece. Campos: `project_id` (FK), `paper_id` (FK), `curation_status` (pending/included/excluded/maybe), `exclusion_reason` (auditável), `notes`, `clinical_categories` (M2M via `ProjectPaperClinicalCategory` com `confidence_score` e `is_manual`), `user_categories` (M2M com `UserCategory`), `relevance_score` (0.0 a 1.0). UNIQUE(`project`, `paper`).

**`ProjectPaperClinicalCategory`** — Tabela through que conecta Paper ↔ Categoria Clínica dentro de um projeto. Permite classificação automática (Rust, com score de confiança) e manual (pesquisador, score = 1.0). Campos: `project_paper_id` (FK), `category_id` (FK), `confidence_score`, `is_manual`. UNIQUE(`project_paper`, `category`).

**`ProjectDataset`** — Relação Dataset ↔ Projeto. Mesma lógica de curadoria. Campos: `project_id` (FK), `dataset_id` (FK), `curation_status` (pending/included/excluded/queued/downloaded), `exclusion_reason`, `notes`, `relevance_score`. UNIQUE(`project`, `dataset`).

**`ProjectPaperDataset`** — A ponte entre literatura e ômicas dentro de um projeto. Permite a análise integrada: "Este paper está relacionado a este dataset neste projeto, e o pesquisador confirmou/rejeitou a relação." Campos: `project_id` (FK), `project_paper_id` (FK), `project_dataset_id` (FK), `confidence` (auto/confirmed/rejected).

**`ProjectStats`** — Cache de estatísticas do projeto. OneToOne com `DaVinciProject`. Campos: `total_papers`, `included_papers`, `excluded_papers`, `pending_papers`, `total_datasets`, `included_datasets`, `total_samples`, `papers_by_year` (JSON), `papers_by_journal` (JSON), `papers_by_country` (JSON — distribuição geográfica via PaperAuthor.country), `papers_by_clinical_category` (JSON — contagem por eixo clínico), `datasets_by_omic_type` (JSON), `datasets_by_organism` (JSON), `top_genes` (JSON), `top_drugs` (JSON), `top_mesh_terms` (JSON), `top_variants` (JSON).

### 3.3 Tabelas de Controle

**`IngestionJob`** — Registro de cada job disparado pelo Django para o Rust. PK = UUID. Campos: `project_id` (FK), `job_type` (pubmed_search/pubmed_fetch/geo_search/sra_search/gwas_search/variant_annotation/gene_ner/drug_ner/context_extraction), `status` (pending/running/completed/failed/cancelled), `parameters` (JSONField), `records_processed`, `records_inserted`, `records_updated`, `error_message`, `started_at`, `completed_at`.

### 3.4 SQL Auxiliar (Migrations RunSQL)

Os seguintes scripts SQL devem ser executados como migrations ou aplicados diretamente no Postgres antes da primeira ingestão:

**Trigger FTS para Paper:**
```sql
CREATE OR REPLACE FUNCTION update_paper_search_vector() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('english', COALESCE(NEW.title, '')), 'A') ||
        setweight(to_tsvector('english', COALESCE(NEW.abstract, '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER paper_search_trigger
    BEFORE INSERT OR UPDATE OF title, abstract
    ON davinci_paper
    FOR EACH ROW
    EXECUTE FUNCTION update_paper_search_vector();
```

**Trigger FTS para OmicDataset:**
```sql
CREATE OR REPLACE FUNCTION update_dataset_search_vector() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('english', COALESCE(NEW.title, '')), 'A') ||
        setweight(to_tsvector('english', COALESCE(NEW.summary, '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER dataset_search_trigger
    BEFORE INSERT OR UPDATE OF title, summary
    ON davinci_omicdataset
    FOR EACH ROW
    EXECUTE FUNCTION update_dataset_search_vector();
```

**Índice para busca por gene:**
```sql
CREATE INDEX idx_papergene_symbol_lower
    ON davinci_papergene (LOWER(gene_symbol));
```

**Índice para busca por droga:**
```sql
CREATE INDEX idx_paperdrug_name_lower
    ON davinci_paperdrug (drug_name_lower);
```

**Índice para busca de contextos por entidade:**
```sql
CREATE INDEX idx_entitycontext_type_name
    ON davinci_entitycontext (entity_type, entity_name);
```

---

## 4. Estrutura de Diretórios do Projeto

```
davinci/
├── manage.py
├── requirements.txt
├── Cargo.toml                          # Workspace Rust
├── pyproject.toml
│
├── config/                             # Django project settings
│   ├── __init__.py
│   ├── settings/
│   │   ├── base.py                     # Settings compartilhados
│   │   ├── local.py                    # Dev (DEBUG=True, Postgres local)
│   │   └── production.py
│   ├── urls.py
│   ├── celery.py                       # Config Celery + Redis
│   └── wsgi.py
│
├── apps/
│   ├── core/                           # App principal DaVinci
│   │   ├── models.py                   # Models conforme Seção 3
│   │   ├── admin.py
│   │   ├── migrations/
│   │   │   ├── 0001_initial.py
│   │   │   └── 0002_fts_triggers.py    # RunSQL com triggers FTS
│   │   ├── serializers/
│   │   │   ├── __init__.py
│   │   │   ├── project.py
│   │   │   ├── paper.py
│   │   │   ├── dataset.py
│   │   │   └── ingestion.py
│   │   ├── services/                   # TODA lógica de negócio aqui
│   │   │   ├── __init__.py
│   │   │   ├── project_service.py      # CRUD de projetos
│   │   │   ├── search_service.py       # Despacha buscas para o Rust
│   │   │   ├── curation_service.py     # Inclusão/exclusão de papers e datasets
│   │   │   ├── stats_service.py        # Refresh de Views Materializadas
│   │   │   └── export_service.py       # Exportação para IA generativa
│   │   ├── views/                      # ViewSets finos (chamam Services)
│   │   │   ├── __init__.py
│   │   │   ├── project_views.py
│   │   │   ├── paper_views.py
│   │   │   ├── dataset_views.py
│   │   │   └── ingestion_views.py
│   │   ├── tasks/                      # Celery tasks
│   │   │   ├── __init__.py
│   │   │   ├── ingestion_tasks.py      # Chama Rust via PyO3
│   │   │   ├── annotation_tasks.py     # Anotação de variantes
│   │   │   └── stats_tasks.py          # Refresh periódico de stats
│   │   ├── urls.py
│   │   └── tests/
│   │       ├── test_models.py
│   │       ├── test_services.py
│   │       ├── test_api.py
│   │       └── test_ingestion.py
│   │
│   └── accounts/                       # App de autenticação (futuro)
│       └── ...
│
├── rust_engine/                        # Crate Rust (compilado como lib Python via PyO3)
│   ├── Cargo.toml
│   ├── src/
│   │   ├── lib.rs                      # Entry point PyO3 — expõe funções ao Python
│   │   ├── ncbi/
│   │   │   ├── mod.rs
│   │   │   ├── client.rs               # HTTP client (reqwest + tokio) com rate limiting
│   │   │   ├── parser.rs               # quick-xml parser — extrai TODOS os campos em uma passada
│   │   │   └── models.rs               # Structs internas (PaperData, AuthorData, etc.)
│   │   ├── omics/
│   │   │   ├── mod.rs
│   │   │   ├── geo_parser.rs           # Parser de metadados GEO
│   │   │   ├── sra_parser.rs           # Parser de metadados SRA
│   │   │   ├── bioproject_parser.rs    # Parser de metadados BioProject
│   │   │   └── gwas_parser.rs          # Parser de metadados GWAS Catalog
│   │   ├── categorization/
│   │   │   ├── mod.rs
│   │   │   ├── heuristic.rs            # Regex compilados + scoring (clínico + ômico)
│   │   │   ├── clinical.rs             # Categorização clínica (5 eixos padrão)
│   │   │   ├── gene_ner.rs             # Extração de genes do abstract
│   │   │   ├── drug_ner.rs             # Extração de drogas do abstract
│   │   │   └── context_extractor.rs    # Extração de sentenças-contexto (EntityContext)
│   │   ├── db/
│   │   │   ├── mod.rs
│   │   │   ├── connection.rs           # Pool de conexões Postgres (tokio-postgres)
│   │   │   ├── copy_writer.rs          # Preparação e execução de COPY FROM STDIN
│   │   │   └── job_tracker.rs          # Atualiza IngestionJob.status
│   │   └── utils/
│   │       ├── mod.rs
│   │       ├── dedup.rs                # IndexMap buffer
│   │       └── hash.rs                 # SHA-256 para raw_xml_hash
│   └── tests/
│       ├── test_parser.rs
│       ├── test_copy_writer.rs
│       └── fixtures/
│           └── sample_pubmed.xml       # XML de teste do PubMed
│
├── scripts/                            # Utilitários de desenvolvimento
│   ├── setup_db.sh                     # Cria banco, aplica migrations, executa SQL auxiliar
│   ├── seed_categories.py              # Popula OmicCategory + ClinicalCategory com keywords padrão
│   └── test_ingestion.py               # Script de teste end-to-end
│
└── docker/                             # Docker para dev local
    ├── docker-compose.yml              # Postgres 16 + Redis
    └── Dockerfile.dev
```

---

## 5. Configuração do Ambiente Local

### 5.1 Pré-requisitos (Apple Silicon)

- Python 3.11+
- Rust toolchain (rustup) com target `aarch64-apple-darwin`
- PostgreSQL 16+ (via Homebrew ou Docker)
- Redis (via Homebrew ou Docker)
- Maturin (para compilar Rust como módulo Python via PyO3)

### 5.2 Setup Inicial

```bash
# 1. Clonar e criar virtualenv
cd /Users/brandao/Documents/biohub.solutions/davinci
python -m venv .venv
source .venv/bin/activate

# 2. Dependências Python
pip install django djangorestframework psycopg[binary] celery[redis] \
    django-filter django-cors-headers maturin pyo3

# 3. Dependências Rust (criar Cargo.toml do workspace)
cargo init rust_engine --lib
cd rust_engine
# Adicionar ao Cargo.toml:
# [lib]
# name = "rust_engine"
# crate-type = ["cdylib"]
#
# [dependencies]
# pyo3 = { version = "0.22", features = ["extension-module"] }
# reqwest = { version = "0.12", features = ["json"] }
# tokio = { version = "1", features = ["full"] }
# quick-xml = "0.36"
# indexmap = "2"
# tokio-postgres = "0.7"
# rayon = "1.10"
# memmap2 = "0.9"
# sha2 = "0.10"
# serde = { version = "1", features = ["derive"] }
# serde_json = "1"

# 4. Compilar Rust como módulo Python
cd rust_engine
maturin develop --release

# 5. Banco de dados
createdb davinci_db
# Ou via Docker:
# docker-compose -f docker/docker-compose.yml up -d

# 6. Django setup
python manage.py migrate
python manage.py createsuperuser
python scripts/seed_categories.py

# 7. Redis + Celery
redis-server &
celery -A config worker -l info &
celery -A config beat -l info &
```

### 5.3 Docker Compose (Dev)

```yaml
# docker/docker-compose.yml
version: '3.9'
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: davinci_db
      POSTGRES_USER: davinci
      POSTGRES_PASSWORD: davinci_dev
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"

volumes:
  pgdata:
```

---

## 6. API Endpoints (DRF)

### 6.1 Projetos

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| `POST` | `/api/v1/projects/` | Criar projeto com query_term, dates, organisms |
| `GET` | `/api/v1/projects/` | Listar projetos do usuário |
| `GET` | `/api/v1/projects/{id}/` | Detalhe do projeto com stats |
| `PATCH` | `/api/v1/projects/{id}/` | Atualizar projeto |
| `DELETE` | `/api/v1/projects/{id}/` | Deletar projeto |
| `POST` | `/api/v1/projects/{id}/search/` | Disparar busca (cria IngestionJob) |

### 6.2 Papers (dentro de um projeto)

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| `GET` | `/api/v1/projects/{id}/papers/` | Listar papers com filtros (status, year, journal, clinical_category) |
| `GET` | `/api/v1/projects/{id}/papers/{paper_id}/` | Detalhe do paper (autores, keywords, MeSH, genes, drogas, variantes, contextos) |
| `PATCH` | `/api/v1/projects/{id}/papers/{paper_id}/` | Curadoria: alterar status, adicionar notas |
| `POST` | `/api/v1/projects/{id}/papers/{paper_id}/categorize/` | Atribuir/remover categorias clínicas ou customizadas |
| `POST` | `/api/v1/projects/{id}/papers/bulk-curate/` | Curadoria em massa |
| `GET` | `/api/v1/projects/{id}/papers/search/?q=term` | FTS nos papers do projeto |

### 6.3 Categorias (por projeto)

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| `GET` | `/api/v1/clinical-categories/` | Listar categorias clínicas globais |
| `GET` | `/api/v1/projects/{id}/categories/` | Listar categorias customizadas do projeto |
| `POST` | `/api/v1/projects/{id}/categories/` | Criar categoria customizada |
| `PATCH` | `/api/v1/projects/{id}/categories/{cat_id}/` | Editar keywords da categoria |
| `DELETE` | `/api/v1/projects/{id}/categories/{cat_id}/` | Remover categoria customizada |

### 6.3 Datasets Ômicos (dentro de um projeto)

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| `GET` | `/api/v1/projects/{id}/datasets/` | Listar datasets com filtros (omic_type, organism, source_db) |
| `GET` | `/api/v1/projects/{id}/datasets/{dataset_id}/` | Detalhe do dataset |
| `PATCH` | `/api/v1/projects/{id}/datasets/{dataset_id}/` | Curadoria |
| `GET` | `/api/v1/projects/{id}/datasets/search/?q=term` | FTS nos datasets do projeto |

### 6.4 Análise Integrada

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| `GET` | `/api/v1/projects/{id}/links/` | Paper-Dataset links (ponte literatura ↔ ômicas) |
| `POST` | `/api/v1/projects/{id}/links/{link_id}/confirm/` | Confirmar relação |
| `POST` | `/api/v1/projects/{id}/links/{link_id}/reject/` | Rejeitar relação |
| `GET` | `/api/v1/projects/{id}/stats/` | Estatísticas completas do projeto |
| `GET` | `/api/v1/projects/{id}/export/` | Exportação estruturada (JSON/CSV) |

### 6.5 Jobs de Ingestão

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| `GET` | `/api/v1/projects/{id}/jobs/` | Listar jobs do projeto |
| `GET` | `/api/v1/projects/{id}/jobs/{job_id}/` | Status detalhado do job |
| `POST` | `/api/v1/projects/{id}/jobs/{job_id}/cancel/` | Cancelar job |

---

## 7. Rust Engine — Especificação Técnica

### 7.1 Interface PyO3

O Rust engine expõe as seguintes funções ao Python via PyO3:

```rust
// lib.rs — funções expostas ao Python

#[pyfunction]
fn search_and_ingest_pubmed(
    job_id: String,          // UUID do IngestionJob
    query: String,           // Termo de busca
    date_from: Option<u16>,  // Ano inicial
    date_to: Option<u16>,    // Ano final
    db_url: String,          // Connection string do Postgres
    ncbi_api_key: Option<String>,
) -> PyResult<IngestionResult>;

#[pyfunction]
fn search_and_ingest_omics(
    job_id: String,
    query: String,
    databases: Vec<String>,  // ["geo", "sra", "bioproject", "gwas_catalog"]
    db_url: String,
    ncbi_api_key: Option<String>,
) -> PyResult<IngestionResult>;

#[pyfunction]
fn annotate_variants(
    job_id: String,
    rs_numbers: Vec<String>,
    db_url: String,
) -> PyResult<IngestionResult>;

#[pyfunction]
fn extract_genes_from_abstracts(
    job_id: String,
    project_id: String,
    db_url: String,
) -> PyResult<IngestionResult>;

#[pyfunction]
fn extract_drugs_from_abstracts(
    job_id: String,
    project_id: String,
    db_url: String,
) -> PyResult<IngestionResult>;

#[pyfunction]
fn extract_entity_contexts(
    job_id: String,
    project_id: String,
    entity_types: Vec<String>,  // ["gene", "drug", "variant"]
    db_url: String,
) -> PyResult<IngestionResult>;

#[pyfunction]
fn categorize_papers_clinical(
    job_id: String,
    project_id: String,
    db_url: String,
) -> PyResult<IngestionResult>;

#[pyclass]
struct IngestionResult {
    records_processed: u64,
    records_inserted: u64,
    records_updated: u64,
    errors: Vec<String>,
}
```

### 7.2 Fluxo Interno do `search_and_ingest_pubmed`

```
1. Atualizar IngestionJob.status = 'running' no Postgres
2. esearch: POST https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi
   - db=pubmed, term=query, datetype=PDAT, mindate, maxdate
   - retmax=100000, usehistory=y
   → Obtém WebEnv + QueryKey + Count
3. efetch (em batches de 500):
   - POST https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi
   - db=pubmed, WebEnv, query_key, rettype=xml, retstart=0, retmax=500
   - Respeitar rate limit (tokio::time::sleep entre batches)
4. Para cada batch XML recebido:
   a. Parser quick-xml extrai em uma passada:
      - PMID, Title, AbstractText, Keywords, MeSH descriptors
      - Authors (LastName, Initials, Affiliation)
      - ArticleId (DOI, PMC)
      - Journal/ISOAbbreviation, PubDate (Year, Month)
   b. Categorização clínica automática (regex compilados → 5 eixos com confidence_score)
   c. Gene NER no abstract → PaperGene
   d. Drug NER no abstract → PaperDrug
   e. Extração de sentenças-contexto para entidades detectadas → EntityContext
   f. Acumula em IndexMap<PMID, PaperData>
5. COPY para Postgres:
   a. Paper (ON CONFLICT pmid DO UPDATE)
   b. PaperAuthor (DELETE + INSERT por paper)
   c. PaperKeyword (ON CONFLICT DO NOTHING)
   d. PaperMeSHTerm (ON CONFLICT DO NOTHING)
   e. PaperGene (ON CONFLICT DO UPDATE mention_count)
   f. PaperDrug (ON CONFLICT DO UPDATE mention_count)
   g. PaperVariant (ON CONFLICT DO NOTHING)
   h. EntityContext (INSERT — sentenças-contexto para cada entidade detectada)
   i. ProjectPaper (INSERT com curation_status='pending')
   j. ProjectPaperClinicalCategory (INSERT categorização automática com confidence_score)
6. Atualizar IngestionJob com records_processed, records_inserted, status='completed'
```

### 7.3 Crates Rust Necessárias

| Crate | Versão | Uso |
|-------|--------|-----|
| `pyo3` | 0.22+ | Interface Python ↔ Rust |
| `reqwest` | 0.12+ | HTTP client assíncrono |
| `tokio` | 1.x | Runtime async (I/O bound) |
| `rayon` | 1.10+ | Paralelismo (CPU bound) |
| `quick-xml` | 0.36+ | Parser XML de alta performance |
| `indexmap` | 2.x | HashMap ordenado para dedup |
| `tokio-postgres` | 0.7+ | Client Postgres assíncrono |
| `memmap2` | 0.9+ | Memory mapping para arquivos grandes |
| `sha2` | 0.10+ | Hash SHA-256 para detecção de mudanças |
| `serde` + `serde_json` | 1.x | Serialização |
| `regex` | 1.x | Regex compilados para categorização |

---

## 8. Fluxo de Interação do Usuário

O DaVinci tem três momentos distintos de interação:

### 8.1 Momento 1 — Configuração da Busca

O pesquisador cria um projeto e define o escopo da investigação: termo de busca principal, sinônimos para normalização entre bases (ex: "hidradenitis AND cancer" no PubMed pode virar "hidradenitis suppurativa" no GEO), intervalo de datas, organismos alvo e tecidos de interesse. O Django mantém um dicionário de sinônimos (`query_synonyms`) e despacha as buscas paralelamente para o Rust engine (literatura) e para as APIs das bases ômicas.

### 8.2 Momento 2 — Curadoria, Categorização e Exclusão

Depois que o motor traz os resultados, o pesquisador revisa, filtra e exclui. A interface apresenta cada paper/dataset com resumo, keywords, categorização clínica automática (com score de confiança), genes e drogas extraídos, e as conexões já identificadas entre o paper e datasets ômicos disponíveis. O pesquisador pode: aceitar ou rejeitar as categorias clínicas atribuídas automaticamente, criar categorias customizadas com seus próprios termos (`UserCategory`), marcar papers como relevantes ou excluí-los (com motivo obrigatório — auditável). A listagem permite filtros por journal, ano, tipo de ômica, organismo, categoria clínica, e status de curadoria. A exclusão é auditável — o pesquisador pode ver o que excluiu e por quê, pois isso é parte da metodologia de revisão sistemática.

### 8.3 Momento 3 — Análise Integrada e Outputs

Com o corpus curado, o DaVinci gera outputs integrados. A correlação entre literatura e dados ômicos é a funcionalidade central: a tabela `ProjectPaperDataset` mostra quais papers estão relacionados a quais datasets, e o pesquisador pode confirmar ou rejeitar essas relações. Os outputs do MVP incluem: matriz de cobertura (quais genes/pathways aparecem na literatura E têm dados ômicos disponíveis), distribuição geográfica da pesquisa (via `PaperAuthor.country`), frequências de genes, drogas e variantes por categoria clínica, grafos de co-ocorrência de termos MeSH cruzados com metadados GEO, e exportação estruturada (JSON/CSV) que inclui os `EntityContext` (sentenças-contexto) pronta para servir como knowledge base para a camada de IA generativa futura.





---



*Este documento é o contrato de desenvolvimento do DaVinci. Qualquer decisão de implementação que conflite com as regras aqui definidas deve ser discutida e aprovada antes de ser aplicada.*