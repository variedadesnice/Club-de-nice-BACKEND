import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException

from app.core.exceptions import supabase_error
from app.core.supabase import get_supabase

logger = logging.getLogger(__name__)


def _compute_status(row: dict) -> str:
    if row.get("used_at"):
        return "usada"
    expires_at = row.get("expires_at")
    if expires_at:
        try:
            exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if exp < datetime.now(timezone.utc):
                return "expirada"
        except Exception:
            pass
    return "pendiente"


def create_invitation(email: str, invited_by: str) -> dict:
    """
    Returns:
        La fila insertada con campo `status` calculado.
    Raises:
        HTTPException 400 — ya existe una invitación pendiente para ese email
        HTTPException 500 — fallo de base de datos
    """
    logger.info("[invitations.create] email=%s invited_by=%s", email, invited_by)
    supabase = get_supabase()

    # Verificar si ya hay una invitación pendiente para este email
    try:
        existing = (
            supabase.table("invitations")
            .select("id, used_at, expires_at")
            .eq("email", email)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[invitations.create] check existing FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    for row in existing.data or []:
        if _compute_status(row) == "pendiente":
            raise HTTPException(
                status_code=400,
                detail=f"Ya existe una invitación pendiente para {email}. Elimínala antes de crear una nueva.",
            )

    token = str(uuid4())
    try:
        result = (
            supabase.table("invitations")
            .insert({"email": email, "token": token, "invited_by": invited_by})
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[invitations.create] insert FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        if "unique" in msg.lower() or "duplicate" in msg.lower():
            raise HTTPException(status_code=400, detail=f"Ya existe una invitación para {email}.")
        raise HTTPException(status_code=500, detail=msg)

    row = result.data[0]
    row["status"] = _compute_status(row)
    logger.info("[invitations.create] OK id=%s token=%s", row["id"], token)
    return row


def list_invitations() -> list:
    """
    Returns:
        Lista de todas las invitaciones ordenadas por created_at desc, cada una con campo `status`.
    Raises:
        HTTPException 500 — fallo de base de datos
    """
    logger.info("[invitations.list] fetching all")
    supabase = get_supabase()

    try:
        result = (
            supabase.table("invitations")
            .select("*")
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[invitations.list] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    rows = result.data or []
    for row in rows:
        row["status"] = _compute_status(row)
    logger.info("[invitations.list] returned %d items", len(rows))
    return rows


def delete_invitation(invitation_id: str) -> None:
    """
    Raises:
        HTTPException 404 — invitación no encontrada
        HTTPException 500 — fallo de base de datos
    """
    logger.info("[invitations.delete] id=%s", invitation_id)
    supabase = get_supabase()

    try:
        result = supabase.table("invitations").delete().eq("id", invitation_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[invitations.delete] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    if not result.data:
        raise HTTPException(status_code=404, detail="Invitación no encontrada.")
    logger.info("[invitations.delete] OK id=%s", invitation_id)


def _normalize_rpc(data) -> dict:
    """Supabase RPCs can return a list or a dict depending on how they're defined."""
    if isinstance(data, list):
        return data[0] if data else {}
    return data or {}


def validate_token(token: str) -> dict:
    """
    Llama al RPC validate_invitation(invite_token) de Supabase.

    Returns:
        { valid: bool, email: str | None, reason: str | None }
    Raises:
        HTTPException 500 — fallo del RPC
    """
    logger.info("[invitations.validate] token=%s", token)
    supabase = get_supabase()

    try:
        result = supabase.rpc("validate_invitation", {"invite_token": token}).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[invitations.validate] RPC FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    normalized = _normalize_rpc(result.data)
    logger.info("[invitations.validate] result=%s", normalized)
    return normalized


def use_token(token: str) -> dict:
    """
    Valida el token y lo marca como usado llamando al RPC use_invitation(invite_token).

    Returns:
        { success: True }
    Raises:
        HTTPException 400 — token inválido, expirado o ya utilizado
        HTTPException 500 — fallo del RPC
    """
    logger.info("[invitations.use] token=%s", token)
    supabase = get_supabase()

    try:
        validate_result = supabase.rpc("validate_invitation", {"invite_token": token}).execute()
        validation = _normalize_rpc(validate_result.data)
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[invitations.use] validate RPC FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    if not validation.get("valid"):
        reason = validation.get("reason", "Token inválido")
        logger.warning("[invitations.use] invalid token reason=%s", reason)
        raise HTTPException(status_code=400, detail=reason)

    try:
        use_result = supabase.rpc("use_invitation", {"invite_token": token}).execute()
        success = use_result.data
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[invitations.use] use RPC FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    if not success:
        raise HTTPException(status_code=400, detail="No se pudo marcar la invitación como usada.")

    logger.info("[invitations.use] OK token=%s", token)
    return {"success": True}
