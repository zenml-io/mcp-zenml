#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx",
#     "mcp[cli]==2.2.0",
#     "zenml==0.97.0",
#     "setuptools",
#     "requests>=2.32.0",
# ]
#
# [tool.uv]
# exclude-newer-package = { mcp = "2026-09-08T00:00:00Z", "mcp-types" = "2026-09-08T00:00:00Z" }
#
# [tool.ty.rules]
# unresolved-import = "ignore"
#
# [tool.ty.environment]
# extra-paths = ["../server"]
# ///
"""Credential-free tests for step-log fetching against fake ZenML servers.

Each test scripts the HTTP answers of a ZenML 0.96 or 0.97 server and checks
which requests `make_step_logs_request` sends and what it returns.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "server"))
os.environ.setdefault("ZENML_MCP_ANALYTICS_ENABLED", "false")

import zenml_server as server  # noqa: E402

SERVER_URL = "https://zenml.example"
STEP_URL = f"{SERVER_URL}/api/v1/steps/step-1"
OLD_LOGS_URL = f"{STEP_URL}/logs"
ENTRIES_URL = f"{SERVER_URL}/api/v1/logs/logs-1/entries"


def _entries(start: int, stop: int) -> list[dict[str, Any]]:
    return [{"message": f"line {index}"} for index in range(start, stop)]


def _messages(result: dict[str, Any]) -> list[str]:
    return [entry["message"] for entry in result["logs"]]


def _response(status: int, body: Any) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = "https://zenml.example/fake"
    response._content = json.dumps(body).encode()
    return response


STEP_WITH_LOGS = _response(
    200,
    {
        "id": "step-1",
        "resources": {
            "log_collection": [
                {"id": "logs-runner", "body": {"source": "runner"}},
                {"id": "logs-1", "body": {"source": "step"}},
            ]
        },
    },
)
ROUTE_MISSING = _response(404, {"detail": "Not Found"})


class FakeServer:
    """Stands in for `requests.Session`: answers GETs from a table and records them."""

    def __init__(self, routes: dict[str, Callable[[dict[str, Any]], Any]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.headers: dict[str, str] = {}

    def __enter__(self) -> FakeServer:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        params = dict(kwargs.get("params") or {})
        self.calls.append((url, params))
        if self.headers.get("Authorization") != "Bearer token":
            raise AssertionError("request sent without the access token")
        return self.routes[url](params)

    def urls(self) -> list[str]:
        return [url for url, _ in self.calls]


def _fetch(fake: FakeServer, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("source", "step")
    with patch.object(server.requests, "Session", return_value=fake):
        return server.make_step_logs_request(
            SERVER_URL + "/", "step-1", "token", **kwargs
        )


def _expect_http_error(fake: FakeServer, status: int, **kwargs: Any) -> None:
    try:
        _fetch(fake, **kwargs)
    except requests.HTTPError as error:
        assert error.response.status_code == status
    else:
        raise AssertionError(f"HTTP {status} did not raise")


def _cursor_pages(
    pages: list[list[dict[str, Any]]], cursor: str
) -> Callable[[dict[str, Any]], requests.Response]:
    """Serve `pages` in order, linked by `before` or `after` cursors."""

    def answer(params: dict[str, Any]) -> requests.Response:
        index = int(params.get(cursor, "0"))
        more = str(index + 1) if index + 1 < len(pages) else None
        body = {"items": pages[index], "before": None, "after": None, cursor: more}
        return _response(200, body)

    return answer


def test_artifact_store_single_page() -> None:
    """A 0.97 artifact store answers one page with no cursors."""
    fake = FakeServer(
        {
            STEP_URL: lambda _: STEP_WITH_LOGS,
            ENTRIES_URL: lambda _: _response(
                200, {"items": _entries(0, 3), "before": None, "after": None}
            ),
        }
    )
    result = _fetch(fake)
    assert _messages(result) == ["line 0", "line 1", "line 2"]
    assert result["possibly_truncated"] is False
    # `start` stays unset: the artifact store rejects start=newest with a 400.
    assert fake.calls == [
        (STEP_URL, {"hydrate": "true"}),
        (ENTRIES_URL, {"limit": server.STEP_LOGS_MAX_ENTRIES}),
    ]

    tail = _fetch(fake, tail=2)
    assert _messages(tail) == ["line 1", "line 2"]
    assert tail["possibly_truncated"] is True


def test_full_page_without_cursor_is_flagged() -> None:
    """A full cursor-less page may have stopped at the server's entry limit."""
    fake = FakeServer(
        {ENTRIES_URL: lambda _: _response(200, {"items": _entries(0, 5)})}
    )
    with patch.object(server, "STEP_LOGS_MAX_ENTRIES", 5):
        result = _fetch(fake, source=None, logs_id="logs-1")
    assert len(result["logs"]) == 5
    assert result["possibly_truncated"] is True


def test_before_cursors_prepend_older_pages() -> None:
    """Datadog-style paging starts at the newest page and walks back."""
    newest_first = [_entries(6, 9), _entries(3, 6), _entries(0, 3)]
    fake = FakeServer(
        {
            STEP_URL: lambda _: STEP_WITH_LOGS,
            ENTRIES_URL: _cursor_pages(newest_first, "before"),
        }
    )
    result = _fetch(fake)
    assert _messages(result) == [f"line {index}" for index in range(9)]
    assert result["possibly_truncated"] is False
    # Follow-up requests carry only the cursor: Datadog rejects a `limit`
    # that differs from the one encoded in the cursor.
    assert [params for _, params in fake.calls[1:]] == [
        {"limit": server.STEP_LOGS_MAX_ENTRIES},
        {"before": "1"},
        {"before": "2"},
    ]


def test_before_cursors_stop_at_tail_and_cap() -> None:
    newest_first = [_entries(6, 9), _entries(3, 6), _entries(0, 3)]
    fake = FakeServer(
        {
            STEP_URL: lambda _: STEP_WITH_LOGS,
            ENTRIES_URL: _cursor_pages(newest_first, "before"),
        }
    )
    tail = _fetch(fake, tail=4)
    assert _messages(tail) == ["line 5", "line 6", "line 7", "line 8"]
    assert tail["possibly_truncated"] is True
    # Two pages hold the newest four entries, so the third is never fetched.
    assert fake.urls().count(ENTRIES_URL) == 2

    fake.calls.clear()
    with patch.object(server, "STEP_LOGS_MAX_ENTRIES", 5):
        capped = _fetch(fake)
    assert _messages(capped) == [f"line {index}" for index in range(4, 9)]
    assert capped["possibly_truncated"] is True


def test_after_cursors_append_newer_pages() -> None:
    oldest_first = [_entries(0, 3), _entries(3, 6), _entries(6, 9)]
    fake = FakeServer(
        {
            STEP_URL: lambda _: STEP_WITH_LOGS,
            ENTRIES_URL: _cursor_pages(oldest_first, "after"),
        }
    )
    result = _fetch(fake)
    assert _messages(result) == [f"line {index}" for index in range(9)]
    assert result["possibly_truncated"] is False

    # The newest entries are on the last page, so a tail reads every page.
    tail = _fetch(fake, tail=2)
    assert _messages(tail) == ["line 7", "line 8"]
    assert tail["possibly_truncated"] is True


def test_empty_page_ends_paging() -> None:
    """A cursor that leads to an empty page does not loop."""
    fake = FakeServer(
        {
            ENTRIES_URL: lambda params: _response(
                200,
                {"items": [] if "before" in params else _entries(0, 2), "before": "x"},
            )
        }
    )
    result = _fetch(fake, source=None, logs_id="logs-1")
    assert _messages(result) == ["line 0", "line 1"]
    assert result["possibly_truncated"] is False
    assert len(fake.calls) == 2


def test_later_page_failure_keeps_earlier_pages() -> None:
    """A rate limit on page two returns page one, marked as incomplete."""

    def answer(params: dict[str, Any]) -> requests.Response:
        if "before" in params:
            return _response(429, {"detail": ["RateLimited", "slow down"]})
        return _response(200, {"items": _entries(3, 6), "before": "1"})

    fake = FakeServer({ENTRIES_URL: answer})
    result = _fetch(fake, source=None, logs_id="logs-1")
    assert _messages(result) == ["line 3", "line 4", "line 5"]
    assert result["possibly_truncated"] is True


def test_first_page_failure_raises() -> None:
    for status, body in (
        (503, {"detail": ["ServiceUnavailable", "log backend down"]}),
        (404, {"detail": ["KeyError", "Unable to get logs with ID logs-1"]}),
    ):
        fake = FakeServer({ENTRIES_URL: lambda _, s=status, b=body: _response(s, b)})
        _expect_http_error(fake, status, source=None, logs_id="logs-1")
        # ZenML's own 404 (list-shaped detail) is a real error, not a sign of
        # an older server, so the old endpoint is never tried.
        assert OLD_LOGS_URL not in fake.urls()
    assert not server._servers_without_log_entries


def test_old_server_falls_back_and_is_remembered() -> None:
    """A 0.96 server has no entries route; use /steps/{id}/logs instead."""
    fake = FakeServer(
        {
            STEP_URL: lambda _: STEP_WITH_LOGS,
            ENTRIES_URL: lambda _: ROUTE_MISSING,
            OLD_LOGS_URL: lambda _: _response(200, _entries(0, 4)),
        }
    )
    with patch.object(server, "_servers_without_log_entries", set()):
        first = _fetch(fake)
        assert _messages(first) == [f"line {index}" for index in range(4)]
        assert first["possibly_truncated"] is False
        assert fake.urls() == [STEP_URL, ENTRIES_URL, OLD_LOGS_URL]
        assert fake.calls[-1][1] == {"source": "step"}

        fake.calls.clear()
        second = _fetch(fake, source=None, logs_id="logs-1", tail=1)
        assert _messages(second) == ["line 3"]
        assert second["possibly_truncated"] is True
        assert fake.calls == [(OLD_LOGS_URL, {"logs_id": "logs-1"})]

    fake = FakeServer({OLD_LOGS_URL: lambda _: _response(200, _entries(0, 5))})
    with (
        patch.object(server, "_servers_without_log_entries", {SERVER_URL}),
        patch.object(server, "STEP_LOGS_MAX_ENTRIES", 5),
    ):
        capped = _fetch(fake)
    assert capped["possibly_truncated"] is True


def test_unknown_source_uses_old_endpoint_error() -> None:
    """With no stream for the source, the old endpoint reports "no logs"."""
    fake = FakeServer(
        {
            STEP_URL: lambda _: STEP_WITH_LOGS,
            OLD_LOGS_URL: lambda _: _response(
                404, {"detail": ["KeyError", "No logs found for source 'hook'"]}
            ),
        }
    )
    _expect_http_error(fake, 404, source="hook")
    assert fake.urls() == [STEP_URL, OLD_LOGS_URL]
    assert not server._servers_without_log_entries


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"All {len(tests)} step log tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
