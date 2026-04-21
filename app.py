"""
マニュアルチャットボット - 認証・ログ管理版
- Google OAuth でログイン
- Google Spreadsheet でユーザー管理・質問ログ
- マニュアル未掲載の質問を Discord に通知
"""

import os
import time
import json
import base64
import requests
from datetime import datetime
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory, session, redirect, url_for
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from authlib.integrations.flask_client import OAuth
from google.oauth2 import service_account
from googleapiclient.discovery import build
import google.auth
import anthropic

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
CORS(app, supports_credentials=True)

# ── 設定 ──────────────────────────────────────────
SECRET_KEY               = os.environ.get("SECRET_KEY", "change-this-in-production")
GOOGLE_CLIENT_ID         = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET     = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_DOC_ID            = os.environ.get("GOOGLE_DOC_ID", "")
SPREADSHEET_ID           = os.environ.get("SPREADSHEET_ID", "")   # ユーザー管理 & ログ用
CLAUDE_API_KEY           = os.environ.get("CLAUDE_API_KEY", "")
GOOGLE_CREDENTIALS_JSON  = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
GOOGLE_CREDENTIALS_FILE  = os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")
DISCORD_WEBHOOK_URL      = os.environ.get("DISCORD_WEBHOOK_URL", "")
CLAUDE_MODEL             = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
CACHE_DURATION           = int(os.environ.get("CACHE_DURATION", "3600"))
PORT                     = int(os.environ.get("PORT", "8080"))

app.secret_key = SECRET_KEY

SCOPES_SERVICE = [
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]

_manual_cache: dict = {"content": None, "last_updated": 0}
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Google OAuth 設定 ─────────────────────────────
oauth = OAuth(app)
google_oauth = oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


# ── Google サービスアカウント認証 ──────────────────
def _build_service_credentials():
    if GOOGLE_CREDENTIALS_JSON:
        creds_dict = json.loads(base64.b64decode(GOOGLE_CREDENTIALS_JSON))
        return service_account.Credentials.from_service_account_info(
            creds_dict, scopes=SCOPES_SERVICE
        )
    if os.path.exists(GOOGLE_CREDENTIALS_FILE):
        return service_account.Credentials.from_service_account_file(
            GOOGLE_CREDENTIALS_FILE, scopes=SCOPES_SERVICE
        )
    credentials, _ = google.auth.default(scopes=SCOPES_SERVICE)
    return credentials


# ── Spreadsheet：ユーザー認証チェック ─────────────
def is_authorized_user(email: str) -> bool:
    """Sheet1「users」のA列にメールが存在するか確認"""
    try:
        creds = _build_service_credentials()
        service = build("sheets", "v4", credentials=creds)
        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="users!A:A",
        ).execute()
        rows = result.get("values", [])
        emails = [row[0].strip().lower() for row in rows if row]
        return email.strip().lower() in emails
    except Exception as e:
        print(f"[ERROR] ユーザー認証チェック失敗: {e}", flush=True)
        return False


# ── Spreadsheet：質問ログ記録 ──────────────────────
def log_question(email: str, question: str):
    """Sheet2「question_log」に未回答質問を記録"""
    try:
        creds = _build_service_credentials()
        service = build("sheets", "v4", credentials=creds)
        now = datetime.now().strftime("%Y/%m/%d %H:%M")
        service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="question_log!A:E",
            valueInputOption="USER_ENTERED",
            body={"values": [[now, email, question, "未回答", ""]]},
        ).execute()
    except Exception as e:
        print(f"[WARN] 質問ログ記録失敗: {e}", flush=True)


# ── Discord 通知 ───────────────────────────────────
def notify_discord(email: str, question: str):
    """マニュアル未掲載の質問を Discord に通知"""
    if not DISCORD_WEBHOOK_URL:
        return
    now = datetime.now().strftime("%Y/%m/%d %H:%M")
    payload = {
        "content": (
            f"📋 **マニュアル未掲載の質問**\n"
            f"👤 **質問者:** {email}\n"
            f"❓ **質問内容:** {question}\n"
            f"🕐 **日時:** {now}"
        )
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        print(f"[WARN] Discord 通知失敗: {e}", flush=True)


# ── Google Docs：マニュアル取得 ────────────────────
def get_manual_content() -> str:
    current_time = time.time()
    if (
        _manual_cache["content"] is not None
        and (current_time - _manual_cache["last_updated"]) < CACHE_DURATION
    ):
        return _manual_cache["content"]
    try:
        creds = _build_service_credentials()
        service = build("docs", "v1", credentials=creds)
        doc = service.documents().get(documentId=GOOGLE_DOC_ID).execute()
        lines = []
        for block in doc.get("body", {}).get("content", []):
            if "paragraph" in block:
                for el in block["paragraph"].get("elements", []):
                    if "textRun" in el:
                        lines.append(el["textRun"]["content"])
        content = "".join(lines)
        _manual_cache["content"] = content
        _manual_cache["last_updated"] = current_time
        print(f"[INFO] マニュアル取得完了 ({len(content)} 文字)", flush=True)
        return content
    except Exception as e:
        return f"[ERROR] マニュアル取得失敗: {e}"


# ── ログイン必須デコレーター ──────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_email" not in session:
            if request.is_json:
                return jsonify({"error": "ログインが必要です", "login_required": True}), 401
            return redirect("/")
        return f(*args, **kwargs)
    return decorated


# ── ルーティング ────────────────────────────────────

@app.route("/")
def index():
    if "user_email" in session:
        return send_from_directory(BASE_DIR, "index.html")
    return send_from_directory(BASE_DIR, "login.html")


@app.route("/login")
def login():
    redirect_uri = url_for("auth_callback", _external=True)
    return google_oauth.authorize_redirect(redirect_uri)


@app.route("/auth/callback")
def auth_callback():
    token = google_oauth.authorize_access_token()
    userinfo = token.get("userinfo", {})
    email = userinfo.get("email", "")
    name  = userinfo.get("name", email)

    if not email:
        return "メールアドレスを取得できませんでした", 400

    if not is_authorized_user(email):
        return send_from_directory(BASE_DIR, "unauthorized.html"), 403

    session["user_email"] = email
    session["user_name"]  = name
    return redirect("/")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/me")
def me():
    if "user_email" not in session:
        return jsonify({"logged_in": False})
    return jsonify({
        "logged_in": True,
        "email": session["user_email"],
        "name":  session["user_name"],
    })


@app.route("/chat", methods=["POST"])
@login_required
def chat():
    data = request.get_json(force=True)
    user_message: str = data.get("message", "").strip()
    history: list     = data.get("history", [])
    user_email        = session["user_email"]

    if not user_message:
        return jsonify({"error": "メッセージが空です"}), 400
    if not CLAUDE_API_KEY:
        return jsonify({"error": "CLAUDE_API_KEY が設定されていません"}), 500

    manual_content = get_manual_content()
    system_prompt = f"""あなたはマニュアルに基づいて質問に答えるサポートアシスタントです。

ルール：
1. 必ずマニュアルの内容に基づいて回答する
2. マニュアルに記載のない内容は「マニュアルには記載がありません」と明記する
3. 分かりやすい日本語で、必要に応じて箇条書きを使う
4. 推測で回答しない

=== マニュアル ===
{manual_content}
=================
"""
    messages = history + [{"role": "user", "content": user_message}]

    try:
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        resp   = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=2000,
            system=system_prompt, messages=messages,
        )
        reply = resp.content[0].text

        # 未回答を検知 → Spreadsheet に記録 + Discord 通知
        if "マニュアルには記載がありません" in reply:
            log_question(user_email, user_message)
            notify_discord(user_email, user_message)

        return jsonify({
            "response": reply,
            "history": messages + [{"role": "assistant", "content": reply}],
        })

    except anthropic.AuthenticationError:
        return jsonify({"error": "Claude API キーが無効です"}), 401
    except Exception as e:
        return jsonify({"error": f"Claude API エラー: {str(e)}"}), 500


@app.route("/refresh-manual", methods=["POST"])
@login_required
def refresh_manual():
    _manual_cache["content"] = None
    _manual_cache["last_updated"] = 0
    content = get_manual_content()
    char_count = len(content) if not content.startswith("[ERROR]") else 0
    return jsonify({"status": "ok" if char_count > 0 else "error",
                    "message": f"マニュアルを更新しました ({char_count} 文字)"})


@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "server": "running",
        "model": CLAUDE_MODEL,
        "manual_cached": _manual_cache["content"] is not None,
        "manual_chars": len(_manual_cache["content"]) if _manual_cache["content"] else 0,
    })


if __name__ == "__main__":
    print(f"[INFO] Starting on port {PORT}", flush=True)
    app.run(host="0.0.0.0", port=PORT, debug=False)
