FROM python:3.12-slim

ARG VERSION=v1
ARG ERROR_RATE=0
ARG EXTRA_LATENCY_MS=0
ENV VERSION=$VERSION ERROR_RATE=$ERROR_RATE EXTRA_LATENCY_MS=$EXTRA_LATENCY_MS

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

USER nobody
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
