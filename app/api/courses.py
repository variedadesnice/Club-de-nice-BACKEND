from fastapi import APIRouter, Depends

from app.core.deps import get_current_user
from app.schemas.courses import (
    CreateChapterRequest,
    CreateCourseRequest,
    ThumbnailUploadRequest,
    UpdateChapterRequest,
    UpdateCourseRequest,
)
from app.services import courses as courses_service

router = APIRouter()


@router.get("/")
def get_courses():
    return courses_service.get_courses()


@router.post("/", status_code=201)
def create_course(body: CreateCourseRequest, current_user: dict = Depends(get_current_user)):
    return courses_service.create_course(
        body.title, body.description, body.thumbnail,
        current_user["id"], body.category,
    )


@router.post("/thumbnail")
def upload_thumbnail(body: ThumbnailUploadRequest, current_user: dict = Depends(get_current_user)):
    return courses_service.upload_thumbnail(body.imageData)


@router.put("/{course_id}")
def update_course(course_id: str, body: UpdateCourseRequest, current_user: dict = Depends(get_current_user)):
    return courses_service.update_course(
        course_id, body.title, body.description, body.thumbnail, body.category,
    )


@router.delete("/{course_id}")
def delete_course(course_id: str, current_user: dict = Depends(get_current_user)):
    return courses_service.delete_course(course_id)


@router.get("/{course_id}/chapters")
def get_chapters(course_id: str):
    return courses_service.get_chapters(course_id)


@router.post("/{course_id}/chapters", status_code=201)
def create_chapter(course_id: str, body: CreateChapterRequest, current_user: dict = Depends(get_current_user)):
    return courses_service.create_chapter(course_id, body.title, body.videoUrl, body.duration)


@router.put("/{course_id}/chapters/{chapter_id}")
def update_chapter(course_id: str, chapter_id: str, body: UpdateChapterRequest, current_user: dict = Depends(get_current_user)):
    return courses_service.update_chapter(course_id, chapter_id, body.title, body.videoUrl, body.duration)
