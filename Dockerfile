FROM python:3.11-slim

# 作業ディレクトリ
WORKDIR /app

# パッケージを先にコピー（キャッシュ効率化）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリ本体をコピー
COPY app.py .
COPY index.html .

# Cloud Run はポート 8080 を期待する
EXPOSE 8080

# 起動
CMD ["python", "app.py"]
