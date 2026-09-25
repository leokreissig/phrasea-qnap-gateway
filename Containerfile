FROM python:3.13-slim

WORKDIR /app

COPY pyproject.toml ./
COPY gateway ./gateway
COPY tests ./tests

RUN pip install --no-cache-dir ".[test]" \
    && useradd --create-home --uid 10001 gateway

USER 10001

EXPOSE 8080

CMD ["uvicorn", "gateway.main:create_production_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]