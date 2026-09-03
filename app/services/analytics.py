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


def _num(row: dict, key: str):
    """Extrae un valor numérico de la fila, devolviendo 0 si es None o falta."""
    return row.get(key) or 0


# ---------------------------------------------------------------------------
# Estadísticas de miembros — se calculan en Python, no en SQL
#
# v_stats_members / v_stats_locations / v_stats_ages cuentan TODOS los perfiles:
# admins e invitados incluidos. El panel debe describir la comunidad de pago, así
# que el cálculo vive acá. Esas vistas existen solo dentro del proyecto de
# Supabase y no están versionadas en este repo, por lo que cambiarlas obligaría a
# aplicar SQL a mano en producción; esto en cambio se despliega con el backend.
#
# Reglas:
#   - La población de toda estadística de miembros es role = 'miembro'.
#     Los admins no se cuentan en ningún lado.
#   - Los invitados no entran en los totales ni en la demografía: solo se
#     reporta cuántos hay, en `invited`.
#   - La demografía (género, ciudad, edad) mira únicamente a los miembros con
#     subscription_status = 'active'.
# ---------------------------------------------------------------------------

_PROFILES_PAGE_SIZE = 1000  # PostgREST corta en 1000 filas por request

# Rangos etarios: (etiqueta, edad_min, edad_max inclusivo). El orden define el
# orden en que se pintan las barras del gráfico.
_AGE_BUCKETS = (
    ("18-24", 18, 24),
    ("25-34", 25, 34),
    ("35-44", 35, 44),
    ("45-54", 45, 54),
    ("55+", 55, 200),
)


def _role_of(profile: dict) -> str:
    return (profile.get("role") or "").strip().lower()


def _status_of(profile: dict) -> str:
    return (profile.get("subscription_status") or "").strip().lower()


def _fetch_all_profiles(supabase) -> list:
    """Trae todos los perfiles paginando, porque PostgREST corta en 1000 filas."""
    columns = "id, role, subscription_status, gender, city, birthdate, created_at"
    rows: list = []
    offset = 0

    while True:
        try:
            result = _execute_with_retry(
                supabase.table("profiles").select(columns).range(offset, offset + _PROFILES_PAGE_SIZE - 1),
                context="_fetch_all_profiles",
            )
        except Exception as exc:
            msg = supabase_error(exc)
            logger.error("[analytics._fetch_all_profiles] FAILED offset=%d [%s] %s", offset, type(exc).__name__, msg, exc_info=True)
            raise HTTPException(status_code=500, detail=msg)

        page = result.data or []
        rows.extend(page)
        if len(page) < _PROFILES_PAGE_SIZE:
            return rows
        offset += _PROFILES_PAGE_SIZE


def _age_from_birthdate(value) -> Optional[int]:
    """Edad en años a partir de un birthdate ISO ('YYYY-MM-DD'). None si no es parseable."""
    if not value:
        return None
    try:
        born = date.fromisoformat(str(value)[:10])
    except ValueError:
        return None

    today = date.today()
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return age if 0 <= age < 130 else None


def _age_bucket(age: Optional[int]) -> Optional[str]:
    if age is None:
        return None
    for label, low, high in _AGE_BUCKETS:
        if low <= age <= high:
            return label
    return None  # menores de 18: fuera de los rangos del gráfico


def _distribution(counts: dict, order: Optional[list] = None) -> list:
    """
    Convierte {clave: total} en [{key, total, percentage}] ordenado por total desc
    (o por `order` si se pasa). El porcentaje es sobre los miembros que TIENEN el
    dato, no sobre toda la población, para que las porciones sumen 100%.
    """
    universe = sum(counts.values())
    if universe == 0:
        return []

    keys = order if order is not None else sorted(counts, key=lambda k: (-counts[k], str(k)))
    return [
        {"key": key, "total": counts[key], "percentage": round(counts[key] * 100 / universe, 1)}
        for key in keys
        if counts.get(key)
    ]


def _members_summary(profiles: list) -> dict:
    """
    Totales sobre role = 'miembro'. `invited` es solo el conteo de invitados: no
    entra en `total` ni en ningún otro contador, y los admins quedan fuera de todo.
    """
    members = [p for p in profiles if _role_of(p) == "miembro"]

    today = date.today()
    month_start = today.replace(day=1)
    new_today = new_this_month = 0
    for profile in members:
        created = profile.get("created_at")
        if not created:
            continue
        try:
            created_day = date.fromisoformat(str(created)[:10])
        except ValueError:
            continue
        if created_day == today:
            new_today += 1
        if created_day >= month_start:
            new_this_month += 1

    return {
        "total": len(members),
        "active": sum(1 for p in members if _status_of(p) == "active"),
        "inactive": sum(1 for p in members if _status_of(p) == "inactive"),
        "expired": sum(1 for p in members if _status_of(p) == "expired"),
        "invited": sum(1 for p in profiles if _role_of(p) == "invitado"),
        "new_today": new_today,
        "new_this_month": new_this_month,
    }


def _members_demographics(profiles: list) -> dict:
    """Género, ciudad y edad de los miembros activos. Nadie más entra acá."""
    active = [
        p for p in profiles
        if _role_of(p) == "miembro" and _status_of(p) == "active"
    ]

    gender = {"male": 0, "female": 0, "other": 0}
    city_counts: dict = {}
    age_counts: dict = {}

    for profile in active:
        # profiles.gender guarda "Masculino"/"Femenino" capitalizado (viene del
        # dropdown del frontend), de ahí el lower() — mismo bug que tuvo v_stats_members.
        raw_gender = (profile.get("gender") or "").strip().lower()
        if raw_gender == "masculino":
            gender["male"] += 1
        elif raw_gender == "femenino":
            gender["female"] += 1
        elif raw_gender:
            gender["other"] += 1

        # title() para no contar "caracas" y "Caracas" como dos ciudades distintas.
        city = (profile.get("city") or "").strip()
        if city:
            city_counts[city.title()] = city_counts.get(city.title(), 0) + 1

        bucket = _age_bucket(_age_from_birthdate(profile.get("birthdate")))
        if bucket:
            age_counts[bucket] = age_counts.get(bucket, 0) + 1

    return {
        "demographics_base": len(active),
        "gender": gender,
        "locations": [
            {"city": row["key"], "total": row["total"], "percentage": row["percentage"]}
            for row in _distribution(city_counts)
        ],
        "ages": [
            {"age_range": row["key"], "total": row["total"], "percentage": row["percentage"]}
            for row in _distribution(age_counts, order=[label for label, _, _ in _AGE_BUCKETS])
        ],
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
    Resumen general en tiempo real: miembros calculados desde profiles (ver
    _members_summary) e ingresos desde v_stats_revenue.

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

    profiles = _fetch_all_profiles(supabase)
    revenue = _select_single_row(supabase, "v_stats_revenue")

    response = {
        "members": _members_summary(profiles),
        "revenue": _revenue_summary(revenue),
    }

    cache_set(cache_key, response, _OVERVIEW_CACHE_TTL)
    logger.info("[analytics.overview] OK")
    return response


def get_members_detail() -> dict:
    """
    Detalle completo de miembros: totales, género, ciudad y rango de edad.

    Los totales cuentan solo role = 'miembro'; los admins quedan fuera y de los
    invitados solo se reporta cuántos hay. La demografía mira únicamente a los
    miembros con subscription_status = 'active'.

    Returns:
        {
          "total", "active", "inactive", "expired", "invited",
          "new_today", "new_this_month", "demographics_base",
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

    profiles = _fetch_all_profiles(supabase)
    response = {**_members_summary(profiles), **_members_demographics(profiles)}

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
