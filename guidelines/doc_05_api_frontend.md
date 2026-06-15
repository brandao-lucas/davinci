# DaVinci — API, Serializers, Views e Frontend

---

## Autenticação

### Fluxo Firebase → Django

```
Frontend → Firebase SDK → id_token (JWT)
    ↓
Headers: Authorization: Bearer {id_token}
    ↓
FirebaseAuthentication (apps/accounts/authentication.py)
    1. Extrai token do header
    2. firebase_admin.auth.verify_id_token(token)
       → decoded: {uid, email, name, picture, ...}
    3. UserService.get_or_create_from_firebase(decoded)
       → encontra ou cria auth.User + UserProfile
    4. request.user = django_user
    ↓
Todas as views filtradas por request.user
```

### Endpoints de Auth

```
GET  /api/v1/auth/me/       — perfil do usuário autenticado
POST /api/v1/auth/verify/   — verifica token Firebase (teste)
```

---

## Estrutura de URLs

```
/api/v1/
  ├── auth/
  │   ├── me/
  │   └── verify/
  │
  ├── projects/                                   ← CRUD projetos
  │   ├── {id}/search/                            ← despacha busca PubMed
  │   ├── {id}/omics_search/                      ← despacha busca ômica
  │   ├── {id}/stats/                             ← estatísticas do projeto
  │   ├── {id}/export/                            ← exportação JSON/CSV (Fase 5, não implementado)
  │   │
  │   ├── {project_pk}/papers/                    ← listagem + filtros
  │   │   ├── search/?q=                          ← FTS
  │   │   ├── bulk_curate/                        ← curadoria em lote
  │   │   └── {id}/categorize/                    ← atribuir categorias
  │   │
  │   ├── {project_pk}/datasets/                  ← listagem + filtros
  │   │   ├── search/?q=
  │   │   └── bulk_curate/
  │   │
  │   ├── {project_pk}/categories/                ← UserCategory CRUD
  │   │
  │   ├── {project_pk}/links/                     ← ProjectPaperDataset
  │   │   ├── {id}/confirm/
  │   │   └── {id}/reject/
  │   │
  │   └── {project_pk}/jobs/                      ← IngestionJob (read + cancel)
  │       └── {id}/cancel/
  │
  └── clinical-categories/                        ← ClinicalCategory (read-only)
```

---

## Views — Projetos (`apps/core/views/project_views.py`)

### `DaVinciProjectViewSet`

| Action | Method | URL | Descrição |
|--------|--------|-----|-----------|
| `list` | GET | `/projects/` | Lista projetos do usuário |
| `create` | POST | `/projects/` | Cria novo projeto (gera slug auto) |
| `retrieve` | GET | `/projects/{id}/` | Detalhe do projeto |
| `update` | PATCH | `/projects/{id}/` | Atualiza campos do projeto |
| `destroy` | DELETE | `/projects/{id}/` | Remove projeto |
| `search` | POST | `/projects/{id}/search/` | Despacha job PubMed |
| `omics_search` | POST | `/projects/{id}/omics_search/` | Despacha job ômica |
| `stats` | GET | `/projects/{id}/stats/` | Computa + retorna estatísticas |
| `export` | GET | `/projects/{id}/export/?format=json\|csv` | _Fase 5 — não implementado_ |

**Permissões:** `IsAuthenticated`. Queryset filtrado por `user=request.user`.

**`search` action:**
```python
def search(self, request, pk=None):
    project = self.get_object()
    job = SearchService.dispatch_pubmed_search(project, user=request.user)
    return Response(IngestionJobSerializer(job).data, status=201)
```

**`omics_search` action:**
```python
def omics_search(self, request, pk=None):
    sources = request.data.get("sources", ["geo", "sra", "bioproject", "gwas"])
    max_per_source = request.data.get("max_per_source", 10000)
    job = SearchService.dispatch_omics_search(project, sources, max_per_source, user=request.user)
    return Response(IngestionJobSerializer(job).data, status=201)
```

---

## Views — Papers (`apps/core/views/paper_views.py`)

### `ProjectPaperViewSet`

| Action | Method | URL | Descrição |
|--------|--------|-----|-----------|
| `list` | GET | `/papers/` | Lista papers com filtros |
| `retrieve` | GET | `/papers/{id}/` | Detalhe completo |
| `partial_update` | PATCH | `/papers/{id}/` | Atualiza curadoria |
| `search` | GET | `/papers/search/?q=` | FTS nos papers |
| `categorize` | POST | `/papers/{id}/categorize/` | Atribui/remove categorias |
| `bulk_curate` | POST | `/papers/bulk_curate/` | Curadoria em lote |

**Filtros disponíveis em `list`:**

```python
# query params:
?curation_status=included         # pending|included|excluded|maybe
?pub_year_min=2020
?pub_year_max=2024
?journal=Nature
?pub_type=Review
?has_abstract=true
?free_full_text=true
?clinical_category=diagnosis      # slug
?ordering=-relevance_score        # ordenação
?page=2                           # paginação (50 por página padrão)
```

**`bulk_curate` payload:**
```json
{
    "paper_ids": [1, 2, 3, 4],
    "curation_status": "excluded",
    "exclusion_reason": "Não é sobre humanos"
}
```

**`categorize` payload:**
```json
{
    "clinical_categories": ["diagnosis", "treatment"],
    "user_categories": [42, 55],
    "remove_clinical": ["epidemiology"]
}
```

---

## Views — Datasets (`apps/core/views/dataset_views.py`)

### `ProjectDatasetViewSet`

| Action | Method | URL | Descrição |
|--------|--------|-----|-----------|
| `list` | GET | `/datasets/` | Lista datasets com filtros |
| `retrieve` | GET | `/datasets/{id}/` | Detalhe |
| `partial_update` | PATCH | `/datasets/{id}/` | Atualiza curadoria |
| `search` | GET | `/datasets/search/?q=` | FTS |
| `bulk_curate` | POST | `/datasets/bulk_curate/` | Curadoria em lote |

**Filtros:**
```python
?curation_status=pending
?omic_type=transcriptomic
?organism=Homo+sapiens
?source_db=geo
?has_summary=true
```

---

## Serializers (`apps/core/serializers/`)

### Paper Serializers (`paper.py`)

**`ProjectPaperListSerializer`** — Listagem compacta
```json
{
    "id": 1,
    "paper": {
        "pmid": "37124580",
        "title": "...",
        "abstract": "...",
        "journal": "Nature",
        "pub_year": 2023,
        "pub_type": "Research Article",
        "doi": "10.1038/..."
    },
    "curation_status": "pending",
    "relevance_score": 0.85,
    "clinical_categories": [{"id": 1, "slug": "diagnosis", "name": "Diagnóstico"}],
    "user_categories": [{"id": 42, "name": "Minha categoria", "color": "#FF5733"}]
}
```

**`ProjectPaperDetailSerializer`** — Detalhe completo com todas as entidades
```json
{
    "id": 1,
    "paper": {
        "pmid": "37124580",
        "title": "...",
        "abstract": "...",
        "authors": [
            {"position": 1, "last_name": "Smith", "initials": "JA", "country": "USA"}
        ],
        "keywords": [{"keyword": "cardiovascular"}],
        "mesh_terms": [
            {"descriptor": "Cardiovascular Diseases", "is_major_topic": true}
        ],
        "genes": [
            {"gene_symbol": "BRCA1", "mention_count": 3}
        ],
        "drugs": [
            {"drug_name": "atorvastatin", "mention_count": 2}
        ],
        "variants": [{"rs_number": "rs12345678"}],
        "contexts": [
            {"entity_type": "gene", "entity_name": "BRCA1", "sentence": "..."}
        ]
    },
    "curation_status": "included",
    "exclusion_reason": null,
    "notes": "Relevante para a revisão",
    "curated_at": "2024-03-15T10:30:00Z",
    "clinical_categories": [...],
    "user_categories": [...]
}
```

**`ProjectPaperCurateSerializer`** — Write-only para PATCH
```json
{
    "curation_status": "excluded",
    "exclusion_reason": "Fora do escopo",
    "notes": "Animal study",
    "relevance_score": 0.1
}
```

---

### Dataset Serializers (`dataset.py`)

**`ProjectDatasetListSerializer`**
```json
{
    "id": 1,
    "dataset": {
        "accession": "GSE12345",
        "source_db": "geo",
        "title": "...",
        "summary": "...",
        "omic_type": "transcriptomic",
        "omic_subcategory": "RNA-Seq",
        "organism": "Homo sapiens",
        "n_samples": 48,
        "platform": "GPL570"
    },
    "curation_status": "pending",
    "relevance_score": 0.72
}
```

---

### Project Serializers

**`DaVinciProjectSerializer`**
```json
{
    "id": "uuid",
    "title": "Cardiovascular Disease Review",
    "slug": "cardiovascular-disease-review",
    "description": "...",
    "query_term": "cardiovascular disease",
    "query_synonyms": ["CVD", "heart disease"],
    "date_from": 2010,
    "date_to": 2024,
    "target_organisms": ["Homo sapiens"],
    "target_tissues": ["heart", "aorta"],
    "status": "CURATING",
    "created_at": "2024-01-15T...",
    "updated_at": "2024-03-20T..."
}
```

**`ProjectStatsSerializer`** — Retorno do endpoint `/stats/`
```json
{
    "total_papers": 3421,
    "included_papers": 890,
    "excluded_papers": 1200,
    "pending_papers": 1331,
    "total_datasets": 567,
    "included_datasets": 120,
    "total_samples": 8934,
    "papers_by_year": {"2020": 234, "2021": 445, "2022": 567, "2023": 890},
    "papers_by_journal": {"Nature": 45, "Science": 38, ...},
    "papers_by_country": {"USA": 1200, "UK": 345, ...},
    "papers_by_clinical_category": {"diagnosis": 567, "treatment": 890, ...},
    "datasets_by_omic_type": {"transcriptomic": 234, "genomic": 189, ...},
    "datasets_by_organism": {"Homo sapiens": 456, "Mus musculus": 89, ...},
    "top_genes": [["BRCA1", 145], ["TP53", 123], ...],
    "top_drugs": [["atorvastatin", 89], ...],
    "top_mesh_terms": [["Cardiovascular Diseases", 567], ...],
    "last_computed": "2024-03-20T15:30:00Z"
}
```

---

## StatsService (`apps/core/services/stats_service.py`)

Chamado pelo endpoint `GET /projects/{id}/stats/`.

```python
class StatsService:
    @staticmethod
    def compute_and_save(project: DaVinciProject) -> ProjectStats:
        # 1. Contagens de curation_status
        paper_counts = ProjectPaper.objects.filter(project=project).values("curation_status").annotate(n=Count("id"))

        # 2. Papers por ano
        papers_by_year = ProjectPaper.objects.filter(
            project=project, curation_status="included"
        ).values("paper__pub_year").annotate(n=Count("id"))

        # 3. Top genes (soma mention_count)
        top_genes = PaperGene.objects.filter(
            paper__projectpaper__project=project,
            paper__projectpaper__curation_status="included"
        ).values("gene_symbol").annotate(total=Sum("mention_count")).order_by("-total")[:20]

        # ... + 9 outros cálculos ...

        stats, _ = ProjectStats.objects.update_or_create(
            project=project,
            defaults={...all computed fields...}
        )
        return stats
```

---

## Frontend (`davinci-frontend/src/`)

### Estrutura Next.js (App Router)

```
src/app/
├── layout.tsx                     — Root: Providers (Firebase, QueryClient)
├── page.tsx                       — Landing page
├── login/page.tsx                 — Firebase UI Auth
└── (dashboard)/
    ├── layout.tsx                 — Sidebar + Header
    ├── settings/page.tsx          — UserProfile (ORCID, NCBI key, instituição)
    └── projects/
        ├── page.tsx               — Lista de projetos
        └── [projectId]/
            ├── page.tsx           — Overview do projeto + ProjectStats
            ├── papers/page.tsx    — Tabela de papers + filtros + curadoria
            ├── datasets/page.tsx  — Tabela de datasets
            ├── links/page.tsx     — Links literatura ↔ ômica
            ├── analysis/page.tsx  — Gráficos e visualizações (Fase 5, não implementado)
            ├── export/page.tsx    — Exportação (Fase 5, não implementado)
            └── jobs/page.tsx      — Monitoramento de jobs
```

### API Client (`src/lib/api/client.ts`)

```typescript
// Axios com Firebase token injection automático
const apiClient = axios.create({ baseURL: '/api/v1/' });

apiClient.interceptors.request.use(async (config) => {
    const user = firebase.auth().currentUser;
    if (user) {
        const token = await user.getIdToken();
        config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
});

// 401 → redirect para /login
apiClient.interceptors.response.use(
    res => res,
    err => {
        if (err.response?.status === 401) router.push('/login');
        return Promise.reject(err);
    }
);
```

### Proxy Next.js → Django (`src/app/api/v1/[...path]/route.ts`)

```typescript
// Evita CORS: frontend chama /api/v1/, Next.js proxeia para Django
export async function GET/POST/PATCH/DELETE(request, { params }) {
    const backendUrl = `${DJANGO_API_URL}/api/v1/${params.path.join('/')}`;
    return fetch(backendUrl, { method, headers, body });
}
```

### Hooks (`src/lib/hooks/`)

```typescript
// use-projects.ts
export function useProjects() {
    return useQuery({ queryKey: ['projects'], queryFn: projectsApi.list });
}

export function useProject(id: string) {
    return useQuery({ queryKey: ['projects', id], queryFn: () => projectsApi.get(id) });
}

export function useProjectStats(id: string) {
    return useQuery({
        queryKey: ['projects', id, 'stats'],
        queryFn: () => projectsApi.getStats(id),
    });
}

// use-datasets.ts
export function useProjectDatasets(projectId: string, filters: DatasetFilters) {
    return useInfiniteQuery({
        queryKey: ['projects', projectId, 'datasets', filters],
        queryFn: ({ pageParam = 1 }) => datasetsApi.list(projectId, { ...filters, page: pageParam }),
    });
}
```

### Componentes Principais

**`papers-table.tsx`** — Tabela de papers com:
- Filtros em sidebar (`paper-filters.tsx`)
- Seleção múltipla para bulk actions
- Detail panel lateral (`paper-detail-panel.tsx`) com todos os campos
- Bulk curation bar (`bulk-curation-bar.tsx`)
- Paginação server-side

**`datasets-table.tsx`** — Tabela de datasets com:
- Filtros por omic_type, source_db, organismo
- Detail panel (`dataset-detail-panel.tsx`)
- Bulk curation bar (`dataset-bulk-curation-bar.tsx`)

**`create-project-dialog.tsx`** — Modal de criação de projeto:
- query_term, synonyms (tags input)
- date_from / date_to
- target_organisms, target_tissues

### TypeScript Types (`src/lib/types/`)

```typescript
// paper.ts
interface Paper {
    pmid: string;
    title: string;
    abstract?: string;
    journal: string;
    pub_year: number;
    pub_type?: string;
    genes: PaperGene[];
    drugs: PaperDrug[];
    mesh_terms: PaperMeSHTerm[];
}

interface ProjectPaper {
    id: number;
    paper: Paper;
    curation_status: 'pending' | 'included' | 'excluded' | 'maybe';
    relevance_score: number;
    clinical_categories: ClinicalCategoryBrief[];
    user_categories: UserCategoryBrief[];
}

// dataset.ts
interface OmicDataset {
    accession: string;
    source_db: 'geo' | 'sra' | 'arrayexpress' | 'tcga' | 'bioproject' | 'gwas_catalog';
    omic_type: string;
    omic_subcategory?: string;
    n_samples?: number;
}

// project.ts
interface DaVinciProject {
    id: string; // UUID
    title: string;
    slug: string;
    query_term: string;
    query_synonyms: string[];
    status: 'DRAFT' | 'SEARCHING' | 'CURATING' | 'ANALYZING' | 'COMPLETE';
}
```

---

## Configuração Django REST Framework

```python
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.accounts.authentication.FirebaseAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
}
```

---

## Celery

```python
# config/celery.py
app = Celery("davinci")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# settings
CELERY_BROKER_URL = "redis://localhost:6379/0"
CELERY_RESULT_BACKEND = "redis://localhost:6379/0"
CELERY_TASK_SERIALIZER = "json"
```

---

## Variáveis de Ambiente Necessárias

```bash
# PostgreSQL
DB_NAME=davinci
DB_USER=postgres
DB_PASSWORD=secret
DB_HOST=localhost
DB_PORT=5432

# Redis / Celery
REDIS_URL=redis://localhost:6379/0

# Firebase Admin SDK
FIREBASE_CREDENTIALS_FILE=/path/to/serviceAccountKey.json
# OU
FIREBASE_CREDENTIALS_JSON={"type": "service_account", ...}

# NCBI (opcional — fallback para chaves por usuário)
NCBI_API_KEY=abc123

# Django
SECRET_KEY=django-insecure-...
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1

# Frontend
NEXT_PUBLIC_FIREBASE_API_KEY=...
NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=...
NEXT_PUBLIC_FIREBASE_PROJECT_ID=...
DJANGO_API_URL=http://localhost:8000
```
