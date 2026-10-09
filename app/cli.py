"""Operational commands: python -m app.cli <command>

migrate            apply database migrations (alembic upgrade head)
ensure-app-role    create/refresh the least-privilege database role the API connects as (needs admin DB credentials)
seed               import curriculum + activities from a bundle file or the content-curriculum folder
create-admin       create or promote a platform user (educator | content_admin | super_admin)
release            migrate + ensure-app-role (when APP_DB_PASSWORD is set) + seed (when SEED_BUNDLE_PATH is set)
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sqlalchemy import func, select

from app.config import get_settings

ROOT = Path(__file__).resolve().parent.parent


def migrate() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")


def raw_conninfo() -> str:
    s = get_settings()
    if s.database_url:
        return s.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    return make_conninfo(
        host=s.db_host,
        port=s.db_port,
        dbname=s.db_name,
        user=s.db_user,
        password=s.db_password.get_secret_value() or None,
        sslmode=s.db_sslmode,
    )


def ensure_app_role(role: str, password: str) -> None:
    """Runs with the ADMIN credentials. The API then connects as `role`, which is not the table owner, cannot bypass
    Row-Level Security, cannot change the schema, and cannot rewrite the audit log."""
    if not role.replace("_", "").isalnum():
        raise SystemExit("APP_DB_USER may only contain letters, digits and underscores")
    if len(password) < 16:
        raise SystemExit("APP_DB_PASSWORD must be at least 16 characters")
    ident = sql.Identifier(role)
    with psycopg.connect(raw_conninfo(), autocommit=True) as conn:
        dbname = conn.execute("select current_database()").fetchone()[0]
        exists = conn.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(
            sql.SQL(f"{verb} ROLE {{}} LOGIN PASSWORD {{}} NOBYPASSRLS NOCREATEDB NOCREATEROLE").format(
                ident, sql.Literal(password)
            )
        )
        stmts = [
            "GRANT CONNECT ON DATABASE {db} TO {r}",
            "GRANT USAGE ON SCHEMA public, content TO {r}",
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public, content TO {r}",
            "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public, content TO {r}",
            "REVOKE ALL ON alembic_version FROM {r}",
            "REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM {r}",
            "REVOKE DELETE, TRUNCATE ON content.activity_events FROM {r}",
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public, content GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {r}",
        ]
        for st in stmts:
            conn.execute(sql.SQL(st).format(db=sql.Identifier(dbname), r=ident))
    print(f"database role '{role}' is ready")


def load_bundle(path: str) -> dict:
    p = Path(path)
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    read = lambda f: json.loads((p / f).read_text(encoding="utf-8"))  # noqa: E731
    skills: list = []
    for f in sorted((p / "taxonomy" / "skills").glob("*.json")):
        skills += json.loads(f.read_text(encoding="utf-8"))
    activities = [json.loads(f.read_text(encoding="utf-8")) for f in sorted((p / "activities").glob("*/*.json"))]
    return {
        "version": "folder",
        "levels": read("taxonomy/levels.json"),
        "subjects": read("taxonomy/subjects.json"),
        "interests": read("taxonomy/interests.json"),
        "skills": skills,
        "activities": activities,
    }


def seed_from_path(path: str, auto_publish: bool = False, dry_run: bool = False) -> dict:
    from app.db import get_factory
    from app.schemas import BundleIn
    from app.services.catalog import import_bundle

    data = load_bundle(path)
    bundle = BundleIn(**{**data, "dry_run": dry_run, "auto_publish": auto_publish})
    with get_factory()() as db:
        report = import_bundle(db, bundle)
        if not report.ok:
            db.rollback()
            print("bundle rejected:\n  " + "\n  ".join(report.problems), file=sys.stderr)
            raise SystemExit(2)
        db.commit()
    print(f"bundle {bundle.version}: created={report.created} updated={report.updated} unchanged={report.unchanged}")
    return report.model_dump()


def create_admin(email: str, name: str, role: str) -> None:
    from app.db import get_factory
    from app.models import User
    from app.security import hash_secret

    password = os.getenv("ADMIN_PASSWORD") or getpass.getpass("Admin password (min 12 characters): ")
    if len(password) < 12:
        raise SystemExit("Password must be at least 12 characters")
    with get_factory()() as db:
        user = db.scalar(select(User).where(func.lower(User.email) == email.lower()))
        if user is None:
            db.add(User(email=email, full_name=name, password_hash=hash_secret(password), platform_role=role))
            print(f"created {role} {email}")
        else:
            user.platform_role, user.password_hash = role, hash_secret(password)
            print(f"updated {email} -> {role}")
        db.commit()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m app.cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    r = sub.add_parser("ensure-app-role")
    r.add_argument("--user", default=os.getenv("APP_DB_USER", "homeschool_app"))
    sd = sub.add_parser("seed")
    sd.add_argument("--path", default=os.getenv("SEED_BUNDLE_PATH"), required=not os.getenv("SEED_BUNDLE_PATH"))
    sd.add_argument("--publish", action="store_true", help="publish imported activities immediately")
    sd.add_argument("--dry-run", action="store_true")
    a = sub.add_parser("create-admin")
    a.add_argument("--email", required=True)
    a.add_argument("--name", required=True)
    a.add_argument("--role", choices=["educator", "content_admin", "super_admin"], default="content_admin")
    sub.add_parser("release")
    args = ap.parse_args(argv)

    if args.cmd == "migrate":
        migrate()
    elif args.cmd == "ensure-app-role":
        ensure_app_role(args.user, os.environ.get("APP_DB_PASSWORD", ""))
    elif args.cmd == "seed":
        seed_from_path(args.path, auto_publish=args.publish, dry_run=args.dry_run)
    elif args.cmd == "create-admin":
        create_admin(args.email, args.name, args.role)
    elif args.cmd == "release":
        migrate()
        if os.getenv("APP_DB_PASSWORD"):
            ensure_app_role(os.getenv("APP_DB_USER", "homeschool_app"), os.environ["APP_DB_PASSWORD"])
        if os.getenv("SEED_BUNDLE_PATH"):
            seed_from_path(
                os.environ["SEED_BUNDLE_PATH"], auto_publish=os.getenv("SEED_PUBLISH", "false").lower() == "true"
            )


if __name__ == "__main__":
    main()
