"""Worker persistence layer (docs/m2-persistence-design.md sections 3-4).

PostgreSQL access for the worker side of the queue: enqueue (T0), claim and
reclaim (T1), and the lease/event helpers they need. It is deliberately
separate from the pure ``flowforge.domain`` and ``flowforge.engine`` packages,
which never import it (design section 7). SQLAlchemy Core only, with literal,
auditable SQL; no ORM.

M2.3: connection plumbing, ``enqueue_run`` (T0), ``claim_run`` (T1),
``ClaimedRun``, ``LeaseProof``, and ``append_event``.

M2.4: the fenced guard, the heartbeat service, ordinary and shutdown park, and
the graceful-shutdown primitives. T2-T4 belong to later stages.
"""
