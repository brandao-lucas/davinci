# DaVinci — Motor de Busca

O motor de busca do DaVinci é composto por duas camadas: a **camada de despacho** (Django/Python) e a **camada de execução** (Rust). O Django cria o job e chama o Rust; o Rust faz o trabalho pesado.

---

## Visão Geral do Fluxo

```
Requisição do Frontend
    POST /api/v1/projects/{id}/search/          ← busca literatura
    POST /api/v1/projects/{id}/omics_search/    ← busca ômica
    ↓
SearchService (apps/core/services/search_service.py)
    → cria IngestionJob no PostgreSQL
    → dispara Celery task
    ↓
Celery (apps/core/tasks/ingestion_tasks.py)
    → chama rust_engine via PyO3
    ↓
Rust Engine (rust_src/src/lib.rs)
    → esearch → efetch → parse → NER → COPY
```

---

## SearchService (`apps/core/services/search_service.py`)

Classe estática. Não manipula dados diretamente — apenas cria jobs e despacha tarefas.

### `dispatch_pubmed_search(project, user=None) → IngestionJob`

1. Recupera `ncbi_api_key` do `UserProfile` do usuário
2. Constrói a query NCBI: `query_term OR syn1 OR syn2 OR ...`
3. Cria `IngestionJob` com:
   ```python
   parameters = {
       "query": "cardiovascular disease OR CVD OR ...",
       "date_from": project.date_from,
       "date_to": project.date_to,
       "synonyms": project.query_synonyms,
       "ncbi_api_key": api_key,  # None se usuário não tem chave
   }
   ```
4. Despacha: `run_pubmed_ingestion.delay(str(job.id))`
5. Retorna o `IngestionJob` criado para polling imediato

### `dispatch_omics_search(project, sources=None, max_per_source=10000, user=None) → IngestionJob`

1. Fontes padrão: `['geo', 'sra', 'bioproject', 'gwas']`
2. Recupera `ncbi_api_key`
3. Cria `IngestionJob` com `job_type='geo_search'` e parâmetros de fontes
4. Despacha: `run_omics_ingestion.delay(str(job.id))`
5. Retorna o job

---

## Celery Tasks (`apps/core/tasks/ingestion_tasks.py`)

### `run_pubmed_ingestion(job_id: str)` — `@shared_task(max_retries=3)`

```python
job = IngestionJob.objects.get(id=job_id)
db_url = build_postgres_url(settings)

result = rust_engine.search_and_ingest_pubmed(
    job_id=str(job.id),
    query=job.parameters["query"],
    db_url=db_url,
    project_id=str(job.project_id),
    date_from=job.parameters.get("date_from"),
    date_to=job.parameters.get("date_to"),
    ncbi_api_key=job.parameters.get("ncbi_api_key"),
)
# result = {"records_processed": N, "records_inserted": M, "errors": [...]}
```

- Em `ImportError`: retorna stub result (Rust não instalado — modo MVP)
- Em exceção: atualiza `job.status = 'failed'`, retry com backoff de 60s

### `run_omics_ingestion(job_id: str)` — `@shared_task(max_retries=3)`

```python
result = rust_engine.search_and_ingest_omics(
    job_id=str(job.id),
    query=...,
    db_url=db_url,
    project_id=str(job.project_id),
    sources=job.parameters["sources"],      # ["geo", "sra", "bioproject", "gwas"]
    max_per_source=job.parameters["max_per_source"],
    ncbi_api_key=...,
    synonyms=...,
)
# result = {"datasets_processed": N, "datasets_inserted": M, "links_inserted": K, "errors": [...]}
```

---

## Rust Engine — Busca de Literatura

### Entry Point: `search_and_ingest_pubmed(...)` em `rust_src/src/lib.rs`

```rust
#[pyfunction]
pub fn search_and_ingest_pubmed(
    job_id: &str,
    query: &str,
    db_url: &str,
    project_id: &str,
    date_from: Option<i32>,
    date_to: Option<i32>,
    ncbi_api_key: Option<&str>,
) -> PyResult<IngestionResult>
```

#### Pipeline Interno

```
1. Tokio runtime + PostgreSQL connection pool
2. job_tracker::mark_running(job_id)
3. ncbi::esearch(query, date_from, date_to, ncbi_api_key)
      → retorna Vec<String> de PMIDs
      → máx 5.000 sem API key, 10.000 com
4. ncbi::efetch_batch(pmids, ncbi_api_key)
      → batches de 200 PMIDs por requisição
      → rate-limited (3 ou 10 req/s)
      → retorna XML bruto
5. ncbi::parser::parse_pubmed_xml(xml)
      → Vec<PaperData> com todos os campos
6. categorization::gene_ner(abstract)
      → Vec<GeneData> por paper
7. categorization::drug_ner(abstract)
      → Vec<DrugData> por paper
8. categorization::context_extractor(abstract, entities)
      → Vec<EntityContext> por paper
9. categorization::clinical::classify(abstract, mesh_terms)
      → Vec<ClinicalCategoryScore> por paper
10. db::copy_writer::copy_papers(papers, conn)
      → PostgreSQL COPY INTO core_paper (ON CONFLICT DO UPDATE)
11. Resolução PMID → paper_id (query em lote)
12. db::copy_writer::copy_paper_authors(authors, conn)
13. db::copy_writer::copy_paper_keywords(keywords, conn)
14. db::copy_writer::copy_paper_mesh(mesh_terms, conn)
15. db::copy_writer::copy_paper_genes(genes, conn)
16. db::copy_writer::copy_paper_drugs(drugs, conn)
17. db::copy_writer::copy_entity_contexts(contexts, conn)
18. db::copy_writer::link_project_papers(pmids, project_id, conn)
19. db::copy_writer::link_clinical_categories(scores, conn)
20. job_tracker::mark_completed(job_id, counts)
21. Return IngestionResult
```

---

## Rust Engine — Busca de Ômica

### Entry Point: `search_and_ingest_omics(...)` em `rust_src/src/lib.rs`

```rust
#[pyfunction]
pub fn search_and_ingest_omics(
    job_id: &str,
    query: &str,
    db_url: &str,
    project_id: &str,
    sources: Vec<String>,       // ["geo", "sra", "bioproject", "gwas"]
    max_per_source: i64,
    ncbi_api_key: Option<&str>,
    synonyms: Vec<String>,
) -> PyResult<OmicsResult>
```

#### Pipeline Interno (por fonte)

**GEO:**
```
geo_parser::fetch_geo_datasets(query, ncbi_api_key, max)
  → esearch no db=gds → esummary → parse JSON
  → Vec<OmicDatasetData> com source_db="geo"
  → Para datasets sem PMID no XML:
      omics::elink::discover_links_via_elink(gse_ids, ncbi_api_key)
      → elink db=gds&dbto=pubmed → Vec<DatasetPaperLinkData>
```

**SRA:**
```
sra_parser::fetch_sra_datasets(query, ncbi_api_key, max)
  → esearch no db=sra → esummary → parse XML
  → Vec<OmicDatasetData> com source_db="sra"
```

**BioProject:**
```
bioproject_parser::fetch_bioproject_datasets(query, ncbi_api_key, max)
  → esearch no db=bioproject → esummary → parse XML
  → Vec<OmicDatasetData> com source_db="bioproject"
```

**GWAS Catalog:**
```
gwas_parser::fetch_gwas_datasets(query, max)
  → NHGRI GWAS Catalog REST API (não NCBI)
  → Vec<OmicDatasetData> com source_db="gwas_catalog"
  → extra_metadata: {trait, p_value, mapped_gene, risk_allele}
```

**Após cada fonte:**
```
omics::type_classifier::classify_omic_type(title, summary, platform)
  → Atribui omic_type + omic_subcategory baseado em keywords

db::copy_writer::copy_omic_datasets(datasets, conn)
  → COPY INTO core_omicdataset (ON CONFLICT DO UPDATE)
  → Deduplicação intra-batch: agrega omic_type, mantém título/summary mais longo

db::copy_writer::copy_dataset_paper_links(links, conn)
  → Tenta COPY INTO core_datasetpaperlink (ON CONFLICT DO NOTHING)
  → Links com paper ainda não ingerido vão para core_datasetpaperlinkpending (staging)
  → Resolução pendente feita após ingestion de papers pela Celery task

db::copy_writer::link_project_datasets(accessions, project_id, conn)
  → Insere ProjectDataset para cada dataset encontrado
```

---

## NCBI Client (`rust_engine/src/ncbi/client.rs`)

### Rate Limiting e Backoff

```rust
pub struct NcbiClient {
    client: reqwest::Client,
    api_key: Option<String>,
    rate_limiter: Arc<RateLimiter>,  // 3 ou 10 req/s
}
```

- Sem API key: 3 requisições/segundo
- Com API key: 10 requisições/segundo
- `max_retries = 5`
- Backoff exponencial: 1s → 2s → 4s → 8s → 16s
- Respeita header `Retry-After` quando presente (HTTP 429)
- Timeout por requisição: 120s

### E-utilities Usadas

| Endpoint | Uso |
|----------|-----|
| `esearch.fcgi` | Busca IDs (PMIDs, GSE IDs, SRA IDs) |
| `efetch.fcgi` | Download XML de records pelo ID |
| `esummary.fcgi` | Summaries de múltiplos IDs |
| `elink.fcgi` | Descoberta de links entre bases (gds → pubmed) |

---

## Parser de XML PubMed (`rust_engine/src/ncbi/parser.rs`)

Usa `quick-xml` (streaming SAX-like) para parsing eficiente. **Uma única passada** pelo XML extrai todos os campos:

```
PubmedArticle
  ├── MedlineCitation
  │   ├── PMID → pmid
  │   ├── Article
  │   │   ├── ArticleTitle → title
  │   │   ├── Abstract/AbstractText → abstract_text
  │   │   ├── Journal/Title → journal
  │   │   ├── Journal/JournalIssue/PubDate → pub_year, pub_month
  │   │   ├── AuthorList/Author → Vec<AuthorData>
  │   │   │   └── AffiliationInfo → country (regex)
  │   │   ├── PublicationTypeList → pub_type
  │   │   └── ELocationID[@EIdType="doi"] → doi
  │   ├── KeywordList/Keyword → Vec<KeywordData>
  │   └── MeshHeadingList/MeshHeading → Vec<MeSHTermData>
  └── PubmedData
      ├── ArticleIdList/ArticleId[@IdType="pmc"] → pmc_id
      └── ArticleIdList/ArticleId[@IdType="doi"] → doi (fallback)
```

---

## Classificador de Tipo Ômico (`rust_engine/src/omics/type_classifier.rs`)

Classifica datasets por `omic_type` e `omic_subcategory` baseado em keywords no título, summary e plataforma:

```
Título/Summary/Plataforma
  ↓
Regex compilados por tipo (em ordem de prioridade):
  microbiome   → ["16S", "microbiome", "metagenom*", "gut microbiota"]
  epigenomic   → ["ChIP-seq", "ATAC-seq", "methylat*", "histone"]
  transcriptomic → ["RNA-seq", "mRNA", "transcriptom*", "gene expression"]
  genomic      → ["WGS", "whole genome", "SNP", "variant calling"]
  proteomic    → ["proteom*", "mass spectrometry", "iTRAQ"]
  metabolomic  → ["metabolom*", "metabolite", "NMR", "LC-MS"]
  multi_omic   → ["multi-om*", "integrat*"]
  other        → fallback
  ↓
omic_type + omic_subcategory atribuídos ao OmicDatasetData
```

---

## Full-Text Search (FTS) em PostgreSQL

O FTS é nativo do PostgreSQL, via campo `search_vector` do tipo `tsvector`.

### Como Funciona

1. **Triggers** atualizam `search_vector` automaticamente em INSERT/UPDATE
2. **Índice GIN** sobre `search_vector` garante busca O(log n)
3. **Query FTS** via `SearchQuery` do Django ORM:

```python
# Em paper_views.py
query = SearchQuery(q, config='english')
qs = ProjectPaper.objects.filter(
    paper__search_vector=query
).annotate(
    rank=SearchRank('paper__search_vector', query)
).order_by('-rank')
# FTS indexado sobre title (peso A) e abstract (peso B)
```

### Endpoints de FTS

```
GET /api/v1/projects/{id}/papers/search/?q=<termo>
GET /api/v1/projects/{id}/datasets/search/?q=<termo>
```

---

## Filtros de Curadoria (API)

Além do FTS, os endpoints de listagem suportam filtros:

### Papers (`/papers/`)
| Parâmetro | Descrição |
|-----------|-----------|
| `curation_status` | `pending`, `included`, `excluded`, `maybe` |
| `pub_year_min` / `pub_year_max` | Intervalo de anos |
| `journal` | Nome exato do periódico |
| `pub_type` | Tipo de publicação |
| `has_abstract` | Boolean — tem abstract? |
| `clinical_category` | Slug da ClinicalCategory |

### Datasets (`/datasets/`)
| Parâmetro | Descrição |
|-----------|-----------|
| `curation_status` | Status de curadoria |
| `omic_type` | Tipo ômico |
| `organism` | Organismo |
| `source_db` | `geo`, `sra`, etc. |
| `has_summary` | Boolean |

---

## Monitoramento de Jobs

O frontend faz polling no endpoint de jobs para acompanhar o progresso:

```
GET /api/v1/projects/{id}/jobs/{job_id}/
→ {
    "id": "uuid",
    "status": "running",         ← pending | running | completed | failed | cancelled
    "records_processed": 3421,
    "records_inserted": 2890,
    "error_message": null,
    "started_at": "2024-...",
    "completed_at": null
  }
```

```
POST /api/v1/projects/{id}/jobs/{job_id}/cancel/
→ Marca job como cancelled (o Rust detecta e para)
```

---

## Performance

### Por que COPY e não ORM?

O PostgreSQL `COPY` é 10–100x mais rápido que `INSERT` individual via ORM para grandes volumes:

- **ORM**: 1 INSERT = 1 round-trip = ~1ms → 10.000 papers = ~10s
- **COPY**: buffer em memória → 1 COPY = 1 round-trip = ~0.1s para 10.000 rows

### Deduplicação

```sql
-- Paper: ON CONFLICT (pmid) DO UPDATE
-- Só atualiza se raw_xml_hash mudou (evita reprocessamento desnecessário)

-- OmicDataset: ON CONFLICT (accession) DO UPDATE
-- DatasetPaperLink: ON CONFLICT (dataset_id, paper_id) DO NOTHING
```

### Paralelismo no Rust

- Parsing XML dos batches de efetch: paralelo via `rayon`
- NER por paper: paralelo via `rayon`
- Fontes ômicas: sequential por fonte, mas interno pode ser parallel

---

## Limites e Configurações

| Parâmetro | Sem API Key | Com API Key |
|-----------|-------------|-------------|
| Rate limit | 3 req/s | 10 req/s |
| Max PMIDs por esearch | 5.000 | 10.000 |
| Batch size efetch | 200 | 200 |
| Max datasets por fonte | 10.000 | 10.000 |

A chave NCBI é configurada em:
- Global: `NCBI_API_KEY` env var (`config/settings/base.py`)
- Por usuário: `UserProfile.ncbi_api_key` (tem precedência)
