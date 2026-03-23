from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils.text import slugify
from apps.core.models import DaVinciProject
from apps.core.serializers.project import DaVinciProjectSerializer
from apps.core.services.search_service import SearchService

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
        """Dispara busca no PubMed + bases ômicas."""
        project = self.get_object()
        job = SearchService.dispatch_pubmed_search(project)
        return Response(
            {'job_id': str(job.id), 'status': job.status},
            status=status.HTTP_202_ACCEPTED
        )
