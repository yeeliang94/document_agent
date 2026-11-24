FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DI_HOST=0.0.0.0 DI_PORT=8003 DI_DATA_DIR=/data
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY web ./web
RUN mkdir /data && useradd --uid 10001 --create-home appuser && chown appuser:appuser /data
USER appuser
EXPOSE 8003
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8003", "--workers", "1"]
