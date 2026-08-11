import os
from urllib.parse import quote
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool


config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
database_url = os.environ.get("AGENT_DATABASE_URL", "")
if not database_url and os.getenv("PGHOST"):
    database_url = "postgresql+psycopg://{}:{}@{}:{}/{}".format(
        quote(os.getenv("PGUSER", "agent"), safe=""), quote(os.getenv("PGPASSWORD", ""), safe=""),
        os.environ["PGHOST"], os.getenv("PGPORT", "5432"), os.getenv("PGDATABASE", "agent"),
    )
elif database_url.startswith("postgresql://"):
    database_url = "postgresql+psycopg://" + database_url[len("postgresql://"):]
config.set_main_option("sqlalchemy.url", database_url or config.get_main_option("sqlalchemy.url"))


def run_migrations_offline():
    context.configure(
        url=config.get_main_option("sqlalchemy.url"), literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    engine = engine_from_config(
        config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool,
    )
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
