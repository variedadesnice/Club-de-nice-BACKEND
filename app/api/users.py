import logging

from fastapi import APIRouter, Depends

from app.core.deps import get_current_admin, get_current_user
from app.services import users as users_service

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/{user_id}/profile")
def get_user_public_profile(user_id: str, current_user: dict = Depends(get_current_user)):
    """
    Devuelve el perfil público de cualquier miembro autenticado.
    No expone email, teléfono ni fecha de nacimiento.
    """
    return users_service.get_public_profile(user_id)


@router.get("/{user_id}/admin-profile")
def get_user_admin_profile(user_id: str, current_user: dict = Depends(get_current_admin)):
    """
    Admin — perfil completo: lo público más email, teléfono, género, fecha de
    nacimiento, estado de la suscripción y el último pago.

    Va aparte de /profile porque ese lo puede pedir cualquier miembro: mezclar
    ambos expondría los datos de contacto de toda la comunidad.
    """
    return users_service.get_admin_profile(user_id)
