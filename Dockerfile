FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY index.html .
COPY login.html .
COPY unauthorized.html .
EXPOSE 8080
CMD ["python", "app.py"]
