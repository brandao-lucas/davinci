# DaVinci -- Relatorio Final de Diagnostico

**Data:** 2026-04-06
**Python:** 3.13.2 (venv) | **Rust:** 1.93.1 | **Django:** 6.0 | **Postgres:** 16-alpine

---

## Arquivos de Log Gerados

| Arquivo | Linhas | Status |
|---------|--------|--------|
| `01_inventory.log` | 523 | OK |
| `02_roadmap_checklist.md` | 83 | OK |
| `03_db_integrity.log` | 140 | OK |
| `04_rust_engine.log` | 33 | OK |
| `05_pubmed_ingestion.log` | 33 | OK |
| `06_omics_ingestion.log` | 74 | OK |
| `07_api_tests.log` | ~60 | OK |
| `08_django_tests.log` | 105 | 1 FAIL, 1 ERROR |
| `09_rust_django_consistency.log` | 81 | OK |
| `10_omics_deep_debug.log` | 266 | OK |

---

## Resumo Executivo

O projeto DaVinci esta **surpreendentemente completo** -- 40 de 46 items do roadmap existem e funcionam. A ingestao PubMed e OMICs funciona corretamente quando chamada diretamente. Os bugs encontrados sao corrigiveis.

### Resultados dos Testes de Ingestao

| Fonte | Processados | Inseridos | Erros | Status |
|-------|-------------|-----------|-------|--------|
| PubMed | 136 | 136 | 0 | OK |
| GEO | 10 | 10 | 0 | OK |
| SRA | 0 | 0 | 0 | Vazio (sem erro) |
| BioProject | 10 | 9 | 0 | OK (1 duplicata) |
| GWAS | 3 | 3 | 0 | OK |

### Resultados da API REST

| Endpoint | Status |
|----------|--------|
| GET /projects/ | 200 |
| POST /projects/ | 201 (mas slug duplicado = 500) |
| POST /search/ | 202 |
| POST /omics_search/ | 202 |
| GET /papers/ | 200 |
| GET /papers/search/ | 200 |
| POST /papers/bulk_curate/ | 200 |
| GET /datasets/ | 200 |
| GET /datasets/search/ | 200 |
| GET /stats/ | 200 |
| GET /export/ | 200 |
| GET /links/ | 200 |
| GET /jobs/ | 200 |
| GET /auth/me/ | **500** |

### Consistencia Rust/Django

- 14/14 tabelas criticas existem
- 12/12 constraints UNIQUE para ON CONFLICT estao presentes
- SearchVectorField em Paper e OmicDataset OK
- Triggers FTS e indices GIN OK

---

## BUGS ENCONTRADOS (por prioridade)

### CRITICO

**BUG 1: Shadow de `rust_engine` pelo diretorio do projeto**
- **Problema:** O diretorio `rust_engine/` (codigo-fonte Rust) no root do projeto faz shadow do pacote PyO3 compilado no site-packages quando o CWD esta no `sys.path`. O `import rust_engine` importa o diretorio (namespace package vazio) em vez do modulo compilado.
- **Impacto:** `search_and_ingest_pubmed` e `search_and_ingest_omics` nao estao disponiveis quando rodando via `manage.py shell` ou Celery worker no diretorio do projeto.
- **Arquivo:** Afeta qualquer `import rust_engine` quando CWD = root do projeto
- **Correcao:** Adicionar `__init__.py` ao diretorio `rust_engine/` que re-exporta do modulo compilado, OU renomear o diretorio de codigo Rust (ex: `rust_src/`), OU adicionar `rust_engine` ao `.gitignore` do sys.path

### ALTO

**BUG 2: SRA retorna 0 datasets**
- **Problema:** A busca SRA retornou 0 datasets para "cardiovascular disease" enquanto a API NCBI tem milhares de resultados. O parser pode nao estar extraindo os study accessions corretamente.
- **Arquivo:** `rust_engine/src/omics/sra_parser.rs`
- **Investigar:** Formato do XML de esummary do SRA e como o parser extrai SRP accessions

**BUG 3: DatasetPaperLinks sempre 0**
- **Problema:** Nenhum link dataset-paper foi criado em nenhuma das fontes. O elink nao esta encontrando relacoes ou os resultados nao estao sendo persistidos.
- **Arquivo:** `rust_engine/src/omics/elink.rs`, `rust_engine/src/db/copy_writer.rs::copy_dataset_paper_links()`
- **Investigar:** Se o elink esta sendo chamado, se retorna PMIDs, e se o copy_writer faz FK resolution corretamente

**BUG 4: POST /projects/ nao trata slug duplicado**
- **Problema:** `perform_create` gera slug automatico mas nao trata `UniqueViolation`, resultando em 500 Internal Server Error.
- **Arquivo:** `apps/core/views/project_views.py:26`
- **Correcao:** Adicionar try/except IntegrityError ou gerar slug com sufixo random

**BUG 5: GET /auth/me/ falha sem UserProfile**
- **Problema:** `MeView.get()` faz `UserProfile.objects.get(user=request.user)` que falha com `DoesNotExist` se o user nao tem profile.
- **Arquivo:** `apps/accounts/views.py:20`
- **Correcao:** Usar `get_or_create` ou criar profile automaticamente

### MEDIO

**BUG 6: manage.py usa Python 3.11 do sistema, nao venv 3.13**
- **Problema:** `python manage.py` invoca Python 3.11 do sistema onde rust_engine nao esta instalado. Precisa usar `.venv/bin/python manage.py`.
- **Correcao:** Ativar venv antes de usar, ou atualizar shebang do manage.py

**BUG 7: Django test `test_list_papers_filter_by_status` falha**
- **Problema:** Espera 1 resultado mas recebe 2. Provavelmente o filtro de curation_status nao funciona corretamente ou o setup do teste cria dados extras.
- **Arquivo:** `apps/core/tests/test_api.py:102`

**BUG 8: Django test `efetch failed: error decoding response body`**
- **Problema:** O teste de ingestao faz chamada real ao NCBI e falha no decode da resposta XML. Pode ser rate limiting ou resposta inesperada.
- **Arquivo:** `apps/core/tests/test_ingestion.py`

---

## FEATURES FALTANTES DO ROADMAP

### Faltantes (2 items)

1. **Management command `seed_categories.py`**
   - Nao existe `apps/*/management/commands/`
   - Categories estao no banco (provavelmente populadas via script externo)

2. **`export_service.py` dedicado**
   - Export esta inline em `apps/core/views/project_views.py`
   - Funciona (200 OK) mas nao esta modularizado

### Incompletos (4 items)

1. **Gene NER** - Apenas 6 genes hardcoded (BRCA1, TP53, EGFR, TNF, IL6, BRAF)
2. **Drug NER** - Apenas 6 drogas hardcoded
3. **Context extractor** - Implementacao naive (split por ". ")
4. **OmicCategory seeds** - Apenas 2 categorias (pode precisar de mais)

---

## CORRECOES SUGERIDAS (em ordem de prioridade)

### 1. Corrigir shadow de rust_engine (CRITICO)
```
# Opcao A: renomear diretorio
mv rust_engine rust_src
# Atualizar Cargo.toml, maturin configs, .gitignore

# Opcao B: adicionar __init__.py ao diretorio
# rust_engine/__init__.py:
try:
    from rust_engine.rust_engine import *
except ImportError:
    pass
```

### 2. Investigar SRA parser (ALTO)
- Verificar `sra_parser.rs` -- o formato de esummary do SRA pode ter mudado
- Testar com query diferente

### 3. Investigar elink/DatasetPaperLinks (ALTO)
- Verificar se `discover_links_via_elink` esta sendo chamado no fluxo de ingestao
- Verificar se os IDs retornados pelo elink sao validos

### 4. Tratar slug duplicado em projects (ALTO)
- `apps/core/views/project_views.py:26` -- adicionar sufixo numerico ou UUID ao slug

### 5. Tratar UserProfile.DoesNotExist (ALTO)
- `apps/accounts/views.py:20` -- usar `get_or_create`

### 6. Criar management command seed_categories (MEDIO)
### 7. Extrair export_service.py (BAIXO)
### 8. Expandir gene/drug NER dictionaries (BAIXO)

---

## Dados no Banco Apos Diagnostico

| Tabela | Registros |
|--------|-----------|
| Paper | 7,132 |
| PaperAuthor | 49,951 |
| PaperKeyword | 31,903 |
| PaperMeSHTerm | 46,370 |
| PaperGene | 52 |
| PaperVariant | 0 |
| OmicDataset | 703 |
| DatasetPaperLink | 0 |
| ProjectPaper | 136 (projeto diag) |
| ProjectDataset | 22 (projeto diag) |
| IngestionJob | ~10 |
| ClinicalCategory | 5 |
| OmicCategory | 2 |

---

## Django Unit Tests

- **Total:** 57 testes
- **Passaram:** 53
- **Falharam:** 1 (`test_list_papers_filter_by_status`)
- **Erros:** 1 (`efetch failed: error decoding response body`)
- **Skipped:** 2
