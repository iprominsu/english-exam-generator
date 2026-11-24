import os

from dotenv import load_dotenv

load_dotenv()  # 로컬 개발용 .env 로드. 배포 환경(Cloud Run)엔 .env가 없으므로 no-op.


def _get(name, default=None):
    return os.environ.get(name, default)


# 접속 보호 (HTTP Basic Auth — 브라우저 네이티브 팝업, 별도 로그인 화면 불필요)
# 비워두면(로컬 개발 등) 인증을 건너뛴다.
APP_BASIC_USER = _get("APP_BASIC_USER", "admin")
APP_BASIC_PASS = _get("APP_BASIC_PASS", "")

# 브랜드 / 드라이브 폴더
BRAND = _get("BRAND", "Melody English")
PROJECT_FOLDER = _get("PROJECT_FOLDER", "프로젝트_멜로디잉글리시")

# 구글 OAuth (사용자 본인 계정으로 Drive/Sheets에 접근 — 서비스 계정 아님)
GOOGLE_OAUTH_CLIENT_ID = _get("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_OAUTH_CLIENT_SECRET = _get("GOOGLE_OAUTH_CLIENT_SECRET")
GOOGLE_OAUTH_REFRESH_TOKEN = _get("GOOGLE_OAUTH_REFRESH_TOKEN")

# LLM
LLM_PROVIDERS = ("gpt", "gemini")
GPT_MODEL = "gpt-6.1-sol"
GPT_REASONING_LEVELS = ("low", "medium", "high", "xhigh")
GPT_REASONING_DEFAULT = "high"
CODEX_BINARY = _get("CODEX_BINARY", "codex")
CODEX_AUTH_FILE = _get("CODEX_AUTH_FILE", "")
CODEX_AUTH_SECRET = _get("CODEX_AUTH_SECRET", "")
CODEX_TIMEOUT = int(_get("CODEX_TIMEOUT", "720"))
IMPORT_ENGINE_ENABLED = _get("IMPORT_ENGINE_ENABLED", "false").lower() == "true"
IMPORT_HOSTED_AUTH_CONFIRMED = _get("IMPORT_HOSTED_AUTH_CONFIRMED", "false").lower() == "true"
IMPORT_PERSONAL_USE_CONFIRMED = _get("IMPORT_PERSONAL_USE_CONFIRMED", "false").lower() == "true"
GEMINI_MODEL = _get("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_TEMPERATURE = float(_get("GEMINI_TEMPERATURE", "0.6"))

MAX_OUTPUT_TOKENS = int(_get("MAX_OUTPUT_TOKENS", "48000"))
MAX_RETRY = int(_get("MAX_RETRY", "3"))
EXAM_COUNT_DEFAULT = int(_get("EXAM_COUNT_DEFAULT", "10"))

# 스캔 대상 Day 범위 (목록 조회 시 드라이브에서 뒤질 폴더 개수 상한)
DAY_SCAN_MAX = int(_get("DAY_SCAN_MAX", "40"))


def day_title(day: int) -> str:
    return f"부스터보카 수능 DAY {day}"


def day_container_title(day: int, book_title: str = "부스터보카 수능") -> str:
    return f"{book_title} DAY {day}"
