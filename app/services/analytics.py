import logging
import time
from datetime import date, timedelta
from typing import Optional

from fastapi import HTTPException

from app.core.cache import cache_delete_pattern, cache_get, cache_set
from app.core.exceptions import supabase_error
from app.core.supabase import get_supabase

logger = logging.getLogger(__name__)

_HISTORY_DEFAULT_DAYS = 30
_MAX_HISTORY_LIMIT = 365

_QUERY_MAX_RETRIES = 2
_QUERY_RETRY_DELAY_SECONDS = 0.5

# TTLs de caché — cortos porque los datos son "en tiempo real"; sirven sobre
# todo para amortiguar ráfagas de requests paralelos del panel de admin.
_OVERVIEW_CACHE_TTL = 30
_MEMBERS_CACHE_TTL = 30
_REVENUE_CACHE_TTL = 30
_HISTORY_CACHE_TTL = 300


# ---------------------------------------------------------------------------
# Helpers privados
# ---------------------------------------------------------------------------

def _execute_with_retry(query, context: str):
    """
    Ejecuta una query reintentando ante errores transitorios de red/socket
    (p.ej. "[Errno 11] Resource temporarily unavailable"), comunes en las
    consultas de analytics por la cantidad de vistas agregadas que se piden.
    """
    last_exc: Exception = RuntimeError("unreachable")
    for attempt in range(1, _QUERY_MAX_RETRIES + 2):
        try:
            return query.execute()
        except Exception as exc:
            last_exc = exc
            if attempt <= _QUERY_MAX_RETRIES:
                logger.warning(
                    "[analytics.%s] attempt %d/%d FAILED [%s] %s - retrying",
                    context, attempt, _QUERY_MAX_RETRIES + 1, type(exc).__name__, str(exc),
                )
                time.sleep(_QUERY_RETRY_DELAY_SECONDS)

    raise last_exc


def _select_single_row(supabase, view_name: str) -> dict:
    """Lee la única fila de una vista de stats. Devuelve {} si la vista está vacía."""
    try:
        result = _execute_with_retry(
            supabase.table(view_name).select("*").limit(1),
            context=f"_select_single_row[{view_name}]",
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[analytics._select_single_row] FAILED view=%s [%s] %s", view_name, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    rows = result.data or []
    return rows[0] if rows else {}


def _select_all_rows(supabase, view_name: str, order_column: Optional[str] = None) -> list:
    """Lee todas las filas de una vista de stats. Devuelve [] si está vacía."""
    query = supabase.table(view_name).select("*")
    if order_column:
        query = query.order(order_column, desc=True)

    try:
        result = _execute_with_retry(query, context=f"_select_all_rows[{view_name}]")
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[analytics._select_all_rows] FAILED view=%s [%s] %s", view_name, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    return result.data or []


def _num(row: dict, key: str):
    """Extrae un valor numérico de la fila, devolviendo 0 si es None o falta."""
    return row.get(key) or 0


def _members_summary(members: dict) -> dict:
    return {
        "total": _num(members, "total_members"),
        "active": _num(members, "active_members"),
        "inactive": _num(members, "inactive_members"),
        "expired": _num(members, "expired_members"),
        "invited": _num(members, "invited_members"),
        "new_today": _num(members, "new_today"),
        "new_this_month": _num(members, "new_this_month"),
    }


def _revenue_summary(revenue: dict) -> dict:
    return {
        "today": _num(revenue, "revenue_today"),
        "this_month": _num(revenue, "revenue_this_month"),
        "total": _num(revenue, "revenue_total"),
        "by_plan": {
            "1m": _num(revenue, "revenue_plan_1m"),
            "3m": _num(revenue, "revenue_plan_3m"),
            "6m": _num(revenue, "revenue_plan_6m"),
            "1y": _num(revenue, "revenue_plan_1y"),
        },
        "payments_pending": _num(revenue, "payments_pending"),
        "non_renewals": _num(revenue, "non_renewals"),
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

def get_overview() -> dict:
    """
    Resumen general en tiempo real combinando v_stats_members + v_stats_revenue.

    Returns:
        {"members": {...}, "revenue": {...}}
    Raises:
        HTTPException 500 — fallo de base de datos
    """
    logger.info("[analytics.overview] fetching")

    cache_key = "analytics:overview"
    cached = cache_get(cache_key)
    if cached is not None:
        logger.info("[analytics.overview] cache HIT")
        return cached

    supabase = get_supabase()

    members = _select_single_row(supabase, "v_stats_members")
    revenue = _select_single_row(supabase, "v_stats_revenue")

    response = {
        "members": _members_summary(members),
        "revenue": _revenue_summary(revenue),
    }

    cache_set(cache_key, response, _OVERVIEW_CACHE_TTL)
    logger.info("[analytics.overview] OK")
    return response


def get_members_detail() -> dict:
    """
    Detalle completo de miembros: totales, género, ciudad y rango de edad.

    Returns:
        {
          "total", "active", "inactive", "expired", "invited", "admin",
          "new_today", "new_this_month",
          "gender": {"male", "female", "other"},
          "locations": [{"city", "total", "percentage"}],
          "ages": [{"age_range", "total", "percentage"}],
        }
    Raises:
        HTTPException 500 — fallo de base de datos
    """
    logger.info("[analytics.members] fetching")

    cache_key = "analytics:members"
    cached = cache_get(cache_key)
    if cached is not None:
        logger.info("[analytics.members] cache HIT")
        return cached

    supabase = get_supabase()

    members = _select_single_row(supabase, "v_stats_members")
    locations = _select_all_rows(supabase, "v_stats_locations", order_column="total")
    ages = _select_all_rows(supabase, "v_stats_ages")

    response = {
        "total": _num(members, "total_members"),
        "active": _num(members, "active_members"),
        "inactive": _num(members, "inactive_members"),
        "expired": _num(members, "expired_members"),
        "invited": _num(members, "invited_members"),
        "admin": _num(members, "admin_members"),
        "new_today": _num(members, "new_today"),
        "new_this_month": _num(members, "new_this_month"),
        "gender": {
            "male": _num(members, "gender_male"),
            "female": _num(members, "gender_female"),
            "other": _num(members, "gender_other"),
        },
        "locations": [
            {"city": row.get("city"), "total": _num(row, "total"), "percentage": _num(row, "percentage")}
            for row in locations
        ],
        "ages": [
            {"age_range": row.get("age_range"), "total": _num(row, "total"), "percentage": _num(row, "percentage")}
            for row in ages
        ],
    }

    cache_set(cache_key, response, _MEMBERS_CACHE_TTL)
    logger.info("[analytics.members] OK")
    return response


def get_revenue_detail() -> dict:
    """
    Detalle completo de ingresos en tiempo real desde v_stats_revenue.

    Returns:
        {"today", "this_month", "total", "by_plan": {...}, "payments_pending", "non_renewals"}
    Raises:
        HTTPException 500 — fallo de base de datos
    """
    logger.info("[analytics.revenue] fetching")

    cache_key = "analytics:revenue"
    cached = cache_get(cache_key)
    if cached is not None:
        logger.info("[analytics.revenue] cache HIT")
        return cached

    supabase = get_supabase()

    revenue = _select_single_row(supabase, "v_stats_revenue")
    response = _revenue_summary(revenue)

    cache_set(cache_key, response, _REVENUE_CACHE_TTL)
    logger.info("[analytics.revenue] OK")
    return response


def get_history(from_date: Optional[date], to_date: Optional[date], limit: int) -> list:
    """
    Histórico de snapshots diarios desde analytics_daily_snapshots, ordenado
    por snapshot_date desc.

    Args:
        from_date: fecha inicio (default: hoy - 30 días)
        to_date: fecha fin (default: hoy)
        limit: máximo de registros a devolver

    Returns:
        Lista de snapshots (puede ser vacía si no hay datos).
    Raises:
        HTTPException 400 — from_date posterior a to_date, o limit fuera de rango
        HTTPException 500 — fallo de base de datos
    """
    today = date.today()
    if to_date is None:
        to_date = today
    if from_date is None:
        from_date = today - timedelta(days=_HISTORY_DEFAULT_DAYS)

    if from_date > to_date:
        raise HTTPException(status_code=400, detail="from_date no puede ser posterior a to_date.")

    if limit < 1 or limit > _MAX_HISTORY_LIMIT:
        raise HTTPException(status_code=400, detail=f"limit debe estar entre 1 y {_MAX_HISTORY_LIMIT}.")

    logger.info("[analytics.history] from=%s to=%s limit=%d", from_date, to_date, limit)

    cache_key = f"analytics:history:{from_date.isoformat()}:{to_date.isoformat()}:{limit}"
    cached = cache_get(cache_key)
    if cached is not None:
        logger.info("[analytics.history] cache HIT")
        return cached

    supabase = get_supabase()

    try:
        result = _execute_with_retry(
            supabase.table("analytics_daily_snapshots")
            .select("*")
            .gte("snapshot_date", from_date.isoformat())
            .lte("snapshot_date", to_date.isoformat())
            .order("snapshot_date", desc=True)
            .limit(limit),
            context="get_history",
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[analytics.history] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    rows = result.data or []
    cache_set(cache_key, rows, _HISTORY_CACHE_TTL)
    logger.info("[analytics.history] returned %d items", len(rows))
    return rows


def generate_snapshot() -> dict:
    """
    Genera o actualiza manualmente el snapshot del día actual mediante la RPC
    `generate_daily_snapshot`.

    Returns:
        {"success": true, "snapshot": {...}}
    Raises:
        HTTPException 500 — fallo al ejecutar la RPC
    """
    logger.info("[analytics.generate_snapshot] start")
    supabase = get_supabase()

    try:
        result = supabase.rpc("generate_daily_snapshot").execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[analytics.generate_snapshot] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    data = result.data
    if isinstance(data, list):
        snapshot = data[0] if data else {}
    elif isinstance(data, dict):
        snapshot = data
    else:
        snapshot = {}

    cache_delete_pattern("analytics:history:*")
    logger.info("[analytics.generate_snapshot] OK")
    return {"success": True, "snapshot": snapshot}
