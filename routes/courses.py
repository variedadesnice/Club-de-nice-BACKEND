import base64
import re
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from lib.supabase import get_supabase

router = APIRouter()


def sync_course_module_label(supabase, course_id: str) -> str:
    chapters_response = (
        supabase.table("course_chapters")
        .select("title")
        .eq("course_id", course_id)
        .order("sort_order")
        .execute()
    )
    chapters = chapters_response.data or []
    count = len(chapters)

    if count == 0:
        module = "Sin capítulos"
    elif count == 1:
        module = chapters[0]["title"]
    else:
        module = f"{count} capítulos"

    supabase.table("courses").update({"module": module}).eq("id", course_id).execute()
    return module


def map_course(c: dict) -> dict:
    return {
        "id": c.get("id"),
        "title": c.get("title"),
        "description": c.get("description"),
        "thumbnail": c.get("thumbnail"),
        "category": c.get("category"),
        "module": c.get("module"),
        "progress": c.get("progress"),
        "created_at": c.get("created_at"),
        "created_by": c.get("created_by"),
    }


def map_chapter(c: dict) -> dict:
    return {
        "id": c.get("id"),
        "courseId": c.get("course_id"),
        "title": c.get("title"),
        "videoUrl": c.get("video_url"),
        "duration": c.get("duration"),
        "sortOrder": c.get("sort_order"),
    }


class CreateCourseBody(BaseModel):
    title: str
    description: str
    thumbnail: str
    userId: Optional[str] = None
    category: str = "General"


class UpdateCourseBody(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    thumbnail: Optional[str] = None
    category: Optional[str] = None


class ThumbnailBody(BaseModel):
    imageData: str


class CreateChapterBody(BaseModel):
    title: str
    videoUrl: Optional[str] = None
    duration: Optional[str] = None


class UpdateChapterBody(BaseModel):
    title: Optional[str] = None
    videoUrl: Optional[str] = None
    duration: Optional[str] = None


@router.get("/")
def get_courses():
    supabase = get_supabase()
    response = supabase.table("courses").select("*").order("created_at", desc=True).execute()
    return [map_course(c) for c in (response.data or [])]


@router.post("/", status_code=201)
def create_course(body: CreateCourseBody):
    supabase = get_supabase()

    course_data = {
        "title": body.title,
        "description": body.description,
        "thumbnail": body.thumbnail,
        "category": body.category,
        "module": "Sin capítulos",
        "progress": 0,
    }

    if body.userId:
        course_data["created_by"] = body.userId

    try:
        response = supabase.table("courses").insert(course_data).execute()
        course = response.data[0]
    except Exception as e:
        if body.userId and "created_by" in str(e):
            course_data.pop("created_by", None)
            response = supabase.table("courses").insert(course_data).execute()
            course = response.data[0]
        else:
            raise HTTPException(status_code=500, detail=str(e))

    return map_course(course)


@router.post("/thumbnail")
def upload_thumbnail(body: ThumbnailBody):
    from lib.env import is_supabase_configured

    if not is_supabase_configured() and len(body.imageData) <= 800_000:
        return {"url": body.imageData, "storage": "inline"}

    try:
        match = re.match(r"^data:(.+);base64,(.+)$", body.imageData)
        if not match:
            raise HTTPException(status_code=400, detail="Formato de imagen inválido")

        mime_type = match.group(1)
        raw_bytes = base64.b64decode(match.group(2))

        supabase = get_supabase()
        timestamp = int(time.time() * 1000)
        path = f"course-{timestamp}.jpg"

        supabase.storage.from_("Avatars").upload(
            path,
            raw_bytes,
            file_options={"content-type": mime_type, "upsert": "false"},
        )

        url = supabase.storage.from_("Avatars").get_public_url(path)
        return {"url": url, "storage": "supabase"}

    except HTTPException:
        raise
    except Exception as e:
        if len(body.imageData) <= 800_000:
            return {"url": body.imageData, "storage": "inline"}
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{course_id}")
def update_course(course_id: str, body: UpdateCourseBody):
    supabase = get_supabase()

    updates = {}
    if body.title is not None:
        updates["title"] = body.title
    if body.description is not None:
        updates["description"] = body.description
    if body.thumbnail is not None:
        updates["thumbnail"] = body.thumbnail
    if body.category is not None:
        updates["category"] = body.category

    if updates:
        supabase.table("courses").update(updates).eq("id", course_id).execute()

    response = supabase.table("courses").select("*").eq("id", course_id).single().execute()
    return map_course(response.data)


@router.get("/{course_id}/chapters")
def get_chapters(course_id: str):
    supabase = get_supabase()
    response = (
        supabase.table("course_chapters")
        .select("*")
        .eq("course_id", course_id)
        .order("sort_order")
        .execute()
    )
    return [map_chapter(c) for c in (response.data or [])]


@router.post("/{course_id}/chapters", status_code=201)
def create_chapter(course_id: str, body: CreateChapterBody):
    supabase = get_supabase()

    existing = (
        supabase.table("course_chapters").select("id").eq("course_id", course_id).execute()
    )
    sort_order = len(existing.data) if existing.data else 0

    chapter_data = {
        "course_id": course_id,
        "title": body.title,
        "video_url": body.videoUrl,
        "duration": body.duration,
        "sort_order": sort_order,
    }

    response = supabase.table("course_chapters").insert(chapter_data).execute()
    chapter = response.data[0]

    sync_course_module_label(supabase, course_id)

    return map_chapter(chapter)


@router.put("/{course_id}/chapters/{chapter_id}")
def update_chapter(course_id: str, chapter_id: str, body: UpdateChapterBody):
    supabase = get_supabase()

    updates = {}
    if body.title is not None:
        updates["title"] = body.title
    if body.videoUrl is not None:
        updates["video_url"] = body.videoUrl
    if body.duration is not None:
        updates["duration"] = body.duration

    if updates:
        supabase.table("course_chapters").update(updates).eq("id", chapter_id).execute()

    sync_course_module_label(supabase, course_id)

    response = supabase.table("course_chapters").select("*").eq("id", chapter_id).single().execute()
    return map_chapter(response.data)
