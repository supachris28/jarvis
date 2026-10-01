"""Admin commands, run inside the container:

    docker exec -it jarvis python -m jarvis.admin set-password
    docker exec -it jarvis python -m jarvis.admin totp-setup
    docker exec -it jarvis python -m jarvis.admin totp-disable
    docker exec -it jarvis python -m jarvis.admin logout-all
"""

from __future__ import annotations

import getpass
import sys

from .auth import Auth, totp_uri
from .config import Settings
from .db import Database


def main(argv: list[str]) -> int:
    command = argv[0] if argv else ""
    settings = Settings.from_env()
    auth = Auth(Database(settings.db_path))
    if command == "set-password":
        first = getpass.getpass("New password (12+ characters): ")
        second = getpass.getpass("Repeat: ")
        if first != second:
            print("Passwords do not match.")
            return 1
        try:
            auth.set_password(first)
        except ValueError as error:
            print(error)
            return 1
        print("Password set. All existing sessions were signed out.")
        return 0
    if command == "totp-setup":
        secret = auth.begin_totp()
        print("Add this to your authenticator app (manual entry, time-based, 6 digits):")
        print(f"  Key: {secret}")
        print(f"  URI: {totp_uri(secret)}")
        code = input("Enter the current 6-digit code to confirm: ")
        if auth.confirm_totp(code):
            print("Two-factor login enabled.")
            return 0
        print("Code did not match; two-factor login NOT enabled. Run totp-setup again.")
        return 1
    if command == "totp-disable":
        auth.disable_totp()
        print("Two-factor login disabled.")
        return 0
    if command == "logout-all":
        auth.db.execute("DELETE FROM sessions")
        print("All sessions signed out.")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
