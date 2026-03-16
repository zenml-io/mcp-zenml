#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx",
#     "mcp[cli]",
#     "zenml",
#     "setuptools",
#     "requests>=2.32.0",
# ]
# ///
"""
Unit tests for datetime filter normalization and exception classification.

Validates that _normalize_datetime_filter correctly transforms common LLM
datetime inputs (date-only, ISO-8601, range syntax) into ZenML's required
"%Y-%m-%d %H:%M:%S" format, and that _classify_exception produces appropriate
error messages for different exception types.

Usage:
    uv run scripts/test_datetime_normalization.py
"""

import sys
from pathlib import Path

# Add server directory to path so we can import zenml_server
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from zenml_server import (
    _classify_exception,
    _normalize_datetime_filter,
)

# ---------------------------------------------------------------------------
# Test cases for _normalize_datetime_filter
# ---------------------------------------------------------------------------
# Each tuple: (input, expected_output, description)
NORMALIZATION_CASES: list[tuple[str, str, str]] = [
    # --- Date-only (no operator) ---
    ("2026-02-01", "2026-02-01 00:00:00", "bare date-only defaults to start of day"),
    # --- Date-only with operators ---
    ("gte:2026-02-01", "gte:2026-02-01 00:00:00", "gte + date-only → start of day"),
    ("gt:2026-02-01", "gt:2026-02-01 00:00:00", "gt + date-only → start of day"),
    ("lte:2026-02-01", "lte:2026-02-01 23:59:59", "lte + date-only → end of day"),
    ("lt:2026-02-01", "lt:2026-02-01 23:59:59", "lt + date-only → end of day"),
    (
        "equals:2026-02-01",
        "equals:2026-02-01 00:00:00",
        "equals + date-only → start of day",
    ),
    # --- Already correct format (passthrough) ---
    (
        "gte:2026-02-01 00:00:00",
        "gte:2026-02-01 00:00:00",
        "already correct format unchanged",
    ),
    (
        "2026-02-01 10:30:00",
        "2026-02-01 10:30:00",
        "already correct bare datetime unchanged",
    ),
    # --- ISO-8601 with T separator ---
    ("2026-02-01T10:00:00", "2026-02-01 10:00:00", "ISO T separator normalized"),
    ("gte:2026-02-01T10:00:00", "gte:2026-02-01 10:00:00", "gte + ISO T separator"),
    # --- ISO-8601 with Z suffix ---
    ("2026-02-01T10:00:00Z", "2026-02-01 10:00:00", "ISO with Z → UTC"),
    ("gte:2026-02-01T10:00:00Z", "gte:2026-02-01 10:00:00", "gte + ISO with Z"),
    # --- ISO-8601 with fractional seconds ---
    (
        "2026-02-01T10:00:00.123Z",
        "2026-02-01 10:00:00",
        "ISO fractional seconds stripped",
    ),
    (
        "2026-02-01T10:00:00.123456",
        "2026-02-01 10:00:00",
        "ISO fractional (no Z) stripped",
    ),
    # --- ISO-8601 without seconds ---
    ("2026-02-01T10:00Z", "2026-02-01 10:00:00", "ISO without seconds + Z"),
    ("2026-02-01T10:00", "2026-02-01 10:00:00", "ISO without seconds (no Z)"),
    # --- ISO-8601 with timezone offsets ---
    (
        "2026-02-01T12:00:00+02:00",
        "2026-02-01 10:00:00",
        "ISO +02:00 → converted to UTC",
    ),
    (
        "2026-02-01T05:00:00-05:00",
        "2026-02-01 10:00:00",
        "ISO -05:00 → converted to UTC",
    ),
    (
        "gte:2026-02-01T12:00:00+02:00",
        "gte:2026-02-01 10:00:00",
        "gte + ISO with offset",
    ),
    # --- Space-separated with fractional seconds ---
    (
        "2026-02-01 10:00:00.123",
        "2026-02-01 10:00:00",
        "space-separated fractional stripped",
    ),
    # --- range: syntax → in: syntax ---
    (
        "range:2026-02-01..2026-02-07",
        "in:2026-02-01 00:00:00,2026-02-07 23:59:59",
        "range: converted to in: with full datetimes",
    ),
    (
        "range:2026-02-01T00:00:00Z..2026-02-07T23:59:59Z",
        "in:2026-02-01 00:00:00,2026-02-07 23:59:59",
        "range: with ISO values normalized",
    ),
    # --- in: syntax ---
    (
        "in:2026-02-01,2026-02-07",
        "in:2026-02-01 00:00:00,2026-02-07 23:59:59",
        "in: with date-only values gets times appended",
    ),
    (
        "in:2026-02-01 00:00:00,2026-02-07 23:59:59",
        "in:2026-02-01 00:00:00,2026-02-07 23:59:59",
        "in: with correct format unchanged",
    ),
    # --- Non-datetime filters (passthrough) ---
    ("contains:foo", "contains:foo", "non-datetime string filter unchanged"),
    ("oneof:completed,failed", "oneof:completed,failed", "oneof filter unchanged"),
    ("startswith:my-pipeline", "startswith:my-pipeline", "startswith filter unchanged"),
    # --- Edge cases ---
    ("", "", "empty string unchanged"),
    ("  2026-02-01  ", "2026-02-01 00:00:00", "whitespace trimmed before normalizing"),
]


def test_normalization() -> tuple[int, int, list[str]]:
    """Run all normalization test cases. Returns (passed, failed, failure_messages)."""
    passed = 0
    failed = 0
    failures: list[str] = []

    for inp, expected, desc in NORMALIZATION_CASES:
        actual = _normalize_datetime_filter(inp)
        if actual == expected:
            passed += 1
        else:
            failed += 1
            failures.append(
                f"  FAIL: {desc}\n"
                f"    input:    {inp!r}\n"
                f"    expected: {expected!r}\n"
                f"    actual:   {actual!r}"
            )

    return passed, failed, failures


# ---------------------------------------------------------------------------
# Test cases for _classify_exception
# ---------------------------------------------------------------------------


# Custom exception classes that mimic pydantic's ValidationError identity
# (class name + module) without importing pydantic itself.
_FakeValidationError = type("ValidationError", (Exception,), {"__module__": "pydantic"})


def _make_pydantic_validation_error() -> Exception:
    """Create a real-ish ValidationError by simulating what pydantic raises."""
    return _FakeValidationError(
        "1 validation error for PipelineRunFilter\ncreated\n  invalid datetime"
    )


def _make_non_filter_validation_error() -> Exception:
    """Create a ValidationError that isn't filter-related (e.g. bad UUID)."""
    return _FakeValidationError(
        "1 validation error for UUID\n  value is not a valid uuid"
    )


def test_classify_exception() -> tuple[int, int, list[str]]:
    """Test _classify_exception for correct categorization."""
    passed = 0
    failed = 0
    failures: list[str] = []

    def check(
        desc: str,
        category: str,
        msg_contains: str | None,
        msg_not_contains: str | None,
        *,
        tool_name: str,
        exc: Exception,
    ):
        nonlocal passed, failed
        cat, msg, _details = _classify_exception(tool_name=tool_name, exc=exc)
        ok = True
        reasons = []
        if cat != category:
            ok = False
            reasons.append(f"category: expected {category!r}, got {cat!r}")
        if msg_contains and msg_contains not in msg:
            ok = False
            reasons.append(f"message missing {msg_contains!r}")
        if msg_not_contains and msg_not_contains in msg:
            ok = False
            reasons.append(f"message unexpectedly contains {msg_not_contains!r}")
        if ok:
            passed += 1
        else:
            failed += 1
            failures.append(f"  FAIL: {desc}\n    " + "\n    ".join(reasons))

    # ValidationError on a list tool → should include filter syntax help
    check(
        "filter ValidationError on list tool includes syntax help",
        category="ValidationError",
        msg_contains="FILTER SYNTAX REFERENCE",
        msg_not_contains=None,
        tool_name="list_pipeline_runs",
        exc=_make_pydantic_validation_error(),
    )

    # ValidationError on a non-list tool → should NOT include filter syntax help
    check(
        "ValidationError on get tool omits filter syntax",
        category="ValidationError",
        msg_contains="Validation failed",
        msg_not_contains="FILTER SYNTAX REFERENCE",
        tool_name="get_pipeline_run",
        exc=_make_non_filter_validation_error(),
    )

    # A plain ValueError should NOT be classified as ValidationError
    check(
        "plain ValueError is not classified as ValidationError",
        category="UnexpectedError",
        msg_contains=None,
        msg_not_contains="FILTER SYNTAX REFERENCE",
        tool_name="list_pipeline_runs",
        exc=ValueError("something went wrong"),
    )

    return passed, failed, failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    print("=" * 60)
    print("Datetime Normalization & Classification Tests")
    print("=" * 60)

    total_passed = 0
    total_failed = 0
    all_failures: list[str] = []

    # Normalization tests
    print("\n--- _normalize_datetime_filter ---")
    p, f, fails = test_normalization()
    total_passed += p
    total_failed += f
    all_failures.extend(fails)
    print(f"  {p} passed, {f} failed")

    # Classification tests
    print("\n--- _classify_exception ---")
    p, f, fails = test_classify_exception()
    total_passed += p
    total_failed += f
    all_failures.extend(fails)
    print(f"  {p} passed, {f} failed")

    # Summary
    print("\n" + "=" * 60)
    if all_failures:
        print(f"FAILED: {total_failed} failures, {total_passed} passed\n")
        for failure in all_failures:
            print(failure)
        sys.exit(1)
    else:
        print(f"ALL PASSED: {total_passed} tests")
        sys.exit(0)


if __name__ == "__main__":
    main()
