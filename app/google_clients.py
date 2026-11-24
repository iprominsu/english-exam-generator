"""구글 Drive/Sheets 클라이언트.

콜랩의 `auth.authenticate_user()`(대화형 로그인) 대신, 최초 1회
scripts/get_refresh_token.py 로 발급받은 refresh token으로 서버가
'주인 계정' 권한을 계속 재사용한다. 그래서 생성되는 파일의 소유자가
서비스 계정이 아니라 사용자 본인이 되어 My Drive에 그대로 보인다.
"""

import functools
import io
import threading
import time
from collections import deque
from copy import deepcopy
from .errors import status_of

import gspread
from gspread.http_client import HTTPClient
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from . import config

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]


class _ReadQuota:
    """Share the owner's Sheets read budget across all worker threads."""

    def __init__(self, limit=45, window=60.0, *, clock=None, sleeper=None):
        self.limit = limit
        self.window = window
        self._clock = clock or time.monotonic
        self._sleep = sleeper or time.sleep
        self._calls = deque()
        self._not_before = 0.0
        self._lock = threading.Lock()

    def acquire(self):
        while True:
            with self._lock:
                now = self._clock()
                while self._calls and now - self._calls[0] >= self.window:
                    self._calls.popleft()
                delay = max(0.0, self._not_before - now)
                if len(self._calls) >= self.limit:
                    delay = max(delay, self.window + 0.05 - (now - self._calls[0]))
                if delay <= 0:
                    self._calls.append(now)
                    return
            # The lock is released while waiting, so one thread cannot block
            # completed reads or writes on another thread.
            self._sleep(min(delay, self.window))

    def defer(self, seconds):
        with self._lock:
            self._not_before = max(self._not_before, self._clock() + seconds)


_sheets_read_quota = _ReadQuota()


class QuotaAwareHTTPClient(HTTPClient):
    """Pace/retry reads only; never replay a possibly completed write."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_metadata = None

    def request(self, method, endpoint, *args, **kwargs):
        if method.lower() != "get":
            # Even a failed write may have reached Google. Discard the tiny
            # metadata reuse window before sending any mutation.
            self._last_metadata = None
            return super().request(method, endpoint, *args, **kwargs)
        for attempt in range(3):
            _sheets_read_quota.acquire()
            try:
                return super().request(method, endpoint, *args, **kwargs)
            except Exception as error:
                status = status_of(error)
                if status not in (429, 500, 502, 503, 504):
                    raise
                if status == 429:
                    # Other actors using the same owner account can consume
                    # quota too. Wait through one full refill window.
                    retry_after = getattr(getattr(error, "response", None), "headers", {}).get("Retry-After", "")
                    try:
                        delay = max(60.05, float(retry_after))
                    except (TypeError, ValueError):
                        delay = 60.05
                    _sheets_read_quota.defer(min(delay, 120.0))
                elif attempt < 2:
                    time.sleep(2 ** attempt)
                if attempt == 2:
                    raise

    def fetch_sheet_metadata(self, id, params=None):
        # gspread open_by_key immediately reads the same metadata again for
        # sheet1. Reuse that response once for at most one second. No values
        # are cached, custom metadata queries always read fresh, and all
        # writes invalidate this reuse before the request is sent.
        previous, self._last_metadata = self._last_metadata, None
        if params is None and previous and previous[0] == id and time.monotonic() - previous[1] < 1.0:
            return deepcopy(previous[2])
        result = super().fetch_sheet_metadata(id, params=params)
        if params is None:
            self._last_metadata = (id, time.monotonic(), deepcopy(result))
        return result


@functools.lru_cache(maxsize=1)
def _credentials() -> Credentials:
    if not (
        config.GOOGLE_OAUTH_CLIENT_ID
        and config.GOOGLE_OAUTH_CLIENT_SECRET
        and config.GOOGLE_OAUTH_REFRESH_TOKEN
    ):
        raise RuntimeError(
            "구글 OAuth 설정이 없습니다. GOOGLE_OAUTH_CLIENT_ID / "
            "GOOGLE_OAUTH_CLIENT_SECRET / GOOGLE_OAUTH_REFRESH_TOKEN 환경변수를 "
            "설정하세요. scripts/get_refresh_token.py 로 먼저 refresh token을 "
            "발급받아야 합니다."
        )
    creds = Credentials(
        token=None,
        refresh_token=config.GOOGLE_OAUTH_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=config.GOOGLE_OAUTH_CLIENT_ID,
        client_secret=config.GOOGLE_OAUTH_CLIENT_SECRET,
        scopes=SCOPES,
    )
    creds.refresh(GoogleAuthRequest())
    return creds


_local = threading.local()  # 스레드마다 하나씩 — httplib2/requests 세션을 스레드 간 공유하지 않기 위함


def drive_service():
    # httplib2 기반이라 여러 스레드가 같은 인스턴스를 동시에 쓰면 TLS 연결이 깨진다.
    # 그렇다고 매번 새로 만들면 접속마다 새 TLS 핸드셰이크가 필요해 간헐적으로 끊긴다.
    # → 스레드별로 하나씩만 만들어 재사용한다.
    svc = getattr(_local, "drive", None)
    if svc is None:
        svc = build("drive", "v3", credentials=_credentials(), cache_discovery=False)
        _local.drive = svc
    return svc


def sheets_client():
    cli = getattr(_local, "sheets", None)
    if cli is None:
        cli = gspread.authorize(_credentials(), http_client=QuotaAwareHTTPClient)
        _local.sheets = cli
    return cli


def download_to_path(file_id: str, dest_path: str) -> None:
    request = drive_service().files().get_media(fileId=file_id)
    with io.FileIO(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def upload_bytes(data: bytes, name: str, mime: str, folder_id: str, file_id: str = None) -> dict:
    """xlsx 업로드처럼 메모리의 바이트를 그대로 드라이브에 올릴 때 사용.

    디스크에 임시파일을 쓰지 않는다 — 굳이 쓰고 지우려 하면 Windows에서는
    MediaFileUpload가 아직 파일 핸들을 쥐고 있어 삭제가 실패할 수 있다.
    """
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)
    if file_id:
        return drive_service().files().update(
            fileId=file_id,
            media_body=media,
            fields="id,name,webViewLink,modifiedTime",
        ).execute()
    return (
        drive_service()
        .files()
        .create(
            body={"name": name, "parents": [folder_id]},
            media_body=media,
            fields="id,name,webViewLink,modifiedTime",
        )
        .execute()
    )


def read_with_retry(call):
    """Only read operations; never automatically replay creates or AI calls."""
    for attempt in range(3):
        try:
            return call()
        except Exception as error:
            if status_of(error) not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
            time.sleep(2 ** attempt)
