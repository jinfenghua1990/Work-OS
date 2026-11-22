"""Locally provision a named finance login; never pass passwords in argv or GitHub.

Examples:
    python backend/manage_finance_users.py add --username finance01 --role finance_clerk
    python backend/manage_finance_users.py add --username reviewer01 --role finance_approver
Requires WORKOS_AUTH_MODE=named and an initialized Schema 45 database.
"""
from __future__ import annotations
import argparse
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent/"app"))
import server
import finance_identity


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("command",choices=("add",))
    parser.add_argument("--username",required=True)
    parser.add_argument("--role",choices=sorted(finance_identity.ROLES),required=True)
    args=parser.parse_args()
    if os.environ.get("WORKOS_AUTH_MODE","").strip().lower()!="named":
        raise RuntimeError("Explicit WORKOS_AUTH_MODE=named is required")
    server.require_database_ready()
    password=getpass.getpass("New finance-user password (min 12 chars): ")
    confirm=getpass.getpass("Confirm: ")
    if password!=confirm:
        raise ValueError("password confirmation does not match")
    with server.db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        user=finance_identity.create_user(conn,args.username,password,args.role)
    print("Named finance user created:",user["username"],"role:",user["role"])


if __name__=="__main__":
    main()
