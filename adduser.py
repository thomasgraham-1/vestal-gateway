"""
Create a Vestal web-app login.

    python3 adduser.py <username> <password> <role>

role = 'admin' (sees the whole org) or an owner from gateway.ORG
(e.g. 'support team') to scope that user to only their agents.
"""
import sys, gateway


def main(username, pw, role):
    gateway._db()
    if role != "admin" and role not in gateway.ORG:
        print(f"role must be 'admin' or one of: {list(gateway.ORG)}")
        sys.exit(1)
    gateway.create_user(username, pw, role)
    print(f"created user '{username}'  role={role}  -> sign in at /login")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("usage: python3 adduser.py <username> <password> <role>")
        print("roles: admin | " + " | ".join(repr(o) for o in gateway.ORG))
        sys.exit(1)
    main(*sys.argv[1:4])
