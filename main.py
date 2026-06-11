import logging

from dotenv import load_dotenv

load_dotenv()

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api import api_router
from app.core.config import get_settings
from app.core.redis_client import get_redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.is_supabase_configured():
        logger.info("Supabase configured — URL: %s", settings.supabase_url)
    else:
        logger.warning("Supabase NOT configured — endpoints que usen DB fallarán")
    yield


app = FastAPI(title="Comunyapp API", version="1.0.0", lifespan=lifespan)

# Railway usa un proxy que pasa X-Forwarded-Proto: https
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"error": str(exc)})


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url)
    return JSONResponse(status_code=500, content={"error": str(exc) or "Internal server error"})


app.include_router(api_router, prefix="/api")


@app.get("/")
def health():
    settings = get_settings()
    return {
        "status": "ok",
        "supabase": settings.is_supabase_configured(),
        "redis": get_redis() is not None,
    }


if __name__ == "__main__":
    import uvicorn
    port = get_settings().port
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
