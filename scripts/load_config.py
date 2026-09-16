"""Validate config.yaml, write it into the `channel` row, bump config_version, and
refresh config.schema.json.

Usage: uv run python scripts/load_config.py config.yaml [--handle main]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.config import dump_config_schema, load_channel_config
from app.db import Base, Channel, get_engine, get_sessionmaker


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config_path", type=Path)
    parser.add_argument("--handle", default="main", help="channel handle to create/update")
    args = parser.parse_args()

    channel_config = load_channel_config(args.config_path)
    Base.metadata.create_all(get_engine())

    with get_sessionmaker()() as session:
        channel = session.query(Channel).filter_by(handle=args.handle).one_or_none()
        config_dump = channel_config.model_dump(mode="json")
        if channel is None:
            channel = Channel(handle=args.handle, name=channel_config.channel.name, config=config_dump)
            session.add(channel)
        else:
            channel.name = channel_config.channel.name
            channel.config = config_dump
            channel.config_version += 1
        session.commit()
        print(f"channel {args.handle!r}: config_version={channel.config_version}")

    schema_path = dump_config_schema()
    print(f"wrote {schema_path}")


if __name__ == "__main__":
    main()
