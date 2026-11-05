#!/usr/bin/env python3
"""Инструмент DevOps-агента: управление сервисами Render через REST API.

Только стандартная библиотека. Каждая команда печатает JSON в stdout
и завершается с кодом 0 при успехе и 1 при ошибке.

Ограничения зашиты здесь, а не только в инструкциях агента:
  * платный план нельзя создать без --allow-paid;
  * сервисы из PROTECTED_SERVICES нельзя менять;
  * удаление требует --yes;
  * API-ключ никогда не попадает в вывод.
"""
import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_API = "https://api.render.com/v1"
USER_AGENT = "devops-agent/1.0"

DEPLOY_OK = {"live"}
DEPLOY_FAILED = {"build_failed", "update_failed", "pre_deploy_failed", "canceled", "deactivated"}
METRICS = ("cpu", "memory", "http-requests")


class ToolError(Exception):
    pass


def load_env():
    """Читает .env из корня проекта, не перезаписывая уже заданные переменные."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def url_path(value):
    """Git Bash превращает аргумент /health в D:/Git/health, поэтому слэш в начале не обязателен."""
    if re.match(r"^[A-Za-z]:[\\/]", value):
        raise ToolError(f"Оболочка исказила путь: {value}. Передайте его без начального слэша: health")
    return "/" + value.lstrip("/") if value else ""


class Render:
    def __init__(self):
        self.key = os.environ.get("RENDER_API_KEY", "")
        self.base = os.environ.get("RENDER_API_BASE", DEFAULT_API).rstrip("/")
        self.protected = {
            item.strip().lower()
            for item in os.environ.get("PROTECTED_SERVICES", "").split(",")
            if item.strip()
        }
        self._owner = os.environ.get("RENDER_OWNER_ID", "")

    def api(self, method, path, params=None, body=None):
        if not self.key:
            raise ToolError(f"RENDER_API_KEY не задан: скопируйте .env.example в .env и впишите ключ. Файл: {ROOT / '.env'}")
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(4):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    raw = response.read()
                return json.loads(raw) if raw else None
            except urllib.error.HTTPError as error:
                if error.code == 429 and attempt < 3:
                    time.sleep(int(error.headers.get("Retry-After") or 5))
                    continue
                detail = error.read().decode("utf-8", "replace")[:500]
                raise ToolError(f"{method} {path}: HTTP {error.code}: {detail}")
            except (OSError, http.client.HTTPException) as error:
                # Повторяем только чтение: POST мог дойти до Render, и повтор задвоит действие.
                if method == "GET" and attempt < 3:
                    time.sleep(3)
                    continue
                raise ToolError(f"{method} {path}: сеть недоступна: {getattr(error, 'reason', error)}")

    def owner_id(self):
        if self._owner:
            return self._owner
        owners = [item["owner"] for item in self.api("GET", "/owners", {"limit": 20})]
        if len(owners) != 1:
            names = ", ".join(f'{o["name"]} ({o["id"]})' for o in owners) or "нет"
            raise ToolError(f"Задайте RENDER_OWNER_ID в .env. Доступные workspace: {names}")
        self._owner = owners[0]["id"]
        return self._owner

    def services(self, name=None):
        params = {"limit": 100, "ownerId": self.owner_id()}
        if name:
            params["name"] = name
        return [item["service"] for item in self.api("GET", "/services", params)]

    def find(self, name):
        return next((s for s in self.services(name) if s["name"] == name), None)

    def require(self, name):
        service = self.find(name)
        if service is None:
            raise ToolError(f"Сервис '{name}' не найден")
        return service

    def mutable(self, name):
        """Возвращает сервис, если его разрешено менять."""
        service = self.require(name)
        ids = {service["id"], service["name"], service.get("slug", "")}
        if {value.lower() for value in ids} & self.protected:
            raise ToolError(f"Сервис '{name}' защищён (PROTECTED_SERVICES), менять его нельзя")
        return service

    def deploys(self, service_id, limit=10):
        items = self.api("GET", f"/services/{service_id}/deploys", {"limit": limit})
        return [item["deploy"] for item in items]


def summary(service):
    details = service.get("serviceDetails") or {}
    return {
        "id": service["id"],
        "name": service["name"],
        "type": service.get("type"),
        "url": details.get("url"),
        "plan": details.get("plan"),
        "region": details.get("region"),
        "runtime": details.get("runtime"),
        "repo": service.get("repo"),
        "branch": service.get("branch"),
        "suspended": service.get("suspended"),
        "dashboardUrl": service.get("dashboardUrl"),
    }


def deploy_summary(deploy):
    commit = deploy.get("commit") or {}
    message = (commit.get("message") or "").splitlines()
    return {
        "id": deploy["id"],
        "status": deploy.get("status"),
        "commit": (commit.get("id") or "")[:7],
        "message": message[0] if message else "",
        "trigger": deploy.get("trigger"),
        "createdAt": deploy.get("createdAt"),
        "finishedAt": deploy.get("finishedAt"),
    }


def cmd_whoami(render, args):
    emit([item["owner"] for item in render.api("GET", "/owners", {"limit": 20})])


def cmd_list(render, args):
    emit([summary(service) for service in render.services()])


def cmd_create(render, args):
    existing = render.find(args.name)
    if existing:
        emit({"created": False, "reason": "уже существует", "service": summary(existing)})
        return
    if args.type == "web" and args.plan != "free" and not args.allow_paid:
        raise ToolError(f"План '{args.plan}' платный. Нужен явный --allow-paid")
    body = {
        "name": args.name,
        "ownerId": render.owner_id(),
        "repo": args.repo,
        "autoDeployTrigger": "commit",
    }
    if args.branch:
        body["branch"] = args.branch
    if args.type == "static":
        body["type"] = "static_site"
        body["serviceDetails"] = {"buildCommand": args.build_cmd or "", "publishPath": args.publish_path}
    else:
        if args.runtime == "docker":
            env_details = {"dockerfilePath": args.dockerfile, "dockerContext": "."}
        elif args.build_cmd and args.start_cmd:
            env_details = {"buildCommand": args.build_cmd, "startCommand": args.start_cmd}
        else:
            raise ToolError("Для runtime без Docker нужны --build-cmd и --start-cmd")
        body["type"] = "web_service"
        body["serviceDetails"] = {
            "runtime": args.runtime,
            "plan": args.plan,
            "region": args.region,
            "envSpecificDetails": env_details,
        }
        if args.health:
            body["serviceDetails"]["healthCheckPath"] = url_path(args.health)
    result = render.api("POST", "/services", body=body)
    emit({"created": True, "deployId": result.get("deployId"), "service": summary(result["service"])})


def cmd_status(render, args):
    service = render.require(args.name)
    emit({
        "service": summary(service),
        "deploys": [deploy_summary(d) for d in render.deploys(service["id"], 5)],
    })


def cmd_deploys(render, args):
    service = render.require(args.name)
    emit([deploy_summary(d) for d in render.deploys(service["id"], args.limit)])


def cmd_deploy(render, args):
    service = render.mutable(args.name)
    body = {"clearCache": "clear" if args.clear_cache else "do_not_clear"}
    if args.commit:
        body["commitId"] = args.commit
    result = render.api("POST", f"/services/{service['id']}/deploys", body=body)
    emit({"triggered": True, "deploy": deploy_summary(result) if result else None})


def cmd_wait(render, args):
    """Ждёт завершения деплоя. --commit нужен сразу после push: деплой появляется не мгновенно."""
    service = render.require(args.name)
    deadline = time.monotonic() + args.timeout
    last = None
    while True:
        deploys = render.deploys(service["id"], 10)
        if args.commit:
            deploys = [d for d in deploys if (d.get("commit") or {}).get("id", "").startswith(args.commit)]
        if deploys:
            last = deploy_summary(deploys[0])
            if last["status"] in DEPLOY_OK:
                emit({"result": "live", "deploy": last})
                return
            if last["status"] in DEPLOY_FAILED:
                emit({"result": "failed", "deploy": last})
                raise SystemExit(1)
        if time.monotonic() >= deadline:
            raise ToolError(f"Таймаут {args.timeout} с, последний деплой: {json.dumps(last, ensure_ascii=False)}")
        time.sleep(args.interval)


def cmd_logs(render, args):
    service = render.require(args.name)
    params = {
        "ownerId": render.owner_id(),
        "resource": [service["id"]],
        "direction": "backward",
        "limit": args.limit,
    }
    if args.text:
        params["text"] = [args.text]
    if args.level:
        params["level"] = [args.level]
    if args.type:
        params["type"] = [args.type]
    logs = render.api("GET", "/logs", params)["logs"]
    logs.sort(key=lambda item: item["timestamp"])
    emit([{"time": item["timestamp"], "message": item["message"]} for item in logs])


def cmd_metrics(render, args):
    service = render.require(args.name)
    end = datetime.now(timezone.utc)
    params = {
        "resource": service["id"],
        "startTime": (end - timedelta(minutes=args.minutes)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "resolutionSeconds": 60,
    }
    report = {}
    for metric in METRICS:
        try:
            series = render.api("GET", f"/metrics/{metric}", params)
        except ToolError as error:
            report[metric] = {"error": str(error)}
            continue
        values = [point["value"] for line in series for point in line.get("values", [])]
        unit = series[0].get("unit") if series else None
        report[metric] = {"points": len(values), "last": values[-1] if values else None, "unit": unit}
    emit(report)


def cmd_restart(render, args):
    service = render.mutable(args.name)
    render.api("POST", f"/services/{service['id']}/restart")
    emit({"restarted": service["name"]})


def cmd_rollback(render, args):
    service = render.mutable(args.name)
    target = args.deploy_id
    if not target:
        deploys = render.deploys(service["id"], 20)
        if not deploys:
            raise ToolError("У сервиса нет деплоев")
        if deploys[0].get("status") != "live":
            raise ToolError(
                f"Последний деплой в статусе {deploys[0].get('status')}, он не вышел в live. "
                "Откат не нужен: Render продолжает обслуживать прошлую рабочую версию. Исправьте код"
            )
        previous = [d for d in deploys[1:] if d.get("status") in ("live", "deactivated")]
        if not previous:
            raise ToolError("Нет предыдущей рабочей версии для отката")
        target = previous[0]["id"]
    result = render.api("POST", f"/services/{service['id']}/rollback", body={"deployId": target})
    emit({
        "rolledBackTo": target,
        "deploy": deploy_summary(result) if result else None,
        "warning": "Render отключил автодеплой: новые коммиты разворачивайте командой deploy",
    })


def cmd_delete(render, args):
    service = render.mutable(args.name)
    if not args.yes:
        raise ToolError("Удаление необратимо: нужно подтверждение человека и флаг --yes")
    render.api("DELETE", f"/services/{service['id']}")
    emit({"deleted": service["name"], "id": service["id"]})


def cmd_check(render, args):
    """Проверяет URL с повторами: Free-сервис просыпается около минуты."""
    url = args.url
    if not url:
        if not args.name:
            raise ToolError("Нужен --url или --name")
        url = summary(render.require(args.name))["url"]
    url = url.rstrip("/") + url_path(args.path)
    started = time.monotonic()
    attempts = 0
    last = None
    while True:
        attempts += 1
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=args.request_timeout) as response:
                body = response.read(500).decode("utf-8", "replace")
            emit({
                "ok": True,
                "url": url,
                "status": response.status,
                "attempts": attempts,
                "seconds": round(time.monotonic() - started, 1),
                "body": body,
            })
            return
        except urllib.error.HTTPError as error:
            last = f"HTTP {error.code}"
        except (OSError, http.client.HTTPException) as error:
            last = str(getattr(error, "reason", error))
        if time.monotonic() - started >= args.timeout:
            emit({"ok": False, "url": url, "attempts": attempts, "last": last})
            raise SystemExit(1)
        time.sleep(args.interval)


def build_parser():
    parser = argparse.ArgumentParser(description="Инструмент DevOps-агента для Render")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name, handler, needs_name=True, help_text=None):
        sub = commands.add_parser(name, help=help_text)
        sub.set_defaults(handler=handler)
        if needs_name:
            sub.add_argument("--name", required=True, help="имя сервиса")
        return sub

    command("whoami", cmd_whoami, False, "показать workspace")
    command("list", cmd_list, False, "показать сервисы")

    create = command("create", cmd_create, help_text="создать сервис (идемпотентно)")
    create.add_argument("--repo", required=True, help="URL репозитория без ветки")
    create.add_argument("--branch")
    create.add_argument("--type", choices=("web", "static"), default="web")
    create.add_argument("--runtime", default="docker")
    create.add_argument("--plan", default="free")
    create.add_argument("--region", default="frankfurt")
    create.add_argument("--health", default="/health", help="путь проверки; пустая строка отключает")
    create.add_argument("--dockerfile", default="./Dockerfile")
    create.add_argument("--build-cmd")
    create.add_argument("--start-cmd")
    create.add_argument("--publish-path", default=".")
    create.add_argument("--allow-paid", action="store_true")

    command("status", cmd_status, help_text="сервис и последние деплои")

    deploys = command("deploys", cmd_deploys, help_text="история деплоев")
    deploys.add_argument("--limit", type=int, default=10)

    deploy = command("deploy", cmd_deploy, help_text="запустить деплой")
    deploy.add_argument("--commit")
    deploy.add_argument("--clear-cache", action="store_true")

    wait = command("wait", cmd_wait, help_text="дождаться конца деплоя")
    wait.add_argument("--commit", help="ждать деплой конкретного коммита")
    wait.add_argument("--timeout", type=int, default=900)
    wait.add_argument("--interval", type=float, default=10)

    logs = command("logs", cmd_logs, help_text="логи сервиса")
    logs.add_argument("--limit", type=int, default=50)
    logs.add_argument("--text", help="фильтр по тексту")
    logs.add_argument("--level")
    logs.add_argument("--type", choices=("app", "build", "request"))

    metrics = command("metrics", cmd_metrics, help_text="CPU, память, запросы")
    metrics.add_argument("--minutes", type=int, default=60)

    command("restart", cmd_restart, help_text="перезапустить сервис")

    rollback = command("rollback", cmd_rollback, help_text="откатить на прошлую версию")
    rollback.add_argument("--deploy-id")

    delete = command("delete", cmd_delete, help_text="удалить сервис")
    delete.add_argument("--yes", action="store_true")

    check = command("check", cmd_check, False, "проверить URL с учётом холодного старта")
    check.add_argument("--name")
    check.add_argument("--url")
    check.add_argument("--path", default="/health")
    check.add_argument("--timeout", type=float, default=150)
    check.add_argument("--interval", type=float, default=5)
    check.add_argument("--request-timeout", type=float, default=70)

    return parser


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    load_env()
    args = build_parser().parse_args()
    render = Render()
    try:
        args.handler(render, args)
    except ToolError as error:
        message = str(error)
        if render.key:
            message = message.replace(render.key, "***")
        emit({"error": message})
        raise SystemExit(1)


if __name__ == "__main__":
    main()
