# Make Celery app available when Django starts so CELERY_BROKER_URL is applied.
from .celery import app as celery_app

__all__ = ('celery_app',)
