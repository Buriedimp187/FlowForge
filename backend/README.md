# FlowForge backend

Python backend. M1 is the pure domain and execution engine; M2.1 adds the
PostgreSQL persistence foundation (`flowforge.db`, SQLAlchemy Core + Alembic).
The schema's source of truth is the Alembic migration under
`src/flowforge/db/migrations/versions/`.

## Setup

Requires Python 3.14 or newer (`uuid.uuid7()`).

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-lock.txt -r requirements-dev-lock.txt
```

## Tests

Run from `backend/` with `PYTHONPATH=src` (use `src;.` on Windows if running a
sub-directory with `-t .`).

```bash
python -m unittest discover -s tests -t . -p "test_*.py"
```

The integration tests need PostgreSQL 17 and a Docker daemon (Testcontainers
starts the digest-pinned image), or an existing server:

| Variable | Meaning |
| --- | --- |
| `FLOWFORGE_TEST_DATABASE_URL` | admin URL of an existing server (used by CI); each test class gets its own temporary database |
| `FLOWFORGE_REQUIRE_DB_TESTS=1` | fail instead of skipping when no database is available |

## Migrations

```bash
FLOWFORGE_DATABASE_URL=postgresql+psycopg://user:pw@host/db alembic -c alembic.ini upgrade head
```
