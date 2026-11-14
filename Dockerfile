# The controller itself, for running it inside the cluster (`make incluster-deploy`).
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml alembic.ini ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf src
COPY policies ./policies

# The package is installed into site-packages, so point at the copied policies.
ENV CANARY_DEFAULT_POLICY=/app/policies/default.yaml PYTHONUNBUFFERED=1
USER nobody
EXPOSE 8020
CMD ["uvicorn", "--factory", "canary.api:create_app", "--host", "0.0.0.0", "--port", "8020"]
