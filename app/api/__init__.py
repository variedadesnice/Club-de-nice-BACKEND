from fastapi import APIRouter

from app.api.achievements import router as achievements_router
from app.api.admin_gamification import admin_achievements_router, admin_levels_router
from app.api.analytics import router as analytics_router
from app.api.auth import router as auth_router
from app.api.courses import router as courses_router
from app.api.invitations import router as invitations_router
from app.api.levels import router as levels_router
from app.api.payments import router as payments_router
from app.api.posts import router as posts_router
from app.api.tags import router as tags_router

api_router = APIRouter()
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(posts_router, prefix="/posts", tags=["posts"])
api_router.include_router(courses_router, prefix="/courses", tags=["courses"])
api_router.include_router(tags_router, prefix="/tags", tags=["tags"])
api_router.include_router(invitations_router, prefix="/invitations", tags=["invitations"])
api_router.include_router(payments_router, prefix="/payments", tags=["payments"])
api_router.include_router(levels_router, prefix="/levels", tags=["levels"])
api_router.include_router(achievements_router, prefix="/achievements", tags=["achievements"])
api_router.include_router(admin_levels_router, prefix="/admin/levels", tags=["admin-levels"])
api_router.include_router(admin_achievements_router, prefix="/admin/achievements", tags=["admin-achievements"])
api_router.include_router(analytics_router, prefix="/admin/analytics", tags=["admin-analytics"])
