import csv
import io

from django.http import JsonResponse, StreamingHttpResponse
from django.utils.text import slugify
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.models import DaVinciProject, ProjectPaper
from apps.core.serializers.project import DaVinciProjectSerializer
from apps.core.serializers.stats import ProjectStatsSerializer
from apps.core.services.search_service import SearchService
from apps.core.services.stats_service import StatsService


class DaVinciProjectViewSet(viewsets.ModelViewSet):
    serializer_class = DaVinciProjectSerializer

    def get_queryset(self):
        return DaVinciProject.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        title = serializer.validated_data.get('title', 'project')
        slug = slugify(f"{title}-{self.request.user.username}-davinci")
        serializer.save(user=self.request.user, slug=slug)

    @action(detail=True, methods=['post'])
    def search(self, request, pk=None):
        """Dispara busca no PubMed."""
        project = self.get_object()
        job = SearchService.dispatch_pubmed_search(project)
        return Response(
            {'job_id': str(job.id), 'status': job.status},
            status=status.HTTP_202_ACCEPTED,
        )

    @action(detail=True, methods=['post'], url_path='omics_search')
    def omics_search(self, request, pk=None):
        """Dispara busca em GEO/SRA/BioProject/GWAS."""
        project = self.get_object()
        sources = request.data.get('sources', None)
        max_per_source = request.data.get('max_per_source', 500)
        job = SearchService.dispatch_omics_search(
            project, sources=sources, max_per_source=max_per_source
        )
        return Response(
            {'job_id': str(job.id), 'status': job.status},
            status=status.HTTP_202_ACCEPTED,
        )

    @action(detail=True, methods=['get'])
    def stats(self, request, pk=None):
        """Return ProjectStats, computing on the fly if missing."""
        project = self.get_object()
        project_stats = StatsService.compute_and_save(project)
        serializer = ProjectStatsSerializer(project_stats)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def export(self, request, pk=None):
        """
        Export included papers as JSON or CSV.

        Query param: ?format=json (default) | ?format=csv
        """
        project = self.get_object()
        export_format = request.query_params.get('export_format', 'json')

        included = (
            ProjectPaper.objects.filter(
                project=project,
                curation_status=ProjectPaper.CurationStatus.INCLUDED,
            )
            .select_related('paper')
            .prefetch_related(
                'paper__authors', 'paper__genes', 'paper__drugs',
                'paper__mesh_terms', 'paper__contexts',
                'clinical_categories', 'user_categories',
            )
        )

        if export_format == 'csv':
            return _export_csv(project, included)

        return _export_json(project, included)


# ── Export helpers ────────────────────────────────────────────────────────────

def _export_json(project, included_qs):
    data = {
        'project': project.title,
        'query_term': project.query_term,
        'papers': [],
    }
    for pp in included_qs:
        p = pp.paper
        data['papers'].append({
            'pmid': p.pmid,
            'title': p.title,
            'abstract': p.abstract,
            'journal': p.journal,
            'pub_year': p.pub_year,
            'curation_notes': pp.notes,
            'clinical_categories': [c.slug for c in pp.clinical_categories.all()],
            'user_categories': [c.name for c in pp.user_categories.all()],
            'genes': [
                {'symbol': g.gene_symbol, 'mentions': g.mention_count}
                for g in p.genes.all()
            ],
            'drugs': [
                {'name': d.drug_name, 'mentions': d.mention_count}
                for d in p.drugs.all()
            ],
            'mesh_terms': [
                m.descriptor for m in p.mesh_terms.filter(is_major_topic=True)
            ],
            'contexts': [
                {
                    'entity_type': c.entity_type,
                    'entity_name': c.entity_name,
                    'sentence': c.sentence,
                }
                for c in p.contexts.all()
            ],
        })
    return JsonResponse(data, json_dumps_params={'ensure_ascii': False})


def _export_csv(project, included_qs):
    def rows():
        header = ['pmid', 'title', 'journal', 'pub_year', 'clinical_categories', 'notes']
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(header)
        yield buf.getvalue()
        for pp in included_qs:
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow([
                pp.paper.pmid,
                pp.paper.title,
                pp.paper.journal,
                pp.paper.pub_year,
                '|'.join(c.slug for c in pp.clinical_categories.all()),
                pp.notes,
            ])
            yield buf.getvalue()

    response = StreamingHttpResponse(rows(), content_type='text/csv')
    response['Content-Disposition'] = (
        f'attachment; filename="{slugify(project.title)}_papers.csv"'
    )
    return response
