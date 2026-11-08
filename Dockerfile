FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Istanbul

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY config ./config
COPY reference ./reference

# Root olmayan kullanici
RUN useradd --create-home --uid 10001 bist
USER bist

ENTRYPOINT ["bist"]
CMD ["--help"]
