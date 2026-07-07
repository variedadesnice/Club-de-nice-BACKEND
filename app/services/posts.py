import base64
import logging
import re
from typing import List, Optional

from fastapi import HTTPException

from app.core.exceptions import supabase_error
from app.core.supabase import get_supabase

logger = logging.getLogger(__name__)


def _award(user_id: str, code: str, metadata: dict = None) -> None:
    """Fire-and-forget: otorga un logro sin bloquear ni lanzar excepciones."""
    try:
        from app.services.levels import process_achievement
        process_achievement(user_id, code, metadata)
    except Exception as exc:
        logger.warning("[posts._award] silenced error user_id=%s code=%s [%s]", user_id, code, exc)


_MIME_TO_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


# ---------------------------------------------------------------------------
# Helpers privados
# ---------------------------------------------------------------------------

def _ext_from_mime(mime_type: str) -> str:
    return _MIME_TO_EXT.get(mime_type, "jpg")


def _upload_image(bucket: str, path: str, image_data: str) -> str:
    match = re.match(r"^data:(.+);base64,(.+)$", image_data)
    if not match:
        raise HTTPException(status_code=400, detail="Formato de imagen inválido")
    mime_type = match.group(1)
    raw_bytes = base64.b64decode(match.group(2))
    supabase = get_supabase()
    try:
        supabase.storage.from_(bucket).upload(
            path, raw_bytes,
            file_options={"content-type": mime_type, "upsert": "true"},
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[_upload_image] FAILED bucket=%s path=%s [%s] %s", bucket, path, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error subiendo imagen: {msg}")
    return supabase.storage.from_(bucket).get_public_url(path)


def _check_permission(supabase, post_id: str, user_id: str) -> dict:
    """Verifica que user_id sea dueño del post o sea admin. Lanza 403/404/500 si no."""
    try:
        post_resp = supabase.table("posts").select("user_id").eq("id", post_id).single().execute()
    except Exception as exc:
        logger.error("[_check_permission] fetch FAILED post_id=%s [%s] %s", post_id, type(exc).__name__, supabase_error(exc), exc_info=True)
        raise HTTPException(status_code=500, detail="Error al verificar el post")

    if not post_resp.data:
        raise HTTPException(status_code=404, detail="Post no encontrado")

    if post_resp.data["user_id"] != user_id:
        try:
            profile_resp = supabase.table("profiles").select("role").eq("id", user_id).single().execute()
            if not profile_resp.data or profile_resp.data.get("role") != "admin":
                raise HTTPException(status_code=403, detail="Sin permiso")
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("[_check_permission] profile fetch FAILED user_id=%s [%s] %s", user_id, type(exc).__name__, supabase_error(exc))
            raise HTTPException(status_code=403, detail="Sin permiso")

    return post_resp.data


def _build_comment_tree(flat: list) -> list:
    map_ = {c["id"]: {**c, "replies": []} for c in flat}
    roots = []
    for c in map_.values():
        if c.get("parent_id") and c["parent_id"] in map_:
            map_[c["parent_id"]]["replies"].append(c)
        else:
            roots.append(c)
    return roots


# ---------------------------------------------------------------------------
# Servicios públicos
# ---------------------------------------------------------------------------

def get_posts(limit: int, cursor: Optional[str], user_id: Optional[str], tags: Optional[str]) -> dict:
    """
    Returns:
        {"posts": [...], "nextCursor": str | None}
    Raises:
        HTTPException 500 — fallo al consultar posts_view
    """
    logger.info("[posts.get_posts] limit=%s cursor=%s userId=%s tags=%s", limit, cursor, user_id, tags)
    supabase = get_supabase()

    query = (
        supabase.table("posts_view")
        .select("*")
        .order("pinned", desc=True)
        .order("created_at", desc=True)
        .limit(limit)
    )
    if cursor:
        query = query.lt("created_at", cursor)
    if tags:
        tag_names = [t.strip() for t in tags.split(",") if t.strip()]
        if tag_names:
            query = query.overlaps("tags", tag_names)

    try:
        response = query.execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[posts.get_posts] query FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener posts: {msg}")

    posts = response.data or []

    if posts:
        post_ids = [p["id"] for p in posts]

        try:
            reactions_resp = (
                supabase.table("post_reactions")
                .select("post_id, user_id, reaction_type")
                .in_("post_id", post_ids)
                .execute()
            )
            reactions_data = reactions_resp.data or []
        except Exception as exc:
            logger.warning("[posts.get_posts] reactions fetch FAILED [%s] %s", type(exc).__name__, supabase_error(exc))
            reactions_data = []

        reactions_by_post: dict = {}
        for r in reactions_data:
            reactions_by_post.setdefault(r["post_id"], []).append(r)

        for post in posts:
            pid = post["id"]
            counts: dict = {}
            user_reaction = None
            for r in reactions_by_post.get(pid, []):
                rt = r["reaction_type"]
                counts[rt] = counts.get(rt, 0) + 1
                if user_id and r["user_id"] == user_id:
                    user_reaction = rt
            post["reactions"] = counts
            post["userReaction"] = user_reaction

    next_cursor = posts[-1]["created_at"] if len(posts) == limit else None
    logger.info("[posts.get_posts] returning %d posts", len(posts))
    return {"posts": posts, "nextCursor": next_cursor}


def create_post(content: str, user_id: str, tag_ids: List[str], image_data: Optional[str]) -> dict:
    """
    Returns:
        Post completo con likes, comments, reactions, userReaction
    Raises:
        HTTPException 500 — fallo al insertar
    """
    logger.info("[posts.create_post] userId=%s", user_id)
    supabase = get_supabase()

    try:
        post_resp = supabase.table("posts").insert({"user_id": user_id, "content": content}).execute()
        post = post_resp.data[0]
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[posts.create_post] insert FAILED userId=%s [%s] %s", user_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al crear post: {msg}")

    post_id = post["id"]
    logger.info("[posts.create_post] inserted post_id=%s", post_id)

    if image_data:
        match = re.match(r"^data:(.+);base64,", image_data)
        if match:
            ext = _ext_from_mime(match.group(1))
            try:
                image_url = _upload_image("posts", f"post-{post_id}.{ext}", image_data)
                supabase.table("posts").update({"image_url": image_url}).eq("id", post_id).execute()
            except HTTPException:
                logger.warning("[posts.create_post] image upload failed for post_id=%s, continuing", post_id)

    if tag_ids:
        try:
            supabase.table("post_tags").insert(
                [{"post_id": post_id, "tag_id": tid} for tid in tag_ids]
            ).execute()
        except Exception as exc:
            logger.warning("[posts.create_post] tag insert FAILED post_id=%s [%s] %s", post_id, type(exc).__name__, supabase_error(exc))

    try:
        view_resp = supabase.table("posts_view").select("*").eq("id", post_id).single().execute()
        full_post = view_resp.data or post
    except Exception as exc:
        logger.warning("[posts.create_post] posts_view fetch FAILED post_id=%s [%s] %s", post_id, type(exc).__name__, supabase_error(exc))
        full_post = post

    full_post.update({"likes": full_post.get("likes", 0), "comments": full_post.get("comments", 0), "userReaction": None, "reactions": {}})
    _award(user_id, "post_created")
    return full_post


def delete_post(post_id: str, user_id: str) -> dict:
    """
    Raises:
        HTTPException 404 — post no encontrado
        HTTPException 403 — sin permiso
        HTTPException 500 — fallo al eliminar
    """
    logger.info("[posts.delete_post] post_id=%s userId=%s", post_id, user_id)
    supabase = get_supabase()
    _check_permission(supabase, post_id, user_id)
    try:
        supabase.table("posts").delete().eq("id", post_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[posts.delete_post] FAILED post_id=%s [%s] %s", post_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al eliminar post: {msg}")
    logger.info("[posts.delete_post] OK post_id=%s", post_id)
    return {"deleted": True}


def patch_post(
    post_id: str,
    user_id: str,
    content: Optional[str],
    image_data: Optional[str],
    remove_image: bool,
    tag_ids: Optional[List[str]],
) -> dict:
    """
    Raises:
        HTTPException 404/403/500
    """
    logger.info("[posts.patch_post] post_id=%s userId=%s", post_id, user_id)
    supabase = get_supabase()
    _check_permission(supabase, post_id, user_id)

    updates: dict = {}
    if content is not None:
        updates["content"] = content
    if remove_image:
        updates["image_url"] = None
    elif image_data:
        match = re.match(r"^data:(.+);base64,", image_data)
        if match:
            ext = _ext_from_mime(match.group(1))
            try:
                updates["image_url"] = _upload_image("posts", f"post-{post_id}.{ext}", image_data)
            except HTTPException:
                logger.warning("[posts.patch_post] image upload failed for post_id=%s", post_id)

    if updates:
        try:
            supabase.table("posts").update(updates).eq("id", post_id).execute()
        except Exception as exc:
            msg = supabase_error(exc)
            logger.error("[posts.patch_post] update FAILED post_id=%s [%s] %s", post_id, type(exc).__name__, msg, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error al actualizar post: {msg}")

    updated_tags = None
    if tag_ids is not None:
        try:
            supabase.table("post_tags").delete().eq("post_id", post_id).execute()
            if tag_ids:
                supabase.table("post_tags").insert(
                    [{"post_id": post_id, "tag_id": tid} for tid in tag_ids]
                ).execute()
            view_resp = supabase.table("posts_view").select("tags").eq("id", post_id).single().execute()
            updated_tags = view_resp.data.get("tags") if view_resp.data else None
        except Exception as exc:
            logger.warning("[posts.patch_post] tag update FAILED post_id=%s [%s] %s", post_id, type(exc).__name__, supabase_error(exc))

    result: dict = {"updated": True}
    if content is not None:
        result["content"] = content
    if "image_url" in updates:
        result["image_url"] = updates["image_url"]
    if updated_tags is not None:
        result["tags"] = updated_tags
    logger.info("[posts.patch_post] OK post_id=%s", post_id)
    return result


def pin_post(post_id: str, user_id: str) -> dict:
    """
    Raises:
        HTTPException 404/403/500
    """
    logger.info("[posts.pin_post] post_id=%s userId=%s", post_id, user_id)
    supabase = get_supabase()
    _check_permission(supabase, post_id, user_id)
    try:
        post_resp = supabase.table("posts").select("pinned").eq("id", post_id).single().execute()
        new_pinned = not (post_resp.data.get("pinned", False) if post_resp.data else False)
        supabase.table("posts").update({"pinned": new_pinned}).eq("id", post_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[posts.pin_post] FAILED post_id=%s [%s] %s", post_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al fijar post: {msg}")
    logger.info("[posts.pin_post] OK post_id=%s pinned=%s", post_id, new_pinned)
    return {"pinned": new_pinned}


def react_to_post(post_id: str, user_id: str, reaction_type: str) -> dict:
    """
    Raises:
        HTTPException 500
    """
    logger.info("[posts.react_to_post] post_id=%s userId=%s reaction=%s", post_id, user_id, reaction_type)
    supabase = get_supabase()
    _is_new_reaction = False
    _post_owner_id = None

    try:
        existing = (
            supabase.table("post_reactions")
            .select("*").eq("post_id", post_id).eq("user_id", user_id).execute()
        )
        existing_data = existing.data or []

        if not existing_data:
            supabase.table("post_reactions").insert({"post_id": post_id, "user_id": user_id, "reaction_type": reaction_type}).execute()
            user_reaction = reaction_type
            _is_new_reaction = True
            try:
                owner = supabase.table("posts").select("user_id").eq("id", post_id).limit(1).execute()
                if owner.data:
                    _post_owner_id = owner.data[0]["user_id"]
            except Exception:
                pass
        elif existing_data[0]["reaction_type"] == reaction_type:
            supabase.table("post_reactions").delete().eq("post_id", post_id).eq("user_id", user_id).execute()
            user_reaction = None
        else:
            supabase.table("post_reactions").update({"reaction_type": reaction_type}).eq("post_id", post_id).eq("user_id", user_id).execute()
            user_reaction = reaction_type

        all_resp = supabase.table("post_reactions").select("reaction_type").eq("post_id", post_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[posts.react_to_post] FAILED post_id=%s userId=%s [%s] %s", post_id, user_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al reaccionar: {msg}")

    if _is_new_reaction:
        _award(user_id, "post_liked")
        if _post_owner_id and _post_owner_id != user_id:
            _award(_post_owner_id, "post_received_like")

    counts: dict = {}
    for r in (all_resp.data or []):
        rt = r["reaction_type"]
        counts[rt] = counts.get(rt, 0) + 1
    return {"reactions": counts, "userReaction": user_reaction}


def get_comments(post_id: str, user_id: Optional[str]) -> list:
    """
    Raises:
        HTTPException 500 — fallo en RPC
    """
    logger.info("[posts.get_comments] post_id=%s userId=%s", post_id, user_id)
    supabase = get_supabase()
    try:
        rpc_resp = supabase.rpc("get_post_comments", {"p_post_id": post_id}).execute()
        flat = rpc_resp.data or []
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[posts.get_comments] rpc FAILED post_id=%s [%s] %s", post_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener comentarios: {msg}")

    if user_id and flat:
        comment_ids = [c["id"] for c in flat]
        try:
            ur_resp = (
                supabase.table("comment_reactions")
                .select("comment_id, reaction_type")
                .eq("user_id", user_id)
                .in_("comment_id", comment_ids)
                .execute()
            )
            reaction_map = {r["comment_id"]: r["reaction_type"] for r in (ur_resp.data or [])}
        except Exception as exc:
            logger.warning("[posts.get_comments] user reactions FAILED post_id=%s [%s] %s", post_id, type(exc).__name__, supabase_error(exc))
            reaction_map = {}
        for c in flat:
            c["userReaction"] = reaction_map.get(c["id"])
    else:
        for c in flat:
            c["userReaction"] = None

    return _build_comment_tree(flat)


def create_comment(post_id: str, user_id: str, content: str, parent_id: Optional[str]) -> dict:
    """
    Raises:
        HTTPException 500 — fallo al insertar
    """
    logger.info("[posts.create_comment] post_id=%s userId=%s parentId=%s", post_id, user_id, parent_id)
    supabase = get_supabase()
    try:
        resp = supabase.table("post_comments").insert({
            "post_id": post_id, "user_id": user_id,
            "content": content, "parent_id": parent_id,
        }).execute()
        comment = resp.data[0]
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[posts.create_comment] insert FAILED post_id=%s [%s] %s", post_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al crear comentario: {msg}")

    try:
        profile_resp = supabase.table("profiles").select("name, avatar, role").eq("id", user_id).single().execute()
        profile = profile_resp.data or {}
    except Exception as exc:
        logger.warning("[posts.create_comment] profile fetch FAILED userId=%s [%s] %s", user_id, type(exc).__name__, supabase_error(exc))
        profile = {}

    comment.update({
        "author": profile.get("name"),
        "avatar": profile.get("avatar"),
        "role": profile.get("role"),
        "reactions": {}, "userReaction": None, "replies": [],
    })
    logger.info("[posts.create_comment] OK comment_id=%s", comment.get("id"))
    _award(user_id, "comment_created")
    return comment


def react_to_comment(comment_id: str, user_id: str, reaction_type: str) -> dict:
    """
    Raises:
        HTTPException 500
    """
    logger.info("[posts.react_to_comment] comment_id=%s userId=%s reaction=%s", comment_id, user_id, reaction_type)
    supabase = get_supabase()
    try:
        existing = (
            supabase.table("comment_reactions")
            .select("*").eq("comment_id", comment_id).eq("user_id", user_id).execute()
        )
        existing_data = existing.data or []

        if not existing_data:
            supabase.table("comment_reactions").insert({"comment_id": comment_id, "user_id": user_id, "reaction_type": reaction_type}).execute()
            user_reaction = reaction_type
        elif existing_data[0]["reaction_type"] == reaction_type:
            supabase.table("comment_reactions").delete().eq("comment_id", comment_id).eq("user_id", user_id).execute()
            user_reaction = None
        else:
            supabase.table("comment_reactions").update({"reaction_type": reaction_type}).eq("comment_id", comment_id).eq("user_id", user_id).execute()
            user_reaction = reaction_type

        all_resp = supabase.table("comment_reactions").select("reaction_type").eq("comment_id", comment_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[posts.react_to_comment] FAILED comment_id=%s userId=%s [%s] %s", comment_id, user_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al reaccionar al comentario: {msg}")

    counts: dict = {}
    for r in (all_resp.data or []):
        rt = r["reaction_type"]
        counts[rt] = counts.get(rt, 0) + 1
    return {"reactions": counts, "userReaction": user_reaction}


def get_comment_reactions(comment_id: str) -> list:
    logger.info("[posts.get_comment_reactions] comment_id=%s", comment_id)
    supabase = get_supabase()
    try:
        resp = (
            supabase.table("comment_reactions")
            .select("reaction_type, profiles(name, avatar)")
            .eq("comment_id", comment_id)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[posts.get_comment_reactions] FAILED comment_id=%s [%s] %s", comment_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener reacciones: {msg}")
    return [
        {
            "reaction_type": r["reaction_type"],
            "name": (r.get("profiles") or {}).get("name"),
            "avatar": (r.get("profiles") or {}).get("avatar"),
        }
        for r in (resp.data or [])
    ]


def get_social_impact(user_id: str) -> dict:
    """
    Suma de likes (post_reactions) y comentarios (post_comments) recibidos en
    todas las publicaciones del usuario.

    Returns: { likes, comments, totalImpact }
    Raises: HTTPException 500
    """
    logger.info("[posts.get_social_impact] user_id=%s", user_id)
    supabase = get_supabase()

    try:
        posts_resp = supabase.table("posts").select("id").eq("user_id", user_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[posts.get_social_impact] posts FAILED user_id=%s [%s] %s", user_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener impacto social: {msg}")

    post_ids = [p["id"] for p in (posts_resp.data or [])]
    if not post_ids:
        return {"likes": 0, "comments": 0, "totalImpact": 0}

    try:
        likes_resp = supabase.table("post_reactions").select("id", count="exact").in_("post_id", post_ids).execute()
        likes = likes_resp.count or 0
    except Exception as exc:
        logger.warning("[posts.get_social_impact] likes count FAILED user_id=%s [%s] %s", user_id, type(exc).__name__, supabase_error(exc))
        likes = 0

    try:
        comments_resp = supabase.table("post_comments").select("id", count="exact").in_("post_id", post_ids).execute()
        comments = comments_resp.count or 0
    except Exception as exc:
        logger.warning("[posts.get_social_impact] comments count FAILED user_id=%s [%s] %s", user_id, type(exc).__name__, supabase_error(exc))
        comments = 0

    total = likes + comments
    logger.info("[posts.get_social_impact] OK user_id=%s likes=%d comments=%d", user_id, likes, comments)
    return {"likes": likes, "comments": comments, "totalImpact": total}


def get_post_reactions(post_id: str) -> list:
    logger.info("[posts.get_post_reactions] post_id=%s", post_id)
    supabase = get_supabase()
    try:
        resp = (
            supabase.table("post_reactions")
            .select("reaction_type, profiles(name, avatar)")
            .eq("post_id", post_id)
            .limit(50)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[posts.get_post_reactions] FAILED post_id=%s [%s] %s", post_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener reacciones: {msg}")
    return [
        {
            "reaction_type": r["reaction_type"],
            "name": (r.get("profiles") or {}).get("name"),
            "avatar": (r.get("profiles") or {}).get("avatar"),
        }
        for r in (resp.data or [])
    ]
