"""Set a password directly, for the one account nobody above can help: a Muhaffiz.

Students get a reset link from their Muhaffiz. A Muhaffiz has nobody to ask, so
recovery for them is a shell command on the server.

    python -m scripts.reset_password someone@example.com
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.models import User  # noqa: E402
from app.security import check_password_strength, hash_password  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(1)
    email = sys.argv[1].strip().lower()

    with session_scope() as db:
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None:
            print(f"No account for {email}.")
            raise SystemExit(1)
        pw = getpass.getpass(f"New password for {user.name} <{email}>: ")
        problem = check_password_strength(pw)
        if problem:
            print(problem)
            raise SystemExit(1)
        if pw != getpass.getpass("Confirm: "):
            print("Passwords did not match.")
            raise SystemExit(1)
        user.password_hash = hash_password(pw)
    print("Password updated.")
