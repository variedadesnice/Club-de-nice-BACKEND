from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from lib.supabase import get_supabase

router = APIRouter()


class TagBody(BaseModel):
    name: str


@router.get("/")
def get_tags():
    supabase = get_supabase()
    response = supabase.table("tags").select("id, name").order("name").execute()
    return response.data or []


@router.post("/")
def create_tag(body: TagBody):
    supabase = get_supabase()

    name = body.name.strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="El nombre es requerido")

    existing = supabase.table("tags").select("id, name").eq("name", name).execute()
    if existing.data:
        return existing.data[0]

    response = supabase.table("tags").insert({"name": name}).execute()
    return response.data[0]


@router.delete("/{tag_id}")
def delete_tag(tag_id: str):
    supabase = get_supabase()
    supabase.table("post_tags").delete().eq("tag_id", tag_id).execute()
    supabase.table("tags").delete().eq("id", tag_id).execute()
    return {"deleted": True}
