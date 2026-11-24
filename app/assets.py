"""A release fingerprint changes whenever HTML, JS, CSS or app contracts change."""
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSET_NAMES = (
    "app.js", "workspace.js", "vocabulary-editor.js", "vocabulary-editor.css", "contracts.js", "worksheet-ui.js", "worksheet-delete.js", "worksheet-download.js", "worksheet-download.css", "styles.css", "word-meaning.css",
    "design-system/tokens.css", "design-system/base.css",
    "design-system/components.css", "design-system/components.js",
    "design-system/icons.js", "design-system/icons.svg",
)
STATIC_URLS = frozenset("/static/" + name for name in ASSET_NAMES)
FILES = sorted((ROOT / "app").glob("*.py")) + [ROOT / "static" / n for n in ("index.html", "about.html", "privacy.html", *ASSET_NAMES)]
FILES += sorted((ROOT / "feature_word_meaning").glob("*.py")) + [ROOT / "feature_word_meaning/static/index.html"]
BUILD_ID = hashlib.sha256(b"".join(p.relative_to(ROOT).as_posix().encode() + p.read_bytes() for p in FILES)).hexdigest()[:16]
CONFIG = {"build": BUILD_ID, "providers": ["gpt", "gemini"],
          "reasoning": ["low", "medium", "high", "xhigh"], "max_count": 20, "max_batch": 100}


def versioned_html(text):
    """Version known product assets without importing the production app."""
    def replace(match):
        prefix, url, quote = match.groups()
        resource, separator, fragment = url.partition("#")
        path = resource.split("?", 1)[0]
        versioned = path + "?v=" + BUILD_ID + (separator + fragment if separator else "")
        return prefix + (versioned if path in STATIC_URLS else url) + quote
    return re.sub(r'''((?:src|href)=["'])(/static/[^"']+)(["'])''', replace, text)


def index_html():
    text = versioned_html((ROOT / "static/index.html").read_text(encoding="utf-8"))
    return text.replace("</head>", '<meta name="app-build" content="' + BUILD_ID + '">'
        + '<script>window.APP_CONFIG=' + json.dumps(CONFIG) + ';</script></head>')
