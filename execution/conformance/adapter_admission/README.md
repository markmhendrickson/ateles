# Adapter admission conformance fixture (AD-21 to AD-34)

`docs/foundation/conformance_suite.md#adapter-admission` specifies one reference ("sixth") adapter
that satisfies all six admission obligations (AD-21 to AD-26 green), and six negative variants —
each required to turn exactly its mapped row(s) red. This fixture executes that specification and
is **self-checking**: a variant that turns zero rows red (no failing artefact) or more than the
mapped set (rows coupled) is itself a reported failure, never a silent pass.

## Invoke

```
python3 execution/scripts/check_adapter_admission.py [--filter SELECTOR] [SELECTOR]
```

This is the **primary and today the only** invoke surface, wired into `scripts/lint.sh` and
`.github/workflows/foundation-checks.yml` beside the other `check_foundation_*.py` checks — never a
standalone CLI brand. Never named `connector test`, `smoke test`, `sync test`, or `cursor check`;
the vocabulary is `AD-NN`, `obligation N`, `sixth`/`reference` adapter, `negative variant`,
`self-check`, `dropped`/`unmapped`, `window`.

**Filter vocabulary** (positional or `--filter`, greppable as `adapter-admission` or `AD-21`):

| Selector | Selects |
|---|---|
| `adapter-admission` (default) | every row: reference pass, all six variants, AD-27..34 |
| `AD-21` | one row by id |
| `3` | obligation 3's mapped row(s) plus its AD-23 row |

**Precondition:** the sixth reference adapter must be importable from `lib.adapters`
(ateles#1190). That runtime does not exist on this checkout yet — running the command above
against real `main` today reports `RUNTIME_MISSING` and exits non-zero, which is the correct,
specified behaviour (never a soft skip-green). See the error-hint table below.

## Expected-output examples

### (a) Reference pass, all green

Once `lib.adapters` exists and exposes the sixth, the reference rows report:

```
AD-21	green
AD-22	green
AD-23	green
AD-24	green
AD-25	green
AD-26	green
```

### (b) One negative variant, exactly one row red

Obligation 1's variant (drop counter unwired) turns exactly `AD-21` red and nothing else:

```
AD-21	red	variant=1
```

A variant that turned `AD-21` **and** any other row red would instead report
`SELF_CHECK_MULTI_RED` (see below) — turning exactly the mapped set red is the pass condition,
not merely turning something red.

### (c) A deliberate self-check failure

If the obligation-1 mutant's failing artefact is accidentally fixed (the drop counter re-wired),
the variant turns zero rows red — the fixture reports this as a first-class failure block, not a
silent pass:

```
AD-21	green	variant=1
SELF_CHECK_ZERO_RED: variant for obligation 1 (drop counter unwired; an unmapped event logged/discarded without counted dropped/unmapped) turned no rows red | next: obligation 1 has no failing artefact — fix the mutant or the instrument | rows=AD-21 | variant=1 | expected_red={AD-21} observed_red={}
```

## Error-hint table

| Code | When | Hint |
|---|---|---|
| `RUNTIME_MISSING` | `lib.adapters` (ateles#1190) not importable, or missing `SIXTH_ADAPTER_FACTORY` | land ateles#1190 first; never skip-green |
| `EMPTY_SELECTION` | filter matched zero rows | use `adapter-admission`, an `AD-NN` row id, or an obligation number 1-6 |
| `SELF_CHECK_ZERO_RED` | a variant turned no rows red | names the variant, obligation, and expected row id(s); no failing artefact |
| `SELF_CHECK_MULTI_RED` | a variant turned more rows red than mapped | names expected vs. observed red sets |
| `REFERENCE_RED` | the sixth turned any of AD-21..26 red | names the row id(s); check ateles#1190's implementation before blaming the harness |
| `ROW_UNEXPECTED` | a variant's observed red set is wrong but not simply "more" (or AD-31/AD-32 lint fails) | names expected vs. observed |
| `INSTRUMENT_MISSING` | AD-27..30 or AD-33..34 selected; no fixture exists in-repo yet | out of scope for ateles#1191 (see #921/#963); never a silent skip |
| `SURFACE_PARITY` | a future second invoke surface disagrees with this one | names both surfaces and the differing row/self-check |

## What AD-27..34 report today

No grant/workflow/provenance fixtures exist in this repo. Selecting any of `AD-27`, `AD-28`,
`AD-29`, `AD-30`, `AD-33`, `AD-34` reports `INSTRUMENT_MISSING` — the row is never omitted from the
catalog and never passes by skip. Expanding these instruments is out of scope here (see #921/#963).

## Related

- `docs/foundation/conformance_suite.md#adapter-admission` — the specification this fixture executes.
- `docs/foundation/adapters.md#the-admission-contract` — the six obligations and their failing artefacts.
- ateles#1190 — the adapter runtime (`lib.adapters` + the sixth reference adapter) this fixture depends on.
