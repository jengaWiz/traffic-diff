FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY shadowdiff ./shadowdiff
RUN pip install --no-cache-dir .

COPY demo ./demo
COPY shadowdiff.yaml ./

EXPOSE 8080
CMD ["shadowdiff", "--help"]
