"""批次召回编排服务向 API 和 CLI 暴露的稳定错误。"""


class RecallError(RuntimeError):
    code = "recall_error"
    status = 400


class NotFound(RecallError):
    code = "not_found"
    status = 404


class Conflict(RecallError):
    code = "conflict"
    status = 409


class Forbidden(RecallError):
    code = "forbidden"
    status = 403


class InvalidState(RecallError):
    code = "invalid_state"
    status = 409


class ValidationFailed(RecallError):
    code = "validation_failed"
    status = 422
