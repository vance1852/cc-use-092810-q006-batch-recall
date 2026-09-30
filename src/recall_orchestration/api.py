"""无第三方依赖的批次召回编排 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import RecallError, ValidationFailed
from .service import RecallOrchestrationService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """将 HTTP 路由映射到召回编排服务，便于无网络单元测试。"""

    def __init__(self, service: RecallOrchestrationService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            # /users 与健康检查不需要 X-Actor-Id，其余接口统一鉴权。
            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(
                    payload["user_id"], payload["display_name"], payload["role"]
                ))
            actor = self._actor(normalized)
            if method == "POST" and path == "/notices":
                return Response(201, self.service.create_notice(actor, payload))
            if method == "POST" and path == "/lineage/revisions":
                return Response(201, self.service.record_lineage_revision(actor, payload))
            if method == "POST" and len(parts) == 4 and parts[0] == "notices" and parts[2:4] == ["scope", "initial"]:
                return Response(201, self.service.compute_initial_scope(actor, parts[1], payload))
            if method == "POST" and len(parts) == 4 and parts[0] == "notices" and parts[2:4] == ["scope", "proposals"]:
                return Response(201, self.service.propose_scope_revision(actor, parts[1], payload))
            if method == "POST" and len(parts) == 4 and parts[0] == "notices" and parts[2:4] == ["scope", "shrinks"]:
                return Response(201, self.service.propose_shrink(actor, parts[1], payload))
            if (method == "POST" and len(parts) == 6 and parts[0] == "notices"
                    and parts[2:4] == ["scope", "versions"] and parts[5] == "decision"):
                return Response(200, self.service.decide_scope_revision(
                    actor, parts[1], int(parts[4]), bool(payload["approve"]), payload.get("note", "")
                ))
            if (method == "GET" and len(parts) == 5 and parts[0] == "notices"
                    and parts[2:4] == ["scope", "versions"]):
                return Response(200, self.service.scope_version(actor, parts[1], parts[4]))
            if method == "GET" and len(parts) == 4 and parts[0] == "notices" and parts[2:4] == ["scope", "history"]:
                return Response(200, self.service.scope_history(actor, parts[1]))
            if (method == "POST" and len(parts) == 6 and parts[0] == "notices"
                    and parts[2:4] == ["scope", "versions"] and parts[5] == "recompute"):
                return Response(200, self.service.recompute_scope(actor, parts[1], int(parts[4])))
            if (method == "POST" and len(parts) == 5 and parts[0] == "notices"
                    and parts[2] == "assets" and parts[4] == "receipts"):
                key = payload.get("idempotency_key") or normalized.get("idempotency-key", "")
                return Response(201, self.service.record_receipt(
                    actor, parts[1], parts[3], payload["action"], str(key),
                    holder_id=payload.get("holder_id"), note=payload.get("note", ""),
                ))
            if (method == "GET" and len(parts) == 5 and parts[0] == "notices"
                    and parts[2] == "assets" and parts[4] == "tracking"):
                return Response(200, self.service.asset_tracking(actor, parts[1], parts[3]))
            if (method == "POST" and len(parts) == 4 and parts[0] == "notices"
                    and parts[2] == "escalations" and parts[3] == "unreachable"):
                return Response(201, self.service.mark_unreachable(
                    actor, parts[1], payload["asset_id"], payload.get("detail", "")
                ))
            if method == "POST" and len(parts) == 4 and parts[0] == "escalations" and parts[2] == "resolve":
                return Response(200, self.service.resolve_escalation(
                    actor, int(parts[1]), payload.get("note", "")
                ))
            if method == "POST" and len(parts) == 4 and parts[0] == "notices" and parts[2:4] == ["overdue", "scan"]:
                return Response(200, self.service.scan_overdue(actor, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "notices" and parts[2] == "dashboard":
                return Response(200, self.service.dashboard(actor, parts[1]))
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except RecallError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RecallOrchestration/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动批次召回编排 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("recall_orchestration.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    application = JsonApplication(RecallOrchestrationService(connection))
    server = ThreadingHTTPServer((args.host, args.port), make_handler(application))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
