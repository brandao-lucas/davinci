# PlatOMICs — Panorama Técnico (DaVinci)

Documento de referência para planejamento do repositório multi-ômico humano em cima do DaVinci.
Estado: **junho/2026**.

---

## 1. Coleta / Conectores

| Repositório | Estado | Método | O que captura |
|---|---|---|---|
| **PubMed/PMC** | Estável | API oficial NCBI E-utilities (esearch → efetch XML) | Metadados + abstract; PMC full-text **não** |
| **GEO** | Estável | API NCBI (esearch → esummary JSON, batches de 25 UIDs) | Metadados de estudo (GSE); dados brutos **não** |
| **SRA** | Estável | API NCBI (esearch → esummary, dedup por SRP) | Metadados de experimento; FASTQs **não** |
| **BioProject** | Estável | API NCBI (esearch → esummary) | Metadados de projeto (PRJNA) |
| **GWAS Catalog** | Estável | API REST EBI (`/gwas/rest/api/studies`) | Metadados de estudo (GCST) |
| **ArrayExpress** | Não implementado | — | Só existe como `SourceDB.ARRAYEXPRESS` no schema |
| **TCGA** | Não implementado | — | Só existe como `SourceDB.TCGA` no schema |
| **PRIDE, MetaboLights, MassIVE, iProx** | Não existe | — | Nem no schema |

**Onde quebra com mais frequência:** rate limit do NCBI (HTTP 429). O cliente Rust implementa retry com exponential backoff (5 tentativas, header `Retry-After` respeitado), mas sem API key o teto é 3 req/s para todos os endpoints NCBI combinados. GEO esummary às vezes retorna campos ausentes — tratado via `#[serde(default)]`.

---

## 2. Esquema de Metadados

### Paper (PubMed)

Campos diretos: `pmid`, `pmc_id`, `doi`, `title`, `abstract`, `journal`, `pub_year`, `pub_month`, `pub_type`, `raw_xml_hash`

Relacionados:

| Tabela | Campos principais |
|---|---|
| `PaperAuthor` | nome, país |
| `PaperKeyword` | texto livre |
| `PaperMeSHTerm` | texto livre (sem mapping para ontologia) |
| `PaperGene` | gene_symbol, entrez_id, mention_count |
| `PaperDrug` | drug_name, drugbank_id, mention_count |
| `PaperVariant` | rs_number |
| `EntityContext` | sentenças do abstract ao redor de entidades (para RAG futuro) |

### Dataset (OmicDataset)

Campos diretos: `accession`, `source_db`, `bioproject_id`, `title`, `summary`, `omic_type`, `omic_subcategory`, `organism`, `tax_id`, `n_samples`, `platform`, `extra_metadata` (JSONB)

Os campos core são unificados entre fontes. Campos específicos de cada fonte (ex: `gds_type`, `target_scope`, `association_count`) vão em `extra_metadata`.

### Cobertura de campos relevantes para repositório multi-ômico

| Campo | Capturado? | Observação |
|---|---|---|
| Tipo de ômica | Sim | `omic_type` (9 valores) + `omic_subcategory` (RNA-Seq, WGS, etc.) |
| Nº de ômicas por estudo | Parcial | `omic_type` armazena múltiplos como string comma-separated |
| Single-cell vs bulk | Não | Pode estar implícito em `omic_subcategory`, sem campo explícito |
| Grupo controle | Não | — |
| Tecido | Não | Texto livre no `summary` |
| Doença | Não | Texto livre no `summary` ou via link com paper |
| Formato dos dados (raw/processed) | Não | — |
| Acesso público/controlado | Não | — |

---

## 3. Armazenamento

- **Banco:** PostgreSQL 16. Sem SQLite, MongoDB ou arquivos soltos no pipeline principal.
- **Schema:** Tabelas prefixadas em `core_` — `core_paper`, `core_omicdataset`, `core_davincipro`, `core_projectpaper`, `core_projectdataset`, `core_ingestionjob`, etc. 6 migrations aplicadas.
- **Inserção:** Rust faz bulk COPY via staging table + `INSERT … ON CONFLICT DO UPDATE`. Sem ORM, sem Django Signals.
- **FTS:** Campo `search_vector` (tsvector) em `core_paper` e `core_omicdataset`, mantido por triggers Postgres, indexado com GinIndex.
- **Volume:** Base de desenvolvimento local — sem dados de produção para reportar no momento.

---

## 4. Classificação / Anotação

| Recurso | Estado | Observação |
|---|---|---|
| MeSH Terms | Capturados | Texto livre, sem mapping para ontologia |
| DOID / MONDO / UBERON | Não implementado | — |
| Omic type classification | Implementado | Keyword matching via Aho-Corasick no Rust (`OmicCategory.keywords`) |
| Categorias clínicas de paper | Implementado | 5 eixos padrão: `diagnosis`, `treatment`, `epidemiology`, `mechanism`, `signs_symptoms` |
| Doença/tecido | Texto livre | Só via `summary`, `PaperMeSHTerm` e `EntityContext` |
| Mendeliana vs complexa | Não implementado | — |
| NER de genes | Implementado | Aho-Corasick sobre abstract |
| NER de drogas | Implementado | — |
| NER de variantes | Implementado | Regex `rs[0-9]+` |
| Anotação externa de variantes (dbSNP/ClinVar) | Incerto | Schema `VariantAnnotation` existe, job type `variant_annotation` definido, implementação não verificada |

---

## 5. Estado do Código

### Stack

| Camada | Tecnologia |
|---|---|
| Backend | Python 3.13 + Django 6.0 + DRF + Celery |
| Engine | Rust (PyO3 + Maturin, compilado como `cdylib`) |
| Banco | PostgreSQL 16 + Redis |
| Frontend | Next.js 16.2 + React 19 + TypeScript + Tailwind + Shadcn/ui |

### Módulos Rust (`rust_src/src/`)

| Módulo | Estado |
|---|---|
| `ncbi/` — client, fetch, parser, models | Funcional |
| `omics/geo_parser`, `sra_parser`, `bioproject_parser`, `gwas_parser`, `elink`, `type_classifier` | Funcional |
| `categorization/` — gene_ner, drug_ner, clinical, context_extractor | Funcional |
| `db/` — copy_writer, job_tracker, connection | Funcional |

### Módulos Django (`apps/core/`)

| Módulo | Estado |
|---|---|
| `models.py` — schema completo | Funcional |
| `tasks/` — ingestion_tasks, stats_tasks | Funcional |
| `services/` — search_service, stats_service | Funcional |
| `views/` — paper, dataset, job, project, category, link | Funcional |
| `serializers/` | Completo |
| `tests/` — test_api, test_ingestion | Existem |

### Pendências / Incompletos

| Item | Estado |
|---|---|
| ArrayExpress connector | Schema existe, zero código de coleta |
| TCGA connector | Schema existe, zero código de coleta |
| `variant_annotation` job | Schema e job_type definidos, lookup dbSNP/ClinVar incerto |
| Export de dados (CSV/JSON) | Nenhum endpoint de dump visível |
| `mv_project_paper_stats` (view materializada) | SQL definido em `models.py` como comentário, não em migration — status de deploy incerto |

---

## 6. Saída / Interface

| Canal | Estado |
|---|---|
| API REST (DRF) | Funcional — papers, datasets, jobs, categorias, projetos, stats |
| Frontend Next.js | Funcional — listagem, filtros, curadoria |
| Busca FTS | Via `?search=` nos ViewSets, usa `search_vector` com `@@` Postgres |
| Export CSV/JSON | Não implementado |
| Dump de banco | Só via `pg_dump` manual |
| Polling de job | Via `GET /api/jobs/<id>/` — sem WebSocket |

---

## Gaps para o Repositório Multi-Ômico Humano

Os principais buracos a cobrir:

1. **Proteômica** (PRIDE) e **Metabolômica** (MetaboLights, MassIVE) — zero connectors
2. **Single-cell vs bulk** — não é campo de primeiro nível no schema
3. **Tissue / Disease como ontologia** — tudo texto livre hoje (sem UBERON / DOID / MONDO)
4. **Acesso controlado** (dbGaP, TCGA controlled access) — não modelado
5. **Export** — nenhum mecanismo de dump para downstream
