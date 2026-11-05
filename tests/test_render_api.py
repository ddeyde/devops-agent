"""Тесты инструмента на фейковом Render API. Запуск: python -I tests/test_render_api.py"""
import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

TOOL = Path(__file__).resolve().parent.parent / "tools" / "render_api.py"
KEY = "rnd_secret_test_key"


def service(name, service_id):
    return {
        "id": service_id,
        "name": name,
        "slug": name,
        "type": "web_service",
        "repo": "https://github.com/example/app",
        "branch": "main",
        "suspended": "not_suspended",
        "serviceDetails": {"url": f"https://{name}.onrender.com", "plan": "free"},
    }


def deploy(deploy_id, status, commit="abcdef1234"):
    return {"id": deploy_id, "status": status, "commit": {"id": commit, "message": "msg"}}


class FakeRender(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, payload=None):
        raw = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def handle_any(self):
        state = self.server.state
        url = urlparse(self.path)
        query = parse_qs(url.query)
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        state["calls"].append((self.command, url.path, query, body))

        if state["drop"] > 0 or (state.get("drop_post") and self.command == "POST"):
            state["drop"] = max(state["drop"] - 1, 0)
            self.close_connection = True
            return
        if url.path == "/flaky":
            state["flaky"] -= 1
            return self.reply(503, {}) if state["flaky"] >= 0 else self.reply(200, {"status": "healthy"})
        if self.headers.get("Authorization") != f"Bearer {KEY}":
            return self.reply(401, {"message": f"bad token {KEY}"})
        if url.path == "/owners":
            return self.reply(200, [{"owner": {"id": "tea-1", "name": "me", "email": "e", "type": "user"}}])
        if url.path == "/services" and self.command == "GET":
            names = query.get("name")
            found = [s for s in state["services"] if not names or s["name"] in names]
            return self.reply(200, [{"service": s, "cursor": "c"} for s in found])
        if url.path == "/services" and self.command == "POST":
            created = service(body["name"], "srv-new")
            state["services"].append(created)
            return self.reply(201, {"service": created, "deployId": "dep-new"})
        if url.path == "/logs":
            logs = [
                {"id": "2", "message": "second", "timestamp": "t2", "labels": []},
                {"id": "1", "message": "first", "timestamp": "t1", "labels": []},
            ]
            return self.reply(200, {"hasMore": False, "nextStartTime": "", "nextEndTime": "", "logs": logs})
        if url.path.startswith("/metrics/"):
            if url.path.endswith("http-requests"):
                return self.reply(400, {"message": "unsupported"})
            return self.reply(200, [{"labels": [], "unit": "cpu", "values": [{"timestamp": "t", "value": 0.5}]}])
        if url.path.endswith("/deploys") and self.command == "GET":
            queue = state["deploys"]
            current = queue.pop(0) if len(queue) > 1 else queue[0]
            return self.reply(200, [{"deploy": d, "cursor": "c"} for d in current])
        if url.path.endswith("/deploys") and self.command == "POST":
            return self.reply(201, deploy("dep-x", "created"))
        if url.path.endswith("/rollback"):
            return self.reply(201, deploy(body["deployId"], "created"))
        if url.path.endswith("/restart"):
            return self.reply(200)
        if self.command == "DELETE":
            return self.reply(204)
        return self.reply(404, {"message": "not found"})

    do_GET = do_POST = do_DELETE = handle_any


class ToolTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeRender)
        self.server.state = {
            "calls": [],
            "services": [service("demo", "srv-demo"), service("prod", "srv-prod")],
            "deploys": [[deploy("dep-1", "live")]],
            "flaky": 2,
            "drop": 0,
        }
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def run_tool(self, *args, key=KEY):
        env = {
            **os.environ,
            "RENDER_API_KEY": key,
            "RENDER_API_BASE": self.base,
            "PROTECTED_SERVICES": "prod",
            "RENDER_OWNER_ID": "",
        }
        result = subprocess.run(
            [sys.executable, str(TOOL), *args],
            capture_output=True, text=True, encoding="utf-8", env=env, timeout=30,
        )
        return result.returncode, result.stdout + result.stderr

    def writes(self):
        return [c for c in self.server.state["calls"] if c[0] != "GET"]

    def test_create_free_docker_service(self):
        code, out = self.run_tool("create", "--name", "new", "--repo", "https://github.com/example/app", "--branch", "demo")
        self.assertEqual(code, 0, out)
        self.assertTrue(json.loads(out)["created"])
        body = self.writes()[0][3]
        self.assertEqual(body["type"], "web_service")
        self.assertEqual(body["ownerId"], "tea-1")
        self.assertEqual(body["branch"], "demo")
        self.assertEqual(body["serviceDetails"]["plan"], "free")
        self.assertEqual(body["serviceDetails"]["runtime"], "docker")
        self.assertEqual(body["serviceDetails"]["healthCheckPath"], "/health")

    def test_create_is_idempotent(self):
        code, out = self.run_tool("create", "--name", "demo", "--repo", "https://github.com/example/app")
        self.assertEqual(code, 0, out)
        self.assertFalse(json.loads(out)["created"])
        self.assertEqual(self.writes(), [])

    def test_paid_plan_needs_flag(self):
        code, out = self.run_tool("create", "--name", "new", "--repo", "r", "--plan", "starter")
        self.assertEqual(code, 1)
        self.assertIn("платный", out)
        self.assertEqual(self.writes(), [])

    def test_static_site_body(self):
        code, out = self.run_tool("create", "--name", "site", "--repo", "r", "--type", "static")
        self.assertEqual(code, 0, out)
        body = self.writes()[0][3]
        self.assertEqual(body["type"], "static_site")
        self.assertEqual(body["serviceDetails"]["publishPath"], ".")

    def test_protected_service_is_read_only(self):
        for args in (("restart",), ("deploy",), ("rollback",), ("delete", "--yes")):
            code, out = self.run_tool(*args, "--name", "prod")
            self.assertEqual(code, 1, args)
            self.assertIn("защищён", out)
        self.assertEqual(self.writes(), [])
        code, out = self.run_tool("status", "--name", "prod")
        self.assertEqual(code, 0, out)

    def test_delete_needs_yes(self):
        code, out = self.run_tool("delete", "--name", "demo")
        self.assertEqual(code, 1)
        self.assertEqual(self.writes(), [])
        code, out = self.run_tool("delete", "--name", "demo", "--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.writes()[0][:2], ("DELETE", "/services/srv-demo"))

    def test_wait_polls_until_live(self):
        self.server.state["deploys"] = [[deploy("d", "build_in_progress")], [deploy("d", "live")]]
        code, out = self.run_tool("wait", "--name", "demo", "--interval", "0")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["result"], "live")

    def test_wait_reports_failure(self):
        self.server.state["deploys"] = [[deploy("d", "update_failed")]]
        code, out = self.run_tool("wait", "--name", "demo", "--interval", "0")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["result"], "failed")

    def test_wait_filters_by_commit(self):
        self.server.state["deploys"] = [
            [deploy("old", "live", "1111111")],
            [deploy("new", "live", "2222222"), deploy("old", "deactivated", "1111111")],
        ]
        code, out = self.run_tool("wait", "--name", "demo", "--commit", "2222222", "--interval", "0")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["deploy"]["id"], "new")

    def test_rollback_picks_previous_version(self):
        self.server.state["deploys"] = [[deploy("bad", "live"), deploy("x", "build_failed"), deploy("good", "deactivated")]]
        code, out = self.run_tool("rollback", "--name", "demo")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.writes()[0][3], {"deployId": "good"})

    def test_rollback_refused_when_latest_failed(self):
        self.server.state["deploys"] = [[deploy("bad", "update_failed"), deploy("good", "live")]]
        code, out = self.run_tool("rollback", "--name", "demo")
        self.assertEqual(code, 1)
        self.assertIn("Откат не нужен", out)
        self.assertEqual(self.writes(), [])

    def test_logs_are_chronological(self):
        code, out = self.run_tool("logs", "--name", "demo", "--type", "build")
        self.assertEqual(code, 0, out)
        self.assertEqual([item["message"] for item in json.loads(out)], ["first", "second"])
        query = next(c[2] for c in self.server.state["calls"] if c[1] == "/logs")
        self.assertEqual(query["resource"], ["srv-demo"])
        self.assertEqual(query["ownerId"], ["tea-1"])
        self.assertEqual(query["type"], ["build"])

    def test_metrics_survive_one_failing_series(self):
        code, out = self.run_tool("metrics", "--name", "demo")
        self.assertEqual(code, 0, out)
        report = json.loads(out)
        self.assertEqual(report["cpu"]["last"], 0.5)
        self.assertIn("error", report["http-requests"])

    def test_check_retries_through_cold_start(self):
        code, out = self.run_tool("check", "--url", self.base, "--path", "/flaky", "--interval", "0", "--timeout", "10")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["attempts"], 3)

    def test_get_retries_after_network_error(self):
        self.server.state["drop"] = 1
        code, out = self.run_tool("list")
        self.assertEqual(code, 0, out)

    def test_post_is_not_retried(self):
        self.server.state["drop_post"] = True
        code, out = self.run_tool("restart", "--name", "demo")
        self.assertEqual(code, 1)
        self.assertIn("сеть недоступна", out)
        self.assertEqual(len(self.writes()), 1)

    def test_path_without_leading_slash(self):
        code, out = self.run_tool("check", "--url", self.base, "--path", "flaky", "--interval", "0", "--timeout", "10")
        self.assertEqual(code, 0, out)
        self.assertTrue(json.loads(out)["url"].endswith("/flaky"))

    def test_path_mangled_by_git_bash_is_rejected(self):
        code, out = self.run_tool("check", "--url", self.base, "--path", "D:/Git/health")
        self.assertEqual(code, 1)
        self.assertIn("без начального слэша", out)
        code, out = self.run_tool("create", "--name", "new", "--repo", "r", "--health", "D:/Git/health")
        self.assertEqual(code, 1)
        self.assertEqual(self.writes(), [])

    def test_key_never_printed(self):
        code, out = self.run_tool("list", key=KEY + "x")
        self.assertEqual(code, 1)
        self.assertNotIn(KEY + "x", out)
        code, out = self.run_tool("list", key="")
        self.assertEqual(code, 1)
        self.assertIn("RENDER_API_KEY", out)


if __name__ == "__main__":
    unittest.main()
