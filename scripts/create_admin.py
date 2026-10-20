
from getpass import getpass

from app.database.admin_auth import create_admin_user


def main():
    username = input("Admin username: ").strip()
    password = getpass("Admin password (12+ characters): ")
    confirmation = getpass("Confirm admin password: ")

    if password != confirmation:
        raise SystemExit("Passwords do not match.")

    try:
        user = create_admin_user(username=username, password=password)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    print(f"Admin account created: {user['username']}")


if __name__ == "__main__":
    main()
