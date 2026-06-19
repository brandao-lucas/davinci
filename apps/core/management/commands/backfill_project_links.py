"""
backfill_project_links — Popula ProjectPaperDataset para projetos existentes.

Roda o mesmo INSERT … SELECT … ON CONFLICT DO NOTHING de link_service, mas
para TODOS os projetos de uma vez ou para um projeto específico via --project.

Seguro para re-execução: ON CONFLICT DO NOTHING garante idempotência.

Uso:
    .venv/bin/python manage.py backfill_project_links
    .venv/bin/python manage.py backfill_project_links --project <uuid>
"""

import uuid

from django.core.management.base import BaseCommand, CommandError

from apps.core.services.link_service import (
    materialize_all_projects_links,
    materialize_project_links,
)


class Command(BaseCommand):
    help = 'Backfill ProjectPaperDataset para projetos existentes (idempotente).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--project',
            dest='project_id',
            default=None,
            help='UUID do projeto específico (omitir para processar todos).',
        )

    def handle(self, *args, **options):
        project_id = options.get('project_id')

        if project_id:
            try:
                uuid.UUID(project_id)
            except ValueError:
                raise CommandError(f'UUID inválido: {project_id!r}')

            self.stdout.write(f'Materializando vínculos para projeto {project_id}...')
            inserted = materialize_project_links(project_id)
            self.stdout.write(
                self.style.SUCCESS(
                    f'Concluído: {inserted} vínculos inseridos para projeto {project_id}.'
                )
            )
        else:
            self.stdout.write('Materializando vínculos para TODOS os projetos...')
            inserted = materialize_all_projects_links()
            self.stdout.write(
                self.style.SUCCESS(
                    f'Concluído: {inserted} vínculos inseridos no total (todos os projetos).'
                )
            )
