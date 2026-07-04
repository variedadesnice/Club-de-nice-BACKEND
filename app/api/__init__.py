from fastapi import APIRouter

from app.api.achievements import router as achievements_router
from app.api.currencies import router as currencies_router
from app.api.admin_currencies import router as admin_currencies_router
from app.api.admin_classroom import router as admin_classroom_router
from app.api.admin_gamification import admin_achievements_router, admin_levels_router
from app.api.admin_lives import router as admin_lives_router
from app.api.analytics import router as analytics_router
from app.api.auth import router as auth_router
from app.api.classroom import router as classroom_router
from app.api.courses import router as courses_router
from app.api.invitations import router as invitations_router
from app.api.levels import router as levels_router
from app.api.lives import router as lives_router
from app.api.payment_methods import router as payment_methods_router
from app.api.admin_payment_methods import router as admin_payment_methods_router
from app.api.payments import router as payments_router
from app.api.posts import router as posts_router
from app.api.emails import public_router as email_public_router, admin_router as email_admin_router
from app.api.raffles import router as raffles_router
from app.api.streaks import router as streaks_router
from app.api.tags import router as tags_router
from app.api.users import router as users_router

api_router = APIRouter()
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(posts_router, prefix="/posts", tags=["posts"])
api_router.include_router(courses_router, prefix="/courses", tags=["courses"])
api_router.include_router(tags_router, prefix="/tags", tags=["tags"])
api_router.include_router(invitations_router, prefix="/invitations", tags=["invitations"])
api_router.include_router(payments_router, prefix="/payments", tags=["payments"])
api_router.include_router(payment_methods_router, prefix="/payment-methods", tags=["payment-methods"])
api_router.include_router(admin_payment_methods_router, prefix="/admin/payment-methods", tags=["admin-payment-methods"])
api_router.include_router(levels_router, prefix="/levels", tags=["levels"])
api_router.include_router(achievements_router, prefix="/achievements", tags=["achievements"])
api_router.include_router(admin_levels_router, prefix="/admin/levels", tags=["admin-levels"])
api_router.include_router(admin_achievements_router, prefix="/admin/achievements", tags=["admin-achievements"])
api_router.include_router(analytics_router, prefix="/admin/analytics", tags=["admin-analytics"])
api_router.include_router(classroom_router, prefix="/classroom", tags=["classroom"])
api_router.include_router(admin_classroom_router, prefix="/admin/classroom", tags=["admin-classroom"])
api_router.include_router(lives_router, prefix="/lives", tags=["lives"])
api_router.include_router(admin_lives_router, prefix="/admin/lives", tags=["admin-lives"])
api_router.include_router(currencies_router, prefix="/currencies", tags=["currencies"])
api_router.include_router(admin_currencies_router, prefix="/admin/currencies", tags=["admin-currencies"])
api_router.include_router(streaks_router, prefix="/streaks", tags=["streaks"])
api_router.include_router(raffles_router, prefix="/admin/raffles", tags=["admin-raffles"])
api_router.include_router(email_public_router, prefix="/auth", tags=["email"])
api_router.include_router(email_admin_router, prefix="/admin/emails", tags=["admin-email"])
api_router.include_router(users_router, prefix="/users", tags=["users"])

