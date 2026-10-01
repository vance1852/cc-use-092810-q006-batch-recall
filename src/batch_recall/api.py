"""无第三方依赖的批次召回 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import RecallError, ValidationFailed
from .service import RecallService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """将 HTTP 路由映射到召回领域服务，便于无网络单元测试。"""

    def __init__(self, service: RecallService) -> None:
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
        normalized_headers = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(
                    payload["user_id"], payload["display_name"], payload["role"]))
            if method == "POST" and path == "/assets":
                return Response(201, self.service.register_asset(
                    self._actor(normalized_headers), payload["asset_id"], payload["asset_kind"],
                    payload.get("capacity_kwh", "0"), bool(payload.get("running", True))))
            if method == "POST" and path == "/genealogy/edges":
                return Response(201, self.service.record_genealogy_edges(
                    self._actor(normalized_headers), payload.get("edges", [])))
            if method == "POST" and path == "/ownership/versions":
                return Response(201, self.service.record_ownership(
                    self._actor(normalized_headers), payload.get("versions", [])))
            if method == "POST" and path == "/notices":
                return Response(201, self.service.publish_notice(
                    self._actor(normalized_headers), payload))
            if method == "POST" and path == "/recalls":
                return Response(201, self.service.initiate_recall(
                    self._actor(normalized_headers), payload["recall_id"], payload["notice_id"],
                    int(payload.get("sla_hours", 72))))
            if method == "POST" and len(parts) == 3 and parts[0] == "recalls" and parts[2] == "expand":
                return Response(200, self.service.expand_scope(
                    self._actor(normalized_headers), parts[1], payload.get("reason", "新谱系证据扩散")))
            if method == "POST" and len(parts) == 3 and parts[0] == "recalls" and parts[2] == "reductions":
                return Response(201, self.service.request_scope_reduction(
                    self._actor(normalized_headers), parts[1], payload.get("asset_ids", []),
                    payload["reason"]))
            if method == "POST" and len(parts) == 4 and parts[0] == "recalls" \
                    and parts[2] == "reductions" and parts[3] == "review":
                return Response(200, self.service.review_scope_reduction(
                    self._actor(normalized_headers), parts[1], int(payload["scope_version"]),
                    bool(payload["approve"]), payload.get("note", "")))
            if method == "POST" and len(parts) == 4 and parts[0] == "recalls" \
                    and parts[2] == "assets" and parts[3] == "actions":
                return Response(201, self.service.record_action(
                    self._actor(normalized_headers), parts[1], payload["asset_id"], payload["stage"],
                    payload["idempotency_key"], evidence_sha256=payload.get("evidence_sha256"),
                    note=payload.get("note", ""), reject=bool(payload.get("reject", False))))
            if method == "POST" and len(parts) == 4 and parts[0] == "recalls" \
                    and parts[2] == "assets" and parts[3] == "unreachable":
                return Response(201, self.service.mark_unreachable(
                    self._actor(normalized_headers), parts[1], payload["asset_id"],
                    payload.get("note", "持有人无法联系")))
            if method == "POST" and path == "/escalations/sweep":
                return Response(200, self.service.sweep_overdue(
                    self._actor(normalized_headers), payload.get("recall_id")))
            if method == "POST" and len(parts) == 3 and parts[0] == "escalations" and parts[2] == "resolve":
                return Response(200, self.service.resolve_escalation(
                    self._actor(normalized_headers), int(parts[1]), payload.get("note", ""),
                    reengage=bool(payload.get("reengage", False))))
            if method == "GET" and len(parts) == 3 and parts[0] == "recalls" and parts[2] == "dashboard":
                return Response(200, self.service.dashboard(
                    self._actor(normalized_headers), parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "recalls" and parts[2] == "escalations":
                return Response(200, self.service.list_escalations(
                    self._actor(normalized_headers), parts[1], query.get("status", [None])[0]))
            if method == "GET" and len(parts) == 4 and parts[0] == "recalls" \
                    and parts[2] == "assets" :
                return Response(200, self.service.asset_detail(
                    self._actor(normalized_headers), parts[1], parts[3]))
            if method == "GET" and len(parts) == 4 and parts[0] == "recalls" \
                    and parts[2] == "scopes":
                return Response(200, self.service.scope_version(
                    self._actor(normalized_headers), parts[1], int(parts[3])))
            if method == "POST" and len(parts) == 5 and parts[0] == "recalls" \
                    and parts[2] == "scopes" and parts[4] == "recompute":
                return Response(200, self.service.recompute_scope(
                    self._actor(normalized_headers), parts[1], int(parts[3])))
            if method == "GET" and len(parts) == 3 and parts[0] == "recalls" and parts[2] == "history":
                return Response(200, self.service.recall_history(
                    self._actor(normalized_headers), parts[1]))
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(self._actor(normalized_headers)))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except RecallError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BatchRecall/1"

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
    parser = argparse.ArgumentParser(description="启动电芯批次召回编排 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("batch-recall.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    application = JsonApplication(RecallService(connection))
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
