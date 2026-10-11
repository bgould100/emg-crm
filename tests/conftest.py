"""Keep database-backed tests isolated from the main EMG database."""

import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"

# Read the project's connection settings without displaying credentials.
configured_url = (
    os.environ.get("DATABASE_URL")
    or dotenv_values(ENV_FILE).get("DATABASE_URL")
)

if not configured_url:
    raise RuntimeError(
        "Cannot run tests: DATABASE_URL is missing from the environment "
        "and project .env file."
    )

parsed_url = urlsplit(configured_url)

if not parsed_url.scheme or not parsed_url.netloc:
    raise RuntimeError("Cannot run tests: DATABASE_URL is invalid.")

# Force the database name to emg_test, preserving the other connection settings.
test_url = urlunsplit(
    (
        parsed_url.scheme,
        parsed_url.netloc,
        "/emg_test",
        parsed_url.query,
        parsed_url.fragment,
    )
)

os.environ["DATABASE_URL"] = test_url
