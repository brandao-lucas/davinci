from django.urls import path
from apps.accounts.views import MeView, VerifyTokenView

urlpatterns = [
    path('me/', MeView.as_view(), name='user-me'),
    path('verify/', VerifyTokenView.as_view(), name='verify-token'),
]
