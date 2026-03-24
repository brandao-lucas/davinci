# DaVinci — Frontend (Prompt de Implementação)

## Resumo Executivo

Este documento define a implementação do frontend do DaVinci usando **Next.js 14+** (web), **Tauri 2** (desktop para Windows e macOS), e **React Native** (mobile simplificado — pós-MVP). O frontend consome a API DRF do Django e usa Firebase Auth SDK para autenticação.

**Pré-requisitos:**
- Fase 4 do DaVinci concluída (models, migrations, API, Celery)
- Firebase Auth implementado conforme `DAVINCI_FIREBASE_AUTH.md`
- API DRF rodando com endpoints das Seções 6.1 a 6.5 do prompt principal

**Escopo deste documento:**
- Web app completo (Next.js)
- Desktop app completo (Tauri wrapping o Next.js)
- Mobile NÃO está no escopo deste prompt (será React Native pós-MVP)

---

## 1. Arquitetura Frontend

### 1.1 Stack

| Camada | Tecnologia | Justificativa |
|--------|-----------|---------------|
| **Framework** | Next.js 14+ (App Router) | SSR para SEO na landing, CSR para o app. Ecossistema React maduro. |
| **Linguagem** | TypeScript (strict) | Type safety end-to-end com os tipos da API. |
| **Estado** | Zustand + TanStack Query (React Query) | Zustand para estado local (auth, UI). TanStack Query para cache e sync com a API. |
| **Estilo** | Tailwind CSS + shadcn/ui | Componentes acessíveis, customizáveis, sem lock-in. |
| **Tabelas** | TanStack Table (+ virtualização) | Papers e datasets podem ter milhares de linhas. |
| **Gráficos** | Recharts + D3 (para custom) | Recharts para gráficos padrão, D3 para visualizações científicas complexas. |
| **Desktop** | Tauri 2 | Shell em Rust (alinha com o Rust engine), binários pequenos (~15MB), cross-platform. |
| **Auth** | Firebase JS SDK | Login no cliente, token enviado ao Django. |
| **HTTP** | Axios (com interceptors para token) | Interceptor automático de refresh de token Firebase. |
| **Forms** | React Hook Form + Zod | Validação type-safe de formulários. |

### 1.2 Princípio de Separação

O frontend é **read-heavy**. Ele nunca processa dados — apenas exibe o que a API do Django entrega. A complexidade está na apresentação: tabelas com milhares de linhas, filtros compostos, FTS, visualizações cruzadas entre papers e datasets.

O Next.js é o app web. O Tauri faz um wrap do Next.js como app desktop nativo, adicionando capacidades como notificações do sistema e acesso ao filesystem local (para exportação de arquivos grandes). O código React é 100% compartilhado entre web e desktop.

### 1.3 Fluxo de Dados

```
Firebase Auth SDK
    ↓ (ID Token JWT)
Axios Interceptor (injeta Bearer token em toda request)
    ↓
TanStack Query (cache, retry, refetch, optimistic updates)
    ↓
API Django DRF (/api/v1/...)
    ↓
Zustand Store (estado UI: sidebar, modais, filtros ativos)
    ↓
Componentes React (renderização)
```

---

## 2. Estrutura de Diretórios

```
davinci-frontend/
├── package.json
├── tsconfig.json
├── tailwind.config.ts
├── next.config.ts
├── .env.local                          # Firebase config + API URL
│
├── src-tauri/                          # Tauri 2 (desktop shell)
│   ├── Cargo.toml
│   ├── tauri.conf.json
│   ├── src/
│   │   └── main.rs                     # Entry point Tauri
│   ├── icons/                          # Ícones do app (gerados pelo Tauri CLI)
│   └── capabilities/
│       └── default.json
│
├── src/
│   ├── app/                            # Next.js App Router
│   │   ├── layout.tsx                  # Root layout (providers, auth check)
│   │   ├── page.tsx                    # Landing page (pública)
│   │   ├── login/
│   │   │   └── page.tsx                # Login (Firebase Auth UI)
│   │   ├── (dashboard)/                # Route group (autenticado)
│   │   │   ├── layout.tsx              # Dashboard layout (sidebar + topbar)
│   │   │   ├── projects/
│   │   │   │   ├── page.tsx            # Lista de projetos
│   │   │   │   └── [projectId]/
│   │   │   │       ├── page.tsx        # Overview do projeto (stats, status)
│   │   │   │       ├── papers/
│   │   │   │       │   └── page.tsx    # Tabela de papers (curadoria)
│   │   │   │       ├── datasets/
│   │   │   │       │   └── page.tsx    # Tabela de datasets ômicos
│   │   │   │       ├── links/
│   │   │   │       │   └── page.tsx    # Paper-Dataset links (análise integrada)
│   │   │   │       ├── analysis/
│   │   │   │       │   └── page.tsx    # Visualizações e outputs
│   │   │   │       ├── jobs/
│   │   │   │       │   └── page.tsx    # Status de jobs de ingestão
│   │   │   │       └── export/
│   │   │   │           └── page.tsx    # Exportação (JSON, CSV)
│   │   │   └── settings/
│   │   │       └── page.tsx            # Perfil do usuário
│   │   └── globals.css
│   │
│   ├── components/
│   │   ├── ui/                         # shadcn/ui components (gerados)
│   │   │   ├── button.tsx
│   │   │   ├── dialog.tsx
│   │   │   ├── input.tsx
│   │   │   ├── select.tsx
│   │   │   ├── badge.tsx
│   │   │   ├── table.tsx
│   │   │   ├── tabs.tsx
│   │   │   ├── toast.tsx
│   │   │   ├── card.tsx
│   │   │   ├── dropdown-menu.tsx
│   │   │   ├── command.tsx             # Para busca rápida (Cmd+K)
│   │   │   └── ...
│   │   ├── layout/
│   │   │   ├── sidebar.tsx             # Sidebar com navegação
│   │   │   ├── topbar.tsx              # Topbar com busca e user menu
│   │   │   ├── breadcrumbs.tsx
│   │   │   └── page-header.tsx
│   │   ├── projects/
│   │   │   ├── project-card.tsx        # Card de projeto na lista
│   │   │   ├── create-project-dialog.tsx
│   │   │   └── project-stats-overview.tsx
│   │   ├── papers/
│   │   │   ├── papers-table.tsx        # Tabela principal com TanStack Table
│   │   │   ├── paper-detail-panel.tsx  # Painel lateral com detalhes
│   │   │   ├── paper-filters.tsx       # Filtros (ano, journal, status, MeSH)
│   │   │   ├── bulk-curation-bar.tsx   # Barra de ações em massa
│   │   │   └── paper-search.tsx        # FTS input
│   │   ├── datasets/
│   │   │   ├── datasets-table.tsx
│   │   │   ├── dataset-detail-panel.tsx
│   │   │   └── dataset-filters.tsx
│   │   ├── links/
│   │   │   ├── links-table.tsx         # Paper-Dataset connections
│   │   │   └── link-confirm-dialog.tsx
│   │   ├── analysis/
│   │   │   ├── coverage-matrix.tsx     # Genes/pathways × ômicas
│   │   │   ├── mesh-cooccurrence.tsx   # Grafo MeSH
│   │   │   ├── year-distribution.tsx   # Publicações por ano
│   │   │   └── omic-breakdown.tsx      # Distribuição por tipo ômico
│   │   ├── jobs/
│   │   │   ├── job-status-card.tsx
│   │   │   └── job-progress-bar.tsx
│   │   └── auth/
│   │       ├── auth-provider.tsx       # Firebase Auth context
│   │       ├── login-form.tsx
│   │       └── user-menu.tsx
│   │
│   ├── lib/
│   │   ├── api/
│   │   │   ├── client.ts               # Axios instance com interceptors
│   │   │   ├── projects.ts             # API calls: projetos
│   │   │   ├── papers.ts               # API calls: papers
│   │   │   ├── datasets.ts             # API calls: datasets
│   │   │   ├── links.ts                # API calls: paper-dataset links
│   │   │   ├── jobs.ts                 # API calls: ingestion jobs
│   │   │   └── auth.ts                 # API calls: /api/v1/auth/
│   │   ├── firebase.ts                 # Firebase app init + auth instance
│   │   ├── hooks/
│   │   │   ├── use-projects.ts         # TanStack Query hooks para projetos
│   │   │   ├── use-papers.ts           # Hooks para papers (com filtros)
│   │   │   ├── use-datasets.ts
│   │   │   ├── use-jobs.ts             # Polling de status de jobs
│   │   │   ├── use-auth.ts             # Hook de auth (user, login, logout)
│   │   │   └── use-debounce.ts
│   │   ├── stores/
│   │   │   ├── ui-store.ts             # Zustand: sidebar, theme, modais
│   │   │   └── filter-store.ts         # Zustand: filtros ativos por página
│   │   ├── types/
│   │   │   ├── api.ts                  # Tipos de resposta da API
│   │   │   ├── project.ts
│   │   │   ├── paper.ts
│   │   │   ├── dataset.ts
│   │   │   └── job.ts
│   │   └── utils/
│   │       ├── format.ts               # Formatação de datas, números
│   │       └── export.ts               # Funções de exportação CSV/JSON
│   │
│   └── styles/
│       └── theme.ts                    # Tokens de design customizados
│
├── public/
│   ├── logo.svg
│   └── favicon.ico
│
└── tests/
    ├── components/
    └── lib/
```

---

## 3. Configuração Inicial

### 3.1 Criar Projeto Next.js

```bash
npx create-next-app@latest davinci-frontend \
    --typescript \
    --tailwind \
    --eslint \
    --app \
    --src-dir \
    --import-alias "@/*"

cd davinci-frontend
```

### 3.2 Dependências

```bash
# UI
npx shadcn@latest init
npx shadcn@latest add button card dialog input select badge table tabs \
    toast dropdown-menu command separator sheet tooltip

# Estado e data fetching
npm install zustand @tanstack/react-query @tanstack/react-table

# HTTP e Auth
npm install axios firebase

# Formulários
npm install react-hook-form @hookform/resolvers zod

# Visualização
npm install recharts d3 @types/d3

# Utilidades
npm install date-fns lucide-react clsx tailwind-merge

# Tauri (desktop)
npm install @tauri-apps/api @tauri-apps/cli
```

### 3.3 Variáveis de Ambiente

```bash
# .env.local

# API Backend (Django)
NEXT_PUBLIC_API_URL=http://localhost:8000/api/v1

# Firebase (obter no Firebase Console → Project Settings → Web App)
NEXT_PUBLIC_FIREBASE_API_KEY=AIza...
NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=platomics-davinci.firebaseapp.com
NEXT_PUBLIC_FIREBASE_PROJECT_ID=platomics-davinci
NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET=platomics-davinci.appspot.com
NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID=123456789
NEXT_PUBLIC_FIREBASE_APP_ID=1:123456789:web:abc123
```

### 3.4 Setup Tauri 2

```bash
# Instalar Tauri CLI
npm install -D @tauri-apps/cli@next

# Inicializar Tauri no projeto
npx tauri init

# Em tauri.conf.json, configurar:
# - "devUrl": "http://localhost:3000"
# - "beforeDevCommand": "npm run dev"
# - "beforeBuildCommand": "npm run build"
```

```json
// src-tauri/tauri.conf.json (campos essenciais)
{
  "productName": "DaVinci",
  "version": "0.1.0",
  "identifier": "com.platomics.davinci",
  "build": {
    "devUrl": "http://localhost:3000",
    "frontendDist": "../out",
    "beforeDevCommand": "npm run dev",
    "beforeBuildCommand": "npm run build && npm run export"
  },
  "app": {
    "title": "DaVinci — PlatOmics",
    "windows": [
      {
        "width": 1400,
        "height": 900,
        "resizable": true,
        "fullscreen": false
      }
    ]
  },
  "bundle": {
    "active": true,
    "targets": "all",
    "icon": [
      "icons/32x32.png",
      "icons/128x128.png",
      "icons/128x128@2x.png",
      "icons/icon.icns",
      "icons/icon.ico"
    ]
  }
}
```

### 3.5 Builds

```bash
# Desenvolvimento web
npm run dev

# Desenvolvimento desktop (abre Tauri + Next.js)
npx tauri dev

# Build web (produção)
npm run build

# Build desktop (gera instaladores para OS atual)
npx tauri build

# Build desktop cross-platform (via CI/CD)
# Windows: .msi e .exe (GitHub Actions com windows-latest)
# macOS: .dmg e .app (GitHub Actions com macos-latest)
```

---

## 4. Implementação Core

### 4.1 Firebase Init

```typescript
// src/lib/firebase.ts

import { initializeApp, getApps } from 'firebase/app';
import { getAuth, GoogleAuthProvider } from 'firebase/auth';

const firebaseConfig = {
  apiKey: process.env.NEXT_PUBLIC_FIREBASE_API_KEY,
  authDomain: process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN,
  projectId: process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID,
  storageBucket: process.env.NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET,
  messagingSenderId: process.env.NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID,
  appId: process.env.NEXT_PUBLIC_FIREBASE_APP_ID,
};

// Evitar re-inicialização em hot reload
const app = getApps().length === 0 ? initializeApp(firebaseConfig) : getApps()[0];

export const auth = getAuth(app);
export const googleProvider = new GoogleAuthProvider();
```

### 4.2 Axios Client com Interceptors

```typescript
// src/lib/api/client.ts

import axios from 'axios';
import { auth } from '@/lib/firebase';

const apiClient = axios.create({
  baseURL: process.env.NEXT_PUBLIC_API_URL,
  headers: {
    'Content-Type': 'application/json',
  },
});

// Interceptor: injeta Firebase token em toda request
apiClient.interceptors.request.use(async (config) => {
  const user = auth.currentUser;
  if (user) {
    // getIdToken() retorna token cacheado ou faz refresh se expirado
    const token = await user.getIdToken();
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// Interceptor: tratamento global de erros
apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      // Token expirado ou inválido — redirecionar para login
      window.location.href = '/login';
    }
    return Promise.reject(error);
  }
);

export default apiClient;
```

### 4.3 Auth Provider e Hook

```typescript
// src/components/auth/auth-provider.tsx

'use client';

import { createContext, useContext, useEffect, useState, ReactNode } from 'react';
import { User, onAuthStateChanged, signInWithPopup, signOut as firebaseSignOut } from 'firebase/auth';
import { auth, googleProvider } from '@/lib/firebase';
import apiClient from '@/lib/api/client';

interface AuthContextType {
  user: User | null;
  profile: UserProfile | null;
  loading: boolean;
  signInWithGoogle: () => Promise<void>;
  signOut: () => Promise<void>;
}

interface UserProfile {
  id: string;
  email: string;
  first_name: string;
  last_name: string;
  firebase_uid: string;
  auth_provider: string;
  orcid_id: string | null;
  institution: string;
  research_area: string;
  avatar_url: string;
}

const AuthContext = createContext<AuthContextType | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const unsubscribe = onAuthStateChanged(auth, async (firebaseUser) => {
      setUser(firebaseUser);
      if (firebaseUser) {
        try {
          const { data } = await apiClient.get('/auth/me/');
          setProfile(data);
        } catch {
          setProfile(null);
        }
      } else {
        setProfile(null);
      }
      setLoading(false);
    });
    return unsubscribe;
  }, []);

  const signInWithGoogle = async () => {
    await signInWithPopup(auth, googleProvider);
  };

  const signOut = async () => {
    await firebaseSignOut(auth);
    setProfile(null);
  };

  return (
    <AuthContext.Provider value={{ user, profile, loading, signInWithGoogle, signOut }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth must be used within AuthProvider');
  return context;
}
```

### 4.4 Tipos da API

```typescript
// src/lib/types/project.ts

export interface DaVinciProject {
  id: string;
  slug: string;
  title: string;
  description: string;
  query_term: string;
  query_synonyms: string[];
  date_from: number | null;
  date_to: number | null;
  target_organisms: string[];
  target_tissues: string[];
  status: 'draft' | 'searching' | 'curating' | 'analyzing' | 'complete';
  created_at: string;
  updated_at: string;
  stats?: ProjectStats;
}

export interface ProjectStats {
  total_papers: number;
  included_papers: number;
  excluded_papers: number;
  pending_papers: number;
  total_datasets: number;
  included_datasets: number;
  total_samples: number;
  papers_by_year: Record<string, number>;
  papers_by_journal: Record<string, number>;
  datasets_by_omic_type: Record<string, number>;
  datasets_by_organism: Record<string, number>;
  top_genes: Array<{ gene: string; count: number }>;
  top_mesh_terms: Array<{ term: string; count: number }>;
  top_variants: Array<{ rs: string; count: number }>;
}

export interface CreateProjectInput {
  title: string;
  description?: string;
  query_term: string;
  query_synonyms?: string[];
  date_from?: number;
  date_to?: number;
  target_organisms?: string[];
  target_tissues?: string[];
}
```

```typescript
// src/lib/types/paper.ts

export interface Paper {
  id: number;
  pmid: string;
  pmc_id: string | null;
  doi: string | null;
  title: string;
  abstract: string;
  journal: string;
  pub_year: number;
  pub_month: number | null;
  authors: PaperAuthor[];
  keywords: string[];
  mesh_terms: MeSHTerm[];
  genes: PaperGene[];
  variants: string[];
  // Campos de curadoria (via ProjectPaper)
  curation_status: 'pending' | 'included' | 'excluded' | 'maybe';
  exclusion_reason: string | null;
  notes: string;
  relevance_score: number | null;
}

export interface PaperAuthor {
  position: number;
  last_name: string;
  initials: string;
  affiliation: string;
  country: string | null;
}

export interface MeSHTerm {
  descriptor: string;
  qualifier: string | null;
  is_major_topic: boolean;
}

export interface PaperGene {
  gene_symbol: string;
  entrez_id: number | null;
  mention_count: number;
}

export interface PaperFilters {
  curation_status?: string;
  pub_year_min?: number;
  pub_year_max?: number;
  journal?: string;
  search?: string;  // FTS
  ordering?: string;
  page?: number;
}
```

```typescript
// src/lib/types/dataset.ts

export interface OmicDataset {
  id: number;
  accession: string;
  source_db: 'GEO' | 'SRA' | 'BioProject' | 'ArrayExpress' | 'TCGA';
  bioproject_id: string | null;
  title: string;
  summary: string;
  omic_type: string;
  omic_subcategory: string | null;
  organism: string;
  n_samples: number | null;
  platform: string | null;
  // Campos de curadoria (via ProjectDataset)
  curation_status: 'pending' | 'included' | 'excluded' | 'queued' | 'downloaded';
  exclusion_reason: string | null;
  notes: string;
  relevance_score: number | null;
}
```

```typescript
// src/lib/types/job.ts

export interface IngestionJob {
  id: string;
  project: string;
  job_type: 'pubmed_search' | 'pubmed_fetch' | 'geo_search' | 'sra_search' | 'variant_annotation' | 'gene_ner';
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  parameters: Record<string, unknown>;
  records_processed: number;
  records_inserted: number;
  records_updated: number;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
}
```

### 4.5 API Functions

```typescript
// src/lib/api/projects.ts

import apiClient from './client';
import { DaVinciProject, CreateProjectInput } from '@/lib/types/project';
import { PaginatedResponse } from '@/lib/types/api';

export const projectsApi = {
  list: () =>
    apiClient.get<PaginatedResponse<DaVinciProject>>('/projects/'),

  get: (id: string) =>
    apiClient.get<DaVinciProject>(`/projects/${id}/`),

  create: (data: CreateProjectInput) =>
    apiClient.post<DaVinciProject>('/projects/', data),

  update: (id: string, data: Partial<CreateProjectInput>) =>
    apiClient.patch<DaVinciProject>(`/projects/${id}/`, data),

  delete: (id: string) =>
    apiClient.delete(`/projects/${id}/`),

  search: (id: string) =>
    apiClient.post<{ job_id: string; status: string }>(`/projects/${id}/search/`),

  getStats: (id: string) =>
    apiClient.get(`/projects/${id}/stats/`),

  exportData: (id: string, format: 'json' | 'csv') =>
    apiClient.get(`/projects/${id}/export/`, { params: { format } }),
};
```

```typescript
// src/lib/api/papers.ts

import apiClient from './client';
import { Paper, PaperFilters } from '@/lib/types/paper';
import { PaginatedResponse } from '@/lib/types/api';

export const papersApi = {
  list: (projectId: string, filters?: PaperFilters) =>
    apiClient.get<PaginatedResponse<Paper>>(`/projects/${projectId}/papers/`, {
      params: filters,
    }),

  get: (projectId: string, paperId: number) =>
    apiClient.get<Paper>(`/projects/${projectId}/papers/${paperId}/`),

  curate: (projectId: string, paperId: number, data: {
    curation_status: string;
    exclusion_reason?: string;
    notes?: string;
  }) =>
    apiClient.patch(`/projects/${projectId}/papers/${paperId}/`, data),

  bulkCurate: (projectId: string, data: {
    paper_ids: number[];
    curation_status: string;
    exclusion_reason?: string;
  }) =>
    apiClient.post(`/projects/${projectId}/papers/bulk-curate/`, data),

  search: (projectId: string, query: string) =>
    apiClient.get<PaginatedResponse<Paper>>(`/projects/${projectId}/papers/search/`, {
      params: { q: query },
    }),
};
```

### 4.6 TanStack Query Hooks

```typescript
// src/lib/hooks/use-projects.ts

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { projectsApi } from '@/lib/api/projects';
import { CreateProjectInput } from '@/lib/types/project';

export function useProjects() {
  return useQuery({
    queryKey: ['projects'],
    queryFn: () => projectsApi.list().then(r => r.data),
  });
}

export function useProject(id: string) {
  return useQuery({
    queryKey: ['projects', id],
    queryFn: () => projectsApi.get(id).then(r => r.data),
    enabled: !!id,
  });
}

export function useCreateProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: CreateProjectInput) => projectsApi.create(data).then(r => r.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
    },
  });
}

export function useDispatchSearch(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => projectsApi.search(projectId).then(r => r.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['jobs', projectId] });
    },
  });
}
```

```typescript
// src/lib/hooks/use-papers.ts

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { papersApi } from '@/lib/api/papers';
import { PaperFilters } from '@/lib/types/paper';

export function usePapers(projectId: string, filters?: PaperFilters) {
  return useQuery({
    queryKey: ['papers', projectId, filters],
    queryFn: () => papersApi.list(projectId, filters).then(r => r.data),
    enabled: !!projectId,
  });
}

export function usePaper(projectId: string, paperId: number) {
  return useQuery({
    queryKey: ['papers', projectId, paperId],
    queryFn: () => papersApi.get(projectId, paperId).then(r => r.data),
    enabled: !!projectId && !!paperId,
  });
}

export function useCuratePaper(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ paperId, data }: { paperId: number; data: any }) =>
      papersApi.curate(projectId, paperId, data).then(r => r.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['papers', projectId] });
      queryClient.invalidateQueries({ queryKey: ['projects', projectId] });
    },
  });
}

export function useBulkCurate(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: { paper_ids: number[]; curation_status: string; exclusion_reason?: string }) =>
      papersApi.bulkCurate(projectId, data).then(r => r.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['papers', projectId] });
      queryClient.invalidateQueries({ queryKey: ['projects', projectId] });
    },
  });
}
```

```typescript
// src/lib/hooks/use-jobs.ts

import { useQuery } from '@tanstack/react-query';
import apiClient from '@/lib/api/client';
import { IngestionJob } from '@/lib/types/job';
import { PaginatedResponse } from '@/lib/types/api';

export function useJobs(projectId: string) {
  return useQuery({
    queryKey: ['jobs', projectId],
    queryFn: () =>
      apiClient.get<PaginatedResponse<IngestionJob>>(`/projects/${projectId}/jobs/`).then(r => r.data),
    enabled: !!projectId,
  });
}

export function useJobPolling(projectId: string, jobId: string) {
  return useQuery({
    queryKey: ['jobs', projectId, jobId],
    queryFn: () =>
      apiClient.get<IngestionJob>(`/projects/${projectId}/jobs/${jobId}/`).then(r => r.data),
    enabled: !!jobId,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      // Polling a cada 2s enquanto o job está rodando
      if (status === 'pending' || status === 'running') return 2000;
      return false;  // Para de fazer polling quando completa
    },
  });
}
```

### 4.7 Tipos Compartilhados da API

```typescript
// src/lib/types/api.ts

export interface PaginatedResponse<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}
```

---

## 5. Páginas Principais

### 5.1 Hierarquia de Navegação

```
/ (Landing — pública)
/login
/projects                         → Lista de projetos
/projects/[id]                    → Overview do projeto
/projects/[id]/papers             → Tabela de papers (curadoria)
/projects/[id]/datasets           → Tabela de datasets
/projects/[id]/links              → Paper ↔ Dataset (análise integrada)
/projects/[id]/analysis           → Visualizações
/projects/[id]/jobs               → Status de ingestão
/projects/[id]/export             → Exportação
/settings                         → Perfil do usuário
```

### 5.2 Descrição de Cada Página

**Projects List** (`/projects`):
- Cards com: título, query_term, status (badge colorido), contagem de papers/datasets, data de criação
- Botão "Novo Projeto" que abre dialog com formulário (query_term, dates, organisms)
- Após criar, redireciona para `/projects/[id]`

**Project Overview** (`/projects/[id]`):
- Stats cards: total papers, included, excluded, pending, total datasets, total samples
- Gráfico de barras: papers por ano
- Pie chart: datasets por tipo ômico
- Top 10 genes, top 10 MeSH terms
- Botão "Iniciar Busca" (dispara job de ingestão)
- Status do último job (com progress bar se running)

**Papers Table** (`/projects/[id]/papers`):
- TanStack Table com colunas: checkbox, PMID, Título (truncado), Journal, Ano, Status (badge), Score
- Filtros laterais: status de curadoria, range de anos, journal (autocomplete), busca FTS
- Click na linha abre painel lateral com: abstract completo, autores, keywords, MeSH, genes, variantes, datasets vinculados
- Seleção múltipla + barra de ações em massa: incluir, excluir (com motivo), marcar como maybe
- Virtualização para performance com milhares de linhas

**Datasets Table** (`/projects/[id]/datasets`):
- Similar à tabela de papers: accession, título, source_db, omic_type, organism, n_samples, status
- Filtros: tipo ômico, organismo, source_db
- Painel lateral com detalhes + papers vinculados

**Links** (`/projects/[id]/links`):
- Tabela de relações Paper ↔ Dataset
- Colunas: Paper (PMID + título), Dataset (accession + título), confidence (auto/confirmed/rejected)
- Ações: confirmar, rejeitar

**Analysis** (`/projects/[id]/analysis`):
- Coverage matrix: tabela/heatmap mostrando quais genes/pathways têm tanto papers quanto dados ômicos
- MeSH co-occurrence: grafo D3 de termos MeSH mais frequentes
- Timeline: distribuição temporal das publicações
- Omic breakdown: distribuição por tipo ômico dos datasets incluídos

**Jobs** (`/projects/[id]/jobs`):
- Lista de jobs com: tipo, status, records processed/inserted, timestamps
- Auto-refresh (polling) para jobs running

**Export** (`/projects/[id]/export`):
- Opções: JSON completo, CSV (papers only, datasets only, links)
- Preview dos dados antes de exportar
- Download direto ou via Tauri (salvar em local específico no desktop)

---

## 6. Regras de Implementação

### 6.1 Regras Gerais

1. **TypeScript strict mode.** Sem `any` exceto em types de terceiros sem tipagem.
2. **Componentes Client vs Server.** Páginas são Server Components por padrão. Adicionar `'use client'` apenas onde necessário (interatividade, hooks).
3. **Sem lógica de negócio nos componentes.** Toda lógica fica nos hooks (TanStack Query) e API functions. Componentes apenas renderizam.
4. **Sem estado global para dados da API.** TanStack Query gerencia cache. Zustand é apenas para estado UI.
5. **Responsivo.** Tailwind breakpoints: `sm`, `md`, `lg`, `xl`. Mobile-first.

### 6.2 Performance

1. **Virtualização obrigatória** em tabelas com > 100 linhas (TanStack Virtual).
2. **Debounce** em inputs de busca (300ms).
3. **Pagination** server-side (Django já retorna paginado).
4. **Prefetch** de dados da próxima página em tabelas.
5. **Optimistic updates** para curadoria (incluir/excluir paper atualiza UI imediatamente).

### 6.3 Padrão de Curadoria

A curadoria é a interação central do pesquisador. O padrão é:

```
Selecionar papers → Ação (incluir/excluir/maybe) → Motivo obrigatório para exclusão → Confirmar
```

O componente `bulk-curation-bar.tsx` aparece fixo no rodapé quando há papers selecionados, mostrando contagem e ações disponíveis. A exclusão abre um dialog pedindo motivo (obrigatório, auditável).

### 6.4 Tauri-Specific

O app desktop compartilha 100% do código React. As únicas diferenças são:

1. **Detecção de ambiente:** `typeof window !== 'undefined' && '__TAURI__' in window`
2. **Exportação de arquivos:** No web, usa download do browser. No Tauri, usa `@tauri-apps/api/dialog` para abrir file picker nativo.
3. **Notificações:** No Tauri, usa `@tauri-apps/api/notification` para notificar quando um job de ingestão completa.
4. **Deep links:** O Tauri pode registrar protocolo `davinci://` para abrir projetos diretamente.

```typescript
// src/lib/utils/export.ts

export async function saveFile(data: string, filename: string) {
  if (typeof window !== 'undefined' && '__TAURI__' in window) {
    // Desktop: file picker nativo
    const { save } = await import('@tauri-apps/plugin-dialog');
    const { writeTextFile } = await import('@tauri-apps/plugin-fs');
    const path = await save({ defaultPath: filename });
    if (path) await writeTextFile(path, data);
  } else {
    // Web: download via blob
    const blob = new Blob([data], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }
}
```

---

## 7. Design System

### 7.1 Paleta de Cores (Tailwind Config)

```typescript
// tailwind.config.ts — extend colors

const colors = {
  davinci: {
    50: '#f0f7ff',
    100: '#e0effe',
    200: '#bae0fd',
    300: '#7cc8fb',
    400: '#36adf6',
    500: '#0c93e7',    // Primary
    600: '#0074c5',
    700: '#015da0',
    800: '#064f84',
    900: '#0b426e',
    950: '#072a49',
  },
  // Status colors para curadoria
  curation: {
    included: '#16a34a',   // green-600
    excluded: '#dc2626',   // red-600
    pending: '#d97706',    // amber-600
    maybe: '#7c3aed',      // violet-600
  },
  // Omic types
  omic: {
    genomic: '#2563eb',
    transcriptomic: '#16a34a',
    proteomic: '#d97706',
    metabolomic: '#dc2626',
    epigenomic: '#7c3aed',
    metagenomic: '#0891b2',
    multi_omic: '#475569',
  },
};
```

### 7.2 Layout Padrão

```
┌──────────────────────────────────────────────────┐
│ Topbar: Logo | Busca rápida (Cmd+K) | User menu │
├──────┬───────────────────────────────────────────┤
│      │                                           │
│ Side │  Main Content Area                        │
│ bar  │                                           │
│      │  ┌─────────────────┐ ┌──────────────────┐ │
│ Nav  │  │   Stats Cards   │ │   Stats Cards    │ │
│      │  └─────────────────┘ └──────────────────┘ │
│      │                                           │
│      │  ┌────────────────────────────────────┐   │
│      │  │                                    │   │
│      │  │        Table / Content             │   │
│      │  │                                    │   │
│      │  └────────────────────────────────────┘   │
│      │                                           │
├──────┴───────────────────────────────────────────┤
│ Bulk Curation Bar (quando há seleção)            │
└──────────────────────────────────────────────────┘
```

---

## 8. Roadmap Frontend

### Fase F1 — Scaffolding e Auth (Semana 1)

1. Criar projeto Next.js com estrutura de diretórios
2. Configurar Tailwind + shadcn/ui
3. Implementar Firebase Auth (login page, auth provider, protected routes)
4. Implementar Axios client com interceptors
5. Configurar TanStack Query provider
6. Layout base: sidebar + topbar

### Fase F2 — Projetos e Ingestão (Semana 2)

1. Lista de projetos (cards)
2. Criar projeto (dialog com formulário)
3. Overview do projeto (stats cards)
4. Botão "Iniciar Busca" → job de ingestão
5. Página de jobs com polling

### Fase F3 — Curadoria de Papers (Semana 3)

1. Tabela de papers com TanStack Table
2. Filtros (status, ano, journal)
3. FTS input
4. Painel lateral de detalhes
5. Curadoria individual e em massa
6. Virtualização

### Fase F4 — Datasets e Links (Semana 4)

1. Tabela de datasets
2. Detalhes de dataset
3. Paper ↔ Dataset links
4. Confirmar/rejeitar links

### Fase F5 — Análise e Exportação (Semana 5)

1. Coverage matrix
2. MeSH co-occurrence graph (D3)
3. Timeline e omic breakdown (Recharts)
4. Exportação JSON/CSV

### Fase F6 — Desktop (Semana 6)

1. Configurar Tauri 2
2. Testar app desktop (Windows + macOS)
3. File picker nativo para exportação
4. Notificações de job completo
5. Gerar instaladores (.dmg, .msi)

---

## 9. Checklist Pré-Desenvolvimento

- [ ] Node.js 18+ instalado
- [ ] Next.js project criado com App Router + TypeScript
- [ ] shadcn/ui inicializado
- [ ] Firebase project configurado (web app registrado)
- [ ] `.env.local` com todas as variáveis Firebase + API URL
- [ ] Django API rodando em `localhost:8000`
- [ ] CORS configurado no Django (`django-cors-headers` com `localhost:3000` permitido)
- [ ] Rust toolchain instalado (para Tauri)
- [ ] Tauri CLI instalado (`npm install -D @tauri-apps/cli@next`)

---

## 10. Notas sobre CORS no Django

Para o frontend funcionar em desenvolvimento, o Django precisa aceitar requests do `localhost:3000`:

```python
# config/settings/local.py

CORS_ALLOWED_ORIGINS = [
    'http://localhost:3000',     # Next.js dev server
    'http://127.0.0.1:3000',
    'tauri://localhost',         # Tauri desktop app
]

CORS_ALLOW_HEADERS = [
    'authorization',
    'content-type',
    'x-requested-with',
]
```

---

*Este documento é o contrato de implementação do frontend DaVinci. O frontend consome a API definida no prompt principal (Seções 6.1 a 6.5) e usa Firebase Auth conforme definido em `DAVINCI_FIREBASE_AUTH.md`. Qualquer decisão de implementação que conflite com as regras aqui definidas deve ser discutida antes de ser aplicada.*
