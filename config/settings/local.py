from .base import *

DEBUG = True

ALLOWED_HOSTS = ['*']

CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = True

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'davinci_db',
        'USER': 'davinci',
        'PASSWORD': 'davinci_dev',
        'HOST': 'localhost',
        'PORT': '5435',
    }
}

CELERY_BROKER_URL = 'redis://localhost:6380/0'
CELERY_RESULT_BACKEND = 'redis://localhost:6380/0'

CELERY_BEAT_SCHEDULE = {
    'refresh-all-project-stats': {
        'task': 'apps.core.tasks.stats_tasks.refresh_all_project_stats',
        'schedule': 3600,  # every hour
    },
}
