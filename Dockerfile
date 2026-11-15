FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN groupadd --gid 10001 roster && useradd --uid 10001 --gid roster --no-create-home roster
COPY pyproject.toml README.md ./
COPY deploy/requirements.lock ./requirements.lock
RUN python -m pip install --upgrade 'pip>=26.2.2,<27' && python -m pip install -r requirements.lock
COPY src ./src
RUN python -m pip install --no-deps . && python -m pip check
COPY alembic.ini ./
COPY alembic ./alembic
COPY deploy/app-entrypoint.sh /usr/local/bin/roster-entrypoint
USER 10001:10001
ENTRYPOINT ["/bin/sh", "/usr/local/bin/roster-entrypoint"]
CMD ["uvicorn", "church_roster_bot.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--no-proxy-headers"]
