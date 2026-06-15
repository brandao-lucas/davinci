# DaVinci — Visão Geral do Projeto

## O Que É

**DaVinci** é uma plataforma de pesquisa bioinformática integrada que permite a pesquisadores curar literatura científica e metadados de datasets ômicos dentro de projetos personalizados. O nome faz referência à síntese entre ciência e organização — o sistema conecta literatura (PubMed/PMC) com repositórios públicos de dados ômicos (GEO, SRA, BioProject, GWAS Catalog, ArrayExpress, TCGA) de forma automática e auditável.

O DaVinci é um módulo do ecossistema **PlatOmics** (BioHub Solutions).

---

## Problema que Resolve

Pesquisadores que realizam revisões sistemáticas ou meta-análises precisam:
1. Buscar papers em múltiplas bases ao mesmo tempo
2. Identificar datasets ômicos relacionados à sua questão de pesquisa
3. Cruzar literatura com dados ômicos disponíveis publicamente
4. Curar manualmente o que é relevante (incluir/excluir com justificativa)
5. Exportar os resultados curados para análise downstream ou input de IA generativa

Hoje, esse processo é manual, fragmentado e lento. O DaVinci automatiza as etapas 1-3 e estrutura a curadoria (passo 4) com rastreabilidade completa.

---

## Stack Tecnológica

| Camada | Tecnologia | Papel |
|--------|-----------|-------|
| **Backend** | Django 6.0 + DRF | Orquestração, API REST, autenticação |
| **Engine de Performance** | Rust (PyO3 / Maturin) | Ingestão em massa, parsing XML, NER |
| **Banco de Dados** | PostgreSQL | Armazenamento principal + FTS nativo |
| **Filas Assíncronas** | Celery + Redis | Processamento em background |
| **Autenticação** | Firebase Authentication | SSO (Google, OrcID, email/senha) |
| **Frontend** | Next.js 16.2 + React 19 + TypeScript | Interface do pesquisador |
| **UI** | Tailwind CSS + Shadcn/ui | Componentes de interface |
| **Desktop** | Tauri 2 | Wrapper desktop (planejado) |

---

## Princípio Fundamental de Arquitetura

> **Django nunca processa dados brutos.**

O Django gerencia metadados, orquestra tarefas e expõe resultados via API. Todo processamento pesado — fetch HTTP das APIs do NCBI, parsing de XML, NER de entidades, injeção em massa no banco — é responsabilidade do **Rust engine**.

A comunicação entre Django e Rust acontece via **PyO3** (Rust compilado como módulo Python) e via tabela de controle `IngestionJob` no PostgreSQL.

---

## Fluxo Principal

```
Pesquisador → cria projeto (query_term + filtros)
    ↓
Django → cria IngestionJob no Postgres
    ↓
Celery → dispara task assíncrona
    ↓
Rust engine:
  1. esearch → lista de PMIDs (NCBI E-utilities)
  2. efetch → XML completo dos papers
  3. Parsing com quick-xml (UMA passada, todos os campos)
  4. NER: extrai genes, drogas, variantes dos abstracts
  5. Categorização clínica (regex compilados → ClinicalCategory)
  6. INSERT via PostgreSQL COPY (bypassa ORM, muito mais rápido)
  7. Atualiza IngestionJob.status = 'completed'
    ↓
Django → detecta job completo (polling)
Django → expõe dados via DRF
    ↓
Pesquisador → curadoria (inclui/exclui papers e datasets)
Pesquisador → categorização + anotações
Pesquisador → exportação (JSON/CSV para IA generativa)
```

---

## Fontes de Dados Integradas

| Fonte | Tipo | Via |
|-------|------|-----|
| **PubMed / PMC** | Literatura científica | NCBI E-utilities (esearch + efetch) |
| **GEO** | Dados ômicos (transcriptômica, etc.) | NCBI E-utilities |
| **SRA** | Dados de sequenciamento bruto | NCBI E-utilities |
| **BioProject** | Projetos de pesquisa ômicos | NCBI E-utilities |
| **GWAS Catalog** | Associações genéticas | EBI REST API |
| **ArrayExpress** | Dados de expressão gênica | _Schema previsto — parser não implementado_ |
| **TCGA** | Dados de câncer | _Schema previsto — parser não implementado_ |

---

## Capacidades do Sistema

### Ingestão Automática
- Busca simultânea em PubMed + bases ômicas com um único `query_term`
- Rate limiting respeitado por fonte (3 req/s sem API key, 10 req/s com)
- Chave NCBI pessoal por usuário (`UserProfile.ncbi_api_key`)
- Deduplicação via natural keys (PMID para papers, accession para datasets)

### NER (Named Entity Recognition) — em Rust
- **Genes**: símbolos de genes extraídos dos abstracts com `mention_count`
- **Drogas**: fármacos/compostos com `mention_count` e `drugbank_id` opcional
- **Variantes**: RS numbers (SNPs) identificados nos textos
- **Contexto semântico**: sentenças ao redor das entidades → `EntityContext`

### Categorização Automática
- **Clínica**: 5 eixos padrão (diagnosis, treatment, epidemiology, mechanism, signs_symptoms) via regex compilados
- **Ômica**: RNA-Seq, WGS, ChIP-Seq, 16S rRNA, etc. via keywords no título/summary
- **Customizável**: pesquisador pode criar `UserCategory` com keywords próprias por projeto

### Curadoria com Auditoria
- Status de curadoria: `pending → included / excluded / maybe`
- Campos de auditoria: `exclusion_reason`, `notes`, `curated_at`
- Curadoria em lote via `bulk_curate` endpoint
- Links literatura ↔ ômica com confirmação manual (`confirmed / rejected / auto`)

### Análise e Exportação _(Fase 5 — não implementado)_
- `ProjectStats` com cache de agregações (genes top, MeSH top, distribuição por ano/journal/país)
- Export em JSON ou CSV dos papers e datasets incluídos
- Dados estruturados para uso como base de conhecimento de IA generativa

---

## Fases de Desenvolvimento

| Fase | Entregável | Status |
|------|-----------|--------|
| **Fase 1** | Fundação Django: models, migrations, API básica de projetos | ✅ Completo |
| **Fase 2** | Rust engine para literatura (PubMed + NER + categorização) | ✅ Completo |
| **Fase 3** | Rust engine para metadados ômicos (GEO, SRA, BioProject, GWAS) | ✅ Completo |
| **Fase 4** | Auth Firebase + Frontend Next.js completo + API de curadoria | ✅ Completo |
| **Fase 5** | Análise integrada, visualizações avançadas, exportação para IA | 🔄 Próxima |

---

## Modelo de Dados — Visão de Alto Nível

```
auth.User
  └── UserProfile (firebase_uid, ncbi_api_key, orcid_id)

DaVinciProject (user FK)
  ├── ProjectPaper (curadoria de papers por projeto)
  │   ├── Paper (compartilhado, chave natural = PMID)
  │   │   ├── PaperAuthor
  │   │   ├── PaperKeyword
  │   │   ├── PaperMeSHTerm
  │   │   ├── PaperGene
  │   │   ├── PaperDrug
  │   │   ├── PaperVariant
  │   │   └── EntityContext
  │   ├── ClinicalCategory (global)
  │   └── UserCategory (por projeto)
  │
  ├── ProjectDataset (curadoria de datasets por projeto)
  │   └── OmicDataset (compartilhado, chave natural = accession)
  │       └── DatasetPaperLink (M2M Dataset ↔ Paper)
  │
  ├── ProjectPaperDataset (bridge literatura ↔ ômica no projeto)
  ├── ProjectStats (cache de estatísticas)
  └── IngestionJob (controle de jobs assíncronos)
```

---

## Onde Está o Código

```
davinci/
├── apps/
│   ├── accounts/       — UserProfile, autenticação Firebase
│   └── core/
│       ├── models.py   — Todos os models principais
│       ├── serializers/
│       │   ├── paper.py
│       │   └── dataset.py
│       ├── views/
│       │   ├── project_views.py
│       │   ├── paper_views.py
│       │   └── dataset_views.py
│       ├── services/
│       │   ├── search_service.py   — Despacho de jobs
│       │   └── stats_service.py    — Cálculo de estatísticas
│       └── tasks/
│           └── ingestion_tasks.py  — Celery tasks
├── rust_src/
│   └── src/
│       ├── lib.rs              — Entry point PyO3
│       ├── ncbi/               — NCBI E-utilities client
│       ├── omics/              — Parsers de datasets ômicos
│       ├── db/                 — COPY writer + job tracker
│       └── categorization/     — NER + categorização clínica
├── config/
│   ├── settings/
│   └── celery.py
└── davinci-frontend/
    └── src/
        ├── app/                — Next.js App Router
        ├── components/         — UI components
        └── lib/
            ├── api/            — Clientes HTTP
            ├── hooks/          — React Query hooks
            └── types/          — TypeScript types
```
