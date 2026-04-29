"""Tideline configure — wire R2 credentials and site URL into local files.

Run AFTER you have your Cloudflare R2 bucket + API token, and AFTER you know
your Cloudflare Pages URL (or custom domain). Idempotent — safe to re-run.

What it does:
  1. Updates workers/.env with R2 credentials (gitignored, never committed)
  2. Patches web/index.html: DATA_URL, LOG_URL, OG/Twitter meta tags
  3. Patches web/methodology.html: research_log fetch URL, OG/Twitter meta tags
  4. Reports a diff summary

Usage:
  python configure.py                            # interactive
  python configure.py --auto                     # read all values from existing .env

What it does NOT do:
  - Sign you into Cloudflare (you do that in browser)
  - Create R2 bucket (browser)
  - Push to GitHub (deploy.sh does that)
  - Set GitHub Actions secrets (deploy.sh does that)
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
ENV_PATH = ROOT / "workers" / ".env"
WEB_DIR = ROOT / "web"

REQUIRED = [
    ("R2_ACCOUNT_ID",        "Cloudflare R2 Account ID (from Manage R2 API Tokens page)"),
    ("R2_ACCESS_KEY_ID",     "R2 Access Key ID"),
    ("R2_SECRET_ACCESS_KEY", "R2 Secret Access Key"),
    ("R2_BUCKET",            "R2 bucket name (e.g. 'tideline')"),
    ("R2_PUBLIC_URL",        "R2 public URL, no trailing slash (e.g. https://pub-abc123.r2.dev)"),
    ("SITE_URL",             "Your live site URL, no trailing slash (Cloudflare Pages or custom domain, e.g. https://tideline-xyz.pages.dev)"),
]
OPTIONAL = [
    ("FRED_API_KEY",         "FRED API key (already set if you came from previous step)"),
    ("COINGECKO_DEMO_KEY",   "CoinGecko demo key (already set if you came from previous step)"),
]


# --------------------------------------------------------------------
# .env handling
# --------------------------------------------------------------------

def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def save_env(env: dict[str, str]) -> None:
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in env.items() if v]
    ENV_PATH.write_text("\n".join(lines) + "\n")


def prompt(name: str, hint: str, current: str = "", secret: bool = False) -> str:
    label_current = " (already set)" if current else ""
    print(f"\n  {name}{label_current}")
    print(f"  {hint}")
    val = input("  > ").strip()
    return val or current


# --------------------------------------------------------------------
# HTML patching
# --------------------------------------------------------------------

def patch_index_html(site_url: str, r2_url: str) -> tuple[Path, list[str]]:
    path = WEB_DIR / "index.html"
    text = path.read_text(encoding="utf-8")
    changes: list[str] = []

    # DATA_URL line
    new = f"const DATA_URL = '{r2_url}/latest.json';"
    pattern = re.compile(r"const DATA_URL\s*=\s*[^;]+;")
    if pattern.search(text):
        text2 = pattern.sub(new, text, count=1)
        if text2 != text:
            changes.append("DATA_URL -> " + r2_url + "/latest.json")
            text = text2

    # LOG_URL line
    new = f"const LOG_URL  = '{r2_url}/decision_log.csv';"
    pattern = re.compile(r"const LOG_URL\s*=\s*[^;]+;")
    if pattern.search(text):
        text2 = pattern.sub(new, text, count=1)
        if text2 != text:
            changes.append("LOG_URL -> " + r2_url + "/decision_log.csv")
            text = text2

    # OG / Twitter meta tags
    text, m1 = patch_meta(text, "og:url", site_url)
    text, m2 = patch_meta(text, "og:image", site_url + "/og.png")
    text, m3 = patch_meta(text, "twitter:url", site_url, attr="name")
    text, m4 = patch_meta(text, "twitter:image", site_url + "/og.png", attr="name")
    for ok, label in [(m1, "og:url"), (m2, "og:image"), (m3, "twitter:url"), (m4, "twitter:image")]:
        if ok:
            changes.append(f"meta {label} -> updated")

    path.write_text(text, encoding="utf-8")
    return path, changes


def patch_methodology_html(site_url: str, r2_url: str) -> tuple[Path, list[str]]:
    path = WEB_DIR / "methodology.html"
    text = path.read_text(encoding="utf-8")
    changes: list[str] = []

    # Research log fetch URL
    new = f"const r = await fetch('{r2_url}/research_log.md', {{ cache: 'no-cache' }});"
    pattern = re.compile(r"const r\s*=\s*await\s*fetch\([^)]+\);")
    if pattern.search(text):
        text2 = pattern.sub(new, text, count=1)
        if text2 != text:
            changes.append("research_log fetch -> " + r2_url + "/research_log.md")
            text = text2

    text, m1 = patch_meta(text, "og:url", site_url + "/methodology.html")
    text, m2 = patch_meta(text, "og:image", site_url + "/og.png")
    text, m3 = patch_meta(text, "twitter:url", site_url + "/methodology.html", attr="name")
    text, m4 = patch_meta(text, "twitter:image", site_url + "/og.png", attr="name")
    for ok, label in [(m1, "og:url"), (m2, "og:image"), (m3, "twitter:url"), (m4, "twitter:image")]:
        if ok:
            changes.append(f"meta {label} -> updated")

    path.write_text(text, encoding="utf-8")
    return path, changes


def patch_meta(text: str, key: str, value: str, attr: str = "property") -> tuple[str, bool]:
    """Replace a <meta property=key content="..."> tag's content attribute."""
    pattern = re.compile(
        rf'(<meta\s+{attr}="{re.escape(key)}"\s+content=")[^"]*(">)',
        re.IGNORECASE,
    )
    new_text, n = pattern.subn(rf'\1{value}\2', text)
    return new_text, n > 0


# --------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------

def validate(env: dict[str, str]) -> list[str]:
    errs = []
    if not env.get("R2_PUBLIC_URL", "").startswith("https://"):
        errs.append("R2_PUBLIC_URL must start with https://")
    if env.get("R2_PUBLIC_URL", "").endswith("/"):
        errs.append("R2_PUBLIC_URL must NOT end with a trailing slash")
    if not env.get("SITE_URL", "").startswith("https://"):
        errs.append("SITE_URL must start with https://")
    if env.get("SITE_URL", "").endswith("/"):
        errs.append("SITE_URL must NOT end with a trailing slash")
    return errs


# --------------------------------------------------------------------
# Main
# --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", action="store_true",
                    help="Skip prompts; require all values to already exist in workers/.env")
    args = ap.parse_args()

    print("=" * 70)
    print(" Tideline configure")
    print("=" * 70)

    env = load_env()
    print(f"\nLoaded {ENV_PATH} ({len(env)} entries)")

    if args.auto:
        # Just validate and use existing values
        missing = [k for k, _ in REQUIRED if not env.get(k)]
        if missing:
            print(f"\nERROR: --auto requires these to be set in .env: {missing}")
            print("Run without --auto to enter them interactively.")
            return 1
    else:
        print("\nEnter values (press Enter to keep current). All stored locally in workers/.env (gitignored).")
        for name, hint in REQUIRED:
            env[name] = prompt(name, hint, env.get(name, ""))
        # Optional — show but don't force
        for name, hint in OPTIONAL:
            if env.get(name):
                continue
            env[name] = prompt(name, hint, env.get(name, ""))

    errs = validate(env)
    if errs:
        print("\nERRORS:")
        for e in errs:
            print(f"  - {e}")
        return 1

    save_env(env)
    print(f"\n[ok] saved to {ENV_PATH}")

    print("\nPatching frontend files...")
    path1, changes1 = patch_index_html(env["SITE_URL"], env["R2_PUBLIC_URL"])
    print(f"  {path1.relative_to(ROOT)}: {len(changes1)} changes")
    for c in changes1:
        print(f"    - {c}")
    path2, changes2 = patch_methodology_html(env["SITE_URL"], env["R2_PUBLIC_URL"])
    print(f"  {path2.relative_to(ROOT)}: {len(changes2)} changes")
    for c in changes2:
        print(f"    - {c}")

    print("\nNext: run ./deploy.sh to push to GitHub + set Actions secrets + trigger first run.")
    print("(Requires GitHub CLI 'gh' to be installed and authenticated.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
