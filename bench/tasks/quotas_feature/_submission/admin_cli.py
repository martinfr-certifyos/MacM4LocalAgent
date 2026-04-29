"""Local admin CLI for operators.

Run via `python -m webhookd.admin_cli <subcommand>`. Connects directly
to the SQLite database -- this is for incident triage, not for routine
tenant operations (those go through the HTTP API).

Subcommands:
    list-tenants
    list-subs <tenant_id>
    show-sub <subscription_id>
    show-delivery <delivery_id>
    list-due [--limit N]
    list-dead-letter [--limit N]
    requeue-dlq <dead_letter_id>
    deactivate-sub <subscription_id>
    rotate-secret <subscription_id> <new_secret>
    purge-delivered --older-than <hours>
    self-check

Most subcommands are read-only. Mutating operations refuse to run
without `--force` so an operator triaging an incident can't cause
secondary damage by typo.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from typing import Any

import storage

log = logging.getLogger(__name__)


# ---- subcommand handlers ----------------------------------------------------

def cmd_list_tenants(conn: Any, args: argparse.Namespace) -> int:
    rows = conn.execute(
        "SELECT DISTINCT tenant_id FROM subscriptions ORDER BY tenant_id ASC",
    ).fetchall()
    for row in rows:
        print(row["tenant_id"])
    return 0


def cmd_list_subs(conn: Any, args: argparse.Namespace) -> int:
    subs = storage.list_subscriptions_for_tenant(conn, args.tenant_id)
    for sub in subs:
        print(json.dumps({
            "id":       sub["id"],
            "url":      sub["url"],
            "active":   sub["active"],
            "events":   sub["event_types"],
            "created":  sub["created_at"],
        }))
    return 0


def cmd_show_sub(conn: Any, args: argparse.Namespace) -> int:
    try:
        sub = storage.get_subscription(conn, args.subscription_id)
    except KeyError:
        print(f"subscription {args.subscription_id} not found", file=sys.stderr)
        return 2
    print(json.dumps(sub, indent=2, default=str))
    return 0


def cmd_show_delivery(conn: Any, args: argparse.Namespace) -> int:
    row = conn.execute(
        "SELECT * FROM deliveries WHERE id = ?", (args.delivery_id,),
    ).fetchone()
    if row is None:
        print(f"delivery {args.delivery_id} not found", file=sys.stderr)
        return 2
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    print(json.dumps(d, indent=2, default=str))
    return 0


def cmd_list_due(conn: Any, args: argparse.Namespace) -> int:
    due = storage.claim_due_deliveries(conn, limit=args.limit)
    for d in due:
        print(json.dumps({
            "id":       d["id"],
            "sub":      d["subscription_id"],
            "event":    d["event_type"],
            "attempts": d["attempts"],
            "due":      d["scheduled_at"],
            "last":     d.get("last_status"),
        }))
    return 0


def cmd_list_dead_letter(conn: Any, args: argparse.Namespace) -> int:
    rows = conn.execute(
        "SELECT * FROM dead_letter ORDER BY moved_at DESC LIMIT ?",
        (args.limit,),
    ).fetchall()
    for row in rows:
        d = dict(row)
        try:
            d["original_payload"] = json.loads(d["original_payload"])
        except json.JSONDecodeError:
            pass
        print(json.dumps(d, default=str))
    return 0


def cmd_requeue_dlq(conn: Any, args: argparse.Namespace) -> int:
    if not args.force:
        print("refusing without --force; this re-enqueues a previously-dead delivery",
              file=sys.stderr)
        return 2
    row = conn.execute(
        "SELECT * FROM dead_letter WHERE id = ?", (args.dead_letter_id,),
    ).fetchone()
    if row is None:
        print(f"dead_letter {args.dead_letter_id} not found", file=sys.stderr)
        return 2
    # Look up the original delivery to reconstruct the metadata we need.
    delivery_row = conn.execute(
        "SELECT * FROM deliveries WHERE id = ?", (row["delivery_id"],),
    ).fetchone()
    if delivery_row is None:
        print(f"original delivery {row['delivery_id']} no longer exists",
              file=sys.stderr)
        return 2
    new_did = storage.enqueue_delivery(
        conn,
        subscription_id=delivery_row["subscription_id"],
        event_id=delivery_row["event_id"],
        event_type=delivery_row["event_type"],
        payload=json.loads(row["original_payload"]),
    )
    conn.execute("DELETE FROM dead_letter WHERE id = ?", (args.dead_letter_id,))
    conn.commit()
    print(f"requeued as {new_did}")
    return 0


def cmd_deactivate_sub(conn: Any, args: argparse.Namespace) -> int:
    if not args.force:
        print("refusing without --force", file=sys.stderr)
        return 2
    storage.deactivate_subscription(conn, args.subscription_id)
    print(f"deactivated {args.subscription_id}")
    return 0


def cmd_rotate_secret(conn: Any, args: argparse.Namespace) -> int:
    if not args.force:
        print("refusing without --force", file=sys.stderr)
        return 2
    if len(args.new_secret) < 16:
        print("new secret too short (min 16 chars)", file=sys.stderr)
        return 2
    now = int(time.time())
    conn.execute(
        "UPDATE subscriptions SET secret = ?, updated_at = ? WHERE id = ?",
        (args.new_secret, now, args.subscription_id),
    )
    conn.commit()
    print(f"rotated secret for {args.subscription_id}")
    return 0


def cmd_purge_delivered(conn: Any, args: argparse.Namespace) -> int:
    if not args.force:
        print("refusing without --force", file=sys.stderr)
        return 2
    cutoff = int(time.time()) - args.older_than * 3600
    cur = conn.execute(
        "DELETE FROM deliveries WHERE delivered_at IS NOT NULL AND delivered_at < ?",
        (cutoff,),
    )
    conn.commit()
    print(f"purged {cur.rowcount} delivered rows older than {args.older_than}h")
    return 0


def cmd_self_check(conn: Any, args: argparse.Namespace) -> int:
    """Quick read-only sanity scan for an on-call operator."""
    counts = {}
    for table in ("subscriptions", "deliveries", "dead_letter"):
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        counts[table] = row["n"]
    overdue_row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM deliveries
        WHERE delivered_at IS NULL AND scheduled_at < ?
        """,
        (int(time.time()) - 600,),
    ).fetchone()
    counts["overdue_more_than_10m"] = overdue_row["n"]
    print(json.dumps(counts, indent=2))
    return 0


# ---- argument parsing ------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="webhookd-admin", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list-tenants")

    s = sub.add_parser("list-subs")
    s.add_argument("tenant_id")

    s = sub.add_parser("show-sub")
    s.add_argument("subscription_id")

    s = sub.add_parser("show-delivery")
    s.add_argument("delivery_id")

    s = sub.add_parser("list-due")
    s.add_argument("--limit", type=int, default=20)

    s = sub.add_parser("list-dead-letter")
    s.add_argument("--limit", type=int, default=20)

    s = sub.add_parser("requeue-dlq")
    s.add_argument("dead_letter_id")
    s.add_argument("--force", action="store_true")

    s = sub.add_parser("deactivate-sub")
    s.add_argument("subscription_id")
    s.add_argument("--force", action="store_true")

    s = sub.add_parser("rotate-secret")
    s.add_argument("subscription_id")
    s.add_argument("new_secret")
    s.add_argument("--force", action="store_true")

    s = sub.add_parser("purge-delivered")
    s.add_argument("--older-than", type=int, default=72,
                   help="hours; rows delivered earlier than this are deleted")
    s.add_argument("--force", action="store_true")

    sub.add_parser("self-check")
    return p


_HANDLERS = {
    "list-tenants":     cmd_list_tenants,
    "list-subs":        cmd_list_subs,
    "show-sub":         cmd_show_sub,
    "show-delivery":    cmd_show_delivery,
    "list-due":         cmd_list_due,
    "list-dead-letter": cmd_list_dead_letter,
    "requeue-dlq":      cmd_requeue_dlq,
    "deactivate-sub":   cmd_deactivate_sub,
    "rotate-secret":    cmd_rotate_secret,
    "purge-delivered":  cmd_purge_delivered,
    "self-check":       cmd_self_check,
}


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = _HANDLERS[args.command]
    conn = storage.connect()
    try:
        return handler(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
