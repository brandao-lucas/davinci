# DaVinci — Ingestão no Banco de Dados

O sistema de ingestão usa **PostgreSQL COPY** (não ORM) para máxima performance. O Rust escreve diretamente no banco, bypassing completamente o Django/ORM.

---

## Arquitetura de Ingestão

```
Rust Engine
  ├── Fetch HTTP (NCBI APIs)
  ├── Parse XML/JSON → structs em memória
  ├── NER + Categorização
  └── db/copy_writer.rs
        ├── COPY INTO core_paper
        ├── COPY INTO core_paperauthor
        ├── COPY INTO core_paperkeyword
        ├── COPY INTO core_papermeshterm
        ├── COPY INTO core_papergene
        ├── COPY INTO core_paperdrug
        ├── COPY INTO core_entitycontext
        ├── COPY INTO core_omicdataset
        ├── COPY INTO core_datasetpaperlink
        ├── INSERT INTO core_projectpaper
        ├── INSERT INTO core_projectdataset
        └── INSERT INTO core_projectpaperclinicalcategory
```

---

## `db/copy_writer.rs` — Funções Principais

### `copy_papers(papers: Vec<PaperData>, conn: &Client) → Result<usize>`

```sql
-- Estrutura do COPY
COPY core_paper (
    pmid, title, abstract_text, journal,
    pub_year, pub_month, pub_type, pmc_id, doi,
    free_full_text, raw_xml_hash, search_vector,
    created_at, updated_at
)
FROM STDIN WITH (FORMAT BINARY)

-- ON CONFLICT: upsert inteligente
INSERT ... ON CONFLICT (pmid) DO UPDATE SET
    title = EXCLUDED.title,
    abstract_text = EXCLUDED.abstract_text,
    raw_xml_hash = EXCLUDED.raw_xml_hash,
    -- só atualiza se hash mudou (detecção de mudança)
    updated_at = now()
WHERE core_paper.raw_xml_hash != EXCLUDED.raw_xml_hash
```

**Nota:** `search_vector` é atualizado automaticamente pelo trigger PostgreSQL após o INSERT/UPDATE — o Rust não precisa calculá-lo.

---

### `copy_paper_authors(authors: Vec<AuthorData>, paper_id_map: &HashMap<String, i64>, conn: &Client)`

```sql
COPY core_paperauthor (
    paper_id, position, last_name, initials,
    affiliation, country
)
FROM STDIN WITH (FORMAT BINARY)
```

- `paper_id_map`: resolve PMID → `paper.id` (necessário após o COPY de papers)
- Extração de país: regex em `utils::extract_country_from_affiliation(affiliation)`

---

### `copy_paper_keywords / copy_paper_mesh / copy_paper_genes / copy_paper_drugs`

Todos seguem o mesmo padrão:
```sql
INSERT INTO table (paper_id, fields...)
VALUES (...)
ON CONFLICT (unique_constraint) DO NOTHING  -- deduplicação silenciosa
```

---

### `copy_omic_datasets(datasets: Vec<OmicDatasetData>, conn: &Client) → Result<usize>`

```sql
COPY core_omicdataset (
    accession, source_db, title, summary,
    omic_type, omic_subcategory, organism, tax_id,
    n_samples, platform, pub_date, extra_metadata,
    created_at, updated_at
)
FROM STDIN WITH (FORMAT BINARY)

ON CONFLICT (accession) DO UPDATE SET
    title = EXCLUDED.title,
    summary = EXCLUDED.summary,
    n_samples = EXCLUDED.n_samples,
    updated_at = now()
```

---

### `copy_dataset_paper_links(links: Vec<DatasetPaperLinkData>, conn: &Client)`

```sql
INSERT INTO core_datasetpaperlink (dataset_id, paper_id, link_source)
VALUES (...)
ON CONFLICT (dataset_id, paper_id) DO NOTHING
```

---

### `link_project_papers(pmids: Vec<String>, project_id: Uuid, conn: &Client)`

Após a ingestão de papers, vincula-os ao projeto via `ProjectPaper`:

```sql
INSERT INTO core_projectpaper (
    project_id, paper_id, curation_status,
    relevance_score, created_at, updated_at
)
SELECT $1, p.id, 'pending', 0.0, now(), now()
FROM core_paper p
WHERE p.pmid = ANY($2)
ON CONFLICT (project_id, paper_id) DO NOTHING
-- Não sobreescreve curadoria existente!
```

---

### `link_project_datasets(accessions: Vec<String>, project_id: Uuid, conn: &Client)`

```sql
INSERT INTO core_projectdataset (
    project_id, dataset_id, curation_status,
    relevance_score, created_at, updated_at
)
SELECT $1, d.id, 'pending', 0.0, now(), now()
FROM core_omicdataset d
WHERE d.accession = ANY($2)
ON CONFLICT (project_id, dataset_id) DO NOTHING
```

---

### `link_clinical_categories(scores: Vec<ClinicalCategoryScore>, project_paper_ids: &HashMap<...>, conn)`

```sql
INSERT INTO core_projectpaperclinicalcategory (
    project_paper_id, category_id, confidence_score, is_manual
)
VALUES (...)
ON CONFLICT (project_paper_id, category_id) DO UPDATE SET
    confidence_score = GREATEST(
        EXCLUDED.confidence_score,
        core_projectpaperclinicalcategory.confidence_score
    )
-- Mantém o maior score entre auto e manual
```

---

## `db/job_tracker.rs` — Controle de Status

```rust
pub async fn mark_running(job_id: &str, conn: &Client) -> Result<()>
pub async fn mark_completed(job_id: &str, counts: JobCounts, conn: &Client) -> Result<()>
pub async fn mark_failed(job_id: &str, error: &str, conn: &Client) -> Result<()>
```

```sql
-- mark_running
UPDATE core_ingestionjob
SET status = 'running', started_at = now()
WHERE id = $1

-- mark_completed
UPDATE core_ingestionjob
SET status = 'completed',
    completed_at = now(),
    records_processed = $2,
    records_inserted = $3,
    records_updated = $4
WHERE id = $1
```

---

## Structs Rust → Tabelas PostgreSQL

### `PaperData` (ncbi/models.rs)
```rust
pub struct PaperData {
    pub pmid: String,
    pub title: String,
    pub abstract_text: Option<String>,
    pub journal: String,
    pub pub_year: i32,
    pub pub_month: Option<i32>,
    pub pub_type: Option<String>,
    pub pmc_id: Option<String>,
    pub doi: Option<String>,
    pub free_full_text: bool,
    pub raw_xml_hash: String,        // SHA-256 do XML original
    pub authors: Vec<AuthorData>,
    pub keywords: Vec<String>,
    pub mesh_terms: Vec<MeSHTerm>,
    pub genes: Vec<GeneData>,        // NER-extracted
    pub drugs: Vec<DrugData>,        // NER-extracted
    pub variants: Vec<String>,       // RS numbers
    pub contexts: Vec<EntityContext>, // Sentence contexts
}
```

### `OmicDatasetData` (omics/models.rs)
```rust
pub struct OmicDatasetData {
    pub accession: String,
    pub source_db: String,
    pub title: String,
    pub summary: Option<String>,
    pub omic_type: String,           // Classificado por type_classifier
    pub omic_subcategory: Option<String>,
    pub organism: Option<String>,
    pub tax_id: Option<String>,
    pub n_samples: Option<i32>,
    pub platform: Option<String>,
    pub pub_date: Option<NaiveDate>,
    pub extra_metadata: serde_json::Value,
    pub linked_pmids: Vec<String>,   // PMIDs vinculados
}
```

---

## Resolução de IDs (PMID → paper.id)

Após o COPY de papers, o Rust precisa do `paper.id` (int gerado pelo Postgres) para inserir as tabelas filhas:

```rust
// Após copy_papers():
let paper_id_map: HashMap<String, i64> = conn
    .query(
        "SELECT pmid, id FROM core_paper WHERE pmid = ANY($1)",
        &[&pmids],
    )
    .await?
    .iter()
    .map(|row| (row.get::<_, String>(0), row.get::<_, i64>(1)))
    .collect();
```

Mesmo padrão para datasets: `accession → dataset.id`.

---

## Migrações de Banco de Dados

### Estrutura de Migrações (`apps/core/migrations/`)

| Arquivo | Conteúdo |
|---------|---------|
| `0001_initial.py` | Criação de todos os models base |
| `0002_*.py` | Migrations incrementais (campos adicionados nas Fases 2-4) |
| `0003_paper_pub_type.py` | Adiciona `pub_type` ao Paper |
| `0004_alter_omicdataset_omic_type.py` | Atualiza choices de omic_type |
| `0005_projectstats_omic_subcategory_and_more.py` | Adiciona subcategoria e campos Phase 4 |

### Triggers e Índices Especiais (RunSQL)

```python
# Em migration inicial ou dedicada:
migrations.RunSQL(
    """
    -- Trigger FTS para Paper
    CREATE TRIGGER paper_search_vector_update
    BEFORE INSERT OR UPDATE ON core_paper
    FOR EACH ROW EXECUTE FUNCTION
    tsvector_update_trigger(search_vector, 'pg_catalog.english', title, abstract_text);

    -- Trigger FTS para OmicDataset
    CREATE TRIGGER dataset_search_vector_update
    BEFORE INSERT OR UPDATE ON core_omicdataset
    FOR EACH ROW EXECUTE FUNCTION
    tsvector_update_trigger(search_vector, 'pg_catalog.english', title, summary);

    -- Índice GIN para FTS em Paper
    CREATE INDEX IF NOT EXISTS ix_paper_search_vector
    ON core_paper USING GIN(search_vector);

    -- Índice GIN para FTS em OmicDataset
    CREATE INDEX IF NOT EXISTS ix_dataset_search_vector
    ON core_omicdataset USING GIN(search_vector);
    """
)
```

---

## Seeds de Dados Iniciais (`management/commands/seed_categories.py`)

Popula tabelas de configuração que o Rust usa para categorização:

### `ClinicalCategory` (5 eixos padrão)
```python
CLINICAL_CATEGORIES = [
    {
        "slug": "diagnosis",
        "name": "Diagnóstico",
        "keywords": ["diagnosis", "diagnostic", "biomarker", "detection", "screening", ...],
        "is_default": True,
        "priority": 1,
    },
    {
        "slug": "treatment",
        "name": "Tratamento",
        "keywords": ["treatment", "therapy", "drug", "intervention", "clinical trial", ...],
        "is_default": True,
        "priority": 2,
    },
    # ... epidemiology, mechanism, signs_symptoms
]
```

### `OmicCategory`
```python
OMIC_CATEGORIES = [
    {
        "omic_type": "microbiome",
        "keywords": ["16S", "microbiome", "metagenom", "gut microbiota", "microbiota"],
        "priority": 1,  # Mais específico → verifica primeiro
    },
    # ... epigenomic, transcriptomic, genomic, etc.
]
```

---

## Fluxo Completo de Ingestão de Literatura

```
POST /projects/{id}/search/
  ↓
SearchService.dispatch_pubmed_search()
  → cria IngestionJob (status=pending)
  → Celery: run_pubmed_ingestion.delay(job_id)
  ↓
[Celery Worker]
  → IngestionJob.status = 'running'
  → rust_engine.search_and_ingest_pubmed(job_id, query, db_url, ...)
  ↓
[Rust — Fetch]
  → esearch: GET /esearch.fcgi?db=pubmed&term=...&retmax=10000
    ← {"esearchresult": {"idlist": ["37124580", "37089234", ...]}}
  → efetch: POST /efetch.fcgi?db=pubmed&id=37124580,37089234,...
    ← XML PubmedArticleSet
  ↓
[Rust — Parse (quick-xml, streaming)]
  → Vec<PaperData> (todos os campos em uma passada)
  ↓
[Rust — NER + Categorização (rayon paralelo)]
  → gene_ner(abstract) → Vec<GeneData>
  → drug_ner(abstract) → Vec<DrugData>
  → context_extractor(abstract, entities) → Vec<EntityContext>
  → clinical::classify(abstract, mesh) → Vec<ClinicalCategoryScore>
  ↓
[Rust — DB Write (tokio-postgres)]
  1. COPY core_paper (upsert)
  2. SELECT pmid → paper_id (resolver IDs)
  3. COPY core_paperauthor
  4. COPY core_paperkeyword
  5. COPY core_papermeshterm
  6. COPY core_papergene
  7. COPY core_paperdrug
  8. COPY core_entitycontext
  9. INSERT core_projectpaper (status=pending, ON CONFLICT DO NOTHING)
  10. INSERT core_projectpaperclinicalcategory
  11. UPDATE core_ingestionjob (status=completed, counts)
  ↓
IngestionResult { records_processed, records_inserted, errors }
  ↓
[Django Celery Task]
  → Atualiza job.status, records_processed, records_inserted
  ↓
[Frontend — polling]
  GET /projects/{id}/jobs/{job_id}/
  ← { "status": "completed", "records_inserted": 2890 }
```

---

## Tratamento de Erros

### Erros Fatais (param)
- Job não encontrado → retorna sem processar
- Falha na conexão PostgreSQL → retry (max 3)
- Exceção não tratada → `job.status = 'failed'`, retry com backoff 60s

### Erros Não-Fatais (non-fatal)
- NCBI retorna XML malformado para 1 paper → log no `IngestionResult.errors`, continua
- COPY falha para 1 linha → log, continua com as demais
- Dataset sem PMID associado → `DatasetPaperLink` não criado (normal)
- Variante sem match no dbSNP → salva só o RS number, anotação fica para job futuro

### Fallback sem Rust
```python
# ingestion_tasks.py
try:
    import rust_engine
except ImportError:
    # Modo MVP — retorna stub
    return {"records_processed": 0, "records_inserted": 0, "errors": ["rust_engine not installed"]}
```

---

## Configuração PostgreSQL (settings)

```python
# config/settings/base.py
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME", default="davinci"),
        "USER": env("DB_USER", default="postgres"),
        "PASSWORD": env("DB_PASSWORD", default=""),
        "HOST": env("DB_HOST", default="localhost"),
        "PORT": env("DB_PORT", default="5432"),
    }
}

# URL construída para o Rust
# postgresql://user:password@host:port/dbname
```

O Rust usa `tokio-postgres` diretamente com a connection string — sem Django ORM.
