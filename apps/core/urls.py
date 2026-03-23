from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views.project_views import DaVinciProjectViewSet

router = DefaultRouter()
router.register(r'projects', DaVinciProjectViewSet, basename='project')

urlpatterns = [
    path('', include(router.urls)),
]
