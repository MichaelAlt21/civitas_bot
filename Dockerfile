FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY pyproject.toml README.md alembic.ini ./
COPY src/ ./src/
COPY migrations/ ./migrations/
RUN python -m pip install --no-cache-dir .

USER 10001:10001
CMD ["python", "-m", "civitas"]
