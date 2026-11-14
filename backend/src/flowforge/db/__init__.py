"""PostgreSQL persistence foundation (M2.1).

SQLAlchemy Core only -- no ORM -- over psycopg 3, with Alembic migrations
(docs/m2-persistence-design.md D2). This package holds connection/engine
configuration, application-side ID generation, and the migration runner. It has
no business logic: queue claiming, leases, fencing, and finalization belong to
later M2 stages and live in a separate worker package. The pure execution
engine (``flowforge.engine``) and domain model (``flowforge.domain``) never
import from here.

The schema's single source of truth is the Alembic migration under
``flowforge.db.migrations``; there are deliberately no parallel
``sqlalchemy.Table`` definitions.
"""
