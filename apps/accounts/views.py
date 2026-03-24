from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from apps.accounts.models import UserProfile
from apps.accounts.serializers import UserProfileSerializer, UserProfileUpdateSerializer


class MeView(APIView):
    """
    GET:  Retorna perfil do usuário autenticado.
    PATCH: Atualiza perfil (institution, research_area, orcid_id, first_name, last_name).

    Primeiro endpoint chamado pelo frontend após login.
    O FirebaseAuthentication já garantiu que request.user existe.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        profile = UserProfile.objects.select_related('user').get(user=request.user)
        return Response(UserProfileSerializer(profile).data)

    def patch(self, request):
        profile = UserProfile.objects.select_related('user').get(user=request.user)
        serializer = UserProfileUpdateSerializer(profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        # Refresh to pick up any user field changes
        profile.refresh_from_db()
        return Response(UserProfileSerializer(profile).data)


class VerifyTokenView(APIView):
    """
    POST: Verifica se o token Firebase é válido.
    Retorna 200 se válido, 401 se inválido (o DRF lida automaticamente).
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        return Response({
            'status': 'valid',
            'uid': request.user.profile.firebase_uid,
        })
