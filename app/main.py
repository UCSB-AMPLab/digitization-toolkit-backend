from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# initialize DB tables
from app.core.db import init_db

# routers
from app.api.documents import router as documents_router
from app.api.cameras import router as cameras_router

app = FastAPI()

# Initialize database tables on startup


@app.on_event("startup")
def on_startup():
    init_db()

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


@app.get("/health")
def health():
    return {"status": "ok"}
