## 9. Roadmap de Desenvolvimento (MVP)

### Fase 1 — Fundação (Semanas 1-2)

**Objetivo:** Django rodando com models, migrations, admin, e API básica de projetos.

**Tarefas:**
1. Scaffolding Django com a estrutura de diretórios definida na Seção 4.
2. Implementar todos os models da Seção 3 com migrations.
3. Criar migration RunSQL para triggers FTS e índices auxiliares.
4. Configurar DRF com serializers e viewsets para `DaVinciProject`.
5. Configurar Celery + Redis.
6. Setup Docker Compose para Postgres + Redis.
7. Seed de `ClinicalCategory` (5 eixos padrão) e `OmicCategory` via `seed_categories.py`.
8. Testes: CRUD de projetos via API.

### Fase 2 — Rust Engine para Literatura (Semanas 3-5)

**Objetivo:** Rust engine funcional para busca, ingestão, categorização e NER do PubMed.

**Tarefas:**
1. Criar crate Rust com PyO3.
2. Implementar `ncbi/client.rs` com rate limiting e backoff.
3. Implementar `ncbi/parser.rs` com quick-xml — extração completa em uma passada.
4. Implementar `categorization/clinical.rs` — categorização clínica automática (5 eixos via regex compilados + scoring → `ProjectPaperClinicalCategory`).
5. Implementar `categorization/gene_ner.rs` — extração de genes dos abstracts → `PaperGene`.
6. Implementar `categorization/drug_ner.rs` — extração de drogas dos abstracts → `PaperDrug`.
7. Implementar `categorization/context_extractor.rs` — extração de sentenças-contexto → `EntityContext`.
8. Implementar `db/copy_writer.rs` para injeção via COPY (Paper + Author + Keyword + MeSH + Gene + Drug + Variant + EntityContext + ProjectPaper + ClinicalCategory).
9. Implementar `db/job_tracker.rs` para atualizar IngestionJob.
10. Conectar via `maturin develop`.
11. Criar Celery tasks em `ingestion_tasks.py` (pubmed_search, gene_ner, drug_ner, context_extraction, clinical_categorization).
12. Testes: buscar "cardiovascular disease" e validar ingestão completa no Postgres (incluindo genes, drogas, contextos e categorias).

### Fase 3 — Rust Engine para Metadados Ômicos (Semanas 6-7)

**Objetivo:** Busca e ingestão de metadados GEO/SRA/BioProject/GWAS Catalog.

**Tarefas:**
1. Implementar parsers de metadados ômicos em Rust (`omics/geo_parser.rs`, `sra_parser.rs`, `bioproject_parser.rs`).
2. Implementar `omics/gwas_parser.rs` — ingestão do GWAS Catalog (traits, associations, p-values → `extra_metadata`).
3. Implementar `DatasetPaperLink` discovery via elink.
4. Implementar categorização heurística de tipo ômico (incluindo microbiome).
5. COPY para `OmicDataset` e `DatasetPaperLink`.
6. Testes: buscar metadados ômicos para "cardiovascular disease" e validar links com papers.

### Fase 4 — API de Curadoria e Análise (Semanas 8-10)

**Objetivo:** API completa para curadoria, FTS, categorização e análise integrada.

**Tarefas:**
1. Implementar endpoints de curadoria para papers (Seção 6.2) — incluindo filtro por `clinical_category`.
2. Implementar endpoints de categorias (Seção 6.3) — CRUD de `UserCategory` + atribuição de categorias a papers.
3. Implementar endpoints de curadoria para datasets (Seção 6.4).
4. Implementar FTS via `search_vector` nos endpoints de busca.
5. Implementar `ProjectPaperDataset` e endpoints de links (Seção 6.5).
6. Implementar `ProjectStats` com refresh via Celery beat (incluindo `papers_by_country`, `papers_by_clinical_category`, `top_drugs`).
7. Implementar endpoint de exportação (JSON/CSV estruturado — incluindo EntityContext para RAG).
8. Testes end-to-end: fluxo completo de busca → categorização → curadoria → análise integrada → exportação.

### Pós-MVP

- **Download de Dados Ômicos:** Fila para download real de arquivos do SRA/GEO.
- **Camada de IA Generativa:** RAG sobre o corpus curado (papers + EntityContext + categorias).
- **Frontend:** Interface completa consumindo a API DRF (Gemini Pro).
- **Conclusões Automáticas:** Sumarização das descobertas por categoria clínica (via IA generativa sobre EntityContext).
