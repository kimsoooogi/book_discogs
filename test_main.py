import pytest
from httpx import ASGITransport, AsyncClient

# 테스트 환경에서는 실제 DB 대신 인메모리 SQLite 사용
import os
os.environ.setdefault("ALADIN_TTB_KEY", "test_key_for_ci")
os.environ.setdefault("ALLOWED_ORIGINS", "*")

from main import app  # noqa: E402
from database import Base, engine  # noqa: E402


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_library_empty():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/library")
        assert res.status_code == 200
        assert res.json() == []


@pytest.mark.anyio
async def test_add_and_get_book():
    book_payload = {
        "title": "파이썬 완벽 가이드",
        "author": "홍길동",
        "publisher": "한빛미디어",
        "pub_year": 2024,
        "isbn13": "9791234567890",
        "cover": None,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        post_res = await client.post("/library", json=book_payload)
        assert post_res.status_code == 201
        data = post_res.json()
        assert data["title"] == "파이썬 완벽 가이드"

        # 중복 추가 시 409
        dup_res = await client.post("/library", json=book_payload)
        assert dup_res.status_code == 409

        # 조회
        get_res = await client.get("/library")
        assert len(get_res.json()) == 1

        # 카운트
        count_res = await client.get("/library/count")
        assert count_res.json()["total_count"] == 1


@pytest.mark.anyio
async def test_update_status():
    book_payload = {
        "title": "테스트 도서", "author": "저자", "publisher": "출판사",
        "pub_year": 2023, "isbn13": "9790000000001", "cover": None,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        post_res = await client.post("/library", json=book_payload)
        book_id = post_res.json()["id"]

        patch_res = await client.patch(
            f"/library/{book_id}/status",
            json={"status": "reading"}
        )
        assert patch_res.status_code == 200
        assert patch_res.json()["new_status"] == "reading"


@pytest.mark.anyio
async def test_delete_book():
    book_payload = {
        "title": "삭제용 도서", "author": "저자", "publisher": "출판사",
        "pub_year": 2023, "isbn13": "9790000000002", "cover": None,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        post_res = await client.post("/library", json=book_payload)
        book_id = post_res.json()["id"]

        del_res = await client.delete(f"/library/{book_id}")
        assert del_res.status_code == 200

        get_res = await client.get(f"/library/{book_id}")
        assert get_res.status_code == 404


@pytest.mark.anyio
async def test_bulk_add():
    payload = {
        "items": [
            {"title": "도서A", "author": "저자A", "publisher": "출판A", "pub_year": 2021, "isbn": "9790000000010", "cover_url": None},
            {"title": "도서B", "author": "저자B", "publisher": "출판B", "pub_year": 2022, "isbn": "9790000000011", "cover_url": None},
        ]
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/library/bulk", json=payload)
        assert res.status_code == 201
        assert res.json()["skipped"] == 0

        # 재요청 시 전부 skip
        res2 = await client.post("/library/bulk", json=payload)
        assert res2.json()["skipped"] == 2
