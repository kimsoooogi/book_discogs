from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, func
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker


def _utcnow():
    return datetime.now(timezone.utc)


SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite:///./library.db"

engine = create_async_engine(SQLALCHEMY_DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()


class BookRecord(Base):
    __tablename__ = "books"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    title = Column(String, index=True, nullable=False)
    author = Column(String, nullable=False)
    publisher = Column(String, nullable=False)
    pub_year = Column(Integer, nullable=False, default=0)
    isbn = Column(String, unique=True, index=True, nullable=False)
    cover_url = Column(String, nullable=True)
    status = Column(String, default="wishing", nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )
