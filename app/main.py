from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

# initialize DB tables
from app.core.db import init_db

# routers
from app.api.documents import router as documents_router
from app.api.cameras import router as cameras_router
from app.api.projects import router as projects_router
from app.api.auth import router as auth_router

# Define lifespan event to initialize the database
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI()

# Create FastAPI app with lifespan
app = FastAPI(lifespan=lifespan)

# Allow the Svelte dev server to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(documents_router, prefix="/documents", tags=["documents"])
app.include_router(cameras_router, prefix="/cameras", tags=["cameras"])
app.include_router(projects_router, prefix="/projects", tags=["projects"])
app.include_router(auth_router, prefix="/auth", tags=["auth"])


@app.get("/health")
def health():
    return {"status": "ok"}
