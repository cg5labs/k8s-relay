FROM python:3.12-slim-bookworm AS build

# git is needed to install the pinned kubernetes client commit.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --prefix=/install -r /tmp/requirements.txt

FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /install /usr/local
WORKDIR /app
COPY main.py ./
COPY lease_scheduler/ ./lease_scheduler/
COPY alembic.ini ./
COPY migrations/ ./migrations/

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
USER 65532:65532
EXPOSE 8080
CMD ["python", "main.py"]
