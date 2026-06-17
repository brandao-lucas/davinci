from drf_spectacular.utils import extend_schema
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from apps.accounts.models import UserProfile
from apps.accounts.serializers import UserProfileSerializer, UserProfileUpdateSerializer


class _VerifyTokenResponseSerializer(UserProfileSerializer):
    """Alias interno usado apenas para declaração de schema."""
    pass


import rest_framework.serializers as _s


class _VerifyTokenOkSerializer(_s.Serializer):
    status = _s.CharField()
    uid = _s.CharField()


class MeView(APIView):
    """
    GET:  Retorna perfil do usuário autenticado.
    PATCH: Atualiza perfil (institution, research_area, orcid_id, first_name, last_name).

    Primeiro endpoint chamado pelo frontend após login.
    O FirebaseAuthentication já garantiu que request.user existe.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={200: UserProfileSerializer},
        summary="Perfil do usuário autenticado",
        description="Retorna (criando se necessário) o UserProfile do usuário Firebase autenticado.",
    )
    def get(self, request):
        profile, _created = UserProfile.objects.select_related('user').get_or_create(
            user=request.user,
            defaults={
                'firebase_uid': getattr(request.user, 'username', '') or str(request.user.pk),
                'auth_provider': 'password',
            },
        )
        return Response(UserProfileSerializer(profile).data)

    @extend_schema(
        request=UserProfileUpdateSerializer,
        responses={200: UserProfileSerializer},
        summary="Atualizar perfil do usuário",
        description="Atualiza campos de perfil (institution, research_area, orcid_id, first_name, last_name).",
    )
    def patch(self, request):
        profile, _created = UserProfile.objects.select_related('user').get_or_create(
            user=request.user,
            defaults={
                'firebase_uid': getattr(request.user, 'username', '') or str(request.user.pk),
                'auth_provider': 'password',
            },
        )
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

    @extend_schema(
        request=None,
        responses={200: _VerifyTokenOkSerializer},
        summary="Verificar token Firebase",
        description="Retorna 200 com status=valid se o token Bearer é válido. 401 caso contrário.",
    )
    def post(self, request):
        return Response({
            'status': 'valid',
            'uid': request.user.profile.firebase_uid,
        })
