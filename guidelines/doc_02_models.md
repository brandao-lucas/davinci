# DaVinci — Models: Detalhes e Conexões

Todos os models ficam em `apps/accounts/models.py` e `apps/core/models.py`.

---

## Princípio de Compartilhamento de Dados

**Paper** e **OmicDataset** são registros **globais** (compartilhados entre todos os projetos).
**ProjectPaper** e **ProjectDataset** são os registros de **curadoria por projeto** — eles conectam o dado global ao projeto específico com status, notas e relevância.

Isso evita duplicação: se dois projetos buscarem o mesmo PMID, o `Paper` existe uma vez, mas cada projeto tem seu próprio `ProjectPaper` com curadoria independente.

---

## Módulo `accounts`

### `UserProfile`
Extensão do `auth.User` do Django.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `id` | UUID PK | Chave primária |
| `user` | OneToOne → User | Bridge com Django auth |
| `firebase_uid` | CharField unique | UID do Firebase — chave de identificação SSO |
| `auth_provider` | CharField | `password`, `google.com`, `oidc.orcid` |
| `orcid_id` | CharField optional | ORCID do pesquisador |
| `institution` | CharField optional | Instituição de pesquisa |
| `research_area` | CharField optional | Área de pesquisa |
| `avatar_url` | URLField optional | Foto de perfil do Firebase |
| `ncbi_api_key` | CharField optional | Chave NCBI pessoal (eleva rate limit de 3 para 10 req/s) |
| `last_firebase_sync` | DateTimeField auto | Último sync com Firebase |

**Conexões:** `User` (1:1)

---

## Módulo `core`

### `DaVinciProject`
Projeto de pesquisa do usuário. Ponto de entrada para toda a curadoria.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `id` | UUID PK | |
| `user` | FK → User (CASCADE) | Dono do projeto |
| `slug` | CharField unique | Identificador legível (gerado auto) |
| `title` | CharField | Título do projeto |
| `description` | TextField optional | Descrição livre |
| `query_term` | CharField | Termo principal de busca (ex: "cardiovascular disease") |
| `query_synonyms` | JSONField | Lista de sinônimos para a query |
| `date_from` | IntegerField optional | Ano inicial do filtro |
| `date_to` | IntegerField optional | Ano final do filtro |
| `target_organisms` | JSONField | Lista de organismos-alvo |
| `target_tissues` | JSONField | Lista de tecidos-alvo |
| `status` | CharField | `DRAFT → SEARCHING → CURATING → ANALYZING → COMPLETE` |
| `created_at` | DateTimeField auto | |
| `updated_at` | DateTimeField auto | |

**Índices:** `(user, status)`, `(query_term)`
**Conexões:** `User` (FK), `ProjectPaper` (1:N), `ProjectDataset` (1:N), `ProjectStats` (1:1), `IngestionJob` (1:N), `UserCategory` (1:N)

---

## Literatura — Dados Compartilhados

### `Paper`
Registro global de paper do PubMed/PMC. Chave natural = PMID.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `id` | BigInt auto PK | |
| `pmid` | CharField unique | PubMed ID — chave natural |
| `pmc_id` | CharField optional | PubMed Central ID |
| `doi` | CharField optional | Digital Object Identifier |
| `title` | TextField | Título do paper |
| `abstract` | TextField | Abstract completo |
| `journal` | CharField | Nome do periódico |
| `pub_year` | IntegerField | Ano de publicação |
| `pub_month` | IntegerField optional | Mês de publicação |
| `pub_type` | CharField optional | Tipo: Review, RCT, Systematic Review, etc. |
| `search_vector` | SearchVectorField | FTS do PostgreSQL (auto-atualizado via trigger) |
| `raw_xml_hash` | CharField | SHA-256 do XML para detecção de mudanças |
| `ingested_at` | DateTimeField auto | Timestamp de primeira ingestão |
| `updated_at` | DateTimeField auto | |

**Índices:** GIN em `search_vector`, `pmid` (unique), `pub_year`, `journal`
**Nota:** O Rust struct usa o nome `abstract_text`; o mapeamento para a coluna `abstract` é feito no COPY writer.
**Conexões:** `PaperAuthor` (1:N), `PaperKeyword` (1:N), `PaperMeSHTerm` (1:N), `PaperGene` (1:N), `PaperDrug` (1:N), `PaperVariant` (1:N), `EntityContext` (1:N), `DatasetPaperLink` (M2M → OmicDataset)

---

### `PaperAuthor`
Autores do paper com posição na lista.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `paper` | FK → Paper (CASCADE) | |
| `position` | IntegerField | 1 = primeiro autor, N = último (senior) |
| `last_name` | CharField | |
| `initials` | CharField | |
| `affiliation` | TextField optional | Afiliação institucional |
| `country` | CharField optional | País extraído da afiliação (Rust regex) |

**Unique:** `(paper, position)`

---

### `PaperKeyword`
Keywords fornecidas pelos autores.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `paper` | FK → Paper (CASCADE) | |
| `keyword` | CharField | Keyword original |
| `keyword_lower` | CharField | Versão lowercase para deduplicação |

**Unique:** `(paper, keyword_lower)`

---

### `PaperMeSHTerm`
Termos MeSH (Medical Subject Headings) do paper.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `paper` | FK → Paper (CASCADE) | |
| `descriptor` | CharField | Descritor MeSH (ex: "Cardiovascular Diseases") |
| `qualifier` | CharField optional | Qualificador MeSH (ex: "genetics") |
| `is_major_topic` | BooleanField | True se `MajorTopicYN="Y"` no XML |

**Índice:** `(descriptor, is_major_topic)`

---

### `PaperGene`
Genes mencionados no abstract (extraídos por NER no Rust).

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `paper` | FK → Paper (CASCADE) | |
| `gene_symbol` | CharField | Símbolo do gene (ex: "BRCA1") |
| `entrez_id` | CharField optional | ID Entrez/NCBI do gene |
| `mention_count` | IntegerField | Quantidade de menções no abstract |

**Unique:** `(paper, gene_symbol)`

---

### `PaperDrug`
Fármacos mencionados no abstract (NER em Rust).

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `paper` | FK → Paper (CASCADE) | |
| `drug_name` | CharField | Nome do fármaco |
| `drug_name_lower` | CharField | Lowercase para deduplicação |
| `mention_count` | IntegerField | Quantidade de menções |
| `drugbank_id` | CharField optional | ID no DrugBank para cross-referência |

**Unique:** `(paper, drug_name_lower)`

---

### `PaperVariant`
RS numbers (SNPs) identificados no abstract.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `paper` | FK → Paper (CASCADE) | |
| `rs_number` | CharField | Ex: "rs12345678" |

**Unique:** `(paper, rs_number)`

---

### `VariantAnnotation`
Anotações externas para variantes (dbSNP/ClinVar). PK = rs_number.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `rs_number` | CharField PK | Chave natural |
| `gene_symbol` | CharField optional | Gene associado |
| `gene_name` | CharField optional | Nome completo do gene |
| `entrez_id` | CharField optional | ID Entrez |
| `chromosome` | CharField optional | Cromossomo |
| `position` | BigIntegerField optional | Posição genômica |
| `alleles` | CharField optional | Alelos (ex: "A/G") |
| `maf` | FloatField optional | Minor Allele Frequency |
| `clinical_significance` | CharField optional | Significado clínico (ClinVar) |

---

### `EntityContext`
Contexto semântico: sentenças do abstract ao redor de uma entidade (gene, droga, variante).

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `paper` | FK → Paper (CASCADE) | |
| `entity_type` | CharField | `gene`, `drug`, `variant`, `disease`, `pathway` |
| `entity_name` | CharField | Nome da entidade |
| `sentence` | TextField | Sentença extraída do abstract |
| `sentence_position` | IntegerField | Posição da sentença no abstract |

**Índices:** `(entity_type, entity_name)`, `(paper, entity_type)`
**Uso:** Base para a camada de IA generativa — contexto estruturado para LLMs.

---

## Ômica — Dados Compartilhados

### `OmicDataset`
Registro global de dataset ômico (GEO, SRA, BioProject, etc.). Chave natural = accession.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `id` | BigInt auto PK | |
| `accession` | CharField unique | Ex: "GSE12345", "SRP123456" — chave natural |
| `source_db` | CharField | `geo`, `sra`, `arrayexpress`, `tcga`, `bioproject`, `gwas_catalog` |
| `title` | TextField | Título do dataset |
| `summary` | TextField optional | Resumo/descrição |
| `omic_type` | CharField | `genomic`, `transcriptomic`, `proteomic`, `metabolomic`, `epigenomic`, `metagenomic`, `microbiome`, `multi_omic`, `other` — multi-valor separado por vírgula |
| `omic_subcategory` | CharField optional | Ex: "RNA-Seq", "WGS", "ChIP-Seq", "16S rRNA" |
| `organism` | CharField optional | Organismo (ex: "Homo sapiens") |
| `tax_id` | IntegerField optional | NCBI Taxonomy ID |
| `n_samples` | IntegerField optional | Número de amostras |
| `platform` | CharField optional | Plataforma experimental |
| `extra_metadata` | JSONField | Campos específicos da fonte (traits, p-values, etc.) |
| `is_active` | BooleanField | Soft-delete |
| `search_vector` | SearchVectorField | FTS em título + summary (trigger) |
| `ingested_at` | DateTimeField auto | Timestamp de primeira ingestão |
| `updated_at` | DateTimeField auto | |

**Índices:** GIN em `search_vector`, `omic_type`, `organism`, `source_db`, `n_samples`, `accession` (unique)
**Nota:** ArrayExpress e TCGA existem como `SourceDB` choices mas não têm parser de ingestão implementado.
**Conexões:** `DatasetPaperLink` (M2M → Paper via through), `DatasetPaperLinkPending` (staging), `ProjectDataset` (1:N)

---

### `DatasetPaperLink`
Relacionamento M2M entre `OmicDataset` e `Paper`.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `dataset` | FK → OmicDataset (CASCADE) | |
| `paper` | FK → Paper (CASCADE) | |
| `link_source` | CharField | `elink` (NCBI), `geo_xml` (GEO XML), `manual` |

**Unique:** `(dataset, paper)`

---

### `DatasetPaperLinkPending`
Staging table para links dataset ↔ paper aguardando resolução de FK. Criada na migration 0006.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `dataset_accession` | CharField | Accession do dataset (ainda sem FK resolvida) |
| `paper_pmid` | BigIntegerField | PMID do paper (ainda sem FK resolvida) |
| `link_source` | CharField | `elink`, `geo_xml`, etc. |
| `created_at` | DateTimeField auto | |

**Unique:** `(dataset_accession, paper_pmid)`
**Fluxo:** Rust popula durante ingestão ômica; Django resolve após ingestão de papers quando ambos os FKs existirem.

---

## Configurações Globais

### `OmicCategory`
Categorias ômicas pré-definidas. Usadas pelo Rust para classificação automática.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `omic_type` | CharField unique | `genomic`, `transcriptomic`, etc. |
| `keywords` | JSONField | Keywords para classificação heurística |
| `priority` | IntegerField | Ordem de verificação (mais específico primeiro) |
| `is_active` | BooleanField | Habilita/desabilita |

---

### `ClinicalCategory`
Eixos clínicos globais para categorização de papers. Imutáveis.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `slug` | CharField unique | `diagnosis`, `treatment`, `epidemiology`, `mechanism`, `signs_symptoms` |
| `name` | CharField | Nome de exibição |
| `description` | TextField | Descrição do eixo |
| `keywords` | JSONField | Keywords para categorização heurística |
| `is_default` | BooleanField | True = imutável (5 eixos padrão) |
| `priority` | IntegerField | Ordem de exibição |

---

### `UserCategory`
Categorias customizadas criadas pelo pesquisador, com escopo por projeto.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `project` | FK → DaVinciProject (CASCADE) | |
| `name` | CharField | Nome da categoria |
| `keywords` | JSONField | Keywords para auto/manual classification |
| `color` | CharField | Cor hex para UI |
| `created_at` | DateTimeField auto | |

**Unique:** `(project, name)`

---

## Curadoria por Projeto

### `ProjectPaper`
Curadoria de um paper dentro de um projeto. **Tabela central da curadoria.**

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `project` | FK → DaVinciProject (CASCADE) | |
| `paper` | FK → Paper (CASCADE) | |
| `curation_status` | CharField | `pending`, `included`, `excluded`, `maybe` |
| `exclusion_reason` | CharField optional | Motivo de exclusão |
| `notes` | TextField optional | Notas do pesquisador |
| `relevance_score` | FloatField | 0.0–1.0 (Rust ou manual) |
| `curated_at` | DateTimeField optional | Timestamp da curadoria |
| `clinical_categories` | M2M → ClinicalCategory via `ProjectPaperClinicalCategory` | |
| `user_categories` | M2M → UserCategory | |

**Índices:** `(project, curation_status)`, `(relevance_score)`
**Unique:** `(project, paper)`

---

### `ProjectPaperClinicalCategory`
Tabela de ligação M2M entre `ProjectPaper` e `ClinicalCategory` com score de confiança.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `project_paper` | FK → ProjectPaper (CASCADE) | |
| `category` | FK → ClinicalCategory (CASCADE) | |
| `confidence_score` | FloatField | 0.0–1.0 |
| `is_manual` | BooleanField | True = atribuição humana |

**Unique:** `(project_paper, category)`

---

### `ProjectDataset`
Curadoria de um dataset ômico dentro de um projeto.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `project` | FK → DaVinciProject (CASCADE) | |
| `dataset` | FK → OmicDataset (CASCADE) | |
| `curation_status` | CharField | `pending`, `included`, `excluded`, `maybe`, `queued` (fila de download), `downloaded` |
| `exclusion_reason` | CharField optional | |
| `notes` | TextField optional | |
| `relevance_score` | FloatField | 0.0–1.0 |
| `curated_at` | DateTimeField optional | |

**Índice:** `(project, curation_status)`
**Unique:** `(project, dataset)`

---

### `ProjectPaperDataset`
Bridge explícito que vincula um paper curado a um dataset curado **dentro do mesmo projeto**.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `project` | FK → DaVinciProject (CASCADE) | |
| `project_paper` | FK → ProjectPaper (CASCADE) | |
| `project_dataset` | FK → ProjectDataset (CASCADE) | |
| `link_confidence` | CharField | `auto` (elink/co-occurrence), `confirmed`, `rejected` |

**Unique:** `(project, project_paper, project_dataset)`

---

## Cache e Controle de Jobs

### `ProjectStats`
Cache de estatísticas do projeto. Atualizado sob demanda.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `project` | OneToOne → DaVinciProject (PK) | |
| `total_papers` | IntegerField | |
| `included_papers` | IntegerField | |
| `excluded_papers` | IntegerField | |
| `pending_papers` | IntegerField | |
| `total_datasets` | IntegerField | |
| `included_datasets` | IntegerField | |
| `total_samples` | IntegerField | |
| `papers_by_year` | JSONField | `{"2020": 45, "2021": 78}` |
| `papers_by_journal` | JSONField | Top 20 periódicos |
| `papers_by_country` | JSONField | Top 30 países (1º autor) |
| `papers_by_clinical_category` | JSONField | Distribuição por eixo clínico |
| `datasets_by_omic_type` | JSONField | Distribuição por tipo ômico |
| `datasets_by_organism` | JSONField | Top 20 organismos |
| `top_genes` | JSONField | Top 20 genes por mention_count |
| `top_drugs` | JSONField | Top 20 drogas |
| `top_mesh_terms` | JSONField | Top 20 MeSH (major topics only) |
| `top_variants` | JSONField | Top variantes (futuro) |
| `last_computed` | DateTimeField auto | |

---

### `IngestionJob`
Controle de jobs assíncronos de ingestão.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `id` | UUID PK | |
| `project` | FK → DaVinciProject (CASCADE) | |
| `job_type` | CharField | `pubmed_search`, `pubmed_fetch`, `geo_search`, `sra_search`, `gwas_search`, `variant_annotation`, `gene_ner`, `drug_ner`, `context_extraction` |
| `status` | CharField | `pending`, `running`, `completed`, `failed`, `cancelled` |
| `parameters` | JSONField | Query, date range, PMIDs, fontes ômicas, etc. |
| `records_processed` | IntegerField | Total processado |
| `records_inserted` | IntegerField | Novos registros criados |
| `records_updated` | IntegerField | Registros atualizados |
| `error_message` | TextField optional | Erros não-fatais |
| `created_at` | DateTimeField auto | |
| `started_at` | DateTimeField optional | |
| `completed_at` | DateTimeField optional | |

**Índices:** `(project, status)`, `(job_type, status)`

---

## Diagrama de Conexões

```
auth.User
  └── UserProfile (1:1)

DaVinciProject (user FK)
  │
  ├── IngestionJob (1:N) — controle de jobs
  ├── ProjectStats (1:1) — cache de agregações
  ├── UserCategory (1:N) — categorias customizadas
  │
  ├── ProjectPaper (1:N) ─────┬──> Paper
  │     ├── ClinicalCategory  │      ├── PaperAuthor
  │     │   via M2M+score     │      ├── PaperKeyword
  │     └── UserCategory      │      ├── PaperMeSHTerm
  │         via M2M           │      ├── PaperGene
  │                           │      ├── PaperDrug
  │                           │      ├── PaperVariant
  │                           │      └── EntityContext
  │                           │
  └── ProjectDataset (1:N) ───┴──> OmicDataset
        └── ProjectPaperDataset     └── DatasetPaperLink
            (bridge paper ↔ dataset       (M2M via link_source)
             dentro do projeto)
```

---

## SQL Especial (Triggers e Views Materializadas)

### Trigger FTS — `Paper`
```sql
-- Atualiza search_vector automaticamente no INSERT/UPDATE
CREATE TRIGGER paper_fts_update
BEFORE INSERT OR UPDATE ON core_paper
FOR EACH ROW EXECUTE FUNCTION tsvector_update_trigger(
  search_vector, 'pg_catalog.english', title, abstract
);
-- Nota: coluna é "abstract" (não abstract_text)
```

### Trigger FTS — `OmicDataset`
```sql
CREATE TRIGGER dataset_fts_update
BEFORE INSERT OR UPDATE ON core_omicdataset
FOR EACH ROW EXECUTE FUNCTION tsvector_update_trigger(
  search_vector, 'pg_catalog.english', title, summary
);
```

### View Materializada: `mv_project_paper_stats`
Agrega contagens de papers por projeto — evita agregações caras em tempo real.
