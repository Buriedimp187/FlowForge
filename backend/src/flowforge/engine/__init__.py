"""The pure execution engine.

Per ADR-0001, this package depends only on ``flowforge.domain`` and on the
abstract ports declared in ``flowforge.engine.ports``. It never imports
FastAPI, SQLAlchemy, or any live database/HTTP client. All state needed by
engine functions is passed in explicitly (a definition, a run id, a
``RunStore``, a ``Clock``) -- there is no hidden global state anywhere in
this package.
"""

__all__: list[str] = []
