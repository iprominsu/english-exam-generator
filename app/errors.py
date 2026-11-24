"""Public errors never contain upstream response bodies or credentials."""
import logging
import re

_logger = logging.getLogger(__name__)


class AppError(Exception):
    def __init__(self, message, status=503, code="temporary", retryable=False, **extra):
        super().__init__(message)
        self.status = status
        self.detail = dict(message=message, code=code, retryable=retryable, **extra)


def status_of(error):
    for value in (getattr(error, "code", None), getattr(error, "status_code", None),
                  getattr(getattr(error, "resp", None), "status", None),
                  getattr(getattr(error, "response", None), "status_code", None)):
        if isinstance(value, int):
            return value
    return None


def _log_unexpected(error):
    """Keep diagnostics useful without formatting exceptions or their payloads."""
    try:
        chain, seen = [], set()
        current = error
        while current is not None and id(current) not in seen and len(chain) < 4:
            seen.add(id(current))
            name = type(current).__name__
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name):
                name = "Exception"
            try:
                status = status_of(current)
            except Exception:
                status = None
            code = f":{status}" if type(status) is int and 100 <= status <= 599 else ""
            chain.append(name + code)
            cause = current.__cause__
            if cause is None and not current.__suppress_context__:
                cause = current.__context__
            current = cause
        _logger.error("Unexpected request error; chain=%s", " > ".join(chain))
    except Exception:
        # A diagnostic failure must not change the established public response.
        pass


def public_error(error):
    from fastapi import HTTPException
    from feature_word_meaning.drive_sync import DriveSyncError

    if isinstance(error, HTTPException):
        return error
    if isinstance(error, AppError):
        return HTTPException(error.status, error.detail)
    # DriveSyncError는 ValueError의 하위 클래스이므로 일반 500/422 판정보다 먼저 처리한다.
    if isinstance(error, DriveSyncError):
        code = error.code
        if code in {"account_mismatch", "ambiguous_destination", "ambiguous_target",
                    "ambiguous_record", "original_changed", "source_mismatch"}:
            status = 409
        elif code in {"invalid_data", "invalid_source", "invalid_destination", "invalid_existing_file",
                      "missing_source", "missing_pdf", "pdf_mismatch", "missing_bank_sheet",
                      "publication_mismatch", "cell_limit", "sheet_limit"}:
            status = 422
        else:
            status = 503
        retryable = status == 503 and code not in {"account_unverified", "missing_dependency"}
        return HTTPException(status, dict(code=code, retryable=retryable, message=str(error)))
    status = status_of(error)
    if status in (429, 500, 502, 503, 504):
        return HTTPException(503, dict(code="temporary", retryable=True,
            message="외부 서비스가 일시적으로 지연되고 있습니다. 잠시 후 다시 시도해 주세요."))
    if status in (401, 403):
        return HTTPException(503, dict(code="connection", retryable=False,
            message="연결 계정의 인증 또는 권한을 확인해야 합니다."))
    _log_unexpected(error)
    return HTTPException(500, dict(code="request_failed", retryable=False,
        message="요청을 완료하지 못했습니다. 작업 목록을 확인한 뒤 다시 시도해 주세요."))
