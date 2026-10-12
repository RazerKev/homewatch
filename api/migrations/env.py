"""Alembic environment: runs migrations against DATABASE_URL."""

import sys
import time
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, Engine, create_engine, pool
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Wait up to ~30 s for a database that is still starting (first boot, a restart).
CONNECT_ATTEMPTS = 15
CONNECT_DELAY_S = 2.0

PASSWORD_REJECTED = """
The database rejected the password in DATABASE_URL.

Most likely cause: POSTGRES_PASSWORD in .env changed after the database was first
created. Postgres only reads POSTGRES_PASSWORD the first time it starts with an empty
data volume, and that volume is kept even if you delete the project folder.

Fix, either:
  * put the original password back in .env, or
  * reset the local database (this deletes its data):
        docker compose down -v
        docker compose up -d --build
"""


def _url() -> str:
    return get_settings().database_url.get_secret_value()


def _connect(engine: Engine) -> Connection:
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            return engine.connect()
        except OperationalError as exc:
            if "password authentication failed" in str(exc):
                sys.exit(PASSWORD_REJECTED)  # retrying can't fix a wrong password
            if attempt == CONNECT_ATTEMPTS:
                raise
            print(
                f"database not reachable yet (attempt {attempt}/{CONNECT_ATTEMPTS}); "
                f"retrying in {CONNECT_DELAY_S:.0f} s",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(CONNECT_DELAY_S)
    raise AssertionError("unreachable")


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it (alembic upgrade head --sql)."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool, hide_parameters=True)
    with _connect(engine) as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
