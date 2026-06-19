"""Celery tasks do core.

Importa os submódulos para que `autodiscover_tasks()` registre todas as tasks
ao carregar o pacote (caso contrário, tasks só referenciadas por string no
beat schedule — ex. refresh_all_project_stats — ficam unregistered no worker).
"""
from . import ingestion_tasks, stats_tasks, gene_tasks, mesh_tasks, drug_tasks, variant_tasks

__all__ = ['ingestion_tasks', 'stats_tasks', 'gene_tasks', 'mesh_tasks', 'drug_tasks', 'variant_tasks']
