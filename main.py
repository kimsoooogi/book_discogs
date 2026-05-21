import logging
import os
import re
import time
from contextlib import asynccontextmanager
from enum import Enum
from typing import List, Optional

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from database import AsyncSessionLocal, Base, BookRecord, engine

# --------------------------------------------------
# 환경 변수 및 로깅
# --------------------------------------------------

load_dotenv()
ALADIN_TTB_KEY = os.getenv("ALADIN_TTB_KEY")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

# --------------------------------------------------
# Rate Limiter
# --------------------------------------------------

limiter = Limiter(key_func=get_remote_address)

# --------------------------------------------------
# Lifespan (startup / shutdown)
# --------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("데이터베이스 테이블 준비 완료")
    yield
    await engine.dispose()
    logger.info("데이터베이스 엔진 종료")


# --------------------------------------------------
# FastAPI 앱
# --------------------------------------------------

app = FastAPI(title="Book Discogs System", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------
# 의존성 주입
# --------------------------------------------------


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# --------------------------------------------------
# Pydantic 스키마
# --------------------------------------------------


class BookItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    title: str = Field(..., alias="title")
    author: str = Field(..., alias="author")
    publisher: str = Field(..., alias="publisher")
    pub_year: int = Field(0, description="출판 연도")
    isbn: str = Field(..., alias="isbn13")
    cover_url: Optional[str] = Field(None, alias="cover")

    @field_validator("title", mode="before")
    @classmethod
    def clean_title(cls, v: str) -> str:
        return v.split(" - ")[0].strip()

    @field_validator("author", mode="before")
    @classmethod
    def clean_author(cls, v: str) -> str:
        return re.sub(r"\s*\(.*?\)\s*", "", v).strip()


class BookItemInternal(BaseModel):
    """내부 저장용 (alias 없이 isbn 필드명 통일)"""

    title: str
    author: str
    publisher: str
    pub_year: int = 0
    isbn: str
    cover_url: Optional[str] = None


class BookListRequest(BaseModel):
    items: List[BookItemInternal]


class BookResponse(BaseModel):
    id: int
    title: str
    author: str
    publisher: str
    pub_year: int
    isbn: str
    cover_url: Optional[str]
    status: str

    model_config = ConfigDict(from_attributes=True)


class BookStatus(str, Enum):
    WISHING = "wishing"
    READING = "reading"
    COMPLETED = "completed"


class StatusUpdateRequest(BaseModel):
    status: BookStatus = Field(..., description="wishing | reading | completed")


class SearchSort(str, Enum):
    ACCURACY = "Accuracy"
    PUBLISH_TIME = "PublishTime"
    SALES_POINT = "SalesPoint"


class SearchQueryType(str, Enum):
    KEYWORD = "Keyword"
    TITLE = "Title"
    AUTHOR = "Author"


# --------------------------------------------------
# 헬퍼
# --------------------------------------------------

ALADIN_BASE_URL = "http://www.aladin.co.kr/ttb/api/ItemSearch.aspx"


def _filter_and_parse(
    items: list,
    keyword: str,
    query_type: SearchQueryType,
    seen_isbns: set,
) -> list:
    """알라딘 API 결과를 정제·필터링하여 반환한다."""
    results = []
    search_kw = keyword.replace(" ", "").lower()

    for item in items:
        isbn = item.get("isbn13")
        if not isbn or isbn in seen_isbns:
            continue

        raw_title = item.get("title", "")
        raw_author = item.get("author", "")

        if query_type == SearchQueryType.TITLE:
            if search_kw not in raw_title.replace(" ", "").lower():
                continue
        if query_type == SearchQueryType.AUTHOR:
            if search_kw not in raw_author.replace(" ", "").lower():
                continue

        seen_isbns.add(isbn)
        pub_date_str = item.get("pubDate", "")
        extracted_year = int(pub_date_str.split("-")[0]) if pub_date_str else 0

        book = BookItem(
            title=raw_title,
            author=raw_author,
            publisher=item.get("publisher", ""),
            pub_year=extracted_year,
            isbn13=isbn,
            cover=item.get("cover"),
        )
        results.append(book.model_dump(by_alias=False))

    return results


# --------------------------------------------------
# 엔드포인트
# --------------------------------------------------


@app.get("/search", response_model=dict)
@limiter.limit("30/minute")
async def search_book(
    request,
    keyword: str = Query(..., min_length=1, description="검색어"),
    query_type: SearchQueryType = Query(SearchQueryType.KEYWORD),
    sort_by: SearchSort = Query(SearchSort.PUBLISH_TIME),
):
    if not ALADIN_TTB_KEY:
        raise HTTPException(status_code=500, detail="서버 환경 변수 누락 (ALADIN_TTB_KEY)")

    base_params = {
        "ttbkey": ALADIN_TTB_KEY,
        "Query": keyword,
        "QueryType": query_type.value,
        "MaxResults": 50,
        "SearchTarget": "Book",
        "Sort": sort_by.value,
        "output": "js",
        "Version": "20131101",
    }

    start_time = time.perf_counter()
    seen_isbns: set = set()
    results: list = []

    async with httpx.AsyncClient(timeout=7.0) as client:
        try:
            first_res = await client.get(ALADIN_BASE_URL, params={**base_params, "start": 1})
            first_res.raise_for_status()

            data = first_res.json()
            total_results = data.get("totalResults", 0)
            results.extend(_filter_and_parse(data.get("item", []), keyword, query_type, seen_isbns))

            max_pages = min(4, (total_results // 50) + 1)
            if max_pages > 1:
                import asyncio

                tasks = [
                    client.get(ALADIN_BASE_URL, params={**base_params, "start": p})
                    for p in range(2, max_pages + 1)
                ]
                responses = await asyncio.gather(*tasks, return_exceptions=True)
                for resp in responses:
                    if isinstance(resp, Exception) or resp.status_code != 200:
                        continue
                    results.extend(
                        _filter_and_parse(resp.json().get("item", []), keyword, query_type, seen_isbns)
                    )

            latency = time.perf_counter() - start_time
            logger.info(
                "검색 완료 | 결과=%d | 유형=%s | 정렬=%s | 지연=%.3fs",
                len(results),
                query_type.value,
                sort_by.value,
                latency,
            )
            return {"total_found": len(results), "items": results}

        except httpx.HTTPStatusError as e:
            logger.error("알라딘 API 오류: %s", e)
            raise HTTPException(status_code=502, detail="외부 API 응답 오류")
        except httpx.RequestError as e:
            logger.error("네트워크 오류: %s", e)
            raise HTTPException(status_code=503, detail="알라딘 API 서버 연결 실패")


@app.post("/library", status_code=201)
async def add_book(book: BookItem, db: AsyncSession = Depends(get_db)):
    start_time = time.perf_counter()
    try:
        result = await db.execute(select(BookRecord).filter(BookRecord.isbn == book.isbn))
        if result.scalars().first():
            raise HTTPException(status_code=409, detail="이미 라이브러리에 존재하는 도서입니다.")

        new_book = BookRecord(
            title=book.title,
            author=book.author,
            publisher=book.publisher,
            pub_year=book.pub_year,
            isbn=book.isbn,
            cover_url=book.cover_url,
        )
        db.add(new_book)
        await db.commit()
        await db.refresh(new_book)

        logger.info("도서 추가 | isbn=%s | %.3fs", new_book.isbn, time.perf_counter() - start_time)
        return {"message": "라이브러리 추가 완료", "id": new_book.id, "title": new_book.title}

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error("도서 추가 오류: %s", e)
        raise HTTPException(status_code=500, detail="데이터베이스 처리 중 오류가 발생했습니다.")


@app.post("/library/bulk", status_code=201)
async def add_books_bulk(payload: BookListRequest, db: AsyncSession = Depends(get_db)):
    """N+1 쿼리 제거: ISBN 목록을 한 번의 IN 쿼리로 일괄 조회한다."""
    start_time = time.perf_counter()
    try:
        incoming_isbns = [b.isbn for b in payload.items]

        # 기존 ISBN을 한 번의 쿼리로 조회
        result = await db.execute(
            select(BookRecord.isbn).where(BookRecord.isbn.in_(incoming_isbns))
        )
        existing_isbns = {row[0] for row in result}

        new_books = [
            BookRecord(
                title=b.title,
                author=b.author,
                publisher=b.publisher,
                pub_year=b.pub_year,
                isbn=b.isbn,
                cover_url=b.cover_url,
            )
            for b in payload.items
            if b.isbn not in existing_isbns
        ]

        db.add_all(new_books)
        await db.commit()

        logger.info(
            "일괄 추가 | 요청=%d | 추가=%d | %.3fs",
            len(payload.items),
            len(new_books),
            time.perf_counter() - start_time,
        )
        return {"message": f"{len(new_books)}건의 도서가 추가되었습니다.", "skipped": len(existing_isbns)}

    except Exception as e:
        await db.rollback()
        logger.error("일괄 추가 오류: %s", e)
        raise HTTPException(status_code=500, detail="데이터베이스 처리 중 오류가 발생했습니다.")


@app.get("/library/count", response_model=dict)
async def get_library_count(db: AsyncSession = Depends(get_db)):
    try:
        result = await db.execute(select(func.count(BookRecord.id)))
        return {"total_count": result.scalar()}
    except Exception as e:
        logger.error("카운트 조회 오류: %s", e)
        raise HTTPException(status_code=500, detail="데이터베이스 조회 중 오류가 발생했습니다.")


@app.get("/library", response_model=List[BookResponse])
async def get_library(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    try:
        result = await db.execute(select(BookRecord).offset(skip).limit(limit))
        return result.scalars().all()
    except Exception as e:
        logger.error("라이브러리 조회 오류: %s", e)
        raise HTTPException(status_code=500, detail="데이터베이스 조회 중 오류가 발생했습니다.")


@app.get("/library/{book_id}", response_model=BookResponse)
async def get_book(book_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(BookRecord).filter(BookRecord.id == book_id))
    book = result.scalars().first()
    if not book:
        raise HTTPException(status_code=404, detail="해당 ID의 도서를 찾을 수 없습니다.")
    return book


@app.patch("/library/{book_id}/status", response_model=dict)
async def update_book_status(
    book_id: int,
    payload: StatusUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        result = await db.execute(select(BookRecord).filter(BookRecord.id == book_id))
        book = result.scalars().first()
        if not book:
            raise HTTPException(status_code=404, detail="해당 ID의 도서를 찾을 수 없습니다.")

        old_status = book.status
        book.status = payload.status.value
        await db.commit()
        await db.refresh(book)

        logger.info("상태 변경 | id=%d | %s → %s", book_id, old_status, book.status)
        return {
            "message": "도서 상태가 변경되었습니다.",
            "book_id": book.id,
            "title": book.title,
            "new_status": book.status,
        }

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error("상태 변경 오류: %s", e)
        raise HTTPException(status_code=500, detail="데이터베이스 처리 중 오류가 발생했습니다.")


@app.delete("/library/{book_id}", status_code=200)
async def delete_book(book_id: int, db: AsyncSession = Depends(get_db)):
    try:
        result = await db.execute(select(BookRecord).filter(BookRecord.id == book_id))
        book = result.scalars().first()
        if not book:
            raise HTTPException(status_code=404, detail="해당 ID의 도서를 찾을 수 없습니다.")

        await db.delete(book)
        await db.commit()
        logger.info("도서 삭제 | id=%d | title=%s", book_id, book.title)
        return {"message": "도서가 삭제되었습니다.", "book_id": book_id}

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error("도서 삭제 오류: %s", e)
        raise HTTPException(status_code=500, detail="데이터베이스 처리 중 오류가 발생했습니다.")
