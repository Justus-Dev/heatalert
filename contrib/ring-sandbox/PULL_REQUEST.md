# PR for josepha-mayo/ring-sandbox

**Branch:** `fix/history-sub-type-and-step-meta` (based on upstream `main` @ `2710bdf`)
**Title:** Declare `HistoryEventAttributes.sub_type`; let scenario steps carry app metadata

## What

1. **`HistoryEventAttributes.sub_type` is declared** as `str | None = None`. History
   resources withhold `sub_type` (it is filter-only upstream), and the model did not
   declare it, so `ev.attributes.sub_type` raised `AttributeError` for every history
   entry. It now reads as `None`.
2. **Scenario steps can carry app metadata.** A step accepts a `meta:` mapping and any
   `x-`-prefixed key. Both are collected into `Step.meta` and are never sent to the
   emulator.
3. **Clearer errors for unknown step keys.** Instead of a raw dataclass `TypeError`
   (`Step.__init__() got an unexpected keyword argument ...`), the loader raises
   `ValueError: <file>: bad step #2 — unknown key(s) subtype; put app-specific data
   under 'meta:' or prefix it with 'x-'`.

## Why it matters

- Any client loop that prints or branches on `event_type`/`sub_type` from
  `client.events()` crashed on the first entry.
- Apps that drive their own demos from ring-sandbox scenario files (HeatWatch does)
  need per-step labels or app options. Until now the only choices were a separate file
  format or losing `ring-sandbox play` compatibility.
- A typo like `subtype:` now points at the exact step and key.

Found while building HeatWatch (Ring track, Amazon Developer Hackathon 2026).

## Tests

- `tests/test_client_emulator.py::test_snapshot_and_clip`: every history entry
  exposes `attributes.sub_type is None`.
- `tests/test_scenarios_live.py::test_steps_carry_app_metadata_and_name_unknown_keys`:
  `meta:` and `x-` keys, the unknown-key error message, and a non-mapping `meta`.
- Both tests fail without the source change. The full suite (93 passed, 1 skipped),
  `ruff check` and `ruff format --check` all pass.

CHANGELOG (Unreleased) and the README scenario paragraph are updated.

## Patch

`0001-history-sub-type-and-step-meta.patch` (`git am`-ready, against upstream `main`).
