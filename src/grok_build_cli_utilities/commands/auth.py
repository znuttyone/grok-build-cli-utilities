"""grok-utils auth - SuperGrok session vs API key path."""

from __future__ import annotations

import json

import typer

from ..utils.auth_status import (
    auth_history_change_points,
    detect_auth,
    format_weekly_reset_local,
    iso_or_none,
    load_auth_history,
    load_billing_snapshot,
    subscription_tier_label,
    weekly_pool_reset_subject,
)
from ..utils.common import console, get_grok_home, make_table, warn

app = typer.Typer(help="Grok Build auth path (SuperGrok session vs API key)", no_args_is_help=True)


@app.command("status")
def auth_status(
    ctx: typer.Context,
    history: bool = typer.Option(
        False,
        "--history",
        help="Also show auth method change-points from ~/.grok/logs/unified.jsonl",
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable JSON"),
) -> None:
    """Show effective auth path (offline; similar to /session-info Auth method).

    SuperGrok login session (auth.json / cached_token) wins over XAI_API_KEY
    unless config has [auth] preferred_method = \"api_key\".
    """
    grok_home = get_grok_home(ctx.obj.get("grok_home") if ctx.obj else None)
    status = detect_auth(grok_home)

    hist_events = load_auth_history(grok_home) if history else []
    changes = auth_history_change_points(hist_events) if hist_events else []
    bill = load_billing_snapshot(grok_home)
    prepaid = bill.prepaid_usd
    weekly_pct = bill.weekly_pct
    plan = bill.subscription_tier
    plan_raw = bill.subscription_tier_raw
    plan_lab = subscription_tier_label(plan) if plan else None
    reset_iso = iso_or_none(bill.weekly_period_end)
    reset_local = format_weekly_reset_local(bill.weekly_period_end)

    if json_out:
        payload = {
            "auth": status.as_dict(),
            "extra_credits_usd": prepaid,
            "weekly_usage_pct": weekly_pct,
            "weekly_period_start": iso_or_none(bill.weekly_period_start),
            "weekly_resets_at": reset_iso,
            "subscription_tier": plan,
            "subscription_tier_raw": plan_raw,
            "history": [e.as_dict() for e in changes] if history else None,
            "history_event_count": len(hist_events) if history else None,
        }
        print(json.dumps(payload, indent=2))
        return

    console.print("[bold]Grok Build auth path[/bold] (this machine · now)\n")
    console.print(f"  Effective: [cyan]{status.label}[/cyan]  ({status.effective})")
    console.print(f"  Session file: {status.session_path}")
    console.print(f"  Session credentials present: {'yes' if status.session_present else 'no'}")
    if status.session_email:
        console.print(f"  Signed in as: {status.session_email}")
    console.print(f"  XAI_API_KEY env: {'set' if status.api_key_env_present else 'not set'}")
    console.print(f"  preferred_method: {status.preferred_method or '(not set in config.toml)'}")
    console.print(f"\n  Spend lens: {status.spend_hint}")
    for n in status.notes:
        console.print(f"  [dim]• {n}[/dim]")

    # Billing snapshot from unified.jsonl (same source as SuperGrok Usage panel)
    console.print("\n[bold]Subscription wallet snapshot[/bold] [dim](from billing log)[/dim]")
    if plan_lab:
        raw_bit = f"  ({plan_raw})" if plan_raw and plan_raw != plan_lab else ""
        console.print(f"  Plan: [cyan]{plan_lab}[/cyan]{raw_bit}")
    else:
        console.print("  Plan: [dim](no subscriptionTier in billing log yet)[/dim]")
    if prepaid is not None:
        console.print(f"  Extra Credits: [cyan]${prepaid:.2f}[/cyan]")
    else:
        console.print("  Extra Credits: [dim](no billing sample in logs yet)[/dim]")
    week_name = plan_lab or "SuperGrok"
    if weekly_pct is not None:
        console.print(f"  Weekly {week_name} limit: [cyan]{weekly_pct:g}%[/cyan] used")
    else:
        console.print(f"  Weekly {week_name} limit: [dim](no billing sample in logs yet)[/dim]")
    reset_what = weekly_pool_reset_subject(plan)
    if reset_local:
        console.print(
            f"  {reset_what} resets: [cyan]{reset_local}[/cyan]",
            no_wrap=True,
            overflow="ignore",
            crop=False,
        )
    elif prepaid is not None or weekly_pct is not None:
        console.print(
            f"  {reset_what} resets: [dim](no currentPeriod.end in billing log yet)[/dim]"
        )
    console.print(
        "  [dim]Best-effort last fetch from ~/.grok/logs/unified.jsonl "
        "(ctx.subscriptionTier + wallet + currentPeriod.end) — not live network; "
        "stale until Build refetches billing. Session /usage turns do not store "
        "the plan. Resets clock is local, same as /usage.[/dim]"
    )

    console.print()
    console.print(
        "Override to force API when logged in:\n"
        "  # ~/.grok/config.toml\n"
        "  [auth]\n"
        '  preferred_method = "api_key"\n'
        "  # or remove ~/.grok/auth.json (recreated if you grok login again)",
        style="dim",
        markup=False,
    )
    console.print(
        "\nAuth method is not stored on turn usage files. "
        "History is best-effort process-level only: "
        "grok-utils auth status --history",
        style="dim",
        markup=False,
    )

    if history:
        if not hist_events:
            warn("No auth method events found in logs/unified.jsonl (missing or truncated).")
            return
        console.print(
            f"\n[bold]Auth history[/bold] "
            f"({len(hist_events)} events in log scan · {len(changes)} change-points)\n"
        )
        t = make_table(
            "Auth method change-points (from unified.jsonl)",
            ["When", "Method", "Log msg"],
        )
        for ev in changes[-30:]:
            t.add_row(ev.ts or "?", ev.method_id, ev.source_msg[:48])
        console.print(t)
        console.print(
            "[dim]cached_token ≈ SuperGrok session · xai.api_key ≈ API key path. "
            "Process-level events, not per-turn billing.[/dim]"
        )
