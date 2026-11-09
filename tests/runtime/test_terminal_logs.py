from io import StringIO

from fastapi.testclient import TestClient

from api.server import create_app
from core.terminal_logs import Tee, TerminalLogs
from tests.support.admin import ADMIN_HEADERS, CALLER_HEADERS, admin_settings


def test_terminal_chunking_preserves_original_output_and_redacts_snapshot():
    logs = TerminalLogs()
    original = StringIO()
    stream = Tee(original, logs, "out")
    stream.write("startup ")
    stream.write("ready\n")
    stream.write('api_key="private-value" Authorization: Bearer private-bearer\n')
    stream.write("https://operator:private-password@example.com api?token=private-query\n")
    stream.write("partial")
    stream.flush()
    logs.write("err", "error\n")
    snapshot = logs.snapshot()
    assert original.getvalue().startswith("startup ready\napi_key=")
    assert snapshot["stdout"] == 4 and snapshot["stderr"] == 1
    assert [row["text"] for row in snapshot["items"]][0] == "startup ready"
    assert snapshot["items"][-2]["text"] == "partial"
    assert all("private-" not in row["text"] and "_time" not in row for row in snapshot["items"])
    assert logs.snapshot(stream="err")["items"][0]["text"] == "error"
    assert len(logs.snapshot(limit=2)["items"]) == 2


def test_terminal_capacity_line_limit_and_retention(monkeypatch):
    logs = TerminalLogs()
    clock = [0]
    monkeypatch.setattr("core.terminal_logs.time.monotonic", lambda: clock[0])
    for index in range(1005):
        logs.write("out", f"line {index}\n")
    snapshot = logs.snapshot(limit=1000)
    assert snapshot["retained"] == 1000 and snapshot["items"][0]["text"] == "line 5"
    logs.write("err", "x" * 5000)
    assert len(logs.snapshot(stream="err")["items"][0]["text"]) == 4096
    clock[0] = 3601
    logs.write("out", "new\n")
    snapshot = logs.snapshot()
    assert snapshot["retained"] == 1 and snapshot["items"][0]["text"] == "new"


def test_terminal_api_is_admin_only_and_bounded(tmp_path):
    app = create_app(admin_settings(), live_config_root=tmp_path)
    with TestClient(app) as client:
        path = "/admin/api/system/terminal-logs"
        assert client.get(path).status_code == 401
        assert client.get(path, headers=CALLER_HEADERS).status_code == 403
        response = client.get(path, headers=ADMIN_HEADERS)
        assert response.status_code == 200
        assert "no-store" in response.headers["cache-control"]
        assert response.json()["capacity"] == 1000
        for suffix in ("?limit=0", "?limit=1001", "?stream=unknown"):
            assert client.get(path + suffix, headers=ADMIN_HEADERS).status_code == 422
