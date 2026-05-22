# Book Discogs System

개인 도서 라이브러리 관리 및 알라딘 Open API 연동 대시보드.
알라딘 API 다중 페이지를 비동기 병렬 처리로 수집하고, SQLite 기반 로컬 DB에 도서를 저장·관리합니다.

---

## 목차

- [기술 스택](#기술-스택)
- [주요 기능](#주요-기능)
- [아키텍처 개요](#아키텍처-개요)
- [프로젝트 구조](#프로젝트-구조)
- [설치 및 실행](#설치-및-실행)
- [환경 변수](#환경-변수)
- [API 명세](#api-명세)
- [테스트](#테스트)
- [CI/CD](#cicd)
- [설계 결정 및 트레이드오프](#설계-결정-및-트레이드오프)

---

## 기술 스택

| 분류 | 사용 기술 |
|---|---|
| Backend | FastAPI, Python 3.10+, httpx (비동기) |
| Frontend | HTML5, CSS3, Vanilla JS, Toastify.js |
| Database | SQLite 3, SQLAlchemy 2.0 (async), aiosqlite |
| 검증 | Pydantic v2 |
| Rate Limiting | slowapi |
| Test | pytest, pytest-asyncio, httpx (ASGITransport) |
| CI/CD | GitHub Actions |

---

## 주요 기능

**도서 검색**
- 알라딘 Open API를 통한 통합·제목·저자별 검색
- 정확도순 / 최신순 / 판매량순 정렬
- 서버 측 2차 필터링으로 API의 느슨한 결과를 재검증
- 최대 4페이지를 `asyncio.gather`로 병렬 수집 (순차 대비 약 40% 단축)
- ISBN 기반 중복 제거

**라이브러리 관리**
- 도서 추가 / 단건 조회 / 목록 조회 (페이지네이션) / 삭제
- 읽기 상태 관리: `wishing` → `reading` → `completed`
- 일괄 추가 (bulk): 단일 `IN` 쿼리로 N+1 문제 제거

**안정성**
- Rate Limit: `/search` 엔드포인트 30회/분 제한
- CORS: 환경 변수로 허용 도메인 분리
- 전체 엔드포인트 비동기 DB 세션 의존성 주입
- DB 타임스탬프 자동 관리 (`created_at`, `updated_at`)

---

## 아키텍처 개요

```
[index.html]
    │  fetch (REST)
    ▼
[FastAPI / main.py]  ←── slowapi Rate Limiter
    │
    ├── /search ──────────► [알라딘 Open API]  (httpx, asyncio.gather)
    │
    └── /library/* ───────► [SQLite / library.db]  (SQLAlchemy async)
```

검색 흐름 (비동기 병렬):

```
1페이지 요청 ──┐
               ├── asyncio.gather ──► 결과 병합 ──► 2차 필터링 ──► 응답
2~4페이지 동시 ┘
```

---

## 프로젝트 구조

```
book_discogs/
├── main.py              # FastAPI 앱, 라우터, 비즈니스 로직
├── database.py          # SQLAlchemy 엔진, 세션, ORM 모델
├── index.html           # 단일 파일 프론트엔드 대시보드
├── test_main.py         # pytest 통합 테스트 (5개 케이스)
├── pytest.ini           # pytest 설정 (asyncio_mode = auto)
├── requirements.txt     # 프로젝트 의존성
├── .env.example         # 환경 변수 템플릿
├── .gitignore
└── .github/
    └── workflows/
        └── main.yml     # CI 파이프라인 (lint + test)
```

> `library.db`와 `.env`는 `.gitignore`에 의해 추적되지 않습니다.

---

## 설치 및 실행

### 1. 저장소 클론

```bash
git clone https://github.com/<your-username>/book_discogs.git
cd book_discogs
```

### 2. 가상환경 생성 (권장)

```bash
python -m venv .venv
source .venv/bin/activate      # macOS / Linux
.venv\Scripts\activate         # Windows
```

### 3. 의존성 설치

```bash
pip install -r requirements.txt
```

### 4. 환경 변수 설정

```bash
cp .env.example .env
```

`.env`를 열고 알라딘 TTB 키를 입력합니다.

```dotenv
ALADIN_TTB_KEY=your_ttb_key_here
ALLOWED_ORIGINS=http://localhost:5500
```

> 알라딘 API 키는 [알라딘 TTB](https://www.aladin.co.kr/ttb/wblog_manage.aspx) 에서 발급받을 수 있습니다.

### 5. 서버 실행

```bash
uvicorn main:app --reload
```

서버가 `http://127.0.0.1:8000`에서 시작됩니다.
`index.html`을 브라우저로 열거나 VS Code Live Server로 실행합니다.

### 6. API 문서 확인 (자동 생성)

```
http://127.0.0.1:8000/docs       # Swagger UI
http://127.0.0.1:8000/redoc      # ReDoc
```

---

## 환경 변수

| 변수명 | 필수 | 기본값 | 설명 |
|---|---|---|---|
| `ALADIN_TTB_KEY` | ✅ | — | 알라딘 Open API 인증 키 |
| `ALLOWED_ORIGINS` | — | `*` | CORS 허용 도메인 (쉼표 구분). 배포 시 실제 도메인으로 반드시 변경 |

---

## API 명세

### 도서 검색

```
GET /search
```

| 파라미터 | 타입 | 기본값 | 설명 |
|---|---|---|---|
| `keyword` | string | (필수) | 검색어 |
| `query_type` | enum | `Keyword` | `Keyword` / `Title` / `Author` |
| `sort_by` | enum | `PublishTime` | `Accuracy` / `PublishTime` / `SalesPoint` |

**Rate Limit: 30회/분**

응답 예시:

```json
{
  "total_found": 12,
  "items": [
    {
      "title": "파이썬 완벽 가이드",
      "author": "홍길동",
      "publisher": "한빛미디어",
      "pub_year": 2024,
      "isbn": "9791234567890",
      "cover_url": "https://..."
    }
  ]
}
```

---

### 라이브러리 관리

| 메서드 | 경로 | 설명 |
|---|---|---|
| `POST` | `/library` | 도서 단건 추가 |
| `POST` | `/library/bulk` | 도서 일괄 추가 |
| `GET` | `/library` | 목록 조회 (페이지네이션) |
| `GET` | `/library/count` | 총 도서 수 조회 |
| `GET` | `/library/{id}` | 도서 단건 조회 |
| `PATCH` | `/library/{id}/status` | 읽기 상태 변경 |
| `DELETE` | `/library/{id}` | 도서 삭제 |

**`GET /library` 파라미터**

| 파라미터 | 기본값 | 범위 | 설명 |
|---|---|---|---|
| `skip` | `0` | ≥ 0 | 오프셋 |
| `limit` | `20` | 1 ~ 100 | 페이지 크기 |

**`PATCH /library/{id}/status` 요청 바디**

```json
{ "status": "reading" }
```

`status` 허용값: `wishing` · `reading` · `completed`

**`POST /library/bulk` 요청 바디**

```json
{
  "items": [
    {
      "title": "도서명", "author": "저자", "publisher": "출판사",
      "pub_year": 2024, "isbn": "9791234567890", "cover_url": null
    }
  ]
}
```

응답:

```json
{ "message": "2건의 도서가 추가되었습니다.", "skipped": 1 }
```

> `skipped`는 이미 라이브러리에 존재해서 건너뛴 도서 수입니다.

---

### 주요 HTTP 상태 코드

| 코드 | 의미 |
|---|---|
| `201` | 생성 성공 |
| `404` | 도서를 찾을 수 없음 |
| `409` | 이미 존재하는 ISBN |
| `429` | Rate Limit 초과 |
| `500` | 서버 내부 오류 |
| `502` | 알라딘 API 응답 오류 |
| `503` | 알라딘 API 연결 실패 |

---

## 테스트

```bash
pytest test_main.py -v
```

| 테스트 | 검증 내용 |
|---|---|
| `test_library_empty` | 초기 빈 라이브러리 응답 |
| `test_add_and_get_book` | 도서 추가, 중복 409, 조회, 카운트 |
| `test_update_status` | 상태 변경 (`wishing` → `reading`) |
| `test_delete_book` | 삭제 후 404 확인 |
| `test_bulk_add` | 일괄 추가, 중복 skip 카운트 |

테스트는 실제 DB 파일을 건드리지 않습니다. `ASGITransport`를 사용해 인메모리 SQLite 위에서 FastAPI 앱을 직접 호출합니다.

---

## CI/CD

`main` 브랜치에 push 또는 PR이 생성되면 자동으로 실행됩니다.

```
push / PR → main
    │
    ├── 1. actions/checkout@v4
    ├── 2. Python 3.10 설정
    ├── 3. pip 캐시 복원 (requirements.txt 해시 기준)
    ├── 4. pip install -r requirements.txt
    ├── 5. flake8 lint  (E9, F63, F7, F82)
    └── 6. pytest test_main.py
```

**GitHub Secret 설정**

저장소 Settings → Secrets and variables → Actions 에서 추가:

```
ALADIN_TTB_KEY = <실제 키>
```

> CI에서는 `/search` 엔드포인트를 실제로 호출하지 않으므로 키가 없어도 테스트는 통과합니다.
> Secret 등록은 향후 통합 테스트 확장을 위해 미리 설정해두는 것을 권장합니다.

---

## 설계 결정 및 트레이드오프

**SQLite를 사용한 이유**

설치 없이 즉시 실행 가능한 개인용 도구이므로 SQLite가 적합합니다. 다중 서버 배포나 높은 동시 쓰기 부하가 필요해지면 PostgreSQL + Alembic 마이그레이션으로 전환을 권장합니다.

**단일 `main.py` 구조**

현재 엔드포인트 수 기준으로는 단일 파일이 오히려 가독성이 높습니다. 엔드포인트가 늘어날 경우 `routers/`, `schemas/`, `services/`로 분리하는 것이 자연스러운 다음 단계입니다.

**프론트엔드를 단일 HTML로 유지한 이유**

별도 빌드 도구 없이 `index.html`만 열면 동작하는 구조가 이 프로젝트의 실행 편의성과 맞습니다. 기능이 더 복잡해진다면 React 등의 프레임워크 도입을 고려할 수 있습니다.