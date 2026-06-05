import base64
import re
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from lib.supabase import get_supabase

router = APIRouter()

_MIME_TO_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


def _ext_from_mime(mime_type: str) -> str:
    return _MIME_TO_EXT.get(mime_type, "jpg")


def _upload_image(bucket: str, path: str, image_data: str, upsert: bool = True) -> str:
    match = re.match(r"^data:(.+);base64,(.+)$", image_data)
    if not match:
        raise HTTPException(status_code=400, detail="Formato de imagen inválido")
    mime_type = match.group(1)
    raw_bytes = base64.b64decode(match.group(2))
    supabase = get_supabase()
    supabase.storage.from_(bucket).upload(
        path,
        raw_bytes,
        file_options={"content-type": mime_type, "upsert": "true" if upsert else "false"},
    )
    return supabase.storage.from_(bucket).get_public_url(path)


def _check_post_permission(supabase, post_id: str, user_id: str) -> dict:
    post_response = supabase.table("posts").select("user_id").eq("id", post_id).single().execute()
    if not post_response.data:
        raise HTTPException(status_code=404, detail="Post no encontrado")

    if post_response.data["user_id"] != user_id:
        try:
            profile_response = (
                supabase.table("profiles").select("role").eq("id", user_id).single().execute()
            )
            if not profile_response.data or profile_response.data.get("role") != "admin":
                raise HTTPException(status_code=403, detail="Sin permiso")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=403, detail="Sin permiso")

    return post_response.data


def build_comment_tree(flat: list) -> list:
    map_ = {c["id"]: {**c, "replies": []} for c in flat}
    roots = []
    for c in map_.values():
        if c.get("parent_id") and c["parent_id"] in map_:
            map_[c["parent_id"]]["replies"].append(c)
        else:
            roots.append(c)
    return roots


class PostBody(BaseModel):
    content: str
    userId: str
    tagIds: Optional[List[str]] = []
    imageData: Optional[str] = None


class DeletePostBody(BaseModel):
    userId: str


class PatchPostBody(BaseModel):
    userId: str
    content: Optional[str] = None
    imageData: Optional[str] = None
    removeImage: Optional[bool] = False
    tagIds: Optional[List[str]] = None


class PinBody(BaseModel):
    userId: str


class ReactBody(BaseModel):
    userId: str
    reactionType: str


class CommentBody(BaseModel):
    content: str
    userId: str
    parentId: Optional[str] = None


@router.get("/")
def get_posts(
    limit: int = Query(10, le=50),
    cursor: Optional[str] = Query(None),
    userId: Optional[str] = Query(None),
    tags: Optional[str] = Query(None),
):
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

    response = query.execute()
    posts = response.data or []

    if posts:
        post_ids = [p["id"] for p in posts]
        reactions_response = (
            supabase.table("post_reactions")
            .select("post_id, user_id, reaction_type")
            .in_("post_id", post_ids)
            .execute()
        )
        reactions_data = reactions_response.data or []

        reactions_by_post: dict = {}
        for r in reactions_data:
            pid = r["post_id"]
            reactions_by_post.setdefault(pid, []).append(r)

        for post in posts:
            pid = post["id"]
            post_reactions = reactions_by_post.get(pid, [])
            reaction_counts: dict = {}
            user_reaction = None

            for r in post_reactions:
                rt = r["reaction_type"]
                reaction_counts[rt] = reaction_counts.get(rt, 0) + 1
                if userId and r["user_id"] == userId:
                    user_reaction = rt

            post["reactions"] = reaction_counts
            post["userReaction"] = user_reaction

    next_cursor = posts[-1]["created_at"] if len(posts) == limit else None
    return {"posts": posts, "nextCursor": next_cursor}


@router.post("/", status_code=201)
def create_post(body: PostBody):
    if not body.content or not body.content.strip():
        raise HTTPException(status_code=400, detail="El contenido es requerido")
    if not body.userId:
        raise HTTPException(status_code=400, detail="userId es requerido")

    supabase = get_supabase()

    post_response = supabase.table("posts").insert({
        "user_id": body.userId,
        "content": body.content,
    }).execute()
    post = post_response.data[0]
    post_id = post["id"]

    if body.imageData:
        match = re.match(r"^data:(.+);base64,", body.imageData)
        if match:
            ext = _ext_from_mime(match.group(1))
            image_url = _upload_image("Post", f"post-{post_id}.{ext}", body.imageData)
            supabase.table("posts").update({"image_url": image_url}).eq("id", post_id).execute()

    if body.tagIds:
        supabase.table("post_tags").insert(
            [{"post_id": post_id, "tag_id": tid} for tid in body.tagIds]
        ).execute()

    try:
        view_response = supabase.table("posts_view").select("*").eq("id", post_id).single().execute()
        full_post = view_response.data or post
    except Exception:
        full_post = post

    full_post["likes"] = full_post.get("likes", 0)
    full_post["comments"] = full_post.get("comments", 0)
    full_post["userReaction"] = None
    full_post["reactions"] = {}

    return full_post


@router.delete("/{post_id}")
def delete_post(post_id: str, body: DeletePostBody):
    supabase = get_supabase()
    _check_post_permission(supabase, post_id, body.userId)
    supabase.table("posts").delete().eq("id", post_id).execute()
    return {"deleted": True}


@router.patch("/{post_id}")
def patch_post(post_id: str, body: PatchPostBody):
    supabase = get_supabase()
    _check_post_permission(supabase, post_id, body.userId)

    updates: dict = {}
    if body.content is not None:
        updates["content"] = body.content

    if body.removeImage:
        updates["image_url"] = None
    elif body.imageData:
        match = re.match(r"^data:(.+);base64,", body.imageData)
        if match:
            ext = _ext_from_mime(match.group(1))
            updates["image_url"] = _upload_image("Post", f"post-{post_id}.{ext}", body.imageData)

    if updates:
        supabase.table("posts").update(updates).eq("id", post_id).execute()

    updated_tags = None
    if body.tagIds is not None:
        supabase.table("post_tags").delete().eq("post_id", post_id).execute()
        if body.tagIds:
            supabase.table("post_tags").insert(
                [{"post_id": post_id, "tag_id": tid} for tid in body.tagIds]
            ).execute()
        try:
            view_response = (
                supabase.table("posts_view").select("tags").eq("id", post_id).single().execute()
            )
            updated_tags = view_response.data.get("tags") if view_response.data else None
        except Exception:
            updated_tags = None

    result: dict = {"updated": True}
    if body.content is not None:
        result["content"] = body.content
    if "image_url" in updates:
        result["image_url"] = updates["image_url"]
    if updated_tags is not None:
        result["tags"] = updated_tags

    return result


@router.post("/{post_id}/pin")
def pin_post(post_id: str, body: PinBody):
    supabase = get_supabase()
    _check_post_permission(supabase, post_id, body.userId)

    post_response = supabase.table("posts").select("pinned").eq("id", post_id).single().execute()
    current_pinned = post_response.data.get("pinned", False) if post_response.data else False
    new_pinned = not current_pinned

    supabase.table("posts").update({"pinned": new_pinned}).eq("id", post_id).execute()
    return {"pinned": new_pinned}


@router.post("/{post_id}/react")
def react_to_post(post_id: str, body: ReactBody):
    supabase = get_supabase()

    existing = (
        supabase.table("post_reactions")
        .select("*")
        .eq("post_id", post_id)
        .eq("user_id", body.userId)
        .execute()
    )
    existing_data = existing.data or []

    if not existing_data:
        supabase.table("post_reactions").insert({
            "post_id": post_id,
            "user_id": body.userId,
            "reaction_type": body.reactionType,
        }).execute()
        user_reaction = body.reactionType
    elif existing_data[0]["reaction_type"] == body.reactionType:
        supabase.table("post_reactions").delete().eq("post_id", post_id).eq("user_id", body.userId).execute()
        user_reaction = None
    else:
        supabase.table("post_reactions").update({"reaction_type": body.reactionType}).eq("post_id", post_id).eq("user_id", body.userId).execute()
        user_reaction = body.reactionType

    all_reactions = (
        supabase.table("post_reactions").select("reaction_type").eq("post_id", post_id).execute()
    )
    reaction_counts: dict = {}
    for r in (all_reactions.data or []):
        rt = r["reaction_type"]
        reaction_counts[rt] = reaction_counts.get(rt, 0) + 1

    return {"reactions": reaction_counts, "userReaction": user_reaction}


@router.get("/{post_id}/comments")
def get_comments(post_id: str, userId: Optional[str] = Query(None)):
    supabase = get_supabase()

    rpc_response = supabase.rpc("get_post_comments", {"p_post_id": post_id}).execute()
    flat_comments = rpc_response.data or []

    if userId and flat_comments:
        comment_ids = [c["id"] for c in flat_comments]
        user_reactions_response = (
            supabase.table("comment_reactions")
            .select("comment_id, reaction_type")
            .eq("user_id", userId)
            .in_("comment_id", comment_ids)
            .execute()
        )
        reaction_map = {
            r["comment_id"]: r["reaction_type"]
            for r in (user_reactions_response.data or [])
        }
        for c in flat_comments:
            c["userReaction"] = reaction_map.get(c["id"])
    else:
        for c in flat_comments:
            c["userReaction"] = None

    return build_comment_tree(flat_comments)


@router.post("/{post_id}/comments", status_code=201)
def create_comment(post_id: str, body: CommentBody):
    supabase = get_supabase()

    response = supabase.table("post_comments").insert({
        "post_id": post_id,
        "user_id": body.userId,
        "content": body.content,
        "parent_id": body.parentId,
    }).execute()
    comment = response.data[0]

    try:
        profile_response = (
            supabase.table("profiles")
            .select("name, avatar, role")
            .eq("id", body.userId)
            .single()
            .execute()
        )
        profile = profile_response.data or {}
    except Exception:
        profile = {}

    comment["author"] = profile.get("name")
    comment["avatar"] = profile.get("avatar")
    comment["role"] = profile.get("role")
    comment["reactions"] = {}
    comment["userReaction"] = None
    comment["replies"] = []

    return comment


@router.post("/{post_id}/comments/{comment_id}/react")
def react_to_comment(post_id: str, comment_id: str, body: ReactBody):
    supabase = get_supabase()

    existing = (
        supabase.table("comment_reactions")
        .select("*")
        .eq("comment_id", comment_id)
        .eq("user_id", body.userId)
        .execute()
    )
    existing_data = existing.data or []

    if not existing_data:
        supabase.table("comment_reactions").insert({
            "comment_id": comment_id,
            "user_id": body.userId,
            "reaction_type": body.reactionType,
        }).execute()
        user_reaction = body.reactionType
    elif existing_data[0]["reaction_type"] == body.reactionType:
        supabase.table("comment_reactions").delete().eq("comment_id", comment_id).eq("user_id", body.userId).execute()
        user_reaction = None
    else:
        supabase.table("comment_reactions").update({"reaction_type": body.reactionType}).eq("comment_id", comment_id).eq("user_id", body.userId).execute()
        user_reaction = body.reactionType

    all_reactions = (
        supabase.table("comment_reactions").select("reaction_type").eq("comment_id", comment_id).execute()
    )
    reaction_counts: dict = {}
    for r in (all_reactions.data or []):
        rt = r["reaction_type"]
        reaction_counts[rt] = reaction_counts.get(rt, 0) + 1

    return {"reactions": reaction_counts, "userReaction": user_reaction}


@router.get("/{post_id}/comments/{comment_id}/reactions")
def get_comment_reactions(post_id: str, comment_id: str):
    supabase = get_supabase()

    response = (
        supabase.table("comment_reactions")
        .select("reaction_type, profiles(name, avatar)")
        .eq("comment_id", comment_id)
        .execute()
    )

    result = []
    for r in (response.data or []):
        profile = r.get("profiles") or {}
        result.append({
            "reaction_type": r["reaction_type"],
            "name": profile.get("name"),
            "avatar": profile.get("avatar"),
        })
    return result


@router.get("/{post_id}/reactions")
def get_post_reactions(post_id: str):
    supabase = get_supabase()

    response = (
        supabase.table("post_reactions")
        .select("reaction_type, profiles(name, avatar)")
        .eq("post_id", post_id)
        .limit(50)
        .execute()
    )

    result = []
    for r in (response.data or []):
        profile = r.get("profiles") or {}
        result.append({
            "reaction_type": r["reaction_type"],
            "name": profile.get("name"),
            "avatar": profile.get("avatar"),
        })
    return result
