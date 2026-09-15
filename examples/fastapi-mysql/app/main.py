"""A small FastAPI service backed by MySQL.

Deliberately realistic: it reads configuration from the environment, talks to a
database over Compose's service-name DNS, and exposes /health so an orchestrator
can tell "process started" apart from "actually ready".
"""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Column, Integer, String, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    db_host: str = "mysql"
    db_port: int = 3306
    db_name: str = "appdb"
    db_user: str = "appuser"
    db_password: str = ""
    app_port: int = 8000
    log_level: str = "INFO"
    items_page_size: int = 25

    @property
    def database_url(self) -> str:
        return (
            f"mysql+pymysql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


settings = Settings()
logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("api")

# pool_pre_ping keeps the app alive across database restarts, which matters a
# lot more under Kubernetes than it does under Compose.
engine = create_engine(settings.database_url, pool_pre_ping=True, pool_recycle=280)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)


class ItemIn(BaseModel):
    name: str


class ItemOut(BaseModel):
    id: int
    name: str


app = FastAPI(title="FastAPI + MySQL example")


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.on_event("startup")
def create_schema() -> None:
    try:
        Base.metadata.create_all(engine)
        logger.info("Database schema ready")
    except SQLAlchemyError as exc:
        # Do not crash on startup: the database may still be coming up.
        logger.warning("Schema creation deferred: %s", exc)


@app.get("/health")
def health() -> dict[str, str]:
    """Readiness: reports unhealthy while the database is unreachable."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        logger.warning("Health check failed: %s", exc)
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "healthy", "database": "connected"}


@app.get("/items", response_model=list[ItemOut])
def list_items(db: Session = Depends(get_db)) -> list[Item]:
    return db.query(Item).limit(settings.items_page_size).all()


@app.post("/items", response_model=ItemOut, status_code=201)
def create_item(payload: ItemIn, db: Session = Depends(get_db)) -> Item:
    item = Item(name=payload.name)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item
