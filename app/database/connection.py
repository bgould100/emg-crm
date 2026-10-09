
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

# Load the project's root .env file regardless of the terminal's directory.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


def get_connection() -> psycopg.Connection:
    """Open a PostgreSQL connection using the project's configuration."""
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is missing. Check the project's .env file."
        )

    return psycopg.connect(database_url, row_factory=dict_row)