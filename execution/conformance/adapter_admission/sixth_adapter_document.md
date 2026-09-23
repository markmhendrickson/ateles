# The sixth adapter (admission fixture reference document)

Fixture-only document satisfying `docs/foundation/adapters.md#what-an-adapters-document-must-contain`'s
eight required parts, used by AD-32's structure lint (`test_ad31_ad32.py`). Not a production adapter
document — synthetic system, synthetic credentials only.

## Scope, and the enumeration's boundary

Covers the fixture's synthetic delivery kinds: `conclusion`, `event`, `check_result`. No other surface
of the fixture system exists.

## The inbound table

| Event | Status | Outcome |
|---|---|---|
| conclusion (cred-owner) | handled | verdict |
| conclusion (cred-none / cred-other) | handled | observation |
| event (mapped) | handled | observation |
| event (unmapped) | handled | dropped, reason unmapped |

## The identity section

`cred-owner` binds to the step owner; `cred-none` and `cred-other` bind to no principal and never
resolve to the operator.

## The linkage section

`system` is `admission-fixture`; `external_id` is the delivery's `external_id`.

## The outbound table

| Step | Operation | Action class | Confirmation |
|---|---|---|---|
| comment | take_outbound | comment | `taken=True`, read back on the attempt |

## Recoveries

Forward-only: no recovery action defined for this fixture's synthetic classes.

## What this adapter never does, at this system specifically

Never resolves an unrecognized credential to the operator; never keeps a `last_seen` cursor or
sync log; never acknowledges before read-back.

## What the document does not decide

General rules (identity, dedup, provenance) are cited from `docs/foundation/adapters.md` and not
restated here.
