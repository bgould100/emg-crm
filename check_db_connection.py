
import os

import psycopg
from dotenv import load_dotenv

load_dotenv()

database_url = os.getenv("DATABASE_URL")

if not database_url:
    raise RuntimeError("DATABASE_URL was not found. Check your .env file.")

try:
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT
                    current_database(),
                    current_user,
                    version()
            """)
            database, user, version = cursor.fetchone()

            print("Database connection successful!")
            print(f"Database: {database}")
            print(f"User: {user}")
            print(f"PostgreSQL: {version.split(',')[0]}")

except Exception as error:
    print(f"Database connection failed: {error}")
    raise