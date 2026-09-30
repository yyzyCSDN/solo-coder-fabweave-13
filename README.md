# FabWeave Operations Lab

FabWeave is a dependency-light Python reference project for semiconductor-fab
operations.  Its production modules model queue-time protection, dispatch,
reticle reservations, recipe qualification, SPC, metrology sampling,
maintenance, chamber matching, holds, genealogy, chemical aging, cleanroom
excursions, demand control, rework, lot merging, equipment dedication,
contamination, takt, capacity calendars, route yield and shipment commitments.

The cross-domain workflow layer composes these services for material
readiness, dispatch-after-hold, qualified reservations and assignments,
metrology/SPC gates, containment, powered scheduling, rework release,
merge-to-commit and performance reporting.  It preserves domain rejection
reasons instead of replacing them with a generic success or failure.

Shared infrastructure supplies half-open time intervals, versioned policy
history, immutable decisions, quantity conservation and an append-only event
log with optimistic concurrency.  Domain services use these primitives to keep
event-time, idempotency and fail-closed behavior consistent.

The capacity calendar stacks line stops, holidays, shortened shifts and
temporary overtime onto the same day with an explicit per-minute precedence
(line stop > overtime > holiday > short shift > baseline).  Available minutes
are computed on half-open intervals, historical reads bind to the revision
effective at the read time, and a day that has already been observed cannot be
rewritten by a later publication without an explicit, audit-logged correction.

Run the tests with `python -m pytest -q`.
