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
    # Cleanup de arquivos órfãos/failed no object storage.
    # Roda a cada 6 horas: remove bytes cujo DatasetFile está 'failed' ou sem
    # vínculo válido; jamais toca registros 'downloaded' (curation-audit-trail).
    'cleanup-orphan-files': {
        'task': 'apps.core.tasks.cleanup_tasks.cleanup_orphan_files',
        'schedule': 6 * 3600,  # 6 horas
    },
}

# ── Object Storage — dev (MinIO via docker-compose) ───────────────────────────
# Defaults de desenvolvimento.  Para uso local, as variáveis abaixo já estão
# preenchidas via os.environ.get(..., '<default>') em base.py.
# Se quiser sobrescrever sem alterar código, exporte as variáveis no shell:
#
#   export AWS_S3_ENDPOINT_URL=http://localhost:9000
#   export AWS_ACCESS_KEY_ID=davinci_dev
#   export AWS_SECRET_ACCESS_KEY=davinci_dev_secret
#   export AWS_STORAGE_BUCKET_NAME=davinci-omics
#   export AWS_S3_REGION_NAME=us-east-1
#
# Console MinIO: http://localhost:9001  (user: davinci_dev / senha: davinci_dev_secret)
#
# NUNCA defina credenciais de produção aqui.  Use variáveis de ambiente ou
# secrets manager no ambiente de deploy.
