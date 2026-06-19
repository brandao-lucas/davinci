from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views.project_views import DaVinciProjectViewSet
from .views.paper_views import ProjectPaperViewSet
from .views.dataset_views import ProjectDatasetViewSet
from .views.sample_views import ProjectSampleViewSet
from .views.category_views import ClinicalCategoryViewSet, UserCategoryViewSet
from .views.link_views import ProjectPaperDatasetViewSet
from .views.job_views import IngestionJobViewSet
from .views.gene_views import ProjectGeneViewSet
from .views.mesh_views import ProjectMeSHViewSet
from .views.drug_views import ProjectDrugViewSet
from .views.variant_views import ProjectVariantViewSet

router = DefaultRouter()
router.register(r'projects', DaVinciProjectViewSet, basename='project')
router.register(r'clinical-categories', ClinicalCategoryViewSet, basename='clinical-category')

# ── Nested routes under /projects/{project_pk}/ ───────────────────────────────

paper_list = ProjectPaperViewSet.as_view({'get': 'list'})
paper_detail = ProjectPaperViewSet.as_view({'get': 'retrieve', 'patch': 'partial_update'})
paper_categorize = ProjectPaperViewSet.as_view({'post': 'categorize'})
paper_bulk_curate = ProjectPaperViewSet.as_view({'post': 'bulk_curate'})
paper_search = ProjectPaperViewSet.as_view({'get': 'search'})

dataset_list = ProjectDatasetViewSet.as_view({'get': 'list'})
dataset_detail = ProjectDatasetViewSet.as_view({'get': 'retrieve', 'patch': 'partial_update'})
dataset_bulk_curate = ProjectDatasetViewSet.as_view({'post': 'bulk_curate'})
dataset_search = ProjectDatasetViewSet.as_view({'get': 'search'})

# Samples — rota plana (todos os samples do projeto) e rota por dataset
sample_list = ProjectSampleViewSet.as_view({'get': 'list'})
sample_detail = ProjectSampleViewSet.as_view({'get': 'retrieve', 'patch': 'partial_update'})
sample_bulk_curate = ProjectSampleViewSet.as_view({'post': 'bulk_curate'})
# Rota aninhada sob dataset (mesmo view, dataset_pk capturado no kwargs)
dataset_sample_list = ProjectSampleViewSet.as_view({'get': 'list'})

category_list = UserCategoryViewSet.as_view({'get': 'list', 'post': 'create'})
category_detail = UserCategoryViewSet.as_view({
    'patch': 'partial_update',
    'delete': 'destroy',
})

link_list = ProjectPaperDatasetViewSet.as_view({'get': 'list'})
link_confirm = ProjectPaperDatasetViewSet.as_view({'post': 'confirm'})
link_reject = ProjectPaperDatasetViewSet.as_view({'post': 'reject'})

job_list = IngestionJobViewSet.as_view({'get': 'list'})
job_detail = IngestionJobViewSet.as_view({'get': 'retrieve'})
job_cancel = IngestionJobViewSet.as_view({'post': 'cancel'})

gene_list = ProjectGeneViewSet.as_view({'get': 'list'})
gene_detail = ProjectGeneViewSet.as_view({'get': 'gene_detail'})

mesh_list = ProjectMeSHViewSet.as_view({'get': 'list'})
mesh_detail = ProjectMeSHViewSet.as_view({'get': 'mesh_detail'})

drug_list = ProjectDrugViewSet.as_view({'get': 'list'})
drug_detail = ProjectDrugViewSet.as_view({'get': 'drug_detail'})

variant_list = ProjectVariantViewSet.as_view({'get': 'list'})
variant_detail = ProjectVariantViewSet.as_view({'get': 'variant_detail'})

PROJECT_PREFIX = r'projects/<uuid:project_pk>/'

urlpatterns = [
    path('', include(router.urls)),

    # Papers
    path(f'{PROJECT_PREFIX}papers/', paper_list, name='project-paper-list'),
    path(f'{PROJECT_PREFIX}papers/search/', paper_search, name='project-paper-search'),
    path(f'{PROJECT_PREFIX}papers/bulk_curate/', paper_bulk_curate, name='project-paper-bulk-curate'),
    path(f'{PROJECT_PREFIX}papers/<int:pk>/', paper_detail, name='project-paper-detail'),
    path(f'{PROJECT_PREFIX}papers/<int:pk>/categorize/', paper_categorize, name='project-paper-categorize'),

    # Datasets
    path(f'{PROJECT_PREFIX}datasets/', dataset_list, name='project-dataset-list'),
    path(f'{PROJECT_PREFIX}datasets/search/', dataset_search, name='project-dataset-search'),
    path(f'{PROJECT_PREFIX}datasets/bulk_curate/', dataset_bulk_curate, name='project-dataset-bulk-curate'),
    path(f'{PROJECT_PREFIX}datasets/<int:pk>/', dataset_detail, name='project-dataset-detail'),

    # Samples por dataset — para a página de samples de um dataset específico (Op 4.4)
    path(
        f'{PROJECT_PREFIX}datasets/<int:dataset_pk>/samples/',
        dataset_sample_list,
        name='project-dataset-sample-list',
    ),

    # Samples do projeto (todos) — filtros ?curation_status=included, ?dataset=<id> (Op 5b)
    path(f'{PROJECT_PREFIX}samples/', sample_list, name='project-sample-list'),
    path(f'{PROJECT_PREFIX}samples/bulk_curate/', sample_bulk_curate, name='project-sample-bulk-curate'),
    path(f'{PROJECT_PREFIX}samples/<int:pk>/', sample_detail, name='project-sample-detail'),

    # Custom categories
    path(f'{PROJECT_PREFIX}categories/', category_list, name='project-category-list'),
    path(f'{PROJECT_PREFIX}categories/<int:pk>/', category_detail, name='project-category-detail'),

    # Literature ↔ Omics links
    path(f'{PROJECT_PREFIX}links/', link_list, name='project-link-list'),
    path(f'{PROJECT_PREFIX}links/<int:pk>/confirm/', link_confirm, name='project-link-confirm'),
    path(f'{PROJECT_PREFIX}links/<int:pk>/reject/', link_reject, name='project-link-reject'),

    # Ingestion jobs
    path(f'{PROJECT_PREFIX}jobs/', job_list, name='project-job-list'),
    path(f'{PROJECT_PREFIX}jobs/<uuid:pk>/', job_detail, name='project-job-detail'),
    path(f'{PROJECT_PREFIX}jobs/<uuid:pk>/cancel/', job_cancel, name='project-job-cancel'),

    # Genes — lista agregada e detalhe por símbolo
    path(f'{PROJECT_PREFIX}genes/', gene_list, name='project-gene-list'),
    path(f'{PROJECT_PREFIX}genes/<str:gene_symbol>/', gene_detail, name='project-gene-detail'),

    # MeSH — lista agregada e detalhe por descriptor
    path(f'{PROJECT_PREFIX}mesh/', mesh_list, name='project-mesh-list'),
    path(f'{PROJECT_PREFIX}mesh/<str:descriptor>/', mesh_detail, name='project-mesh-detail'),

    # Drugs — lista agregada e detalhe por drug_name_lower
    path(f'{PROJECT_PREFIX}drugs/', drug_list, name='project-drug-list'),
    path(f'{PROJECT_PREFIX}drugs/<str:drug_name_lower>/', drug_detail, name='project-drug-detail'),

    # Variants — lista agregada e detalhe por rs_number
    path(f'{PROJECT_PREFIX}variants/', variant_list, name='project-variant-list'),
    path(f'{PROJECT_PREFIX}variants/<str:rs_number>/', variant_detail, name='project-variant-detail'),
]
