"""召回服务层可观察错误。"""


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
