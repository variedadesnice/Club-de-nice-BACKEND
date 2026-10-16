from fastapi import APIRouter, Depends

from app.core.deps import get_current_admin
from app.schemas.content_categories import ContentCategoryCreate, ContentCategoryUpdate
from app.services import content_categories as categories_service

# Montado en /api/content-categories — lo leen Comunidad (atajos) y Classroom (filtro y selector).
public_router = APIRouter()

# Montado en /api/admin/content-categories.
router = APIRouter()


@public_router.get("/")
def get_categories():
    """Público — categorías activas, ordenadas por sort_order."""
    return categories_service.get_active_categories()


@router.get("/")
def admin_get_categories(current_user: dict = Depends(get_current_admin)):
    """Admin — todas las categorías, con `course_count` de cada una."""
    return categories_service.admin_get_all()


@router.post("/", status_code=201)
def admin_create_category(body: ContentCategoryCreate, current_user: dict = Depends(get_current_admin)):
    """Admin — crea una categoría. 409 si el nombre ya existe."""
    return categories_service.admin_create(body.model_dump())


@router.patch("/{category_id}")
def admin_update_category(category_id: str, body: ContentCategoryUpdate, current_user: dict = Depends(get_current_admin)):
    """
    Admin — edita sólo los campos enviados. Renombrar arrastra a los cursos de
    la categoría. `description: null` la limpia; un null en el resto se ignora.
    """
    sent = body.model_dump(exclude_unset=True)
    data = {k: v for k, v in sent.items() if v is not None or k == "description"}
    return categories_service.admin_update(category_id, data)


@router.patch("/{category_id}/toggle")
def admin_toggle_category(category_id: str, current_user: dict = Depends(get_current_admin)):
    """Admin — activa/desactiva una categoría."""
    return categories_service.admin_toggle(category_id)


@router.delete("/{category_id}")
def admin_delete_category(category_id: str, current_user: dict = Depends(get_current_admin)):
    """Admin — elimina una categoría. 409 si tiene cursos."""
    return categories_service.admin_delete(category_id)
