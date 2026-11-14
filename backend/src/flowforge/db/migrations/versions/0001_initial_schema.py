"""M2.1 initial schema: workflows, workflow_versions, runs, node_runs, run_events.

The DDL below is docs/m2-persistence-design.md section 2, which is the approved
specification (docs/architecture.md sections 2.3, 3-6). Notes that are easy to
lose:

* Primary-key ``id`` columns have NO ``DEFAULT``: ids are application-generated
  UUIDv7 (design D1, ``flowforge.db.ids.new_id``). ``gen_random_uuid()`` would
  produce UUIDv4 on PostgreSQL 17 and is deliberately not used.
* Exactly five tables. ``admin_user``, ``sessions``, ``credentials``,
  ``webhook_endpoints`` and ``app_settings`` belong to M4 and are NOT created.
* ``runs`` is the queue itself: no separate queue table.
* ``started_epoch`` and the due-retry/``retry_wait`` constraints implement the
  fencing and orphan model of design sections 4.3 and 4.4.
* The ``effect_class`` immutability trigger enforces architecture section 5.2
  inside the database, independent of application discipline.

Statements are executed through ``sa.text``. None contains a colon followed by
a word character (which ``text`` would read as a bind parameter); the single
``%`` characters in the PL/pgSQL ``RAISE`` are escaped by SQLAlchemy for the
driver.
"""

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_UPGRADE = (
    # -- workflows -----------------------------------------------------------
    """
    CREATE TABLE workflows (
        id                uuid PRIMARY KEY,
        name              text NOT NULL,
        active_version_id uuid,
        webhook_enabled   boolean NOT NULL DEFAULT false,
        created_at        timestamptz NOT NULL DEFAULT now()
    )
    """,
    # -- workflow_versions (immutable definition rows) -----------------------
    """
    CREATE TABLE workflow_versions (
        id             uuid PRIMARY KEY,
        workflow_id    uuid NOT NULL REFERENCES workflows (id),
        version_number integer NOT NULL,
        schema_version integer NOT NULL,
        definition     jsonb NOT NULL,
        created_at     timestamptz NOT NULL DEFAULT now(),
        UNIQUE (workflow_id, version_number),
        UNIQUE (workflow_id, id)
    )
    """,
    # Active-version integrity: the active version must belong to its OWN
    # workflow. A NULL active_version_id satisfies this under MATCH SIMPLE.
    """
    ALTER TABLE workflows
        ADD CONSTRAINT fk_active_version_belongs_to_workflow
        FOREIGN KEY (id, active_version_id)
        REFERENCES workflow_versions (workflow_id, id)
    """,
    # -- runs: the queue and the run record ----------------------------------
    """
    CREATE TABLE runs (
        id                  uuid PRIMARY KEY,
        workflow_version_id uuid NOT NULL REFERENCES workflow_versions (id),
        status              text NOT NULL
            CHECK (status IN ('queued', 'waiting', 'running',
                              'succeeded', 'failed', 'cancelled', 'timed_out')),
        visible_at          timestamptz NOT NULL,
        lease_owner         text,
        lease_epoch         bigint NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
        recovery_count      integer NOT NULL DEFAULT 0,
        cancel_requested_at timestamptz,
        queue_deadline_at   timestamptz NOT NULL,
        deadline_at         timestamptz,
        started_at          timestamptz,
        finished_at         timestamptz,
        error               jsonb,
        has_unknown_outcome boolean NOT NULL DEFAULT false,
        trigger_payload     jsonb NOT NULL,
        idempotency_key     text,
        created_at          timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT lease_owner_matches_status CHECK (
            (status = 'running' AND lease_owner IS NOT NULL) OR
            (status <> 'running' AND lease_owner IS NULL)
        ),
        UNIQUE (workflow_version_id, idempotency_key)
    )
    """,
    # One partial index serves claim, reclaim and park wake-up uniformly;
    # terminal runs fall out of it automatically (architecture section 4.1).
    """
    CREATE INDEX runs_claimable_idx ON runs (visible_at)
        WHERE status IN ('queued', 'waiting', 'running')
    """,
    # -- node_runs: durable node-granular execution state --------------------
    """
    CREATE TABLE node_runs (
        run_id          uuid NOT NULL REFERENCES runs (id),
        node_key        text NOT NULL,
        status          text NOT NULL
            CHECK (status IN ('running', 'retry_wait', 'succeeded',
                              'failed', 'skipped', 'cancelled')),
        attempt         integer NOT NULL DEFAULT 1,
        failures        integer NOT NULL DEFAULT 0,
        outcome         text NOT NULL DEFAULT 'known'
            CHECK (outcome IN ('known', 'unknown')),
        effect_class    text
            CHECK (effect_class IN ('pure', 'read', 'idempotent_write', 'unsafe_write')),
        started_epoch   bigint,
        started_at      timestamptz,
        retry_at        timestamptz,
        resolved_params jsonb,
        output          jsonb,
        error           jsonb,
        item_results    jsonb,
        PRIMARY KEY (run_id, node_key),
        CHECK (status = 'skipped' OR effect_class IS NOT NULL),
        CONSTRAINT retry_wait_requires_retry_at
            CHECK (status <> 'retry_wait' OR retry_at IS NOT NULL)
    )
    """,
    # effect_class is resolved once, at a node's first start, and immutable
    # thereafter (architecture section 5.2): enforced here, not in the app.
    """
    CREATE FUNCTION enforce_effect_class_immutable() RETURNS trigger AS $$
    BEGIN
        IF NEW.effect_class IS DISTINCT FROM OLD.effect_class THEN
            RAISE EXCEPTION 'effect_class is immutable for (run_id=%, node_key=%): % -> %',
                OLD.run_id, OLD.node_key, OLD.effect_class, NEW.effect_class;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE TRIGGER node_runs_effect_class_immutable
        BEFORE UPDATE ON node_runs
        FOR EACH ROW EXECUTE FUNCTION enforce_effect_class_immutable()
    """,
    # -- run_events: written only by the fenced lease holder -----------------
    """
    CREATE TABLE run_events (
        run_id   uuid NOT NULL REFERENCES runs (id),
        seq      bigint NOT NULL,
        level    text NOT NULL CHECK (level IN ('debug', 'info', 'warning', 'error')),
        node_key text,
        message  text NOT NULL,
        ts       timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (run_id, seq)
    )
    """,
)

# Reverse order of creation. The two tables reference each other
# (workflows.active_version_id <-> workflow_versions.workflow_id), so the
# composite foreign key is dropped before either table.
_DOWNGRADE = (
    "DROP TABLE run_events",
    "DROP TRIGGER node_runs_effect_class_immutable ON node_runs",
    "DROP FUNCTION enforce_effect_class_immutable()",
    "DROP TABLE node_runs",
    "DROP INDEX runs_claimable_idx",
    "DROP TABLE runs",
    "ALTER TABLE workflows DROP CONSTRAINT fk_active_version_belongs_to_workflow",
    "DROP TABLE workflow_versions",
    "DROP TABLE workflows",
)


def upgrade() -> None:
    for statement in _UPGRADE:
        op.execute(sa.text(statement))


def downgrade() -> None:
    for statement in _DOWNGRADE:
        op.execute(sa.text(statement))
