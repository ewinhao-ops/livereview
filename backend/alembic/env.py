from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from app import models  # noqa: F401  Ensure every SQLAlchemy model is registered.
from app.db import Base, engine


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# app.db resolves sqlite:///./... relative to backend/ and applies the same
# environment configuration used by FastAPI. Keep the URL on Alembic's config
# as well for Alembic commands and future revisions.
database_url = engine.url.render_as_string(hide_password=False).replace("%", "%%")
config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata

# app.db creates these two compatibility indexes with dialect-specific SQL.
# They intentionally are not represented by generic SQLAlchemy model indexes,
# so autogenerate must not propose deleting them.
LEGACY_DATABASE_INDEXES = {
    "ix_streamer_turns_streamer",
    "ix_streamers_ext_id",
}


def _include_object(  # noqa: ANN001
    object_,
    name,
    type_,
    reflected,
    compare_to,
) -> bool:
    del object_, compare_to
    if type_ == "index" and reflected and name in LEGACY_DATABASE_INDEXES:
        return False
    return True


def _configure(connection=None, *, url: str | None = None) -> None:  # noqa: ANN001
    options = {
        "target_metadata": target_metadata,
        "compare_type": True,
        "compare_server_default": True,
        "include_object": _include_object,
    }
    if connection is not None:
        options["connection"] = connection
        options["render_as_batch"] = connection.dialect.name == "sqlite"
    else:
        options.update(
            {
                "url": url,
                "literal_binds": True,
                "dialect_opts": {"paramstyle": "named"},
            }
        )
    context.configure(**options)


def run_migrations_offline() -> None:
    _configure(url=config.get_main_option("sqlalchemy.url"))
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    with engine.connect() as connection:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
