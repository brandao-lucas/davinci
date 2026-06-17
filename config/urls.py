from django.contrib import admin
from django.conf import settings
from django.urls import path, include
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.permissions import AllowAny, IsAuthenticated


class _SchemaView(SpectacularAPIView):
    """
    Schema OpenAPI da API DaVinci.

    Permissão condicional ao DEBUG:
      - DEBUG=True  → AllowAny (facilita geração de tipos em dev sem auth)
      - DEBUG=False → IsAuthenticated (schema não exposto publicamente em prod)
    """

    def get_permissions(self):
        if settings.DEBUG:
            return [AllowAny()]
        return [IsAuthenticated()]


urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/v1/', include('apps.core.urls')),
    path('api/v1/auth/', include('apps.accounts.urls')),

    # ── OpenAPI schema ────────────────────────────────────────────────────────
    path('api/v1/schema/', _SchemaView.as_view(), name='schema'),
]

# Swagger UI apenas em desenvolvimento
if settings.DEBUG:
    urlpatterns += [
        path(
            'api/v1/docs/',
            SpectacularSwaggerView.as_view(url_name='schema'),
            name='swagger-ui',
        ),
    ]
