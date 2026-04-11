"""
マニュアルチャットボット - Flask バックエンド（Cloud Run 対応版）

認証の優先順位:
  1. 環境変数 GOOGLE_CREDENTIALS_JSON（Base64 エンコードされた JSON）
  2. 環境変数 GOOGLE_CREDENTIALS_FILE で指定したファイル
  3. Application Default Credentials（Cloud Run のサービスアカウントが自動的に使用）
"""

import os
import time
import json
import base64
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build
import google.auth
import anthropic

app = Flask(__name__)
CORS(app)

# ── 設定 ──────────────────────────────────────────
GOOGLE_DOC_ID            = os.environ.get("GOOGLE_DOC_ID", "")
CLAUDE_API_KEY           = os.environ.get("CLAUDE_API_KEY", "")
GOOGLE_CREDENTIALS_JSON  = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")   # Base64 文字列
GOOGLE_CREDENTIALS_FILE  = os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")
CLAUDE_MODEL             = os.environ.get("CLAUDE_MODEL", "claude-opus-4-6")
CACHE_DURATION           = int(os.environ.get("CACHE_DURATION", "3600"))
PORT                     = int(os.environ.get("PORT", "8080"))  # Cloud Run は PORT を自動セット

SCOPES = ["https://www.googleapis.com/auth/documents.readonly"]

_manual_cache: dict = {"content": None, "last_updated": 0}


# ── Google 認証 ────────────────────────────────────
def _build_credentials():
    """環境に応じた Google 認証情報を返す"""
    # 優先度1: 環境変数に Base64 JSON が設定されている
    if GOOGLE_CREDENTIALS_JSON:
        creds_dict = json.loads(base64.b64decode(GOOGLE_CREDENTIALS_JSON))
        return service_account.Credentials.from_service_account_info(
            creds_dict, scopes=SCOPES
        )

    # 優先度2: ローカルのファイルが存在する
    if os.path.exists(GOOGLE_CREDENTIALS_FILE):
        return service_account.Credentials.from_service_account_file(
            GOOGLE_CREDENTIALS_FILE, scopes=SCOPES
        )

    # 優先度3: Cloud Run のサービスアカウント（ADC）
    credentials, _ = google.auth.default(scopes=SCOPES)
    return credentials


# ── Google Docs 取得 ──────────────────────────────
def get_manual_content() -> str:
    current_time = time.time()
    if (
        _manual_cache["content"] is not None
        and (current_time - _manual_cache["last_updated"]) < CACHE_DURATION
    ):
        return _manual_cache["content"]

    try:
        credentials = _build_credentials()
        service = build("docs", "v1", credentials=credentials)
        document = service.documents().get(documentId=GOOGLE_DOC_ID).execute()

        lines = []
        for block in document.get("body", {}).get("content", []):
            if "paragraph" in block:
                for element in block["paragraph"].get("elements", []):
                    if "textRun" in element:
                        lines.append(element["textRun"]["content"])

        content = "".join(lines)
        _manual_cache["content"] = content
        _manual_cache["last_updated"] = current_time
        print(f"[INFO] マニュアル取得完了 ({len(content)} 文字)", flush=True)
        return content

    except Exception as e:
        err = f"[ERROR] Google Docs の取得に失敗しました: {str(e)}"
        print(err, flush=True)
        return err


# ── ルーティング ────────────────────────────────────
@app.route("/")
def index():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(base_dir, "index.html")


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(force=True)
    user_message: str = data.get("message", "").strip()
    conversation_history: list = data.get("history", [])

    if not user_message:
        return jsonify({"error": "メッセージが空です"}), 400
    if not CLAUDE_API_KEY:
        return jsonify({"error": "CLAUDE_API_KEY が設定されていません"}), 500

    manual_content = get_manual_content()

    system_prompt = f"""あなたはマニュアルに基づいて質問に答えるサポートアシスタントです。

以下のルールを守って回答してください：
1. 必ず「マニュアルの内容」に基づいて回答する
2. マニュアルに記載のない内容については「マニュアルには記載がありません」と明記する
3. 回答は分かりやすい日本語で、必要に応じて箇条書きを使う
4. 推測や憶測で回答しない

=== マニュアル ===
{manual_content}
=================
"""

    messages = conversation_history + [{"role": "user", "content": user_message}]

    try:
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2000,
            system=system_prompt,
            messages=messages,
        )
        reply = response.content[0].text
        return jsonify({"response": reply, "history": messages + [{"role": "assistant", "content": reply}]})

    except anthropic.AuthenticationError:
        return jsonify({"error": "Claude API キーが無効です"}), 401
    except Exception as e:
        return jsonify({"error": f"Claude API エラー: {str(e)}"}), 500


@app.route("/refresh-manual", methods=["POST"])
def refresh_manual():
    _manual_cache["content"] = None
    _manual_cache["last_updated"] = 0
    content = get_manual_content()
    char_count = len(content) if not content.startswith("[ERROR]") else 0
    return jsonify({
        "status": "ok" if char_count > 0 else "error",
        "message": f"マニュアルを更新しました ({char_count} 文字)",
    })


@app.route("/status", methods=["GET"])
def status():
    cache_age = int(time.time() - _manual_cache["last_updated"])
    return jsonify({
        "server": "running",
        "model": CLAUDE_MODEL,
        "manual_cached": _manual_cache["content"] is not None,
        "cache_age_seconds": cache_age,
        "manual_chars": len(_manual_cache["content"]) if _manual_cache["content"] else 0,
    })


# ── 起動 ─────────────────────────────────────────
if __name__ == "__main__":
    print(f"[INFO] Starting on port {PORT}", flush=True)
    app.run(host="0.0.0.0", port=PORT, debug=False)
