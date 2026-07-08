import logging
import random
from datetime import datetime, timezone

from fastapi import HTTPException

from app.core.exceptions import supabase_error
from app.core.supabase import get_supabase

logger = logging.getLogger(__name__)

_MIN_PRIZES = 2
_MAX_PRIZES = 12
_DEFAULT_COLOR = "#6366f1"


def _get_settings_row() -> dict:
    supabase = get_supabase()
    try:
        resp = supabase.table("roulette_settings").select("*").limit(1).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[roulette._get_settings_row] FAILED: %s", msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    rows = resp.data or []
    if not rows:
        raise HTTPException(status_code=500, detail="roulette_settings no está inicializada.")
    return rows[0]


def _map_prize(p: dict, include_weight: bool) -> dict:
    prize = {"id": p["id"], "label": p["label"], "color": p.get("color") or _DEFAULT_COLOR}
    if include_weight:
        prize["weight"] = p["weight"]
    return prize


def list_prizes(include_weight: bool = False) -> list:
    supabase = get_supabase()
    try:
        resp = (
            supabase.table("roulette_prizes")
            .select("*")
            .order("sort_order", desc=False)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[roulette.list_prizes] FAILED: %s", msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)
    return [_map_prize(p, include_weight) for p in (resp.data or [])]


def get_admin_settings() -> dict:
    settings = _get_settings_row()
    return {"is_active": settings["is_active"], "prizes": list_prizes(include_weight=True)}


def set_active(is_active: bool) -> dict:
    supabase = get_supabase()
    settings = _get_settings_row()
    try:
        supabase.table("roulette_settings").update({
            "is_active": is_active,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", settings["id"]).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[roulette.set_active] FAILED: %s", msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)
    return get_admin_settings()


def create_prize(label: str, color: str | None, weight: int) -> dict:
    supabase = get_supabase()
    existing = list_prizes()
    if len(existing) >= _MAX_PRIZES:
        raise HTTPException(status_code=400, detail=f"Máximo {_MAX_PRIZES} premios permitidos.")

    try:
        resp = (
            supabase.table("roulette_prizes")
            .insert({
                "label": label,
                "color": color or _DEFAULT_COLOR,
                "weight": weight,
                "sort_order": len(existing),
            })
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[roulette.create_prize] FAILED: %s", msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)
    return _map_prize(resp.data[0], include_weight=True)


def update_prize(prize_id: str, label: str | None, color: str | None, weight: int | None) -> dict:
    supabase = get_supabase()
    updates = {}
    if label is not None:
        updates["label"] = label
    if color is not None:
        updates["color"] = color
    if weight is not None:
        updates["weight"] = weight

    if not updates:
        raise HTTPException(status_code=400, detail="Nada para actualizar.")

    try:
        resp = (
            supabase.table("roulette_prizes")
            .update(updates)
            .eq("id", prize_id)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[roulette.update_prize] FAILED prize_id=%s: %s", prize_id, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    if not resp.data:
        raise HTTPException(status_code=404, detail="Premio no encontrado.")
    return _map_prize(resp.data[0], include_weight=True)


def delete_prize(prize_id: str) -> dict:
    supabase = get_supabase()
    existing = list_prizes()
    if len(existing) <= _MIN_PRIZES:
        raise HTTPException(status_code=400, detail=f"Debe haber al menos {_MIN_PRIZES} premios.")

    try:
        supabase.table("roulette_prizes").delete().eq("id", prize_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[roulette.delete_prize] FAILED prize_id=%s: %s", prize_id, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)
    return {"deleted": True}


def _pick_weighted(prizes: list) -> dict:
    total = sum(p["weight"] for p in prizes)
    r = random.uniform(0, total)
    acc = 0.0
    for p in prizes:
        acc += p["weight"]
        if r <= acc:
            return p
    return prizes[-1]


def _has_spun_today(user_id: str) -> bool:
    supabase = get_supabase()
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        resp = (
            supabase.table("roulette_spins")
            .select("id")
            .eq("user_id", user_id)
            .eq("spun_date", today)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[roulette._has_spun_today] FAILED user_id=%s: %s", user_id, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)
    return bool(resp.data)


def get_status(user_id: str) -> dict:
    settings = _get_settings_row()
    return {
        "is_active": settings["is_active"],
        "already_spun_today": _has_spun_today(user_id),
        "prizes": list_prizes(include_weight=False),
    }


def spin(user_id: str) -> dict:
    settings = _get_settings_row()
    if not settings["is_active"]:
        raise HTTPException(status_code=400, detail="La ruleta no está activa.")

    if _has_spun_today(user_id):
        raise HTTPException(status_code=400, detail="Ya giraste la ruleta hoy. Vuelve mañana.")

    prizes = list_prizes(include_weight=True)
    if len(prizes) < _MIN_PRIZES:
        raise HTTPException(status_code=400, detail="No hay suficientes premios configurados.")

    winner = _pick_weighted(prizes)
    supabase = get_supabase()
    try:
        supabase.table("roulette_spins").insert({
            "user_id": user_id,
            "prize_id": winner["id"],
            "prize_label": winner["label"],
        }).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        if "unique" in msg.lower() or "duplicate" in msg.lower():
            logger.warning("[roulette.spin] race detected, user already spun today user_id=%s", user_id)
            raise HTTPException(status_code=400, detail="Ya giraste la ruleta hoy. Vuelve mañana.")
        logger.error("[roulette.spin] FAILED inserting spin user_id=%s: %s", user_id, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    logger.info("[roulette.spin] OK user_id=%s prize_id=%s", user_id, winner["id"])
    return {"prize_id": winner["id"], "label": winner["label"], "color": winner["color"]}


def list_spins(limit: int = 20, offset: int = 0) -> list:
    supabase = get_supabase()
    try:
        resp = (
            supabase.table("roulette_spins")
            .select("id, user_id, prize_label, spun_at, profiles(name)")
            .order("spun_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[roulette.list_spins] FAILED: %s", msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    result = []
    for row in (resp.data or []):
        profile = row.get("profiles") or {}
        result.append({
            "id": row["id"],
            "user_id": row["user_id"],
            "user_name": profile.get("name") or "Sin nombre",
            "prize_label": row["prize_label"],
            "spun_at": row["spun_at"],
        })
    return result
