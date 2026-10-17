import os
import sys
import shutil
import urllib.parse
from pathlib import Path

# Add root directory to sys.path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# On Vercel / AWS Lambda serverless execution environments:
# 1. /tmp is the only writable directory for SQLite databases
# 2. Keydir for ML-DSA-65 post-quantum keys must be in /tmp
if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    tmp_db = Path("/tmp/denials.db")
    if not tmp_db.exists():
        src_db = ROOT / "denials.db"
        if src_db.exists():
            try:
                shutil.copy2(src_db, tmp_db)
            except Exception:
                pass
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_db}"
    os.environ.setdefault("MEDRECOVER_KEYDIR", "/tmp/.medrecover")

from backend.main import app as fastapi_app

async def app(scope, receive, send):
    """ASGI entrypoint with path-rewriting for Vercel serverless functions."""
    if scope["type"] == "http":
        path = scope.get("path", "")
        headers = dict(scope.get("headers", []))
        forwarded_path = None

        # 1. Route passed via __vercel_route__ query param
        query_bytes = scope.get("query_string", b"")
        if query_bytes and b"__vercel_route__=" in query_bytes:
            try:
                qs_str = query_bytes.decode("utf-8", errors="replace")
                parsed_qs = urllib.parse.parse_qs(qs_str)
                if "__vercel_route__" in parsed_qs:
                    val = parsed_qs["__vercel_route__"][0]
                    if val:
                        forwarded_path = val if val.startswith("/") else "/" + val
                    del parsed_qs["__vercel_route__"]
                    scope["query_string"] = urllib.parse.urlencode(parsed_qs, doseq=True).encode("utf-8")
            except Exception:
                pass

        # 2. Valid application route
        if not forwarded_path and path and not path.startswith("/api/index.py") and not path.startswith("/api/index") and path != "/api":
            await fastapi_app(scope, receive, send)
            return

        # 3. x-now-route-matches header
        if not forwarded_path and b"x-now-route-matches" in headers:
            try:
                matches = headers[b"x-now-route-matches"].decode("utf-8")
                parsed = urllib.parse.parse_qs(matches)
                if "1" in parsed and parsed["1"]:
                    candidate = parsed["1"][0]
                    if candidate:
                        forwarded_path = candidate if candidate.startswith("/") else "/" + candidate
            except Exception:
                pass

        # 4. Proxy path headers
        if not forwarded_path:
            for h in [b"x-invoke-path", b"x-matched-path", b"x-forwarded-uri"]:
                if h in headers:
                    val = headers[h].decode("utf-8", errors="replace")
                    if val and not val.startswith("/api/index.py") and not val.startswith("/api/index") and val != "/api":
                        candidate = val.split("?")[0]
                        forwarded_path = candidate if candidate.startswith("/") else "/" + candidate
                        break

        # 5. Fallback path resolution
        if forwarded_path:
            scope["path"] = forwarded_path
            if "raw_path" in scope:
                scope["raw_path"] = forwarded_path.encode("ascii", "replace")
        else:
            for prefix in ["/api/index.py", "/api/index", "/api"]:
                if path == prefix:
                    scope["path"] = "/"
                    if "raw_path" in scope:
                        scope["raw_path"] = b"/"
                    break
                elif path.startswith(prefix + "/"):
                    clean_path = path[len(prefix):]
                    scope["path"] = clean_path
                    if "raw_path" in scope:
                        scope["raw_path"] = clean_path.encode("ascii", "replace")
                    break

    await fastapi_app(scope, receive, send)
