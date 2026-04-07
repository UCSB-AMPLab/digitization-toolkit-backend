from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

# initialize DB tables
from app.core.db import init_db

# routers
from app.api.records import router as records_router
from app.api.cameras import router as cameras_router
from app.api.projects import router as projects_router
from app.api.collections import router as collections_router
from app.api.auth import router as auth_router
from app.api.system import router as system_router

# Define lifespan event to initialize the database
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

# Create FastAPI app with lifespan
app = FastAPI(lifespan=lifespan)

# Allow the Svelte frontend to call the API
# :5173 = Vite dev server, :3000 = production Node server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(records_router, prefix="/records", tags=["records"])
app.include_router(cameras_router, prefix="/cameras", tags=["cameras"])
app.include_router(projects_router, prefix="/projects", tags=["projects"])
app.include_router(collections_router, prefix="/collections", tags=["collections"])
app.include_router(auth_router, prefix="/auth", tags=["auth"])
app.include_router(system_router, prefix="/system", tags=["system"])


@app.get("/health")
def health():
    return {"status": "ok"}
