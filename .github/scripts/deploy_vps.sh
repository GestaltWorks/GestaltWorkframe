#!/usr/bin/env bash
set -euo pipefail

: "${VPS_HOST:?VPS_HOST is required}"
: "${VPS_USER:?VPS_USER is required}"
: "${APP_DIR:?APP_DIR is required}"
: "${WEB_DIR:?WEB_DIR is required}"
: "${SERVICE_NAME:?SERVICE_NAME is required}"
: "${API_PORT:?API_PORT is required}"

SSH_KEY_PATH="${SSH_KEY_PATH:-$HOME/.ssh/deploy_vps}"
SSH_TARGET="${VPS_USER}@${VPS_HOST}"

ssh_remote() {
  ssh -i "$SSH_KEY_PATH" "$SSH_TARGET" "$@"
}

reject_multiline_env_value() {
  local secret_name="$1"
  local secret_value="$2"

  if [[ "$secret_value" == *$'\n'* || "$secret_value" == *$'\r'* ]]; then
    echo "::error::$secret_name must be a single-line value before syncing to .env. Use unwrapped base64 for encoded secrets." >&2
    exit 1
  fi
}

ssh_remote "sudo mkdir -p '$APP_DIR' '$WEB_DIR/out' && sudo chown -R '$VPS_USER:$VPS_USER' '$APP_DIR' '$WEB_DIR'"

sync_remote_env_secret() {
  local secret_name="$1"
  local secret_value="${!secret_name:-}"

  if [[ -z "$secret_value" ]]; then
    return 0
  fi
  reject_multiline_env_value "$secret_name" "$secret_value"

  {
    cat <<'PY'
import os
from pathlib import Path

name = os.environ["SECRET_NAME"]
value = SECRET_VALUE
path = Path(os.environ["APP_DIR"]) / ".env"
path.parent.mkdir(parents=True, exist_ok=True)
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
prefix = f"{name}="
updated = False
out = []
for line in lines:
    if line.startswith(prefix):
        out.append(prefix + value)
        updated = True
    else:
        out.append(line)
if not updated:
    out.append(prefix + value)
path.write_text("\n".join(out) + "\n", encoding="utf-8")
path.chmod(0o600)
PY
    printf '\0'
    printf '%s' "$secret_value"
  } | ssh -i "$SSH_KEY_PATH" "$SSH_TARGET" \
    "APP_DIR='$APP_DIR' SECRET_NAME='$secret_name' python3 -c 'import sys; data = sys.stdin.buffer.read(); script, value = data.split(b\"\\0\", 1); exec(compile(script.decode(), \"<env-sync>\", \"exec\"), {\"SECRET_VALUE\": value.decode()})'"
}

sync_remote_env_value() {
  local secret_name="$1"
  local secret_value="$2"

  if [[ -z "$secret_value" ]]; then
    return 0
  fi
  reject_multiline_env_value "$secret_name" "$secret_value"

  {
    cat <<'PY'
import os
from pathlib import Path

name = os.environ["SECRET_NAME"]
value = SECRET_VALUE
path = Path(os.environ["APP_DIR"]) / ".env"
path.parent.mkdir(parents=True, exist_ok=True)
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
prefix = f"{name}="
updated = False
out = []
for line in lines:
    if line.startswith(prefix):
        out.append(prefix + value)
        updated = True
    else:
        out.append(line)
if not updated:
    out.append(prefix + value)
path.write_text("\n".join(out) + "\n", encoding="utf-8")
path.chmod(0o600)
PY
    printf '\0'
    printf '%s' "$secret_value"
  } | ssh -i "$SSH_KEY_PATH" "$SSH_TARGET" \
    "APP_DIR='$APP_DIR' SECRET_NAME='$secret_name' python3 -c 'import sys; data = sys.stdin.buffer.read(); script, value = data.split(b\"\\0\", 1); exec(compile(script.decode(), \"<env-sync>\", \"exec\"), {\"SECRET_VALUE\": value.decode()})'"
}

sync_remote_env_secret "ANTHROPIC_API_KEY"
sync_remote_env_secret "GEMINI_CLOUD_API_KEY"
sync_remote_env_secret "GEMINI_CLOUD_BASE_URL"
sync_remote_env_secret "GEMINI_CLOUD_MODEL"
sync_remote_env_secret "ADMIN_POLICY_TOKEN"
sync_remote_env_secret "APP_GITHUB_TOKEN"
sync_remote_env_secret "LIBRARY_PUBLISHER_GITHUB_APP_ID"
sync_remote_env_secret "LIBRARY_PUBLISHER_GITHUB_INSTALLATION_ID"
sync_remote_env_secret "LIBRARY_PUBLISHER_GITHUB_PRIVATE_KEY_B64"
sync_remote_env_secret "LIBRARY_PUBLISHER_REPO"
sync_remote_env_secret "LIBRARY_PUBLISHER_BASE_BRANCH"
sync_remote_env_secret "ENABLE_CLOUD_SPILLOVER"
sync_remote_env_secret "ENABLE_LOW_COST_CLOUD"
sync_remote_env_secret "ENABLE_CLAUDE_FALLBACK"
sync_remote_env_secret "CLOUD_SPILLOVER_MAX_CALLS_PER_TURN"
sync_remote_env_secret "CLOUD_SPILLOVER_MAX_CALLS_PER_SESSION"
sync_remote_env_secret "CLOUD_SPILLOVER_MAX_CALLS_PER_DAY"
sync_remote_env_secret "CLOUD_SPILLOVER_MAX_CALLS_PER_MONTH"
sync_remote_env_secret "CLOUD_SPILLOVER_MAX_DAILY_USD"
sync_remote_env_secret "CLOUD_SPILLOVER_MAX_MONTHLY_USD"
sync_remote_env_secret "CLOUD_SPILLOVER_MAX_INPUT_TOKENS_PER_CALL"
sync_remote_env_secret "CLOUD_SPILLOVER_MAX_OUTPUT_TOKENS_PER_CALL"

rsync -az --delete \
  --exclude '.git/' \
  --exclude '.github/' \
  --exclude '.venv/' \
  --exclude '.ssh/' \
  --exclude '.env' \
  --exclude 'database.db' \
  --exclude 'gestaltworkframe/kb/chroma_db/' \
  --exclude 'web/node_modules/' \
  --exclude 'web/.next/' \
  --exclude 'web/out/' \
  -e "ssh -i $SSH_KEY_PATH" \
  ./ "$SSH_TARGET:$APP_DIR/"

rsync -az --delete \
  -e "ssh -i $SSH_KEY_PATH" \
  web/out/ "$SSH_TARGET:$WEB_DIR/out/"

if [[ -n "${ROOT_SITE_DIR:-}" ]]; then
  ssh_remote "sudo mkdir -p '$ROOT_SITE_DIR'"
  ssh_remote "if [[ -f '$WEB_DIR/out/sitemap.xml' ]]; then sudo cp '$WEB_DIR/out/sitemap.xml' '$ROOT_SITE_DIR/sitemap.xml'; fi"
  ssh_remote "if [[ -f '$WEB_DIR/out/robots.txt' ]]; then sudo cp '$WEB_DIR/out/robots.txt' '$ROOT_SITE_DIR/robots.txt'; fi"
  ssh_remote "if [[ -f '$WEB_DIR/out/llms.txt' ]]; then sudo cp '$WEB_DIR/out/llms.txt' '$ROOT_SITE_DIR/llms.txt'; fi"
fi

if [[ -n "${NGINX_SITE_FILE:-}" ]]; then
  ssh -i "$SSH_KEY_PATH" "$SSH_TARGET" \
    "sudo NGINX_SITE_FILE='$NGINX_SITE_FILE' API_PORT='$API_PORT' WEB_DIR='$WEB_DIR' bash -s" <<'REMOTE'
set -euo pipefail

if [[ ! -f "$NGINX_SITE_FILE" ]]; then
  _site_name="${SERVER_NAME:-$(hostname -f 2>/dev/null || echo localhost)}"
  sudo mkdir -p "$(dirname "$NGINX_SITE_FILE")"
  sudo tee "$NGINX_SITE_FILE" > /dev/null <<NGINX_BASE
server {
    listen 80;
    listen [::]:80;
    server_name ${_site_name};

    location /health {
        proxy_pass http://127.0.0.1:${API_PORT}/health;
    }

    location /contact {
        proxy_pass http://127.0.0.1:${API_PORT}/contact;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
NGINX_BASE
  sudo ln -sf "$NGINX_SITE_FILE" "/etc/nginx/sites-enabled/$(basename "$NGINX_SITE_FILE")" 2>/dev/null || true
  echo "Scaffolded new nginx site file: $NGINX_SITE_FILE"
fi

# Root cutover: production / now serves this repo's terminal landing page.
# Keep /terminal as a hidden noindex alias below, but make the canonical public
# entrypoint the exported Next.js index.html. Use an exact-match location so it
# safely overrides any legacy prefix/root config from the old coming-soon site.
if ! grep -A6 'location = / {' "$NGINX_SITE_FILE" | grep -q 'try_files /index.html =404'; then
  cp "$NGINX_SITE_FILE" "$NGINX_SITE_FILE.bak.$(date +%Y%m%d%H%M%S)"
  python3 - "$NGINX_SITE_FILE" "$WEB_DIR" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
web_dir = sys.argv[2]
text = path.read_text()
block = f"""    location = / {{
        default_type text/html;
        root {web_dir}/out;
        try_files /index.html =404;
    }}

"""
pattern = re.compile(r"\n?    location = / \{\n(?:        .*\n)*?    \}\n", re.MULTILINE)
if pattern.search(text):
    text = pattern.sub("\n" + block, text, count=1)
else:
    marker = "    location = /terminal {\n"
    if marker not in text:
        marker = "    location /health {\n"
    if marker not in text:
        raise SystemExit("expected /terminal or /health location marker not found")
    text = text.replace(marker, block + marker, 1)
path.write_text(text)
PY
fi

if ! grep -q 'location = /chat/stream' "$NGINX_SITE_FILE"; then
  cp "$NGINX_SITE_FILE" "$NGINX_SITE_FILE.bak.$(date +%Y%m%d%H%M%S)"
  python3 - "$NGINX_SITE_FILE" "$API_PORT" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
port = sys.argv[2]
text = path.read_text()
marker = f"""    location /health {{
        proxy_pass http://127.0.0.1:{port}/health;
    }}
"""
block = f"""

    location = /modes {{
        proxy_pass http://127.0.0.1:{port}/modes;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}

    location = /intake/questions {{
        proxy_pass http://127.0.0.1:{port}/intake/questions;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}

    location = /intake/submissions {{
        proxy_pass http://127.0.0.1:{port}/intake/submissions;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}

    location = /chat/stream {{
        proxy_pass http://127.0.0.1:{port}/chat/stream;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
"""
if marker not in text:
    raise SystemExit("expected /health location marker not found")
path.write_text(text.replace(marker, marker + block))
PY
fi

if ! grep -q 'location /newsletter/' "$NGINX_SITE_FILE"; then
  cp "$NGINX_SITE_FILE" "$NGINX_SITE_FILE.bak.$(date +%Y%m%d%H%M%S)"
  python3 - "$NGINX_SITE_FILE" <<'PY'
from pathlib import Path
import os
import sys

path = Path(sys.argv[1])
port = os.environ["API_PORT"]
text = path.read_text()
marker = "    location /contact {\n"
block = f"""    location /newsletter/ {{
        proxy_pass http://127.0.0.1:{port}/newsletter/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}

"""
if marker not in text:
    raise SystemExit("expected /contact location marker not found")
path.write_text(text.replace(marker, block + marker, 1))
PY
fi

# Public newsletter signup page (static export). Exact-match location so
# GET /newsletter/subscribe serves the Next.js HTML page and POST
# /newsletter/api/subscribe falls through to the /newsletter/ prefix
# proxy block above.
if ! grep -q 'location = /newsletter/subscribe' "$NGINX_SITE_FILE"; then
  cp "$NGINX_SITE_FILE" "$NGINX_SITE_FILE.bak.$(date +%Y%m%d%H%M%S)"
  python3 - "$NGINX_SITE_FILE" "$WEB_DIR" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
web_dir = sys.argv[2]
text = path.read_text()
marker = "    location /newsletter/ {\n"
block = f"""    location = /newsletter/subscribe {{
        default_type text/html;
        alias {web_dir}/out/newsletter/subscribe.html;
    }}

"""
if marker not in text:
    raise SystemExit("expected /newsletter/ prefix marker not found")
path.write_text(text.replace(marker, block + marker, 1))
PY
fi

if ! grep -q 'location = /intake/submissions' "$NGINX_SITE_FILE"; then
  cp "$NGINX_SITE_FILE" "$NGINX_SITE_FILE.bak.$(date +%Y%m%d%H%M%S)"
  python3 - "$NGINX_SITE_FILE" <<'PY'
from pathlib import Path
import os
import sys

path = Path(sys.argv[1])
port = os.environ["API_PORT"]
text = path.read_text()
marker = f"""    location = /intake/questions {{
        proxy_pass http://127.0.0.1:{port}/intake/questions;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
"""
block = f"""

    location = /intake/submissions {{
        proxy_pass http://127.0.0.1:{port}/intake/submissions;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
"""
if marker not in text:
    raise SystemExit("expected /intake/questions location marker not found")
path.write_text(text.replace(marker, marker + block, 1))
PY
fi

if ! grep -q 'location = /admin/health' "$NGINX_SITE_FILE"; then
  cp "$NGINX_SITE_FILE" "$NGINX_SITE_FILE.bak.$(date +%Y%m%d%H%M%S)"
  python3 - "$NGINX_SITE_FILE" "$WEB_DIR" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
web_dir = sys.argv[2]
text = path.read_text()
marker = """    location /health {
"""
block = f"""
    location = /admin/health {{
        alias {web_dir}/out/admin/health.html;
        default_type text/html;
        add_header X-Robots-Tag "noindex, nofollow" always;
    }}

    location /admin/ {{
        alias {web_dir}/out/admin/;
        try_files $uri $uri.html $uri/ =404;
        add_header X-Robots-Tag "noindex, nofollow" always;
    }}

"""
if marker not in text:
    raise SystemExit("expected /health location marker not found")
path.write_text(text.replace(marker, block + marker))
PY
fi

if grep -q 'location = /admin/health' "$NGINX_SITE_FILE" && ! grep -A4 'location = /admin/health' "$NGINX_SITE_FILE" | grep -q 'default_type text/html'; then
  cp "$NGINX_SITE_FILE" "$NGINX_SITE_FILE.bak.$(date +%Y%m%d%H%M%S)"
  python3 - "$NGINX_SITE_FILE" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
old = """    location = /admin/health {
        alias """
if old not in text:
    raise SystemExit("expected /admin/health alias not found")
text = text.replace(old, """    location = /admin/health {
        default_type text/html;
        alias """, 1)
path.write_text(text)
PY
fi

if ! grep -q 'location /admin/api/' "$NGINX_SITE_FILE"; then
  cp "$NGINX_SITE_FILE" "$NGINX_SITE_FILE.bak.$(date +%Y%m%d%H%M%S)"
  python3 - "$NGINX_SITE_FILE" <<'PY'
from pathlib import Path
import os
import sys

path = Path(sys.argv[1])
port = os.environ["API_PORT"]
text = path.read_text()
marker = """    location = /admin/health {
"""
block = f"""
    location /admin/api/ {{
        proxy_pass http://127.0.0.1:{port};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}

"""
if marker not in text:
    raise SystemExit("expected /admin/health location marker not found")
path.write_text(text.replace(marker, block + marker))
PY
fi

if ! grep -q 'location = /terminal' "$NGINX_SITE_FILE"; then
  cp "$NGINX_SITE_FILE" "$NGINX_SITE_FILE.bak.$(date +%Y%m%d%H%M%S)"
  python3 - "$NGINX_SITE_FILE" "$WEB_DIR" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
web_dir = sys.argv[2]
text = path.read_text()
marker = """    location /health {
"""
block = f"""    location = /terminal {{
        default_type text/html;
        alias {web_dir}/out/terminal.html;
        add_header X-Robots-Tag "noindex, nofollow" always;
    }}

"""
if marker not in text:
    raise SystemExit("expected /health location marker not found")
path.write_text(text.replace(marker, block + marker, 1))
PY
fi

if ! grep -q 'location = /about' "$NGINX_SITE_FILE"; then
  cp "$NGINX_SITE_FILE" "$NGINX_SITE_FILE.bak.$(date +%Y%m%d%H%M%S)"
  python3 - "$NGINX_SITE_FILE" "$WEB_DIR" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
web_dir = sys.argv[2]
text = path.read_text()
marker = """    location = /terminal {
"""
block = f"""    location = /about {{
        default_type text/html;
        alias {web_dir}/out/about.html;
    }}

"""
if marker not in text:
    raise SystemExit("expected /terminal location marker not found")
path.write_text(text.replace(marker, block + marker, 1))
PY
fi

# /privacy is the static privacy policy page. Follows the same alias pattern
# as /about. Added in the audit-cleanup arc when the page first shipped;
# without this block nginx 404s the route even though the static export
# does include out/privacy.html.
if ! grep -q 'location = /privacy' "$NGINX_SITE_FILE"; then
  cp "$NGINX_SITE_FILE" "$NGINX_SITE_FILE.bak.$(date +%Y%m%d%H%M%S)"
  python3 - "$NGINX_SITE_FILE" "$WEB_DIR" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
web_dir = sys.argv[2]
text = path.read_text()
marker = """    location = /about {
"""
block = f"""    location = /privacy {{
        default_type text/html;
        alias {web_dir}/out/privacy.html;
    }}

"""
if marker not in text:
    raise SystemExit("expected /about location marker not found")
path.write_text(text.replace(marker, block + marker, 1))
PY
fi

if ! grep -q 'location = /product-brief' "$NGINX_SITE_FILE" || ! grep -q 'location = /product-brief.pdf' "$NGINX_SITE_FILE" || ! grep -q 'location /product-brief/assets/' "$NGINX_SITE_FILE"; then
  cp "$NGINX_SITE_FILE" "$NGINX_SITE_FILE.bak.$(date +%Y%m%d%H%M%S)"
  python3 - "$NGINX_SITE_FILE" "$WEB_DIR" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
web_dir = sys.argv[2]
text = path.read_text()
marker = """    location = /privacy {
"""
blocks = []
if "    location = /product-brief {\n" not in text:
    blocks.append(f"""    location = /product-brief {{
        default_type text/html;
        alias {web_dir}/out/product-brief/index.html;
    }}
""")
if "    location = /product-brief.pdf {\n" not in text:
    blocks.append(f"""    location = /product-brief.pdf {{
        default_type application/pdf;
        alias {web_dir}/out/product-brief.pdf;
        add_header Content-Disposition "attachment; filename=product-brief.pdf" always;
    }}
""")
if "    location /product-brief/assets/ {\n" not in text:
    blocks.append(f"""    location /product-brief/assets/ {{
        alias {web_dir}/out/product-brief/assets/;
        try_files $uri =404;
        add_header Cache-Control "public, max-age=86400" always;
    }}
""")
if blocks:
    if marker not in text:
        raise SystemExit("expected /privacy location marker not found")
    text = text.replace(marker, "\n".join(blocks) + "\n" + marker, 1)
path.write_text(text)
PY
fi

LIBRARY_NGINX_TMP="$NGINX_SITE_FILE.library.$$"
set +e
python3 - "$NGINX_SITE_FILE" "$WEB_DIR" "$LIBRARY_NGINX_TMP" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
web_dir = sys.argv[2]
tmp_path = Path(sys.argv[3])
original = path.read_text()
text = original
marker = """    location = /about {
"""
library_block = f"""    location = /library {{
        default_type text/html;
        alias {web_dir}/out/library.html;
    }}
"""
library_slash_block = """    location = /library/ {
        return 301 /library;
    }
"""

text = re.sub(
    r"\n?    location = /library/ \{\n.*?\n    \}\n",
    "\n" + library_slash_block,
    text,
    count=1,
    flags=re.S,
)
blocks = []
if "    location = /library {\n" not in text:
    blocks.append(library_block)
if "    location = /library/ {\n" not in text:
    blocks.append(library_slash_block)
if blocks:
    if marker not in text:
        raise SystemExit("expected /about location marker not found")
    text = text.replace(marker, "\n".join(blocks) + "\n" + marker, 1)
if text == original:
    sys.exit(0)
tmp_path.write_text(text)
sys.exit(2)
PY
LIBRARY_NGINX_STATUS=$?
set -e
if [ "$LIBRARY_NGINX_STATUS" -eq 2 ]; then
  cp "$NGINX_SITE_FILE" "$NGINX_SITE_FILE.bak.$(date +%Y%m%d%H%M%S)"
  mv "$LIBRARY_NGINX_TMP" "$NGINX_SITE_FILE"
elif [ "$LIBRARY_NGINX_STATUS" -ne 0 ]; then
  rm -f "$LIBRARY_NGINX_TMP"
  exit "$LIBRARY_NGINX_STATUS"
else
  rm -f "$LIBRARY_NGINX_TMP"
fi

if ! grep -q 'location = /library/latest' "$NGINX_SITE_FILE" || ! grep -q 'location = /library/latest.json' "$NGINX_SITE_FILE"; then
  cp "$NGINX_SITE_FILE" "$NGINX_SITE_FILE.bak.$(date +%Y%m%d%H%M%S)"
  python3 - "$NGINX_SITE_FILE" "$WEB_DIR" "$API_PORT" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
web_dir = sys.argv[2]
port = sys.argv[3]
text = path.read_text()
marker = """    location = /library {
"""
blocks = []
if "    location = /library/latest {\n" not in text:
    blocks.append(f"""    location = /library/latest {{
        default_type text/html;
        alias {web_dir}/out/library/latest.html;
    }}
""")
if "    location = /library/latest.json {\n" not in text:
    blocks.append(f"""    location = /library/latest.json {{
        proxy_pass http://127.0.0.1:{port}/library/latest.json;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
""")
if not blocks:
    sys.exit(0)
if marker not in text:
    raise SystemExit("expected /library location marker not found")
path.write_text(text.replace(marker, "\n".join(blocks) + "\n" + marker, 1))
PY
fi

# Phase 2 ticker endpoint: /library/ticker.json serves the public ticker
# (ticker_featured finds within their 30-day window).
if ! grep -q 'location = /library/ticker.json' "$NGINX_SITE_FILE"; then
  cp "$NGINX_SITE_FILE" "$NGINX_SITE_FILE.bak.$(date +%Y%m%d%H%M%S)"
  python3 - "$NGINX_SITE_FILE" "$API_PORT" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
port = sys.argv[2]
text = path.read_text()
marker_candidates = [
    f"""    location = /library/latest.json {{
        proxy_pass http://127.0.0.1:{port}/library/latest.json;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
""",
]
block = f"""
    location = /library/ticker.json {{
        proxy_pass http://127.0.0.1:{port}/library/ticker.json;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
"""
for marker in marker_candidates:
    if marker in text:
        path.write_text(text.replace(marker, marker + block, 1))
        sys.exit(0)
raise SystemExit("expected /library/latest.json marker not found")
PY
fi

# Phase 4 newsletter archive: /library/issues.json + per-slug detail.
if ! grep -q 'location = /library/issues.json' "$NGINX_SITE_FILE"; then
  cp "$NGINX_SITE_FILE" "$NGINX_SITE_FILE.bak.$(date +%Y%m%d%H%M%S)"
  python3 - "$NGINX_SITE_FILE" "$API_PORT" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
port = sys.argv[2]
text = path.read_text()
# Anchor: the ticker.json block must exist already (added above).
marker = (
    f"    location = /library/ticker.json {{\n"
    f"        proxy_pass http://127.0.0.1:{port}/library/ticker.json;\n"
)
block = f"""    location = /library/issues.json {{
        proxy_pass http://127.0.0.1:{port}/library/issues.json;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}

    location ~ ^/library/issues/.+\\.json$ {{
        proxy_pass http://127.0.0.1:{port}$request_uri;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}

"""
if marker not in text:
    raise SystemExit("expected /library/ticker.json marker not found (run after the ticker block)")
path.write_text(text.replace(marker, block + marker, 1))
PY
fi

# Phase C: public library source browse. Static page at /library/sources and two
# proxied JSON endpoints (/library/sources.json directory + /library/sources/X.json
# detail). The detail route uses a regex location with proxy_pass to forward
# every {id}.json variant to the FastAPI process.
if ! grep -q 'location = /library/sources' "$NGINX_SITE_FILE"; then
  cp "$NGINX_SITE_FILE" "$NGINX_SITE_FILE.bak.$(date +%Y%m%d%H%M%S)"
  python3 - "$NGINX_SITE_FILE" "$WEB_DIR" "$API_PORT" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
web_dir = sys.argv[2]
port = sys.argv[3]
text = path.read_text()
marker = """    location = /library {
"""
blocks = []
if "    location = /library/sources {\n" not in text:
    blocks.append(f"""    location = /library/sources {{
        default_type text/html;
        alias {web_dir}/out/library/sources.html;
    }}
""")
if "    location = /library/sources.json {\n" not in text:
    blocks.append(f"""    location = /library/sources.json {{
        proxy_pass http://127.0.0.1:{port}/library/sources.json;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
""")
# Detail JSON routes match anything under /library/sources/...json that isn't
# the directory listing. Regex location so per-source ids forward to FastAPI.
if "location ~ ^/library/sources/.+\\.json$" not in text:
    blocks.append(f"""    location ~ ^/library/sources/.+\\.json$ {{
        proxy_pass http://127.0.0.1:{port}$request_uri;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
""")
if not blocks:
    sys.exit(0)
if marker not in text:
    raise SystemExit("expected /library location marker not found")
path.write_text(text.replace(marker, "\n".join(blocks) + "\n" + marker, 1))
PY
fi

if ! grep -q 'location = /favicon.ico' "$NGINX_SITE_FILE"; then
  cp "$NGINX_SITE_FILE" "$NGINX_SITE_FILE.bak.$(date +%Y%m%d%H%M%S)"
  python3 - "$NGINX_SITE_FILE" "$WEB_DIR" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
web_dir = sys.argv[2]
text = path.read_text()
marker = """    location = /terminal {
"""
block = f"""    location = /favicon.ico {{
        default_type image/x-icon;
        alias {web_dir}/out/favicon.ico;
        add_header Cache-Control "public, max-age=3600" always;
    }}

"""
if marker not in text:
    raise SystemExit("expected /terminal location marker not found")
path.write_text(text.replace(marker, block + marker, 1))
PY
fi

# Content-Security-Policy: production hardening for the public static pages
# and the FastAPI surface. The policy locks default-src to self, allows
# inline scripts and styles (Next.js + Tailwind) until a nonce-based pipeline
# is in place, allows data: URIs for fonts and images (Next.js inlines small
# assets), and disallows framing entirely (frame-ancestors 'none').
#
# nginx add_header inheritance: a location with its own add_header does NOT
# inherit server-level add_header. So we inject the CSP directive at server
# level AND right after every existing add_header X-Robots-Tag line. This
# keeps a single source of truth for the policy string while ensuring every
# response carries it.
if ! grep -q 'Content-Security-Policy' "$NGINX_SITE_FILE"; then
  cp "$NGINX_SITE_FILE" "$NGINX_SITE_FILE.bak.$(date +%Y%m%d%H%M%S)"
  python3 - "$NGINX_SITE_FILE" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text()

csp = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "base-uri 'self'; "
    "object-src 'none'"
)
server_directive = f'    add_header Content-Security-Policy "{csp}" always;\n'

# Insert at server level right after the first server_name line.
server_name_match = re.search(r'(^    server_name [^;]+;\n)', text, flags=re.MULTILINE)
if not server_name_match:
    raise SystemExit('could not locate server_name directive to anchor CSP insertion')
insert_at = server_name_match.end()
text = text[:insert_at] + server_directive + text[insert_at:]

# Add CSP after every existing X-Robots-Tag line so location-level overrides
# also carry the policy (nginx add_header does not inherit when the inner
# context has its own add_header).
location_directive = '        add_header Content-Security-Policy "' + csp + '" always;\n'
text = re.sub(
    r'^(        add_header X-Robots-Tag "noindex, nofollow" always;\n)',
    r'\1' + location_directive,
    text,
    flags=re.MULTILINE,
)

path.write_text(text)
PY
fi

nginx -t
systemctl reload nginx
REMOTE
fi

ssh -i "$SSH_KEY_PATH" "$SSH_TARGET" \
  "APP_DIR='$APP_DIR' SERVICE_NAME='$SERVICE_NAME' API_PORT='$API_PORT' RUN_USER='$VPS_USER' bash -s" <<'REMOTE'
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

cd "$APP_DIR"
uv sync --frozen --no-dev

sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null <<UNIT
[Unit]
Description=Gestalt Workframe API (${SERVICE_NAME})
After=network.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${APP_DIR}/.env
ExecStart=${APP_DIR}/.venv/bin/uvicorn gestaltworkframe.api.main:app --host 127.0.0.1 --port ${API_PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"
sudo systemctl --no-pager --full status "$SERVICE_NAME"
REMOTE
