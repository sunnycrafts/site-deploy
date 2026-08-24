"""
Site Deploy — hands-off website update tool.

Stores website connection details (encrypted to your Windows account via
DPAPI — unreadable on any other PC or Windows login), then uploads a zip
file's contents over FTP/FTPS/SFTP to replace files on the live server.

Runs in the system tray and watches one inbox folder per website: drop a
zip into a site's folder and it deploys itself, no clicking. The window
(sites, status, activity, and a manual deploy under Advanced) is opened
from the tray icon.

Visual theme lives in theme.py.
"""

import base64
import ftplib
import json
import os
import posixpath
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal, QSize
from PySide6.QtGui import QAction, QFont, QIcon
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QPushButton, QLabel, QLineEdit, QComboBox, QSpinBox, QListWidget,
    QListWidgetItem, QPlainTextEdit, QFileDialog, QMessageBox, QDialog,
    QDialogButtonBox, QCheckBox, QProgressBar, QMenu, QSystemTrayIcon,
    QFrame, QSizePolicy,
)

import theme
from theme import PALETTE, line_icon, tray_icon, apply_window_effects

APP_NAME = "Site Deploy"
APP_VERSION = "4.5.0"
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "SiteDeploy"
PROFILES_FILE = CONFIG_DIR / "profiles.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
HISTORY_FILE = CONFIG_DIR / "history.log"
HISTORY_JSON = CONFIG_DIR / "history.jsonl"

DEFAULT_INBOX_ROOT = Path.home() / "Desktop" / "claude code" / "Deploy Inbox"
DEPLOYED_DIRNAME = "Deployed"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "SiteDeploy"

# How long a zip's size must stop changing before it is considered
# finished copying (spec: ~2 seconds).
STABLE_SECONDS = 2.0
POLL_SECONDS = 3.0

PROTOCOL_AUTO_FTP = "Automatic — secure if the server allows it (recommended)"
PROTOCOL_FTPS = "Secure FTP (also called FTPS)"
PROTOCOL_FTP = "Plain FTP — NOT secure"
PROTOCOL_SFTP = "Secure SSH transfer (also called SFTP)"
PROTOCOLS = [PROTOCOL_AUTO_FTP, PROTOCOL_FTPS, PROTOCOL_FTP, PROTOCOL_SFTP]

# Saved profiles store the label itself, so old wording has to keep working.
LEGACY_PROTOCOLS = {
    "Auto (try secure FTP, fall back if needed)": PROTOCOL_AUTO_FTP,
    "FTPS (secure FTP)": PROTOCOL_FTPS,
    "FTP (not encrypted)": PROTOCOL_FTP,
    "SFTP (SSH)": PROTOCOL_SFTP,
}

# Informational only — this never guesses or pre-fills the server address,
# since a wrong guess is worse than no guess. It just points you at where to
# look, and remembers your choice for next time you edit the site. Hostinger
# gets the exact path because it's the one host actually verified against
# this app; everyone else gets honest general guidance rather than a
# specific menu path that might be stale or wrong for that host today.
HOSTING_PRESETS = [
    ("", "Not listed / skip this"),
    ("hostinger", "Hostinger"),
    ("godaddy", "GoDaddy"),
    ("bluehost", "Bluehost"),
    ("siteground", "SiteGround"),
    ("namecheap", "Namecheap"),
    ("dreamhost", "DreamHost"),
    ("other", "Other"),
]
GENERIC_HOST_HINT = ("Check your hosting account's control panel for FTP or SFTP "
                    "details — usually under a section called Files, FTP Accounts, "
                    "or Advanced.")
HOSTING_HINTS = {
    "hostinger": ("Find your FTP details in hPanel → Files → FTP Accounts, or "
                 "Advanced → SSH Access for SFTP credentials instead."),
}


# ─────────────────────────────────────────────────────────────────────────
# Credential encryption (Windows DPAPI — tied to this Windows account)
# ─────────────────────────────────────────────────────────────────────────

def _dpapi_encrypt(plaintext: str) -> str:
    import win32crypt
    blob = win32crypt.CryptProtectData(
        plaintext.encode("utf-8"), APP_NAME, None, None, None, 0
    )
    return base64.b64encode(blob).decode("ascii")


def _dpapi_decrypt(ciphertext_b64: str) -> str:
    import win32crypt
    blob = base64.b64decode(ciphertext_b64.encode("ascii"))
    _desc, data = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
    return data.decode("utf-8")


# Deliberately calling win32crypt directly here rather than going through
# _dpapi_encrypt/_dpapi_decrypt above: those two exist to protect one field
# (a password), and tests stand in for them with fake credentials so they
# can run without needing a real encrypted blob. A function that just
# *calls* _dpapi_decrypt would still be caught by that same monkeypatch —
# Python resolves the name at call time through this module's namespace —
# so whole-record storage encryption needs its own path to the OS API,
# immune to a test swapping out password handling.
def _record_encrypt(plaintext: str) -> str:
    import win32crypt
    blob = win32crypt.CryptProtectData(
        plaintext.encode("utf-8"), f"{APP_NAME} record", None, None, None, 0
    )
    return base64.b64encode(blob).decode("ascii")


def _record_decrypt(ciphertext_b64: str) -> str:
    import win32crypt
    blob = base64.b64decode(ciphertext_b64.encode("ascii"))
    _desc, data = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
    return data.decode("utf-8")


# ─────────────────────────────────────────────────────────────────────────
# Credential vault — the actual secret lives in Windows, not in this app
# ─────────────────────────────────────────────────────────────────────────
#
# Bridging "remembered for next time" with "the program doesn't hold it":
# the username and password for a website are never written into
# profiles.json at all, encrypted or not — they're stored in Windows'
# own Credential Manager, the same protected vault the OS and browsers use
# for saved logins. The app keeps only a random ID pointing at that vault
# entry. Windows persists the real secret across restarts (so nothing
# needs re-typing), and the app asks Windows for it only at the moment it
# actually needs to connect.

VAULT_TARGET_PREFIX = "SiteDeploy:"


def vault_save_credential(credential_id: str, username: str, password: str):
    import win32cred
    win32cred.CredWrite({
        "Type": win32cred.CRED_TYPE_GENERIC,
        "TargetName": VAULT_TARGET_PREFIX + credential_id,
        "UserName": username,
        "CredentialBlob": password,
        "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
        "Comment": APP_NAME,
    }, 0)


def vault_read_credential(credential_id: str) -> tuple[str, str] | tuple[None, None]:
    if not credential_id:
        return None, None
    import win32cred
    try:
        cred = win32cred.CredRead(VAULT_TARGET_PREFIX + credential_id,
                                  win32cred.CRED_TYPE_GENERIC)
    except Exception:
        return None, None
    username = cred.get("UserName") or ""
    blob = cred.get("CredentialBlob") or b""
    password = blob.decode("utf-16-le") if isinstance(blob, (bytes, bytearray)) else blob
    return username, password


def vault_delete_credential(credential_id: str):
    if not credential_id:
        return
    import win32cred
    try:
        win32cred.CredDelete(VAULT_TARGET_PREFIX + credential_id, win32cred.CRED_TYPE_GENERIC)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────
# Profile model + storage
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class Profile:
    name: str = ""
    host: str = ""
    port: int = 21
    username: str = ""            # in-memory only — lives in the vault, never on disk
    password_enc: str = ""        # in-memory only — DPAPI-wrapped copy of the vault's secret
    credential_id: str = ""       # the ONLY thing that gets persisted for the credential
    protocol: str = PROTOCOL_AUTO_FTP
    remote_path: str = ""        # e.g. /public_html/wp-content/themes/wingchun-child
    notes: str = ""
    watch: bool = True           # auto-deploy zips dropped in this site's inbox
    tested_ok: bool = False      # has a connection test or deploy ever succeeded
    hosting_provider: str = ""   # which preset was picked, if any — informational only

    def to_dict(self):
        """What gets written to profiles.json. username/password_enc are
        deliberately left out — the real secret lives only in the Windows
        credential vault, referenced by credential_id."""
        d = asdict(self)
        d.pop("username", None)
        d.pop("password_enc", None)
        return d

    @staticmethod
    def from_dict(d):
        p = Profile()
        p.__dict__.update({k: v for k, v in d.items() if k in p.__dict__})
        p.protocol = LEGACY_PROTOCOLS.get(p.protocol, p.protocol)
        return p

    def hydrate_credential(self):
        """Pull username/password out of the vault into memory. Called right
        after loading — nothing about the secret is read until this point,
        and what comes back is re-wrapped with the existing per-field DPAPI
        encryption so every other part of the app keeps working with
        password_enc exactly as it always has."""
        if not self.credential_id:
            return
        username, password = vault_read_credential(self.credential_id)
        if username is not None:
            self.username = username
            self.password_enc = _dpapi_encrypt(password) if password else ""


def clean_host(raw: str) -> tuple[str, int | None]:
    """People paste 'ftp://host/', 'https://site.com', 'user@host:21'.
    Reduce any of those to the bare host (and a port, if one was given) —
    a URL in this field is the #1 cause of 'getaddrinfo failed'."""
    s = (raw or "").strip()
    s = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", "", s)   # drop scheme
    s = s.split("/")[0]                                   # drop any path
    if "@" in s:
        s = s.rsplit("@", 1)[-1]                          # drop user@
    port = None
    if s.count(":") == 1:                                 # host:port (not IPv6)
        head, _, tail = s.partition(":")
        if tail.isdigit():
            s, port = head, int(tail)
    return s.strip().strip(".").lower(), port


def clean_site_name(raw: str) -> str:
    """A website name is a label, not a URL — strip a pasted scheme so the
    drop folder doesn't end up called 'https---example.com'."""
    s = (raw or "").strip()
    s = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", "", s)
    return s.rstrip("/").strip()


def parse_connection_text(text: str) -> dict:
    """Best-effort read of a pasted block of FTP/SFTP details — the kind of
    table a hosting panel shows, or a line-by-line note. Every field is
    optional and only used to PRE-FILL the Add Website dialog; nothing
    from here is ever saved without the user pressing Save themselves."""
    def grab(*patterns):
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
            if m:
                return m.group(1).strip().strip(",;")
        return ""

    out = {}
    host_raw = grab(
        r"(?:ftp\s*/?\s*sftp\s*host|ftp\s*host|sftp\s*host|host\s*name|"
        r"server\s*(?:address|name)?|host)\s*[:=]\s*(\S+)")
    if host_raw:
        host, port = clean_host(host_raw)
        if host:
            out["host"] = host
        if port:
            out["port"] = port

    user = grab(r"(?:ftp\s*/?\s*sftp\s*)?user\s*name\s*[:=]\s*(\S+)",
               r"(?:ftp\s*/?\s*sftp\s*)?username\s*[:=]\s*(\S+)",
               r"(?:ftp\s*/?\s*sftp\s*)?login\s*[:=]\s*(\S+)",
               r"\buser\s*[:=]\s*(\S+)")
    if user:
        out["username"] = user

    pw = grab(r"pass\s*word\s*[:=]\s*(\S+)", r"password\s*[:=]\s*(\S+)",
             r"\bpass\s*[:=]\s*(\S+)")
    if pw:
        out["password"] = pw

    if "port" not in out:
        port = grab(r"\bport\s*[:=]\s*(\d+)")
        if port.isdigit():
            out["port"] = int(port)

    path = grab(r"(?:remote\s*(?:path|dir(?:ectory)?)|(?:target|install)\s*folder|"
               r"folder|directory|path)\s*[:=]\s*(\S+)")
    if path:
        out["remote_path"] = path

    return out


_HOST_LABEL_RE = re.compile(
    r"(?:ftp\s*/?\s*sftp\s*host|ftp\s*host|sftp\s*host|host\s*name|"
    r"server\s*(?:address|name)?|host)\s*[:=]",
    re.IGNORECASE)


def split_connection_blocks(text: str) -> list[str]:
    """Break a note into one chunk per website. parse_connection_text()
    grabs only the FIRST match of each field across whatever text it's
    given — feed it a file with several sites listed and it would happily
    mix a host from one with a username from another. This is what keeps
    each site's fields together: split on blank-line paragraphs first, then
    if a paragraph still has more than one 'Host:'-shaped label in it (a
    continuous list with no blank lines between entries), split that
    paragraph again, right before each such label."""
    paragraphs = re.split(r"\n\s*\n", text.strip())
    blocks = []
    for para in paragraphs:
        if not para.strip():
            continue
        starts = [m.start() for m in _HOST_LABEL_RE.finditer(para)]
        if len(starts) <= 1:
            blocks.append(para)
            continue
        line_starts = sorted({para.rfind("\n", 0, s) + 1 for s in starts})
        for i, ls in enumerate(line_starts):
            end = line_starts[i + 1] if i + 1 < len(line_starts) else len(para)
            blocks.append(para[ls:end])
    return blocks


def parse_connection_entries(text: str) -> list[dict]:
    """One dict per website recognized in the text, in order, with
    duplicates (same host + username appearing twice) dropped."""
    entries = []
    seen = set()
    for block in split_connection_blocks(text):
        info = parse_connection_text(block)
        if not info.get("host") or not (info.get("username") or info.get("password")):
            continue
        key = (info.get("host"), info.get("username"))
        if key in seen:
            continue
        seen.add(key)
        entries.append(info)
    return entries


_HOST_SCHEME_LINE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://\s*(\S+)\s*$")
_DOMAIN_OR_IP_LINE_RE = re.compile(
    r"^(?:\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,})"
    r"(?::\d+)?$")
_DIGITS_ONLY_LINE_RE = re.compile(r"^\d{1,5}$")
_SINGLE_TOKEN_RE = re.compile(r"^\S{1,128}$")


def parse_positional_connection(text: str) -> dict:
    """No labels at all — a personal template like

        name of website
        ftp:// address here
        port number here
        user id here
        pass here

    one value per line, in whatever order, filled in with real values.
    Finds the host and port by SHAPE (a line that looks like an address; a
    line that's nothing but a number), then treats the lines right after
    the host as username then password — the near-universal order for a
    short list like this. Refuses to guess at all if the file doesn't look
    like a short field-per-line list: real prose (sentences with spaces
    and punctuation) is never mistaken for a username or password, and a
    long file is never scanned this way in the first place, so this stays
    a narrow fallback rather than something that could misfire generally."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not (2 <= len(lines) <= 12):
        return {}

    out = {}
    host_idx = None

    # Pass 1: an explicit scheme (ftp://, sftp://...) is a much stronger
    # signal of "this is meant to be a network address" than a bare word
    # that merely looks domain-shaped — a website's own name is very often
    # itself a domain (e.g. "wingchunclub.in" as the display name, with the
    # real FTP host being a completely different address/IP below it). Only
    # look for a bare domain/IP at all if no scheme-prefixed line exists.
    for i, line in enumerate(lines):
        m = _HOST_SCHEME_LINE_RE.match(line)
        if m:
            host, port = clean_host(m.group(1))
            if host:
                out["host"] = host
                if port:
                    out["port"] = port
                host_idx = i
                break

    if host_idx is None:
        for i, line in enumerate(lines):
            if _DOMAIN_OR_IP_LINE_RE.match(line):
                host, port = clean_host(line)
                if host:
                    out["host"] = host
                    if port:
                        out["port"] = port
                    host_idx = i
                    break

    if host_idx is None:
        return {}   # nothing address-shaped found — too little to go on

    port_idx = None
    for i, line in enumerate(lines):
        if i != host_idx and "port" not in out and _DIGITS_ONLY_LINE_RE.match(line):
            out["port"] = int(line)
            port_idx = i
            break

    claimed = {host_idx, port_idx} - {None}
    before = [ln for i, ln in enumerate(lines) if i < host_idx and i not in claimed]
    after = [ln for i, ln in enumerate(lines) if i > host_idx and i not in claimed]

    # A username/password candidate must be one token, no spaces or
    # sentence punctuation — a real credential looks like that; a
    # sentence from an unrelated document never does.
    after = [ln for ln in after if _SINGLE_TOKEN_RE.match(ln) and not ln.endswith((".", "!", "?"))]
    if after:
        out["username"] = after[0]
    if len(after) > 1:
        out["password"] = after[1]
    if before:
        out["name"] = before[0]
    return out


def parse_connection_entries_smart(text: str) -> list[dict]:
    """Try labeled fields first (Host:, Username:, ...); if nothing was
    labeled, fall back to no-labels positional reading — once per
    blank-line block, so several unlabeled sites listed together (the
    same file shape as the multi-entry labeled case, just without keys)
    come back as separate, correctly-matched entries instead of being
    read as one oversized block and rejected outright."""
    entries = parse_connection_entries(text)
    if entries:
        return entries

    results = []
    seen = set()
    for block in re.split(r"\n\s*\n", text.strip()):
        info = parse_positional_connection(block)
        if not info.get("host"):
            continue
        key = (info.get("host"), info.get("username"))
        if key in seen:
            continue
        seen.add(key)
        results.append(info)
    if results:
        return results

    whole = parse_positional_connection(text)
    return [whole] if whole.get("host") else []


def normalize_profile(p: Profile) -> Profile:
    host, port = clean_host(p.host)
    if host:
        p.host = host
    if port:
        p.port = port
    cleaned_name = clean_site_name(p.name) or p.name
    if cleaned_name != p.name:
        # Remembered (not saved to disk) so the drop folder can follow the
        # tidied-up name instead of leaving an orphan behind.
        p.legacy_name = p.name
        p.name = cleaned_name
    return p


PROFILES_BACKUP_FILE = CONFIG_DIR / "profiles.json.bak"


def _decode_profile_record(entry: dict) -> dict | None:
    """A record is either the new fully-encrypted envelope {"enc": "..."} or
    an old plaintext dict from before this format existed. Either way,
    return the plain profile dict (never written back to disk as-is)."""
    if isinstance(entry, dict) and "enc" in entry and len(entry) == 1:
        try:
            return json.loads(_record_decrypt(entry["enc"]))
        except Exception:
            return None
    if isinstance(entry, dict):
        return entry   # legacy plaintext record — still readable, will be upgraded
    return None


def _load_profiles_from(path: Path) -> tuple[list[Profile], bool]:
    """Returns (profiles, needs_resave). needs_resave covers two upgrades:
    an old plaintext record, or a record still carrying its own
    password_enc — from before the credential moved into the vault."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    profiles, needs_resave = [], False
    for entry in raw:
        d = _decode_profile_record(entry)
        if d is None:
            continue   # one unreadable record shouldn't cost you every other site
        is_encrypted_envelope = isinstance(entry, dict) and "enc" in entry and len(entry) == 1
        if not is_encrypted_envelope:
            needs_resave = True

        p = normalize_profile(Profile.from_dict(d))

        legacy_password_enc = d.get("password_enc") or ""
        if legacy_password_enc and not p.credential_id:
            # A profile saved before the credential moved into the vault —
            # migrate it in, once, without asking the user to re-type anything.
            try:
                plaintext_password = _dpapi_decrypt(legacy_password_enc)
            except Exception:
                plaintext_password = ""
            if plaintext_password:
                p.credential_id = str(uuid.uuid4())
                vault_save_credential(p.credential_id, d.get("username", ""),
                                      plaintext_password)
                p.username = d.get("username", "")
                p.password_enc = _dpapi_encrypt(plaintext_password)
                needs_resave = True
        elif p.credential_id:
            p.hydrate_credential()

        profiles.append(p)
    return profiles, needs_resave


def load_profiles() -> list[Profile]:
    """Nothing about a website — name, host, username, folder — sits on disk
    as plain text. Every record is DPAPI-encrypted as a whole, the same way
    the password always has been, so the file itself reveals nothing if
    someone got hold of it without your Windows login."""
    if not PROFILES_FILE.exists():
        return []
    try:
        profiles, legacy = _load_profiles_from(PROFILES_FILE)
    except Exception:
        # The main file didn't parse. Try the backup taken before the last
        # save, rather than presenting an empty list as if nothing was saved.
        if PROFILES_BACKUP_FILE.exists():
            try:
                profiles, legacy = _load_profiles_from(PROFILES_BACKUP_FILE)
            except Exception:
                return []
        else:
            return []
    if legacy and profiles:
        save_profiles(profiles)   # upgrade the file to the encrypted format, once
    return profiles


def save_profiles(profiles: list[Profile]):
    """Every field is wrapped in one DPAPI envelope per profile — not just
    the password. A copy of what was there before is kept as .bak, so a
    bad write (a crash mid-save, two copies of the app racing each other)
    doesn't cost you the previous good version."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if PROFILES_FILE.exists():
        try:
            shutil.copy2(PROFILES_FILE, PROFILES_BACKUP_FILE)
        except Exception:
            pass
    records = [{"enc": _record_encrypt(json.dumps(p.to_dict()))} for p in profiles]
    PROFILES_FILE.write_text(json.dumps(records, indent=2), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────

DEFAULT_SETTINGS = {
    "inbox_root": str(DEFAULT_INBOX_ROOT),
    "watching_enabled": True,
    "start_with_windows": False,     # spec: default off until confirmed wanted
    "tray_hint_shown": False,
    "appearance": "light",           # light / dark / auto (follow Windows)
}


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return s


def save_settings(settings: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def append_history(site: str, filename: str, ok: bool, detail: str,
                   action: str = "", target: str = "", backup: str = ""):
    """The record of everything that happened: a readable text log, plus a
    structured one so the app can show (and restore from) past updates."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    status = "OK   " if ok else "FAIL "
    try:
        with open(HISTORY_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp}  {status} {site}  {filename}  {detail}\n")
    except Exception:
        pass
    try:
        with open(HISTORY_JSON, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "time": now.isoformat(timespec="seconds"),
                "site": site, "file": filename, "ok": ok, "action": action or
                ("update" if ok else "failed"), "target": target,
                "backup": backup, "detail": detail,
            }) + "\n")
    except Exception:
        pass


def read_records(limit: int = 300) -> list[dict]:
    """Structured history, newest last. Falls back to the text log for
    anything written before the structured log existed."""
    records = []
    if HISTORY_JSON.exists():
        try:
            for line in HISTORY_JSON.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        except Exception:
            pass
    if not records:
        for line in read_history(limit):
            parts = [x.strip() for x in line.split("  ") if x.strip()]
            if len(parts) < 3:
                continue
            records.append({"time": parts[0].replace(" ", "T"),
                            "site": parts[2] if len(parts) > 2 else "",
                            "file": parts[3] if len(parts) > 3 else "",
                            "ok": parts[1] == "OK",
                            "action": "update" if parts[1] == "OK" else "failed",
                            "target": "", "backup": "",
                            "detail": " ".join(parts[4:])})
    return records[-limit:]


def read_history(limit: int = 200) -> list[str]:
    if not HISTORY_FILE.exists():
        return []
    try:
        lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    return lines[-limit:]


# ─────────────────────────────────────────────────────────────────────────
# Server-shape cache — WordPress root, matched folders, installed plugins
# ─────────────────────────────────────────────────────────────────────────
#
# All of this is stuff that barely ever changes between one update and the
# next: where WordPress lives, which folder a theme/plugin already matched
# to, what's installed. Re-discovering it from scratch every time is most
# of why things feel slow — it's not processing time, it's round trips.
# The cache is plain JSON (nothing secret lives in it — no host, no
# credentials, just folder paths and plugin names already visible once
# connected) and it's always treated as a hint to verify, never as a fact
# to trust blindly: every cache hit still does one quick existence check
# before being used, so a changed site self-corrects instead of drifting.

CACHE_DIR = CONFIG_DIR / "cache"
CACHE_MAX_AGE_SECONDS = 60 * 60 * 24 * 14   # two weeks — plenty stale enough to force a re-check


def _cache_path(profile_name: str) -> Path:
    return CACHE_DIR / f"{safe_folder_name(profile_name)}.json"


def load_site_cache(profile_name: str) -> dict:
    p = _cache_path(profile_name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_site_cache(profile_name: str, data: dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _cache_path(profile_name).write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def clear_site_cache(profile_name: str):
    try:
        _cache_path(profile_name).unlink(missing_ok=True)
    except Exception:
        pass


def cache_get_wp_root(profile_name: str) -> str | None:
    entry = load_site_cache(profile_name).get("wp_root")
    if not entry:
        return None
    age = time.time() - entry.get("checked_at", 0)
    if age > CACHE_MAX_AGE_SECONDS:
        return None
    return entry.get("path") or None


def cache_set_wp_root(profile_name: str, path: str):
    data = load_site_cache(profile_name)
    data["wp_root"] = {"path": path, "checked_at": time.time()}
    save_site_cache(profile_name, data)


def cache_get_matched_folder(profile_name: str, slug: str) -> str | None:
    entry = load_site_cache(profile_name).get("folder_matches", {}).get(slug)
    if not entry:
        return None
    age = time.time() - entry.get("checked_at", 0)
    if age > CACHE_MAX_AGE_SECONDS:
        return None
    return entry.get("name") or None


def cache_set_matched_folder(profile_name: str, slug: str, name: str):
    data = load_site_cache(profile_name)
    data.setdefault("folder_matches", {})[slug] = {"name": name, "checked_at": time.time()}
    save_site_cache(profile_name, data)


def cache_get_plugins(profile_name: str) -> tuple[list, float] | tuple[None, None]:
    entry = load_site_cache(profile_name).get("plugins")
    if not entry:
        return None, None
    return entry.get("list", []), entry.get("checked_at", 0)


def cache_set_plugins(profile_name: str, plugins: list):
    data = load_site_cache(profile_name)
    data["plugins"] = {
        "list": [asdict(p) for p in plugins],
        "checked_at": time.time(),
    }
    save_site_cache(profile_name, data)


def last_deploy_summary() -> str:
    """'2 hours ago' style line for the dashboard tile."""
    for line in reversed(read_history(400)):
        parts = line.split("  ")
        if len(parts) < 3:
            continue
        try:
            when = datetime.strptime(parts[0].strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        status = parts[1].strip()
        if status not in ("OK", "FAIL"):
            continue
        delta = datetime.now() - when
        mins = int(delta.total_seconds() // 60)
        if mins < 1:
            ago = "just now"
        elif mins < 60:
            ago = f"{mins} min ago"
        elif mins < 60 * 24:
            ago = f"{mins // 60} hr ago"
        else:
            ago = when.strftime("%d %b")
        return f"{ago}" if status == "OK" else f"{ago} (failed)"
    return "Never"


# ─────────────────────────────────────────────────────────────────────────
# Inbox folders (one per website — no filename guessing)
# ─────────────────────────────────────────────────────────────────────────

def safe_folder_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', "-", name).strip().rstrip(".")
    return cleaned or "Website"


def inbox_for(inbox_root: str, profile_name: str) -> Path:
    return Path(inbox_root) / safe_folder_name(profile_name)


def ensure_inbox(inbox_root: str, profile_name: str) -> Path:
    folder = inbox_for(inbox_root, profile_name)
    try:
        (folder / DEPLOYED_DIRNAME).mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return folder


def startup_target() -> str:
    """Command to register under the Windows Run key."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.exists() else Path(sys.executable)
    return f'"{exe}" "{Path(__file__).resolve()}"'


def set_start_with_windows(enabled: bool) -> str | None:
    """Returns an error message, or None on success."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ,
                                  startup_target())
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE_NAME)
                except FileNotFoundError:
                    pass
        return None
    except Exception as e:
        return str(e)


def get_start_with_windows() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE_NAME)
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────
# Deploy engine
# ─────────────────────────────────────────────────────────────────────────

class DeployError(Exception):
    pass


def friendly_error(exc: Exception, profile: Profile) -> str:
    """Turn socket/ftplib/paramiko noise into something you can act on.
    The raw text still goes to the activity log and Technical details."""
    import socket
    text = str(exc)

    if isinstance(exc, socket.gaierror) or "getaddrinfo failed" in text or "11001" in text:
        return (f"Couldn't find the server “{profile.host}”.\n\n"
                "Check the Server address in this website's details. It should be "
                "just the address — like ftp.yoursite.com or 145.79.213.73 — with "
                "no ftp:// or https:// in front and nothing after it.")
    if isinstance(exc, socket.timeout) or "timed out" in text.lower():
        return (f"“{profile.host}” didn't answer on port {profile.port}.\n\n"
                "The address may be right but the port or connection type wrong, "
                "or your host is blocking the connection.")
    if "10061" in text or "refused" in text.lower():
        return (f"“{profile.host}” refused the connection on port {profile.port}.\n\n"
                "Try a different connection type — SFTP usually uses port 22, "
                "FTP/FTPS port 21.")
    if "530" in text or "authentication failed" in text.lower() or "not logged in" in text.lower():
        return ("The username or password was rejected.\n\n"
                "Re-enter them in this website's details. For Hostinger these are "
                "the FTP account details, not your hPanel login.")
    if "450" in text or "no such file" in text.lower() or "not found" in text.lower():
        return (f"The server has no folder called “{profile.remote_path}”.\n\n"
                "Use Test now to see what's actually on the server and pick the "
                "right folder.")
    if "550" in text or "permission" in text.lower():
        return (f"The server wouldn't let it write to “{profile.remote_path}”.\n\n"
                "Check that folder path exists and belongs to this FTP account.")
    if "no files to upload" in text.lower():
        return "That zip is empty — there's nothing in it to upload."
    return text


# What you're allowed to pick. Zips are whole packages; the rest are single
# files you might want to push on their own (a plugin's php, a stylesheet,
# a script, a translation, an image).
SUPPORTED_EXTS = (
    ".zip .php .css .js .json .html .htm .txt .md .po .mo .pot .xml .map .svg "
    ".png .jpg .jpeg .gif .webp .ico .avif .woff .woff2 .ttf .otf .eot"
).split()

FILE_DIALOG_FILTER = (
    "Everything Site Deploy can upload ("
    + " ".join(f"*{e}" for e in SUPPORTED_EXTS) + ");;"
    "Zip packages (*.zip);;"
    "PHP files (*.php);;"
    "Style and script files (*.css *.js);;"
    "All files (*)"
)


@dataclass
class ZipInfo:
    """What the chosen thing actually is — a zip package, or a single file."""
    slug: str = ""            # folder name it wants on the server
    kind: str = "unknown"     # theme | plugin | unknown
    top_folder: str = ""      # single wrapping folder, if any (zips)
    file_count: int = 0
    is_zip: bool = True
    filename: str = ""        # for a single file: its name on the server

    @property
    def label(self) -> str:
        if not self.is_zip:
            if self.kind == "plugin":
                return f"“{self.filename}” (a WordPress plugin file)"
            if self.kind == "theme":
                return f"“{self.filename}” (a WordPress theme file)"
            return f"“{self.filename}” (a single file)"
        if self.kind == "theme":
            return f"“{self.slug}” (a WordPress theme)"
        if self.kind == "plugin":
            return f"“{self.slug}” (a WordPress plugin)"
        return f"“{self.slug}”" if self.slug else "this zip"


def inspect_file(path: str) -> ZipInfo:
    """A single file. Read its head to see whether it announces itself as a
    WordPress plugin or theme file — that says where it belongs."""
    p = Path(path)
    info = ZipInfo(is_zip=False, filename=p.name, slug=base_slug(p.stem),
                   file_count=1)
    if p.suffix.lower() in (".php", ".css"):
        try:
            head = p.read_bytes()[:_HEADER_READ_BYTES].decode("utf-8", "ignore").lower()
        except Exception:
            head = ""
        if "plugin name:" in head:
            info.kind = "plugin"
        elif "theme name:" in head:
            info.kind = "theme"
        elif any(sign in head for sign in _PLUGIN_API_SIGNS):
            info.kind = "plugin"
    return info


def inspect_source(path: str) -> ZipInfo:
    """Whatever was chosen: a zip package or one file."""
    if Path(path).suffix.lower() == ".zip":
        return inspect_zip(path)
    return inspect_file(path)


# Real plugin/theme files are inconsistent about exactly where their header
# sits and how deep they're nested, so detection reads a generous chunk (not
# 2KB) from EVERY plausible candidate file in the zip, not just the one at
# the "expected" top-level path — a real zip that's shaped slightly
# differently than assumed shouldn't fall through to "unknown" and end up
# asking the user something the zip itself already answers.
_HEADER_READ_BYTES = 8192
_PLUGIN_API_SIGNS = ("register_activation_hook(", "add_action(", "add_filter(",
                    "plugin uri:", "text domain:")


def inspect_zip(zip_path: str) -> ZipInfo:
    """Look inside the zip: what's it called, and is it a theme or plugin?
    That's what tells us where it belongs on the server."""
    info = ZipInfo()
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if not n.startswith("__MACOSX")]
        files = [n for n in names if not n.endswith("/")]
        info.file_count = len(files)

        wrapped = len({n.split("/")[0] for n in names}) == 1 and any("/" in n for n in names)
        info.top_folder = names[0].split("/")[0] if wrapped else ""
        info.slug = info.top_folder or Path(zip_path).stem

        def read_head(name: str) -> str:
            try:
                return zf.read(name)[:_HEADER_READ_BYTES].decode("utf-8", "ignore").lower()
            except Exception:
                return ""

        # 1. Any style.css anywhere shallow (not buried in a vendor/library
        #    folder) — real declared header wins outright.
        style_candidates = sorted(
            (n for n in files if n.lower().endswith("style.css") and n.count("/") <= 2),
            key=lambda n: n.count("/"))
        theme_style = None
        for n in style_candidates:
            head = read_head(n)
            if "theme name:" in head:
                info.kind = "theme"
                theme_style = n
                break

        # 2. Any .php anywhere shallow, similarly — a declared plugin header.
        if info.kind == "unknown":
            php_candidates = sorted(
                (n for n in files if n.lower().endswith(".php") and n.count("/") <= 2),
                key=lambda n: n.count("/"))
            for n in php_candidates:
                head = read_head(n)
                if "plugin name:" in head:
                    info.kind = "plugin"
                    break

        # 3. No declared header at all — fall back to strong structural
        #    signals rather than giving up. A style.css sitting next to the
        #    usual theme files is a theme even with a stripped/nonstandard
        #    header; a .php using WordPress's plugin API is a plugin even
        #    without a "Plugin Name:" line (e.g. a must-use plugin snippet).
        if info.kind == "unknown" and style_candidates:
            base_dir = posixpath.dirname(style_candidates[0])
            siblings = {posixpath.basename(n) for n in files
                       if posixpath.dirname(n) == base_dir}
            if siblings & {"functions.php", "index.php", "header.php", "footer.php"}:
                info.kind = "theme"
                theme_style = style_candidates[0]

        if info.kind == "unknown":
            for n in files:
                if not n.lower().endswith(".php") or n.count("/") > 3:
                    continue
                head = read_head(n)
                if any(sign in head for sign in _PLUGIN_API_SIGNS):
                    info.kind = "plugin"
                    break

        if info.kind == "theme" and theme_style:
            # The real theme folder is wherever style.css actually lives,
            # which may not match the zip's own top-level folder name.
            info.slug = posixpath.basename(posixpath.dirname(theme_style)) or info.slug
    return info


# Where a WordPress install usually lives, best guess first.
WP_ROOT_CANDIDATES = ["/public_html", "/", "/htdocs", "/www", "/web", "/httpdocs"]


def find_wp_root(backend, profile: Profile, log) -> str | None:
    """Find the folder that holds wp-content — that's the WordPress root.
    This barely ever changes, so a cached answer is checked first: one
    quick existence call instead of a whole multi-candidate search."""
    cached = cache_get_wp_root(profile.name)
    if cached and backend.dir_exists(posixpath.join(cached, "wp-content")):
        return cached

    tried = []

    # Anything the saved path already implies wins: it's the user's own answer.
    configured = "/" + (profile.remote_path or "").strip("/")
    parts = [p for p in configured.split("/") if p]
    for i in range(len(parts), 0, -1):
        if parts[i - 1] == "wp-content":
            tried.append("/" + "/".join(parts[:i - 1]))
            break
    tried += WP_ROOT_CANDIDATES

    # Shared hosts often nest sites under /domains/<site>/public_html
    for holder in ("/domains", "/home"):
        if backend.dir_exists(holder):
            for name in backend.list_dir(holder)[:12]:
                tried.append(posixpath.join(holder, name, "public_html"))

    seen = set()
    for cand in tried:
        cand = "/" + cand.strip("/") if cand.strip("/") else "/"
        if cand in seen:
            continue
        seen.add(cand)
        if backend.dir_exists(posixpath.join(cand, "wp-content")):
            log(f"WordPress found at {cand}")
            cache_set_wp_root(profile.name, cand)
            return cand
    return None


VERSION_SUFFIX = re.compile(
    r"[-_. ]*(?:v(?:er(?:sion)?)?[-_. ]?)?\d+(?:[._]\d+)*(?:[-_. ]?(?:alpha|beta|rc)\d*)?$",
    re.IGNORECASE)


def base_slug(name: str) -> str:
    """'wingchun-child-1.4.3' → 'wingchun-child'. Version numbers, copy
    markers and separators shouldn't make it look like a different thing."""
    s = (name or "").strip()
    s = re.sub(r"\s*\(\d+\)$", "", s)          # "theme (1)"
    s = re.sub(r"[-_. ]*(copy|final|new|updated|latest)$", "", s, flags=re.IGNORECASE)
    prev = None
    while prev != s:                            # "theme-1.4.3-v2" → "theme"
        prev = s
        s = VERSION_SUFFIX.sub("", s).strip()
    return s.strip("-_. ") or (name or "").strip()


def _match_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", base_slug(name).lower())


def match_existing_folder(entries: list, slug: str) -> tuple[str | None, str]:
    """Find the folder on the server this package is an update of.

    Exact name wins; then the same name ignoring version/separators; then a
    close spelling match. Returns (folder name, why) or (None, "")."""
    import difflib

    folders = [e for e in entries if e and not e.startswith(".")]
    if slug in folders:
        return slug, "same name"

    key = _match_key(slug)
    if not key:
        return None, ""

    keyed = {name: _match_key(name) for name in folders}

    same = [n for n, k in keyed.items() if k == key]
    if same:
        return same[0], f"same name once the version is ignored (“{same[0]}”)"

    # one contains the other, and is not a trivially short word
    contained = [n for n, k in keyed.items()
                 if k and len(k) >= 4 and (k in key or key in k)]
    if len(contained) == 1:
        return contained[0], f"closest match on the server (“{contained[0]}”)"

    close = difflib.get_close_matches(key, [k for k in keyed.values() if k],
                                      n=1, cutoff=0.85)
    if close:
        name = next(n for n, k in keyed.items() if k == close[0])
        return name, f"closest match on the server (“{name}”)"
    return None, ""


def decide_target(backend, profile: Profile, info: ZipInfo, log) -> dict:
    """Work out the folder on the server this zip belongs in — matching an
    existing folder when the names are the same apart from a version."""
    slug = base_slug(info.slug) or info.slug
    folder = {"theme": "themes", "plugin": "plugins"}.get(info.kind)
    parent = None

    if folder:
        wp_root = find_wp_root(backend, profile, log)
        if wp_root:
            parent = posixpath.join(wp_root, "wp-content", folder)
            how = f"it's a {info.kind}, so it belongs in wp-content/{folder}"

    if parent is None:
        # Kind unknown (no theme/plugin header). Don't just trust the saved
        # path — a mis-set path could drop theme files into the site root.
        # Look for a folder of this name where themes and plugins live first.
        wp_root = find_wp_root(backend, profile, log)
        if wp_root:
            for sub in ("themes", "plugins"):
                candidate_parent = posixpath.join(wp_root, "wp-content", sub)
                if not backend.dir_exists(candidate_parent):
                    continue
                found, why = match_existing_folder(backend.list_dir(candidate_parent), slug)
                if found:
                    log(f"Matched “{info.slug}” to {sub}/{found} by name ({why})")
                    return {"target": posixpath.join(candidate_parent, found),
                            "exists": True, "parent": candidate_parent,
                            "how": f"found a folder of that name in wp-content/{sub}",
                            "matched": why}

        saved_raw = (profile.remote_path or "").strip("/")
        if saved_raw:
            saved = "/" + saved_raw
            if backend.dir_exists(saved):
                return {"target": saved, "exists": True, "parent": posixpath.dirname(saved),
                        "how": "using the folder saved in this website's details",
                        "matched": "",
                        "risky": backend.dir_exists(posixpath.join(saved, "wp-content"))}
            parent = posixpath.dirname(saved) or "/"
            how = "looking next to the folder saved in this website's details"
        elif wp_root:
            # Nothing was ever saved, and nothing matched by name — falling
            # back to the site root would be genuinely dangerous (it would
            # scatter files across the whole site), so this defaults to a
            # new plugin folder instead: the safer of the two kinds, and
            # easy to move to wp-content/themes afterward if that's what it
            # actually was.
            parent = posixpath.join(wp_root, "wp-content", "plugins")
            how = ("couldn't tell if this is a theme or a plugin, so this creates "
                  "a new folder under wp-content/plugins — move it into "
                  "wp-content/themes afterward if it's actually a theme")
        else:
            parent = "/"
            how = "couldn't find a WordPress install, and nothing was saved to fall back to"

    if not backend.dir_exists(parent):
        saved = "/" + (profile.remote_path or "").strip("/")
        return {"target": saved, "exists": backend.dir_exists(saved), "parent": parent,
                "how": "using the folder saved in this website's details", "matched": ""}

    # Updating the same theme/plugin again is the common case — if it matched
    # a folder last time, check that one folder still exists instead of
    # re-listing the whole parent and re-running the fuzzy match from scratch.
    cached_name = cache_get_matched_folder(profile.name, slug)
    if cached_name and backend.dir_exists(posixpath.join(parent, cached_name)):
        return {"target": posixpath.join(parent, cached_name), "exists": True,
                "parent": parent, "how": how, "matched": "same as last time"}

    entries = [n for n in backend.list_dir(parent) if "." not in n or backend.dir_exists(
        posixpath.join(parent, n))]
    existing, why = match_existing_folder(entries, slug)
    if existing:
        log(f"Matched “{info.slug}” to the existing folder “{existing}” ({why})")
        cache_set_matched_folder(profile.name, slug, existing)
        return {"target": posixpath.join(parent, existing), "exists": True,
                "parent": parent, "how": how, "matched": why}

    log(f"No existing folder like “{slug}” in {parent} — this will be a new one")
    return {"target": posixpath.join(parent, slug), "exists": False,
            "parent": parent, "how": how, "matched": ""}


def file_exists_remote(backend, path: str) -> bool:
    """A path that exists but isn't a directory is a file."""
    parent, name = posixpath.split(path)
    if not backend.dir_exists(parent):
        return False
    try:
        for entry, is_dir in backend.list_entries(parent):
            if entry == name:
                return not is_dir
    except Exception:
        pass
    return name in backend.list_dir(parent) and not backend.dir_exists(path)


def decide_file_target(backend, profile: Profile, info: ZipInfo, log) -> dict:
    """Where does one loose file belong? Look for the same file already on the
    site — that's the only trustworthy answer — then fall back to sensible
    places for its type."""
    filename = info.filename
    slug = info.slug
    wp_root = find_wp_root(backend, profile, log)
    saved = "/" + (profile.remote_path or "").strip("/")

    def hit(folder, how, matched=""):
        target = posixpath.join(folder, filename)
        return {"target": target, "target_dir": folder, "exists": True,
                "is_file": True, "how": how, "matched": matched}

    # 1. A plugin file names its own folder.
    if info.kind == "plugin" and wp_root:
        plugins = posixpath.join(wp_root, "wp-content", "plugins")
        if backend.dir_exists(plugins):
            found, why = match_existing_folder(backend.list_dir(plugins), slug)
            if found and file_exists_remote(backend, posixpath.join(plugins, found, filename)):
                log(f"Found {filename} in plugins/{found}")
                return hit(posixpath.join(plugins, found),
                           f"it's a plugin file and it's already in wp-content/plugins/{found}",
                           why)
            if file_exists_remote(backend, posixpath.join(plugins, filename)):
                log(f"Found {filename} directly in plugins/")
                return hit(plugins, "it's a single-file plugin in wp-content/plugins")
            if found:
                return {"target": posixpath.join(plugins, found, filename),
                        "target_dir": posixpath.join(plugins, found), "exists": False,
                        "is_file": True,
                        "how": f"it's a plugin file, so it goes in wp-content/plugins/{found}",
                        "matched": why}

    # 2. The same filename already somewhere obvious.
    places = []
    if saved.strip("/"):
        places.append((saved, "the folder saved in this website's details"))
    if wp_root:
        wp_content = posixpath.join(wp_root, "wp-content")
        for sub in ("themes", "plugins"):
            parent = posixpath.join(wp_content, sub)
            if not backend.dir_exists(parent):
                continue
            names = [n for n in backend.list_dir(parent)
                     if backend.dir_exists(posixpath.join(parent, n))]
            # a folder matching the file's own name first, then the rest
            ordered = sorted(names, key=lambda n: 0 if _match_key(n) == _match_key(slug) else 1)
            for name in ordered[:15]:
                places.append((posixpath.join(parent, name),
                               f"the file is already in wp-content/{sub}/{name}"))
        places.append((wp_content, "the file is already in wp-content"))
        places.append((wp_root, "the file is already in your website's main folder"))

    for folder, how in places:
        if file_exists_remote(backend, posixpath.join(folder, filename)):
            log(f"Found the existing {filename} in {folder}")
            return hit(folder, how)

    # 3. Nowhere obvious — make a sensible suggestion and let the user confirm.
    if info.kind == "plugin" and wp_root:
        guess = posixpath.join(wp_root, "wp-content", "plugins", slug)
        how = "it's a plugin file, so this would be a new plugin folder"
    else:
        guess = saved if saved.strip("/") else (wp_root or "/")
        how = "using the folder saved in this website's details"
    log(f"{filename} isn't on the site yet — suggesting {guess}")
    return {"target": posixpath.join(guess, filename), "target_dir": guess,
            "exists": False, "is_file": True, "how": how, "matched": "",
            "needs_location": True}


def walk_remote(backend, root: str, max_files: int = 4000, max_depth: int = 12):
    """Every file under a remote folder, as (remote_path, relative_path)."""
    stack = [(root, "", 0)]
    seen = 0
    while stack:
        path, rel, depth = stack.pop()
        if depth > max_depth:
            continue
        for name, is_dir in backend.list_entries(path):
            full = posixpath.join(path, name)
            rel_child = f"{rel}/{name}" if rel else name
            if is_dir:
                stack.append((full, rel_child, depth + 1))
            else:
                seen += 1
                if seen > max_files:
                    return
                yield full, rel_child


def write_backup_note(out_zip: Path, target: str, kind: str, site: str):
    """A sidecar saying exactly what this backup came from, so putting it
    back never has to guess."""
    try:
        out_zip.with_suffix(".json").write_text(json.dumps({
            "target": target, "kind": kind, "site": site,
            "saved": datetime.now().isoformat(timespec="seconds"),
        }, indent=2), encoding="utf-8")
    except Exception:
        pass


def read_backup_note(backup_zip: str) -> dict:
    note = Path(backup_zip).with_suffix(".json")
    if note.exists():
        try:
            return json.loads(note.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def archive_remote_file(backend, remote_path: str, out_zip: Path, log) -> int:
    """Keep a copy of one file before it's replaced."""
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    name = posixpath.basename(remote_path)
    with tempfile.TemporaryDirectory(prefix="sitedeploy_bak_") as tmp:
        local = Path(tmp) / name
        try:
            backend.download_file(remote_path, str(local))
        except Exception as e:
            log(f"Could not back up {remote_path}: {e}")
            return 0
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(local, arcname=name)
    return 1


def archive_remote_folder(backend, remote_dir: str, out_zip: Path, log,
                          progress=None) -> int:
    """Copy what's on the server right now into a local zip, before it gets
    overwritten. Local on purpose: a backup sitting in wp-content could be
    picked up by WordPress; one on your PC can't touch the site at all, and
    it's a zip, so putting it back is just another update."""
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with tempfile.TemporaryDirectory(prefix="sitedeploy_bak_") as tmp:
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            base = posixpath.basename(remote_dir.rstrip("/")) or "site"
            for remote_path, rel in walk_remote(backend, remote_dir):
                local = Path(tmp) / "f.bin"
                try:
                    backend.download_file(remote_path, str(local))
                except Exception as e:
                    log(f"Could not back up {remote_path}: {e}")
                    continue
                zf.write(local, arcname=f"{base}/{rel}")
                count += 1
                if progress and count % 5 == 0:
                    progress(count)
    if count == 0:
        out_zip.unlink(missing_ok=True)
    return count


def delete_remote_recursive(backend, root: str, log,
                            max_files: int = 4000, max_depth: int = 12):
    """Delete every file, then every folder deepest-first. FTP/SFTP have no
    'delete folder and everything in it' — this is the walk that does it."""
    dirs = [root]
    files = []
    stack = [(root, 0)]
    seen = set()
    while stack:
        path, depth = stack.pop()
        if depth > max_depth or path in seen:
            continue
        seen.add(path)
        for name, is_dir in backend.list_entries(path):
            full = posixpath.join(path, name)
            if is_dir:
                dirs.append(full)
                stack.append((full, depth + 1))
            else:
                files.append(full)
                if len(files) > max_files:
                    raise DeployError(
                        f"That folder has more than {max_files} files — too many "
                        f"to delete safely in one go.")

    for f in files:
        try:
            backend.remove_file(f)
        except Exception as e:
            log(f"Could not delete {f}: {e}")
    for d in sorted(dirs, key=lambda p: -p.count("/")):
        try:
            backend.remove_dir(d)
        except Exception as e:
            log(f"Could not remove folder {d}: {e}")


@dataclass
class PluginInfo:
    """One installed plugin, as found by looking at the server."""
    slug: str = ""
    path: str = ""
    name: str = ""
    version: str = ""

    @property
    def label(self) -> str:
        v = f" v{self.version}" if self.version else ""
        return f"{self.name or self.slug}{v}"


def parse_wp_header(text: str) -> dict:
    def grab(pattern):
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip() if m else ""
    return {"name": grab(r"Plugin Name:\s*(.+)"),
            "version": grab(r"Version:\s*(.+)")}


def find_plugin_main_file(backend, plugin_dir: str, slug: str) -> str | None:
    """The .php file that carries the 'Plugin Name:' header — conventionally
    <slug>.php, but not always, so fall back to the first top-level .php."""
    try:
        entries = backend.list_entries(plugin_dir)
    except Exception:
        return None
    php_files = [n for n, is_dir in entries if not is_dir and n.lower().endswith(".php")]
    if f"{slug}.php" in php_files:
        return posixpath.join(plugin_dir, f"{slug}.php")
    if php_files:
        return posixpath.join(plugin_dir, sorted(php_files)[0])
    return None


def list_installed_plugins(backend, wp_root: str, log) -> list[PluginInfo]:
    """Everything in wp-content/plugins, with a name and version read from
    each plugin's own header — this is a filesystem view, so it can't say
    whether any of these are switched on."""
    plugins_dir = posixpath.join(wp_root, "wp-content", "plugins")
    out = []
    if not backend.dir_exists(plugins_dir):
        return out
    for name, is_dir in backend.list_entries(plugins_dir):
        if not is_dir or name.startswith("."):
            continue
        full = posixpath.join(plugins_dir, name)
        info = PluginInfo(slug=name, path=full, name=name)
        main = find_plugin_main_file(backend, full, name)
        if main:
            with tempfile.TemporaryDirectory(prefix="sitedeploy_hdr_") as tmp:
                local = Path(tmp) / "hdr.php"
                try:
                    backend.download_file(main, str(local))
                    header = parse_wp_header(
                        local.read_text(encoding="utf-8", errors="ignore"))
                    info.name = header.get("name") or name
                    info.version = header.get("version") or ""
                except Exception as e:
                    log(f"Could not read {main}: {e}")
        out.append(info)
    out.sort(key=lambda p: p.name.lower())
    return out


def extract_upload_root(zip_path: str, work_dir: str) -> str:
    """Extract the zip and return the folder whose *contents* should be
    uploaded. If the zip is wrapped in a single top-level folder (the
    normal case for a WP theme zip), unwrap it so the remote_path itself
    doesn't get an extra nested folder."""
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(work_dir)
    entries = [e for e in os.listdir(work_dir) if not e.startswith("__MACOSX")]
    if len(entries) == 1 and os.path.isdir(os.path.join(work_dir, entries[0])):
        return os.path.join(work_dir, entries[0])
    return work_dir


def iter_local_files(root: str):
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            yield full, rel


class FTPBackend:
    """Wraps ftplib for both plain FTP and explicit FTPS, with a uniform
    interface matching SFTPBackend below."""

    def __init__(self, host, port, username, password, use_tls, log):
        self.log = log
        cls = ftplib.FTP_TLS if use_tls else ftplib.FTP
        self.ftp = cls(timeout=20)
        self.ftp.connect(host, port or 21)
        if use_tls:
            self.ftp.auth()
            self.ftp.prot_p()
        self.ftp.login(username, password)
        self.used_tls = use_tls

    def ensure_dir(self, remote_dir):
        parts = [p for p in remote_dir.split("/") if p]
        cur = ""
        for part in parts:
            cur += "/" + part
            try:
                self.ftp.mkd(cur)
            except ftplib.error_perm:
                pass  # already exists

    def upload_file(self, local_path, remote_path):
        remote_dir = posixpath.dirname(remote_path)
        if remote_dir:
            self.ensure_dir(remote_dir)
        with open(local_path, "rb") as fh:
            self.ftp.storbinary(f"STOR {remote_path}", fh)

    def list_dir(self, remote_dir):
        try:
            return [posixpath.basename(x.rstrip("/")) or x
                    for x in self.ftp.nlst(remote_dir)]
        except ftplib.all_errors:
            return []

    def dir_exists(self, remote_dir):
        try:
            here = self.ftp.pwd()
            self.ftp.cwd(remote_dir)
            self.ftp.cwd(here)
            return True
        except ftplib.all_errors:
            return False

    def list_entries(self, remote_dir):
        """[(name, is_dir)] — MLSD when the server supports it, else probe."""
        try:
            out = []
            for name, facts in self.ftp.mlsd(remote_dir):
                if name in (".", ".."):
                    continue
                kind = facts.get("type", "")
                if kind == "dir":
                    out.append((name, True))
                elif kind == "file":
                    out.append((name, False))
            if out:
                return out
        except Exception:
            pass
        return [(name, self.dir_exists(posixpath.join(remote_dir, name)))
                for name in self.list_dir(remote_dir) if name not in (".", "..")]

    def download_file(self, remote_path, local_path):
        with open(local_path, "wb") as fh:
            self.ftp.retrbinary(f"RETR {remote_path}", fh.write)

    def remove_file(self, remote_path):
        self.ftp.delete(remote_path)

    def remove_dir(self, remote_path):
        self.ftp.rmd(remote_path)

    def close(self):
        try:
            self.ftp.quit()
        except Exception:
            try:
                self.ftp.close()
            except Exception:
                pass


class SFTPBackend:
    def __init__(self, host, port, username, password, log):
        import paramiko
        self.log = log
        self.transport = paramiko.Transport((host, port or 22))
        self.transport.connect(username=username, password=password)
        self.sftp = paramiko.SFTPClient.from_transport(self.transport)
        self.used_tls = True  # SFTP is always encrypted

    def ensure_dir(self, remote_dir):
        parts = [p for p in remote_dir.split("/") if p]
        cur = ""
        for part in parts:
            cur += "/" + part
            try:
                self.sftp.stat(cur)
            except IOError:
                self.sftp.mkdir(cur)

    def upload_file(self, local_path, remote_path):
        remote_dir = posixpath.dirname(remote_path)
        if remote_dir:
            self.ensure_dir(remote_dir)
        self.sftp.put(local_path, remote_path)

    def list_dir(self, remote_dir):
        try:
            return self.sftp.listdir(remote_dir)
        except IOError:
            return []

    def dir_exists(self, remote_dir):
        try:
            self.sftp.stat(remote_dir)
            return True
        except IOError:
            return False

    def list_entries(self, remote_dir):
        import stat as stat_mod
        try:
            return [(a.filename, stat_mod.S_ISDIR(a.st_mode))
                    for a in self.sftp.listdir_attr(remote_dir)
                    if a.filename not in (".", "..")]
        except IOError:
            return []

    def download_file(self, remote_path, local_path):
        self.sftp.get(remote_path, local_path)

    def remove_file(self, remote_path):
        self.sftp.remove(remote_path)

    def remove_dir(self, remote_path):
        self.sftp.rmdir(remote_path)

    def close(self):
        try:
            self.sftp.close()
            self.transport.close()
        except Exception:
            pass


def connect_backend(profile: Profile, password: str, log) -> tuple:
    """Returns (backend, protocol_used_description)."""
    proto = profile.protocol

    if proto == PROTOCOL_SFTP:
        backend = SFTPBackend(profile.host, profile.port or 22, profile.username, password, log)
        return backend, "securely (SSH/SFTP)"

    if proto == PROTOCOL_FTPS:
        backend = FTPBackend(profile.host, profile.port or 21, profile.username, password, True, log)
        return backend, "securely (FTPS)"

    if proto == PROTOCOL_FTP:
        backend = FTPBackend(profile.host, profile.port or 21, profile.username, password, False, log)
        return backend, "NOT securely (plain FTP)"

    # Automatic: try the secure one first, fall back to plain, and say so
    try:
        backend = FTPBackend(profile.host, profile.port or 21, profile.username, password, True, log)
        return backend, "securely (FTPS)"
    except Exception as e:
        log(f"A secure connection didn't work ({e}). Trying plain FTP instead...")
        backend = FTPBackend(profile.host, profile.port or 21, profile.username, password, False, log)
        return backend, "NOT securely — this server refused a secure connection"


def probe_remote_path(backend, target: str) -> dict:
    """Work out where a missing remote folder went wrong: walk up the path
    until something exists, and list what's in it. That answers "so what
    should I have typed?" without making the user guess."""
    target = "/" + (target or "").strip("/")
    parts = [p for p in target.split("/") if p]
    chain = ["/" + "/".join(parts[:i]) for i in range(len(parts), 0, -1)] + ["/"]

    for candidate in chain:
        if backend.dir_exists(candidate):
            entries = sorted(e for e in backend.list_dir(candidate)
                             if e not in (".", ".."))
            missing = target[len(candidate):].strip("/")
            return {"deepest": candidate, "entries": entries, "missing": missing}
    return {"deepest": None, "entries": [], "missing": target.strip("/")}


class PluginScanWorker(QThread):
    """Connects once, finds WordPress, and lists what's in wp-content/plugins.
    Runs off the UI thread because reading every plugin's header means one
    download per plugin."""

    logged = Signal(str)
    finished_ok = Signal(list)     # list[PluginInfo]
    finished_err = Signal(str)

    def __init__(self, profile: Profile, password: str):
        super().__init__()
        self.profile = profile
        self.password = password

    def log(self, msg):
        self.logged.emit(msg)

    def run(self):
        backend = None
        try:
            backend, _desc = connect_backend(self.profile, self.password, self.log)
            wp_root = find_wp_root(backend, self.profile, self.log)
            if not wp_root:
                self.finished_err.emit(
                    "Couldn't find a WordPress install on this website (no "
                    "wp-content folder). Plugins can't be listed without it.")
                return
            plugins = list_installed_plugins(backend, wp_root, self.log)
            self.finished_ok.emit(plugins)
        except Exception as e:
            self.finished_err.emit(friendly_error(e, self.profile))
        finally:
            if backend is not None:
                backend.close()


class PluginDeleteWorker(QThread):
    """Backs up a plugin's files, then deletes them from the server. Never
    touches the database — WordPress's active/inactive state is untouched,
    which is exactly why deleting an ACTIVE plugin's files can break a site;
    the UI is responsible for warning about that before this ever runs."""

    logged = Signal(str)
    finished_ok = Signal(str, str)   # message, backup zip path (may be empty)
    finished_err = Signal(str)

    def __init__(self, profile: Profile, password: str, plugin: PluginInfo,
                 archive_dir: Path):
        super().__init__()
        self.profile = profile
        self.password = password
        self.plugin = plugin
        self.archive_dir = archive_dir

    def log(self, msg):
        self.logged.emit(msg)

    def run(self):
        backend = None
        try:
            backend, _desc = connect_backend(self.profile, self.password, self.log)
            if not backend.dir_exists(self.plugin.path):
                self.finished_err.emit(
                    f"{self.plugin.path} is already gone from the server.")
                return

            stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            out_zip = self.archive_dir / f"{self.plugin.slug}_before_delete_{stamp}.zip"
            self.log(f"Backing up {self.plugin.path} to {out_zip}")
            saved = archive_remote_folder(backend, self.plugin.path, out_zip, self.log)
            backup_path = ""
            if saved:
                write_backup_note(out_zip, self.plugin.path, "folder", self.profile.name)
                backup_path = str(out_zip)
                self.log(f"Saved {saved} file(s) before deleting.")
            else:
                self.log("Nothing was backed up (the folder appears to be empty).")

            self.log(f"Deleting {self.plugin.path} ...")
            delete_remote_recursive(backend, self.plugin.path, self.log)
            self.finished_ok.emit(
                f"Deleted {self.plugin.label} from your site.", backup_path)
        except Exception as e:
            self.finished_err.emit(friendly_error(e, self.profile))
        finally:
            if backend is not None:
                backend.close()


class DeployWorker(QThread):
    progress = Signal(int, int)     # done, total
    logged = Signal(str)
    event = Signal(str, str)        # kind, human-readable step
    plan_ready = Signal(dict)       # what it found; UI may ask before it proceeds
    finished_ok = Signal(str)       # summary message
    finished_err = Signal(str)      # error message

    def __init__(self, profile: Profile, password: str, zip_path: str,
                 test_only: bool = False, automatic: bool = False,
                 archive_dir: Path | None = None, needs_approval: bool = False,
                 forced_target: str = "", forced_kind: str = ""):
        super().__init__()
        self.profile = profile
        self.password = password
        self.zip_path = zip_path
        self.test_only = test_only
        self.automatic = automatic
        self.archive_dir = archive_dir
        self.forced_target = forced_target
        self.forced_kind = forced_kind
        self.probe: dict | None = None    # set by a test when the folder is missing
        self.plan: dict | None = None
        self.archived_to: str | None = None

        # The UI gets a say before a live site is touched.
        self._needs_approval = needs_approval
        self._approved = threading.Event()
        self._cancelled = False
        self._retarget = ""

    def approve(self, ok: bool, retarget: str = ""):
        self._cancelled = not ok
        self._retarget = retarget
        self._approved.set()

    def log(self, msg):
        self.logged.emit(msg)

    def run(self):
        backend = None
        temp_dir = None
        try:
            self.log(f"Connecting to {self.profile.host} ...")
            self.event.emit("plug", f"Connecting to {self.profile.host}")
            backend, proto_desc = connect_backend(self.profile, self.password, self.log)
            self.log(f"Connected {proto_desc}.")
            self.event.emit("check", f"Connected {proto_desc}")
            if "NOT" in proto_desc:
                self.event.emit("error",
                                "This connection isn't encrypted. In Change details, set "
                                "Connection type to “Automatic” or the SSH option.")

            if self.test_only:
                target = self.profile.remote_path or "/"
                if backend.dir_exists(target):
                    listing = backend.list_dir(target)
                    self.log(f"Found {len(listing)} item(s) in {target}")
                    self.event.emit("folder", f"Found {len(listing)} item(s) in {target}")
                    self.finished_ok.emit("Connection test succeeded.")
                else:
                    self.log(f"{target} does not exist on the server. Looking for what does...")
                    self.probe = probe_remote_path(backend, target)
                    self.log(f"Deepest folder that exists: {self.probe['deepest']}")
                    self.log("It contains: " + (", ".join(self.probe["entries"]) or "(nothing)"))
                    self.event.emit("folder",
                                    f"Signed in fine, but {target} isn't there yet")
                    self.finished_ok.emit("Connected — but that folder wasn't found.")
                return

            # 1. What did the user pick?
            info = inspect_source(self.zip_path)
            self.log(f"Chosen: {info.label}, {info.file_count} file(s).")
            self.event.emit("info", f"Read it: {info.label}")

            # 2. Where does it go?
            if self.forced_target:
                plan = {"target": self.forced_target,
                        "target_dir": posixpath.dirname(self.forced_target)
                        if self.forced_kind == "file" else self.forced_target,
                        "is_file": self.forced_kind == "file",
                        "how": "putting a saved copy back where it came from",
                        "matched": ""}
                plan["exists"] = (file_exists_remote(backend, self.forced_target)
                                  if plan["is_file"]
                                  else backend.dir_exists(self.forced_target))
            else:
                self.event.emit("folder", "Looking for where it belongs on your site...")
                if info.is_zip:
                    plan = decide_target(backend, self.profile, info, self.log)
                else:
                    plan = decide_file_target(backend, self.profile, info, self.log)
            plan["info"] = info
            self.plan = plan
            target = plan["target"]
            if plan["exists"]:
                what = ("Found it: " + target if not plan.get("matched")
                        else f"Matched to {target} — {plan['matched']}")
            else:
                what = f"New — it will create {target}"
            self.log(what)
            self.event.emit("folder", what)

            self.plan_ready.emit(plan)
            if self._needs_approval:
                self._approved.wait()
                if self._cancelled:
                    self.finished_ok.emit("Cancelled — nothing on your site was changed.")
                    return
                if self._retarget:           # user picked a different destination
                    target = plan["target"] = self._retarget
                    plan["target_dir"] = (posixpath.dirname(target)
                                          if plan.get("is_file") else target)
                    plan["exists"] = (file_exists_remote(backend, target)
                                      if plan.get("is_file")
                                      else backend.dir_exists(target))
                    self.log(f"Destination changed to {target}")

            single_file = bool(plan.get("is_file"))

            # 3. Keep a copy of what's there now.
            if plan["exists"] and self.archive_dir is not None:
                stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
                base = posixpath.basename(target.rstrip("/")) or "site"
                out_zip = Path(self.archive_dir) / f"{base}_before_{stamp}.zip"
                self.event.emit("upload", "Saving a copy of what's there now...")
                self.log(f"Backing up {target} to {out_zip}")
                if single_file:
                    saved = archive_remote_file(backend, target, out_zip, self.log)
                else:
                    saved = archive_remote_folder(
                        backend, target, out_zip, self.log,
                        lambda n: self.progress.emit(n, 0))
                if saved:
                    self.archived_to = str(out_zip)
                    write_backup_note(out_zip, target,
                                      "file" if single_file else "folder",
                                      self.profile.name)
                    self.event.emit("check", "Saved the old version — you can put it "
                                             "back any time")
                else:
                    self.log("Nothing was backed up.")

            # 4. Upload.
            if single_file:
                self.event.emit("upload", f"Uploading {info.filename} to {target}")
                self.log(f"Uploading {self.zip_path} to {target} ...")
                if info.is_zip:
                    # a single-file backup being put back: unpack the one entry
                    temp_dir = tempfile.mkdtemp(prefix="sitedeploy_")
                    with zipfile.ZipFile(self.zip_path) as zf:
                        member = [n for n in zf.namelist() if not n.endswith("/")][0]
                        source = zf.extract(member, temp_dir)
                else:
                    source = self.zip_path
                backend.upload_file(source, target)
                self.progress.emit(1, 1)
                done = "Replaced" if plan["exists"] else "Added"
                self.finished_ok.emit(f"{done} {posixpath.basename(target)} on your site.")
                return

            temp_dir = tempfile.mkdtemp(prefix="sitedeploy_")
            self.log("Extracting zip file...")
            upload_root = extract_upload_root(self.zip_path, temp_dir)

            files = list(iter_local_files(upload_root))
            total = len(files)
            if total == 0:
                raise DeployError("The zip file has no files to upload.")

            verb = "Updating" if plan["exists"] else "Creating"
            self.log(f"Uploading {total} file(s) to {target} ...")
            self.event.emit("upload", f"{verb} {target} — {total} files")
            for i, (local_path, rel_path) in enumerate(files, start=1):
                remote_path = posixpath.join(target.rstrip("/"), rel_path)
                backend.upload_file(local_path, remote_path)
                self.progress.emit(i, total)

            done = "Updated" if plan["exists"] else "Created"
            self.finished_ok.emit(f"{done} {total} file(s) on your site.")

        except Exception as e:
            self.log(traceback.format_exc())
            self.finished_err.emit(friendly_error(e, self.profile))
        finally:
            if backend is not None:
                backend.close()
            if temp_dir and os.path.isdir(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────
# Inbox watcher
# ─────────────────────────────────────────────────────────────────────────

class InboxWatcher(QThread):
    """Watches every website's inbox folder for new .zip files.

    Uses the watchdog package for instant reaction when it's available, with
    a periodic sweep as a backstop (and as the only mechanism if watchdog
    isn't installed). A zip is only reported once its size has stopped
    changing for STABLE_SECONDS, so a half-copied file is never deployed.
    """

    zip_ready = Signal(str, str)     # profile name, zip path
    logged = Signal(str)

    def __init__(self):
        super().__init__()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._folders: dict[str, str] = {}   # profile name -> inbox folder
        self._seen: dict[str, tuple] = {}    # zip path -> (size, mtime) already handled
        self._pending: dict[str, tuple] = {} # zip path -> (size, first_seen_stable_at)
        self._observer = None
        self._watch_root = None

    # -- configuration from the UI thread --------------------------------

    def set_folders(self, folders: dict[str, str], root: str):
        with self._lock:
            self._folders = dict(folders)
        if root != self._watch_root:
            self._watch_root = root
            self._restart_observer()
        self._wake.set()

    def forget(self, zip_path: str):
        """Allow a path to be picked up again (e.g. it was modified)."""
        with self._lock:
            self._seen.pop(zip_path, None)
            self._pending.pop(zip_path, None)

    def mark_handled(self, zip_path: str):
        """Remember a path so a failed deploy isn't retried in a loop."""
        with self._lock:
            self._seen[zip_path] = self._stat(zip_path)

    def stop(self):
        self._stop.set()
        self._wake.set()
        self._stop_observer()

    # -- watchdog plumbing ------------------------------------------------

    def _restart_observer(self):
        self._stop_observer()
        root = self._watch_root
        if not root or not os.path.isdir(root):
            return
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            watcher = self

            class _Handler(FileSystemEventHandler):
                def on_any_event(self, event):
                    if event.is_directory:
                        return
                    watcher._wake.set()

            self._observer = Observer()
            self._observer.schedule(_Handler(), root, recursive=True)
            self._observer.start()
        except Exception:
            self._observer = None   # polling backstop still covers us

    def _stop_observer(self):
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2)
            except Exception:
                pass
            self._observer = None

    # -- scanning ---------------------------------------------------------

    @staticmethod
    def _stat(path: str) -> tuple:
        try:
            st = os.stat(path)
            return (st.st_size, st.st_mtime)
        except OSError:
            return (-1, -1)

    def run(self):
        while not self._stop.is_set():
            self._wake.wait(POLL_SECONDS)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self._scan_once()
            except Exception:
                pass

    def _scan_once(self):
        with self._lock:
            folders = dict(self._folders)

        now = time.time()
        for profile_name, folder in folders.items():
            try:
                entries = os.listdir(folder)
            except OSError:
                continue
            for entry in entries:
                # Same types the picker accepts, minus obvious editor droppings.
                if os.path.splitext(entry)[1].lower() not in SUPPORTED_EXTS:
                    continue
                if entry.startswith((".", "~", "$")) or entry.endswith(("~", ".tmp")):
                    continue
                path = os.path.join(folder, entry)
                if not os.path.isfile(path):
                    continue

                info = self._stat(path)
                if info[0] < 0:
                    continue

                with self._lock:
                    seen = self._seen.get(path)
                    if seen == info:
                        continue                # already handled, unchanged
                    prev = self._pending.get(path)
                    if prev is None or prev[0] != info[0]:
                        self._pending[path] = (info[0], now)   # size still moving
                        continue
                    if now - prev[1] < STABLE_SECONDS:
                        continue                # not stable long enough yet
                    self._pending.pop(path, None)
                    self._seen[path] = info

                self.zip_ready.emit(profile_name, path)


# ─────────────────────────────────────────────────────────────────────────
# Small UI building blocks (Fluent-flavoured: cards, tiles, timeline)
# ─────────────────────────────────────────────────────────────────────────

def h_label(text: str, kind: str = "") -> QLabel:
    lab = QLabel(text)
    if kind:
        lab.setObjectName(kind)
    return lab


class Card(QFrame):
    """A panel: whitespace and a soft edge instead of a group-box border."""

    def __init__(self, parent=None, margins=(20, 18, 20, 18), spacing=12):
        super().__init__(parent)
        self.setObjectName("Card")
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(*margins)
        self.v.setSpacing(spacing)

    def add(self, widget):
        self.v.addWidget(widget)
        return widget

    def add_layout(self, layout):
        self.v.addLayout(layout)
        return layout


class StatusPill(QLabel):
    """The single always-visible status indicator."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.set_state("idle", "Starting")

    def set_state(self, kind: str, text: str):
        color = {
            "ok": PALETTE.ok, "busy": PALETTE.warn,
            "error": PALETTE.err, "idle": PALETTE.text_dim,
        }.get(kind, PALETTE.text_dim)
        self.setText(f"●  {text}")
        self.setStyleSheet(
            f"color: {color}; font-weight: 600; padding: 5px 12px;"
            f"border: 1px solid {PALETTE.border}; border-radius: 13px;"
            f"background: {PALETTE.card_alt};"
        )


class Timeline(QListWidget):
    """Activity as scannable steps with icons, not a wall of log text."""

    MAX_ROWS = 200

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Timeline")
        self.setSelectionMode(QListWidget.NoSelection)
        self.setFocusPolicy(Qt.NoFocus)
        self.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.setMinimumHeight(140)

    def add_event(self, kind: str, text: str, when: str = None):
        icon_name, color = {
            "check":  ("check", PALETTE.ok),
            "error":  ("warning", PALETTE.err),
            "upload": ("upload", PALETTE.accent),
            "plug":   ("plug", PALETTE.text_dim),
            "folder": ("folder", PALETTE.text_dim),
            "site":   ("globe", PALETTE.accent),
            "history": ("history", PALETTE.accent),
            "info":   ("dot", PALETTE.text_dim),
        }.get(kind, ("dot", PALETTE.text_dim))

        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(6, 7, 6, 7)
        lay.setSpacing(10)

        icon_label = QLabel()
        icon_label.setPixmap(line_icon(icon_name, color).pixmap(QSize(16, 16)))
        icon_label.setFixedWidth(18)
        icon_label.setAlignment(Qt.AlignTop | Qt.AlignHCenter)

        msg = QLabel(text)
        msg.setWordWrap(True)
        if kind == "error":
            msg.setStyleSheet(f"color: {PALETTE.err};")

        stamp = QLabel(when or datetime.now().strftime("%H:%M"))
        stamp.setObjectName("Dim")
        stamp.setAlignment(Qt.AlignTop | Qt.AlignRight)

        lay.addWidget(icon_label)
        lay.addWidget(msg, 1)
        lay.addWidget(stamp)

        item = QListWidgetItem()
        item.setSizeHint(row.sizeHint())
        self.addItem(item)
        self.setItemWidget(item, row)
        while self.count() > self.MAX_ROWS:
            self.takeItem(0)
        self.scrollToBottom()


class Collapsible(QWidget):
    """'Advanced' disclosure — keeps secondary things out of the way."""

    toggled_open = Signal(bool)

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._open = False
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        self.toggle = QPushButton(f"  {title}")
        self.toggle.setObjectName("Quiet")
        self.toggle.setIcon(line_icon("chevron-right", PALETTE.text_dim))
        self.toggle.setIconSize(QSize(14, 14))
        self.toggle.setCursor(Qt.PointingHandCursor)
        self.toggle.clicked.connect(self.toggle_open)

        self.content = QWidget()
        self.content.setVisible(False)
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(12)

        v.addWidget(self.toggle, 0, Qt.AlignLeft)
        v.addWidget(self.content)

    def toggle_open(self):
        self._open = not self._open
        self.content.setVisible(self._open)
        self.toggle.setIcon(line_icon(
            "chevron-down" if self._open else "chevron-right", PALETTE.text_dim))
        self.toggled_open.emit(self._open)

    def add(self, widget):
        self.body.addWidget(widget)
        return widget


class StepRow(QWidget):
    """One numbered instruction. Shows a tick when it's already done and a
    button for the step you're actually on."""

    def __init__(self, number: int, title: str, explain: str,
                 action_text: str = None, on_action=None, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(52)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 8)
        lay.setSpacing(12)

        self.number = number
        self.badge = QLabel(str(number))
        self.badge.setFixedSize(26, 26)
        self.badge.setAlignment(Qt.AlignCenter)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        self.title = QLabel(title)
        self.explain = h_label(explain, "Dim")
        self.explain.setWordWrap(True)
        text_col.addWidget(self.title)
        text_col.addWidget(self.explain)

        self.button = QPushButton(action_text or "")
        self.button.setVisible(bool(action_text))
        if on_action:
            self.button.clicked.connect(on_action)

        lay.addWidget(self.badge, 0, Qt.AlignTop)
        lay.addLayout(text_col, 1)
        lay.addWidget(self.button, 0, Qt.AlignVCenter)
        self.set_state("todo")

    def set_state(self, state: str):
        """done | current | todo"""
        if state == "done":
            self.badge.setText("✓")
            self.badge.setStyleSheet(
                f"background: {PALETTE.ok}; color: white; border-radius: 13px; font-weight: 600;")
            self.title.setStyleSheet(f"color: {PALETTE.text_dim};")
            self.button.setObjectName("")
            self.button.setEnabled(True)
        elif state == "current":
            self.badge.setText(str(self.number))
            self.badge.setStyleSheet(
                f"background: {PALETTE.accent}; color: {PALETTE.on_accent};"
                f"border-radius: 13px; font-weight: 600;")
            self.title.setStyleSheet("font-weight: 600;")
            self.button.setObjectName("Primary")
        else:
            self.badge.setText(str(self.number))
            self.badge.setStyleSheet(
                f"background: {PALETTE.card_alt}; color: {PALETTE.text_dim};"
                f"border: 1px solid {PALETTE.border}; border-radius: 13px;")
            self.title.setStyleSheet(f"color: {PALETTE.text_dim};")
            self.button.setObjectName("")
        self.button.style().unpolish(self.button)
        self.button.style().polish(self.button)


def quiet_button(text: str, icon: str = None, on_click=None) -> QPushButton:
    btn = QPushButton(f"  {text}")
    btn.setObjectName("Quiet")
    if icon:
        btn.setIcon(line_icon(icon, PALETTE.text_dim))
        btn.setIconSize(QSize(16, 16))
    btn.setCursor(Qt.PointingHandCursor)
    if on_click:
        btn.clicked.connect(on_click)
    return btn


# ─────────────────────────────────────────────────────────────────────────
# UI — Add/Edit website dialog
# ─────────────────────────────────────────────────────────────────────────

class ProfileDialog(QDialog):
    def __init__(self, parent, profile: Profile = None):
        super().__init__(parent)
        self.setWindowTitle("Website details")
        self.setMinimumWidth(520)
        self.profile = profile or Profile()

        self.provider_combo = QComboBox()
        for key, label in HOSTING_PRESETS:
            self.provider_combo.addItem(label, key)
        idx = self.provider_combo.findData(self.profile.hosting_provider)
        self.provider_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.provider_hint = h_label("", "Dim")
        self.provider_hint.setWordWrap(True)
        self.provider_combo.currentIndexChanged.connect(self._on_provider_change)

        self.name_edit = QLineEdit(self.profile.name)
        self.name_edit.setPlaceholderText("e.g. wingchunclub.in")
        self.host_edit = QLineEdit(self.profile.host)
        self.host_edit.setPlaceholderText("ftp.yoursite.com  or  145.79.213.73")
        self.host_edit.setToolTip("Just the address itself — no ftp:// or https:// in front.")
        self.port_edit = QSpinBox()
        self.port_edit.setRange(1, 65535)
        self.port_edit.setValue(self.profile.port or 21)
        self.protocol_combo = QComboBox()
        self.protocol_combo.addItems(PROTOCOLS)
        if self.profile.protocol in PROTOCOLS:
            self.protocol_combo.setCurrentText(self.profile.protocol)
        self.protocol_combo.currentTextChanged.connect(self._on_protocol_change)

        self.username_edit = QLineEdit(self.profile.username)
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        if self.profile.password_enc:
            self.password_edit.setPlaceholderText("Saved — leave blank to keep")
        self.remote_path_edit = QLineEdit(self.profile.remote_path)
        self.remote_path_edit.setPlaceholderText(
            "Leave blank — Site Deploy finds it automatically from what you upload")
        self.notes_edit = QLineEdit(self.profile.notes)
        self.watch_check = QCheckBox("Deploy automatically when a zip is dropped in this site's folder")
        self.watch_check.setChecked(self.profile.watch)

        form = QFormLayout()
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignLeft)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.addRow("Which host is this on?", self.provider_combo)
        form.addRow("", self.provider_hint)
        form.addRow("Website name", self.name_edit)
        form.addRow("Connection type", self.protocol_combo)
        form.addRow("Server address", self.host_edit)
        form.addRow("", h_label("Just the address — no ftp:// or https:// in front.", "Dim"))
        form.addRow("Port", self.port_edit)
        form.addRow("Username", self.username_edit)
        form.addRow("Password", self.password_edit)
        form.addRow("Folder on server (optional)", self.remote_path_edit)
        form.addRow("", h_label("Not needed for most sites — only fill this in if you "
                                "already know exactly where updates should go.", "Dim"))
        form.addRow("Notes", self.notes_edit)

        card = Card()
        card.add_layout(form)
        card.add(self.watch_check)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setObjectName("Primary")
        buttons.button(QDialogButtonBox.Save).setText("Save")
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)

        hint = h_label("Your username and password are stored in Windows' own "
                       "Credential Manager, tied to this Windows account — not in "
                       "Site Deploy's own files at all. It can't be read on another "
                       "PC or by another user.", "Dim")
        hint.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)
        layout.addWidget(card)
        layout.addWidget(hint)
        layout.addWidget(buttons)

        self._on_protocol_change(self.protocol_combo.currentText())
        self._on_provider_change()

    def showEvent(self, e):
        super().showEvent(e)
        apply_window_effects(self)

    def _on_protocol_change(self, text):
        # Only auto-correct the port if it's still on the other protocol's default.
        wanted = 22 if text == PROTOCOL_SFTP else 21
        if self.port_edit.value() in (21, 22):
            self.port_edit.setValue(wanted)

    def _on_provider_change(self, *_args):
        key = self.provider_combo.currentData()
        self.provider_hint.setText(HOSTING_HINTS.get(key, GENERIC_HOST_HINT if key else ""))

    def _on_save(self):
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, APP_NAME, "Please give this website a name.")
            return
        if not self.host_edit.text().strip():
            QMessageBox.warning(self, APP_NAME, "Please enter the server address.")
            return

        # Tidy a pasted URL into a plain address, and show what will be saved.
        typed_host = self.host_edit.text().strip()
        host, port = clean_host(typed_host)
        if not host:
            QMessageBox.warning(self, APP_NAME,
                                "That server address doesn't look right. Use just the "
                                "address, like ftp.yoursite.com or 145.79.213.73.")
            return
        if host != typed_host:
            self.host_edit.setText(host)
            if port:
                self.port_edit.setValue(port)
            QMessageBox.information(
                self, APP_NAME,
                f"The server address was tidied up to:\n\n{host}\n\n"
                "It only needs the address itself — the extra part would stop it "
                "from connecting.")
        name = clean_site_name(self.name_edit.text())
        if name and name != self.name_edit.text().strip():
            self.name_edit.setText(name)

        # Folder on server is deliberately optional — Site Deploy works out
        # the real destination from whatever you upload. Requiring it here
        # would defeat the entire point of the app.
        self.accept()

    def result_profile(self) -> Profile:
        p = self.profile
        p.name = clean_site_name(self.name_edit.text()) or self.name_edit.text().strip()
        p.host = clean_host(self.host_edit.text())[0]
        p.port = self.port_edit.value()
        p.protocol = self.protocol_combo.currentText()
        p.hosting_provider = self.provider_combo.currentData() or ""
        new_username = self.username_edit.text().strip()
        p.remote_path = self.remote_path_edit.text().strip()
        p.notes = self.notes_edit.text().strip()
        p.watch = self.watch_check.isChecked()

        # The secret itself is never assembled into profiles.json — it goes
        # straight into the Windows credential vault. A blank password field
        # means "keep what's already saved", so the existing plaintext (held
        # only in memory, decrypted from the vault at load time) is re-sent
        # unchanged if just the username or something else here was edited.
        typed_password = self.password_edit.text()
        if typed_password:
            password_to_store = typed_password
        elif p.password_enc:
            try:
                password_to_store = _dpapi_decrypt(p.password_enc)
            except Exception:
                password_to_store = ""
        else:
            password_to_store = ""

        p.username = new_username
        if password_to_store:
            if not p.credential_id:
                p.credential_id = str(uuid.uuid4())
            vault_save_credential(p.credential_id, new_username, password_to_store)
            p.password_enc = _dpapi_encrypt(password_to_store)
        return p


class ConnectionEntriesDialog(QDialog):
    """Shown when a dropped text/file has more than one website's details
    in it. Nothing is saved from here — checking entries and continuing
    opens the normal Add Website dialog for each one in turn, so every
    site still gets its own explicit review-and-Save step."""

    def __init__(self, parent, entries: list[dict]):
        super().__init__(parent)
        self.setWindowTitle("Multiple websites found")
        self.resize(560, 440)
        self.entries = entries

        self.listing = QListWidget()
        for info in entries:
            label = info.get("host") or "(no host found)"
            if info.get("username"):
                label += f"   —   {info['username']}"
            item = QListWidgetItem(label)
            item.setIcon(line_icon("globe", PALETTE.text_dim))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.listing.addItem(item)

        buttons = QDialogButtonBox()
        self.continue_btn = buttons.addButton("Add checked websites",
                                              QDialogButtonBox.AcceptRole)
        self.continue_btn.setObjectName("Primary")
        buttons.addButton("Cancel", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        card = Card()
        card.add(QLabel(
            f"Found {len(entries)} sets of website details in what you dropped. "
            f"Pick which ones to add — each one still opens its own "
            f"confirm-and-save step, nothing is added silently."))
        card.add(self.listing)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 20)
        lay.setSpacing(14)
        lay.addWidget(h_label("Multiple websites found", "H1"))
        lay.addWidget(card, 1)
        lay.addWidget(buttons)

    def showEvent(self, e):
        super().showEvent(e)
        apply_window_effects(self)

    def checked_entries(self) -> list[dict]:
        return [self.entries[i] for i in range(self.listing.count())
                if self.listing.item(i).checkState() == Qt.Checked]


# ─────────────────────────────────────────────────────────────────────────
# UI — Settings
# ─────────────────────────────────────────────────────────────────────────

class SettingsDialog(QDialog):
    def __init__(self, parent, settings: dict):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        self.settings = dict(settings)

        self.watch_check = QCheckBox("Watch my drop folders and deploy automatically")
        self.watch_check.setChecked(self.settings.get("watching_enabled", True))
        self.startup_check = QCheckBox("Start Site Deploy with Windows")
        self.startup_check.setChecked(get_start_with_windows())

        self.appearance = QComboBox()
        self.appearance.addItems(["Light", "Dark", "Match Windows"])
        self.appearance.setCurrentIndex(
            {"light": 0, "dark": 1, "auto": 2}.get(self.settings.get("appearance", "light"), 0))

        self.root_edit = QLineEdit(self.settings.get("inbox_root", str(DEFAULT_INBOX_ROOT)))
        change = QPushButton("Change...")
        change.clicked.connect(self._browse_root)
        root_row = QHBoxLayout()
        root_row.setSpacing(10)
        root_row.addWidget(self.root_edit, 1)
        root_row.addWidget(change)

        card = Card()
        card.add(h_label("Automatic deploy", "H2"))
        card.add(self.watch_check)
        card.add(self.startup_check)
        card.add(h_label("Where your zips go", "H2"))
        card.add_layout(root_row)
        card.add(h_label("Appearance", "H2"))
        card.add(self.appearance)
        card.add(h_label(f"Every update is recorded in {HISTORY_FILE}", "Dim"))

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setObjectName("Primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)
        layout.addWidget(card)
        layout.addWidget(buttons)

    def showEvent(self, e):
        super().showEvent(e)
        apply_window_effects(self)

    def _browse_root(self):
        path = QFileDialog.getExistingDirectory(self, "Choose the drop folder location",
                                                self.root_edit.text())
        if path:
            self.root_edit.setText(path)

    def result_settings(self) -> dict:
        self.settings["watching_enabled"] = self.watch_check.isChecked()
        self.settings["start_with_windows"] = self.startup_check.isChecked()
        self.settings["inbox_root"] = self.root_edit.text().strip() or str(DEFAULT_INBOX_ROOT)
        self.settings["appearance"] = ["light", "dark", "auto"][self.appearance.currentIndex()]
        return self.settings


class FolderNotFoundDialog(QDialog):
    """The login worked but the folder didn't. Show what IS on the server at
    the deepest point that exists, and let one be picked."""

    def __init__(self, parent, profile: Profile, probe: dict):
        super().__init__(parent)
        self.setWindowTitle("That folder wasn't found")
        self.setMinimumWidth(620)
        self.profile = profile
        self.probe = probe
        self.chosen: str | None = None

        deepest = probe.get("deepest") or "/"
        entries = probe.get("entries") or []

        head = h_label("Signed in fine — the folder just isn't there", "H1")
        body = QLabel(
            f"Your username and password work. But the server has no folder called:"
            f"<br><br><b>{profile.remote_path}</b><br><br>"
            f"The deepest part that does exist is <b>{deepest}</b>. "
            f"Here's what's inside it — pick the folder you want to update, or "
            f"double-click a folder to look inside it:")
        body.setWordWrap(True)

        self.path_edit = QLineEdit(deepest)
        self.listing = QListWidget()
        self.listing.setMinimumHeight(200)
        self.listing.itemDoubleClicked.connect(self._descend)
        self._fill(deepest, entries)

        note = h_label(
            "If the folder is simply not created yet (a brand-new child theme, say), "
            "you can keep the path as it is — Site Deploy creates missing folders "
            "when it uploads.", "Dim")
        note.setWordWrap(True)

        buttons = QDialogButtonBox()
        use = buttons.addButton("Use this folder", QDialogButtonBox.AcceptRole)
        use.setObjectName("Primary")
        keep = buttons.addButton("Keep what I had", QDialogButtonBox.RejectRole)
        use.clicked.connect(self._accept_path)
        keep.clicked.connect(self.reject)

        card = Card()
        card.add(body)
        card.add(h_label("Folder to update", "H2"))
        card.add(self.path_edit)
        card.add(self.listing)
        card.add(note)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 20)
        lay.setSpacing(14)
        lay.addWidget(head)
        lay.addWidget(card)
        lay.addWidget(buttons)

    def showEvent(self, e):
        super().showEvent(e)
        apply_window_effects(self)

    def _fill(self, folder: str, entries: list):
        self.listing.clear()
        if not entries:
            item = QListWidgetItem("(this folder is empty)")
            item.setFlags(Qt.NoItemFlags)
            self.listing.addItem(item)
            return
        for name in entries:
            item = QListWidgetItem(name)
            item.setIcon(line_icon("folder", PALETTE.text_dim))
            item.setData(Qt.UserRole, posixpath.join(folder, name))
            self.listing.addItem(item)

    def _descend(self, item):
        path = item.data(Qt.UserRole)
        if not path:
            return
        self.path_edit.setText(path)
        parent = self.parent()
        entries = parent.list_remote_dir(self.profile, path) if parent else []
        self._fill(path, entries)

    def _accept_path(self):
        current = self.listing.currentItem()
        if current is not None and current.data(Qt.UserRole):
            self.chosen = current.data(Qt.UserRole)
        else:
            self.chosen = self.path_edit.text().strip()
        self.accept()


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def human_time(iso: str) -> str:
    try:
        when = datetime.fromisoformat(iso)
    except Exception:
        return iso
    today = datetime.now().date()
    if when.date() == today:
        return f"Today {when.strftime('%H:%M')}"
    return when.strftime("%d %b %Y, %H:%M")


class RestoreDialog(QDialog):
    """Put back a version that was saved before an update."""

    def __init__(self, parent, profile: Profile, folder: Path):
        super().__init__(parent)
        self.setWindowTitle("Put back an earlier version")
        self.resize(720, 500)
        self.profile = profile
        self.folder = folder
        self.chosen: str | None = None

        backups = sorted(folder.glob("*.zip"), key=lambda f: f.stat().st_mtime,
                         reverse=True) if folder.is_dir() else []

        self.listing = QListWidget()
        self.listing.setMinimumHeight(240)
        for f in backups:
            st = f.stat()
            when = datetime.fromtimestamp(st.st_mtime)
            what = f.stem.split("_before_")[0]
            item = QListWidgetItem(
                f"{what}   —   saved {when.strftime('%d %b %Y, %H:%M')}   ·   "
                f"{human_size(st.st_size)}")
            item.setIcon(line_icon("history", PALETTE.text_dim))
            item.setData(Qt.UserRole, str(f))
            self.listing.addItem(item)
        if not backups:
            empty = QListWidgetItem("No saved versions yet for this website.")
            empty.setFlags(Qt.NoItemFlags)
            self.listing.addItem(empty)
        self.listing.itemDoubleClicked.connect(lambda _i: self._go())

        buttons = QDialogButtonBox()
        self.restore_btn = buttons.addButton("Put this version back",
                                             QDialogButtonBox.AcceptRole)
        self.restore_btn.setObjectName("Primary")
        self.restore_btn.setEnabled(bool(backups))
        self.restore_btn.clicked.connect(self._go)
        open_btn = buttons.addButton("Open folder", QDialogButtonBox.ActionRole)
        open_btn.clicked.connect(lambda: os.startfile(str(folder)))
        cancel = buttons.addButton("Cancel", QDialogButtonBox.RejectRole)
        cancel.clicked.connect(self.reject)

        card = Card()
        card.add(QLabel(
            f"These are copies of <b>{profile.name}</b> taken automatically just "
            f"before each update. Putting one back uploads it the same way an "
            f"update does — and saves what's there now first, so this is "
            f"reversible too."))
        card.add(self.listing)
        card.add(h_label(str(folder), "Dim"))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 20)
        lay.setSpacing(14)
        lay.addWidget(h_label("Put back an earlier version", "H1"))
        lay.addWidget(card, 1)
        lay.addWidget(buttons)

    def showEvent(self, e):
        super().showEvent(e)
        apply_window_effects(self)

    def _go(self):
        item = self.listing.currentItem()
        if item is None or not item.data(Qt.UserRole):
            QMessageBox.information(self, APP_NAME, "Pick a version from the list first.")
            return
        self.chosen = item.data(Qt.UserRole)
        self.accept()


class PluginsDialog(QDialog):
    """Everything installed in wp-content/plugins. Install (copy a zip or
    file in), delete (with a backup first), no activate/deactivate — that
    lives in the database, not in files, so it's out of scope here."""

    def __init__(self, parent, profile: Profile, get_password, archive_dir: Path):
        super().__init__(parent)
        self.setWindowTitle("Plugins")
        self.resize(760, 560)
        self.setAcceptDrops(True)   # drop a plugin zip/file here to install it
        self.profile = profile
        self.get_password = get_password
        self.archive_dir = archive_dir
        self.scan_worker: PluginScanWorker | None = None
        self.delete_worker: PluginDeleteWorker | None = None
        self.plugins: list[PluginInfo] = []

        self.status_label = h_label("Connecting...", "Dim")
        self.listing = QListWidget()
        self.listing.setMinimumHeight(280)

        warn = h_label(
            "Site Deploy can't tell whether a plugin is switched on — that's stored "
            "in your website's database, not in its files. <b>Deleting an active "
            "plugin's files can break your site</b> until you deactivate it in "
            "WordPress admin or put the backup back.", "Dim")
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color: {PALETTE.err};")

        buttons = QDialogButtonBox()
        install_btn = buttons.addButton("Install a plugin...", QDialogButtonBox.ActionRole)
        install_btn.clicked.connect(self._install)
        self.delete_btn = buttons.addButton("Delete selected", QDialogButtonBox.ActionRole)
        self.delete_btn.setEnabled(False)
        self.delete_btn.clicked.connect(self._delete_selected)
        refresh_btn = buttons.addButton("Refresh", QDialogButtonBox.ActionRole)
        refresh_btn.clicked.connect(self._scan)
        close_btn = buttons.addButton("Close", QDialogButtonBox.RejectRole)
        close_btn.clicked.connect(self.reject)
        self.buttons = buttons
        self.listing.currentRowChanged.connect(
            lambda row: self.delete_btn.setEnabled(row >= 0 and bool(self.plugins)))

        card = Card(margins=(12, 12, 12, 12))
        card.add(self.status_label)
        card.add(self.listing)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 20)
        lay.setSpacing(14)
        lay.addWidget(h_label(f"Plugins on {profile.name}", "H1"))
        lay.addWidget(warn)
        lay.addWidget(card, 1)
        lay.addWidget(buttons)

        self._load_from_cache_then_refresh()

    def showEvent(self, e):
        super().showEvent(e)
        apply_window_effects(self)

    # -- scanning -----------------------------------------------------------

    def _set_busy(self, busy: bool, status: str = ""):
        for b in self.buttons.buttons():
            b.setEnabled(not busy)
        self.listing.setEnabled(not busy)
        if status:
            self.status_label.setText(status)

    def _fill_listing(self, plugins: list):
        selected = self.listing.currentItem()
        selected_slug = getattr(selected, "_slug", None) if selected else None
        self.listing.clear()
        if not plugins:
            item = QListWidgetItem("No plugins found.")
            item.setFlags(Qt.NoItemFlags)
            self.listing.addItem(item)
        for p in plugins:
            item = QListWidgetItem(f"{p.label}   —   {p.slug}")
            item.setIcon(line_icon("check", PALETTE.ok))
            item.setToolTip(p.path)
            item._slug = p.slug
            self.listing.addItem(item)
            if p.slug == selected_slug:
                self.listing.setCurrentItem(item)

    def _load_from_cache_then_refresh(self):
        """Show what was found last time immediately — no waiting on the
        network for something that was already known a moment ago — then
        quietly check again in the background."""
        cached_list, checked_at = cache_get_plugins(self.profile.name)
        if cached_list:
            self.plugins = [PluginInfo(**d) for d in cached_list]
            self._fill_listing(self.plugins)
            age = human_time(datetime.fromtimestamp(checked_at).isoformat())
            self.status_label.setText(
                f"{len(self.plugins)} plugin(s) — as of {age}. Checking for changes...")
            self._scan(background=True)
        else:
            self._scan(background=False)

    def _scan(self, background: bool = False):
        password = self.get_password(self.profile)
        if password is None:
            return
        if not background:
            self.listing.clear()
            self._set_busy(True, "Connecting and reading wp-content/plugins...")
        self.scan_worker = PluginScanWorker(self.profile, password)
        self.scan_worker.finished_ok.connect(self._on_scanned)
        self.scan_worker.finished_err.connect(
            lambda msg: self._on_scan_failed(msg, background))
        self.scan_worker.start()

    def _on_scanned(self, plugins: list):
        self.plugins = plugins
        self._fill_listing(plugins)
        cache_set_plugins(self.profile.name, plugins)
        self._set_busy(False, f"{len(plugins)} plugin(s) found.")
        self.delete_btn.setEnabled(False)

    def _on_scan_failed(self, message: str, background: bool = False):
        self._set_busy(False, "")
        if background:
            # A cached list is still on screen and still usable — don't
            # interrupt with a popup just because the quiet re-check failed.
            self.status_label.setText(
                f"{len(self.plugins)} plugin(s) — showing the last known list "
                f"(couldn't re-check: {message})")
            return
        QMessageBox.warning(self, APP_NAME, f"Couldn't read the plugins folder:\n\n{message}")

    # -- install --------------------------------------------------------------

    def _install(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose the plugin to install", str(Path.home() / "Desktop"),
            FILE_DIALOG_FILTER)
        if path:
            self._install_path(path)

    def _install_path(self, path: str):
        main = self.parent()
        main.zip_path = path
        try:
            main.zip_info = inspect_source(path)
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, f"That file couldn't be read:\n\n{e}")
            return
        main._deploy()
        if main.worker is not None:
            main.worker.finished_ok.connect(lambda _m: self._scan())
            main.worker.finished_err.connect(lambda _m: None)

    # -- drag and drop ---------------------------------------------------------

    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasUrls() and any(u.isLocalFile() and os.path.isfile(u.toLocalFile())
                                for u in md.urls()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        md = event.mimeData()
        if md.hasUrls():
            files = [u.toLocalFile() for u in md.urls()
                    if u.isLocalFile() and os.path.isfile(u.toLocalFile())]
            if files:
                event.acceptProposedAction()
                self._install_path(files[0])
                if len(files) > 1:
                    QMessageBox.information(
                        self, APP_NAME,
                        f"Only {os.path.basename(files[0])} was installed — drop one "
                        f"plugin at a time here.")
                return
        event.ignore()

    # -- delete ---------------------------------------------------------------

    def _delete_selected(self):
        row = self.listing.currentRow()
        if not (0 <= row < len(self.plugins)):
            return
        plugin = self.plugins[row]
        if QMessageBox.warning(
            self, APP_NAME,
            f"Delete {plugin.label}?\n\n"
            f"A copy of its files is saved first, so this can be undone from "
            f"History. But if this plugin is currently ACTIVE, deleting its "
            f"files now can break your site until you restore the backup.\n\n"
            f"Make sure it's deactivated in WordPress admin first if you're "
            f"not sure.\n\nDelete {plugin.slug}?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        ) != QMessageBox.Yes:
            return

        password = self.get_password(self.profile)
        if password is None:
            return
        self._deleting = plugin
        self._set_busy(True, f"Backing up and deleting {plugin.slug}...")
        self.delete_worker = PluginDeleteWorker(self.profile, password, plugin,
                                                self.archive_dir)
        self.delete_worker.finished_ok.connect(self._on_deleted)
        self.delete_worker.finished_err.connect(self._on_delete_failed)
        self.delete_worker.start()

    def _on_deleted(self, message: str, backup_path: str):
        slug = getattr(self, "_deleting", None)
        append_history(self.profile.name, slug.slug if slug else "-",
                       True, message, action="delete_plugin",
                       target=slug.path if slug else "", backup=backup_path)
        main = self.parent()
        if hasattr(main, "_event"):
            main._event("check", message)
        self._scan()

    def _on_delete_failed(self, message: str):
        self._set_busy(False, "")
        QMessageBox.critical(self, APP_NAME, f"Couldn't delete that plugin:\n\n{message}")


class HistoryDialog(QDialog):
    """Everything that has happened, newest first — and a way back."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("History")
        self.resize(820, 560)
        self.restore_path: str | None = None

        self.listing = QListWidget()
        self.listing.setMinimumHeight(300)
        self.listing.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.listing.setTextElideMode(Qt.ElideRight)
        records = read_records()
        for rec in reversed(records):
            ok = rec.get("ok")
            action = (rec.get("action") or "").lower()
            verb = {"update": "Updated", "create": "Created", "restore": "Put back",
                    "test": "Connection checked", "failed": "Failed",
                    "delete_plugin": "Deleted plugin"}.get(
                        action, "Updated" if ok else "Failed")
            bits = [verb]
            if rec.get("file") and rec["file"] != "-":
                bits.append(rec["file"])
            if rec.get("detail") and action in ("failed", "test"):
                bits.append(rec["detail"])
            item = QListWidgetItem(f"{human_time(rec.get('time', ''))}   ·   "
                                   + "   ·   ".join(bits))
            item.setIcon(line_icon("check" if ok else "warning",
                                   PALETTE.ok if ok else PALETTE.err))
            tip = [f"{rec.get('site', '')}"]
            if rec.get("target"):
                tip.append(f"Folder: {rec['target']}")
            if rec.get("detail"):
                tip.append(rec["detail"])
            if rec.get("backup"):
                item.setData(Qt.UserRole, rec["backup"])
                tip.append(f"Saved copy from before this update:\n{rec['backup']}")
            item.setToolTip("\n".join(t for t in tip if t))
            self.listing.addItem(item)
        if not records:
            empty = QListWidgetItem("Nothing has happened yet.")
            empty.setFlags(Qt.NoItemFlags)
            self.listing.addItem(empty)

        buttons = QDialogButtonBox()
        back = buttons.addButton("Put this version back", QDialogButtonBox.AcceptRole)
        back.setObjectName("Primary")
        back.clicked.connect(self._restore_selected)
        open_log = buttons.addButton("Open the log file", QDialogButtonBox.ActionRole)
        open_log.clicked.connect(lambda: os.startfile(str(CONFIG_DIR)))
        close = buttons.addButton("Close", QDialogButtonBox.RejectRole)
        close.clicked.connect(self.reject)

        card = Card(margins=(12, 12, 12, 12))
        card.add(self.listing)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 20)
        lay.setSpacing(12)
        lay.addWidget(h_label("History", "H1"))
        lay.addWidget(h_label(
            "Every update, restore and failure. Rows with a saved copy can be "
            "put back — select one and use the button below.", "Dim"))
        lay.addWidget(card, 1)
        lay.addWidget(h_label(f"Log files: {CONFIG_DIR}", "Dim"))
        lay.addWidget(buttons)

    def showEvent(self, e):
        super().showEvent(e)
        apply_window_effects(self)

    def _restore_selected(self):
        item = self.listing.currentItem()
        backup = item.data(Qt.UserRole) if item else None
        if not backup:
            QMessageBox.information(
                self, APP_NAME,
                "Pick a row that has a saved copy — those are updates that "
                "replaced something.")
            return
        if not os.path.isfile(backup):
            QMessageBox.warning(self, APP_NAME,
                                f"That saved copy is no longer on disk:\n\n{backup}")
            return
        self.restore_path = backup
        self.accept()


# ─────────────────────────────────────────────────────────────────────────
# Main window
# ─────────────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(980, 740)
        self.setMinimumSize(860, 640)
        self.setWindowIcon(tray_icon("idle"))
        self.setAcceptDrops(True)   # drop a zip/php/file, or FTP details as text

        self.settings = load_settings()
        self.profiles: list[Profile] = load_profiles()
        self.zip_path = ""
        self.zip_info: ZipInfo | None = None
        self.restoring = False
        self.restore_note: dict = {}
        self.worker: DeployWorker | None = None
        self.queue: list[tuple[Profile, str]] = []
        self.upload_queue: list[str] = []   # manually dropped files, waiting their turn
        self._queue_active = False
        self.current_auto_zip: str | None = None
        self.error_state = False
        self._really_quitting = False

        self._build_ui()
        self._migrate_tidied_profiles()
        self._sync_inbox_folders()

        self.watcher = InboxWatcher()
        self.watcher.zip_ready.connect(self._on_zip_ready)
        self.watcher.logged.connect(self._log)
        self.watcher.start()
        self._apply_watch_config()

        self._refresh_profile_list()
        self._update_buttons()
        self._refresh_status()

    def showEvent(self, e):
        super().showEvent(e)
        apply_window_effects(self)

    # -- UI construction ---------------------------------------------------

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(18)

        outer.addWidget(self._build_sidebar())
        outer.addWidget(self._build_main_panel(), 1)
        self._build_tray()

    def _build_sidebar(self) -> QWidget:
        side = QFrame()
        side.setObjectName("Sidebar")
        side.setFixedWidth(250)
        v = QVBoxLayout(side)
        v.setContentsMargins(14, 16, 14, 14)
        v.setSpacing(8)

        v.addWidget(h_label("Websites", "H2"))

        self.profile_list = QListWidget()
        self.profile_list.currentRowChanged.connect(self._on_profile_selected)
        self.profile_list.itemDoubleClicked.connect(lambda _i: self._edit_profile())
        self.profile_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.profile_list.customContextMenuRequested.connect(self._site_context_menu)
        v.addWidget(self.profile_list, 1)

        v.addWidget(quiet_button("Add a website", "plus", self._add_profile))
        v.addWidget(quiet_button("Import website from a file...", "upload",
                                 self._import_connection_file))
        self.edit_btn = quiet_button("Change details", "pencil", self._edit_profile)
        v.addWidget(self.edit_btn)
        hint = h_label("Right-click a website for more", "Dim")
        hint.setStyleSheet(f"color: {PALETTE.text_dim}; font-size: 11px; padding-left: 8px;")
        v.addWidget(hint)

        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {PALETTE.border};")
        v.addWidget(line)

        v.addWidget(quiet_button("Plugins", "plug", self._show_plugins))
        v.addWidget(quiet_button("Put back a version", "history", self._show_restore))
        v.addWidget(quiet_button("History", "history", self._show_history))
        v.addWidget(quiet_button("Settings", "settings", self._open_settings))
        return side

    def _build_main_panel(self) -> QWidget:
        panel = QWidget()
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(16)

        # Header: title + the one status indicator, always visible
        header = QHBoxLayout()
        header.setSpacing(12)
        self.title_label = h_label(APP_NAME, "H1")
        self.status_pill = StatusPill()
        header.addWidget(self.title_label)
        header.addStretch(1)
        header.addWidget(self.status_pill)
        v.addLayout(header)

        # What to do — the instructions, and the main thing on screen
        steps = Card()
        steps.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        steps.add(h_label("What to do", "H2"))
        self.step1 = StepRow(
            1, "Add your website",
            "Your server address, login, and the folder to update.",
            "Add a website", self._add_profile)
        self.step2 = StepRow(
            2, "Choose what to upload",
            "A zip package, or a single file like a .php or .css.",
            "Choose a file", self._browse_zip)
        self.step3 = StepRow(
            3, "Update my site",
            "It finds the right place itself, saves the old version, then updates.",
            "Update my site", self._deploy)
        for s in (self.step1, self.step2, self.step3):
            steps.add(s)

        self.drop_label = QLabel()
        self.drop_label.setWordWrap(True)
        self.drop_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.drop_label.setStyleSheet(
            f"background: {PALETTE.card_alt}; border: 1px solid {PALETTE.border};"
            f"border-radius: 6px; padding: 9px 11px;")
        self.drop_label.setMinimumHeight(56)
        self.drop_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        steps.add(self.drop_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setVisible(False)
        steps.add(self.progress_bar)
        v.addWidget(steps)

        # A one-line summary, not a wall of numbers
        summary = QHBoxLayout()
        summary.setSpacing(10)
        self.summary_label = h_label("", "Dim")
        self.summary_label.setWordWrap(True)
        summary.addWidget(self.summary_label, 1)
        summary.addWidget(quiet_button("Open all folders", "folder", self._open_inbox_root))
        v.addLayout(summary)

        # Activity timeline
        activity = Card(margins=(14, 14, 14, 14), spacing=8)
        activity.add(h_label("What's happened", "H2"))
        self.timeline = Timeline()
        activity.add(self.timeline)
        self.details_toggle = Collapsible("Technical details")
        self.details_toggle.toggled_open.connect(self._grow_to_fit)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFont(QFont("Consolas", 9))
        self.log_box.setFixedHeight(130)
        self.details_toggle.add(self.log_box)
        activity.add(self.details_toggle)
        v.addWidget(activity, 1)

        # Manual deploy, demoted to Advanced
        self.advanced = Collapsible("More options")
        self.advanced.toggled_open.connect(self._grow_to_fit)
        adv_card = Card(margins=(16, 14, 16, 14), spacing=10)
        self.zip_label = h_label("No file chosen.", "Dim")
        self.zip_label.setWordWrap(True)
        adv_card.add(h_label(
            "Site Deploy also watches a folder per website: drop a zip in there "
            "and it updates on its own, with the same backup and folder matching.",
            "Dim"))

        action_row = QHBoxLayout()
        action_row.setSpacing(10)
        self.test_btn = QPushButton("Test connection")
        self.test_btn.clicked.connect(self._test_connection)
        folder_btn = QPushButton("Open drop folder")
        folder_btn.clicked.connect(self._open_current_inbox)
        archive_btn = QPushButton("Open saved copies")
        archive_btn.clicked.connect(self._open_archive)
        self.deploy_btn = QPushButton("Update my site")
        self.deploy_btn.setVisible(False)     # step 3 is the button that matters
        action_row.addWidget(self.test_btn)
        action_row.addWidget(folder_btn)
        action_row.addWidget(archive_btn)
        action_row.addStretch(1)
        adv_card.add_layout(action_row)
        self.advanced.add(adv_card)
        v.addWidget(self.advanced)
        return panel

    def _open_archive(self):
        p = self._current_profile()
        if p:
            self._open_path(str(self._archive_dir_for(p)))
        else:
            self._open_inbox_root()

    def _grow_to_fit(self, opened: bool = True):
        """Opening a disclosure must never squeeze the panels into each other —
        grow the window instead (never past the screen)."""
        if not opened:
            return

        def grow():
            needed = self.sizeHint().height()
            screen = self.screen().availableGeometry().height() if self.screen() else 1080
            if needed > self.height():
                self.resize(self.width(), min(needed, screen - 60))

        QTimer.singleShot(0, grow)

    def _build_tray(self):
        self.tray = QSystemTrayIcon(tray_icon("idle"), self)
        self.tray.setToolTip(f"{APP_NAME} — watching for updates")

        menu = QMenu()
        self.act_open = QAction(line_icon("globe", PALETTE.text), "Open Site Deploy", self)
        self.act_open.triggered.connect(self._show_window)
        self.act_inbox = QAction(line_icon("folder", PALETTE.text), "Open Inbox", self)
        self.act_inbox.triggered.connect(self._open_inbox_root)
        self.act_pause = QAction(line_icon("pause", PALETTE.text), "Pause watching", self)
        self.act_pause.setCheckable(True)
        self.act_pause.setChecked(not self.settings.get("watching_enabled", True))
        self.act_pause.toggled.connect(self._on_pause_toggled)
        self.act_history = QAction(line_icon("history", PALETTE.text), "History", self)
        self.act_history.triggered.connect(self._show_history_from_tray)
        self.act_settings = QAction(line_icon("settings", PALETTE.text), "Settings", self)
        self.act_settings.triggered.connect(self._open_settings_from_tray)
        self.act_clear = QAction(line_icon("check", PALETTE.text), "Clear error state", self)
        self.act_clear.triggered.connect(self._clear_error)
        self.act_clear.setVisible(False)
        act_quit = QAction(line_icon("power", PALETTE.text), "Quit", self)
        act_quit.triggered.connect(self._quit)

        menu.addAction(self.act_open)
        menu.addAction(self.act_inbox)
        menu.addSeparator()
        menu.addAction(self.act_pause)
        menu.addAction(self.act_history)
        menu.addAction(self.act_settings)
        menu.addAction(self.act_clear)
        menu.addSeparator()
        menu.addAction(act_quit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.messageClicked.connect(self._show_window)
        self.tray.show()

    # -- tray behaviour ----------------------------------------------------

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._show_window()

    def _show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if self.error_state:
            self._clear_error()

    def _show_history_from_tray(self):
        self._show_window()
        self._show_history()

    def _open_settings_from_tray(self):
        self._show_window()
        self._open_settings()

    def _quit(self):
        self._really_quitting = True
        self.watcher.stop()
        self.watcher.wait(3000)
        QApplication.instance().quit()

    def closeEvent(self, event):
        if self._really_quitting:
            super().closeEvent(event)
            return
        event.ignore()
        self.hide()
        if not self.settings.get("tray_hint_shown"):
            self.settings["tray_hint_shown"] = True
            save_settings(self.settings)
            self.tray.showMessage(
                APP_NAME,
                "Still running down here. Drop a zip in a site's folder and it deploys itself.",
                tray_icon("idle"), 5000,
            )

    def _set_tray_state(self, state: str, tooltip: str):
        self.tray.setIcon(tray_icon(state))
        self.tray.setToolTip(f"{APP_NAME} — {tooltip}")

    def _clear_error(self):
        self.error_state = False
        self.act_clear.setVisible(False)
        self._refresh_status()

    # -- watcher wiring ----------------------------------------------------

    def _inbox_root(self) -> str:
        return self.settings.get("inbox_root", str(DEFAULT_INBOX_ROOT))

    def _migrate_tidied_profiles(self):
        """A website saved as 'https://example.com' with host 'ftp://1.2.3.4'
        is cleaned up on load — carry its drop folder (and any zips in it)
        across to the tidied name, and write the clean version back."""
        changed = False
        for p in self.profiles:
            legacy = getattr(p, "legacy_name", None)
            if legacy and legacy != p.name:
                self._rename_inbox(legacy, p.name)
                self._event("info", f"Tidied up “{legacy}” to “{p.name}”")
                delattr(p, "legacy_name")
                changed = True
        if changed:
            save_profiles(self.profiles)

    def _sync_inbox_folders(self):
        for p in self.profiles:
            ensure_inbox(self._inbox_root(), p.name)

    def _apply_watch_config(self):
        if self.settings.get("watching_enabled", True):
            folders = {
                p.name: str(inbox_for(self._inbox_root(), p.name))
                for p in self.profiles if p.watch
            }
        else:
            folders = {}
        self.watcher.set_folders(folders, self._inbox_root())
        self._refresh_status()

    def _on_pause_toggled(self, paused: bool):
        self.settings["watching_enabled"] = not paused
        save_settings(self.settings)
        self._apply_watch_config()

    def _refresh_status(self):
        watching = self.settings.get("watching_enabled", True)
        n = len([p for p in self.profiles if p.watch]) if watching else 0
        p = self._current_profile()
        deploying = self.worker is not None and not self.worker.test_only

        if deploying:
            self.status_pill.set_state("busy", "Updating your site")
            state, tip = "busy", "updating"
        elif self.error_state:
            self.status_pill.set_state("error", "Needs a look")
            state, tip = "error", "last update FAILED"
        elif not watching:
            self.status_pill.set_state("idle", "Paused")
            state, tip = "idle", "paused"
        elif n == 0:
            self.status_pill.set_state("idle", "Not set up yet")
            state, tip = "idle", "no websites yet"
        elif p is not None and not p.tested_ok:
            self.status_pill.set_state("idle", "Not tested yet")
            state, tip = "idle", "connection not tested yet"
        else:
            self.status_pill.set_state("ok", "Ready")
            state, tip = "idle", f"watching {n} folder(s)"
        self._set_tray_state("error" if self.error_state else state, tip)

        # Which step are you on?
        if not self.profiles:
            self.step1.set_state("current")
            self.step2.set_state("todo")
            self.step3.set_state("todo")
        elif not self.zip_path:
            self.step1.set_state("done")
            self.step2.set_state("current")
            self.step3.set_state("todo")
        else:
            self.step1.set_state("done")
            self.step2.set_state("done")
            self.step3.set_state("current")
        self.step2.button.setEnabled(p is not None and self.worker is None)
        self.step3.button.setEnabled(p is not None and bool(self.zip_path)
                                     and self.worker is None)

        if self.zip_path and self.zip_info is not None:
            n = self.zip_info.file_count
            plural = "file" if n == 1 else "files"
            self.step2.explain.setText(
                f"{os.path.basename(self.zip_path)} — {self.zip_info.label}, "
                f"{n} {plural}")

        if p and self.zip_path:
            self.drop_label.setText(
                f"Ready to update <b>{p.name}</b> with "
                f"<b>{os.path.basename(self.zip_path)}</b>.<br>"
                "It finds the matching folder, saves what's there now, then updates it.")
        elif p:
            folder = inbox_for(self._inbox_root(), p.name)
            self.drop_label.setText(
                f"Choose a file above — or drop a zip into this folder and it "
                f"updates on its own:<br>{folder}")
        else:
            self.drop_label.setText(
                "Once you add a website, this is where it tells you what to do next.")

        # One plain sentence instead of a row of numbers
        if self.error_state:
            summary = ("The last update didn't finish. Open “What's happened” below "
                       "to see why, fix it, then put the zip in the folder again.")
        elif deploying:
            summary = "Uploading now. You can close this window — it keeps going."
        elif not watching:
            summary = "Paused — zips you drop in will wait until you switch watching back on."
        elif p is not None and not p.tested_ok:
            summary = "Do step 2 before your first real update — it takes a second."
        else:
            waiting = len(self.queue)
            extra = f" · {waiting} waiting" if waiting else ""
            summary = (f"Watching {n} website folder(s) · last update "
                       f"{last_deploy_summary().lower()}{extra}")
        self.summary_label.setText(summary)

    def _open_path(self, path: str):
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
            os.startfile(path)
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, f"Could not open the folder:\n{e}")

    def _open_inbox_root(self):
        self._open_path(self._inbox_root())

    def _open_current_inbox(self):
        p = self._current_profile()
        if p:
            self._open_path(str(ensure_inbox(self._inbox_root(), p.name)))
        else:
            self._open_inbox_root()

    # -- automatic deploy queue -------------------------------------------

    def _on_zip_ready(self, profile_name: str, zip_path: str):
        profile = next((p for p in self.profiles if p.name == profile_name), None)
        if profile is None:
            return
        self._event("site", f"New update for {profile_name}: {os.path.basename(zip_path)}")
        self._log(f"New update detected for {profile_name}: {os.path.basename(zip_path)}")
        self.queue.append((profile, zip_path))
        self._refresh_status()
        self._pump_queue()

    def _pump_queue(self):
        if self.worker is not None or not self.queue:
            return
        profile, zip_path = self.queue.pop(0)
        if not os.path.isfile(zip_path):
            self._pump_queue()
            return
        if not profile.password_enc:
            self._auto_failed(profile, zip_path, "No password saved for this website.")
            self._pump_queue()
            return
        try:
            password = _dpapi_decrypt(profile.password_enc)
        except Exception:
            self._auto_failed(profile, zip_path,
                              "Saved password could not be read on this Windows account.")
            self._pump_queue()
            return
        self.current_auto_zip = zip_path
        self._run_worker(profile, password, zip_path, test_only=False, automatic=True)

    def _archive_zip(self, profile: Profile, zip_path: str) -> str:
        """Move a deployed zip into <inbox>/Deployed/ with a timestamp so it
        is never picked up again."""
        inbox = ensure_inbox(self._inbox_root(), profile.name)
        target_dir = inbox / DEPLOYED_DIRNAME
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        target = target_dir / f"{stamp}_{os.path.basename(zip_path)}"
        try:
            shutil.move(zip_path, target)
            self.watcher.forget(zip_path)
            return str(target)
        except Exception as e:
            self._log(f"Deployed, but the zip could not be moved to {target_dir}: {e}")
            self.watcher.mark_handled(zip_path)
            return zip_path

    def _auto_failed(self, profile: Profile, zip_path: str, message: str):
        name = os.path.basename(zip_path)
        self._log(f"ERROR deploying {name} to {profile.name}: {message}")
        self._event("error", f"{profile.name} — {name} failed: {message}")
        append_history(profile.name, name, False, message.split("\n")[0], action="failed")
        self.watcher.mark_handled(zip_path)
        self.error_state = True
        self.act_clear.setVisible(True)
        self._set_tray_state("error", "last deploy FAILED")
        # Failure must not disappear quietly: long toast + persistent error icon
        # that stays until it's acknowledged from the tray menu or the window.
        self.tray.showMessage(
            f"{APP_NAME}: deploy FAILED",
            f"{profile.name} — {name}\n{message}\nThe site was not fully updated.",
            QSystemTrayIcon.Critical, 60000,
        )
        self._refresh_status()

    # -- website list management ------------------------------------------

    def _refresh_profile_list(self):
        current = self.profile_list.currentRow()
        self.profile_list.clear()
        for p in self.profiles:
            item = QListWidgetItem(p.name if p.watch else f"{p.name}  ·  manual")
            item.setIcon(line_icon("globe", PALETTE.text))
            self.profile_list.addItem(item)
        if self.profiles:
            self.profile_list.setCurrentRow(min(max(current, 0), len(self.profiles) - 1))

    def _site_context_menu(self, pos):
        if self._current_profile() is None:
            return
        menu = QMenu(self)
        menu.addAction(line_icon("pencil", PALETTE.text), "Edit site", self._edit_profile)
        menu.addAction(line_icon("folder", PALETTE.text), "Open drop folder",
                       self._open_current_inbox)
        menu.addSeparator()
        menu.addAction(line_icon("trash", PALETTE.text), "Remove site", self._delete_profile)
        menu.exec(self.profile_list.mapToGlobal(pos))

    def _current_profile(self) -> Profile | None:
        row = self.profile_list.currentRow()
        if 0 <= row < len(self.profiles):
            return self.profiles[row]
        return None

    def _on_profile_selected(self, _row):
        self._update_buttons()
        self._refresh_status()

    def _add_profile(self):
        dlg = ProfileDialog(self)
        if dlg.exec() == QDialog.Accepted:
            p = dlg.result_profile()
            self.profiles.append(p)
            save_profiles(self.profiles)
            folder = ensure_inbox(self._inbox_root(), p.name)
            self._refresh_profile_list()
            self.profile_list.setCurrentRow(len(self.profiles) - 1)
            self._apply_watch_config()
            self._event("folder", f"Drop folder ready for {p.name}")
            self._log(f"Drop folder for {p.name}: {folder}")

    def _edit_profile(self):
        p = self._current_profile()
        if not p:
            return
        old_name = p.name
        dlg = ProfileDialog(self, p)
        if dlg.exec() == QDialog.Accepted:
            dlg.result_profile()
            save_profiles(self.profiles)
            if p.name != old_name:
                self._rename_inbox(old_name, p.name)
            ensure_inbox(self._inbox_root(), p.name)
            self._refresh_profile_list()
            self._apply_watch_config()

    def _rename_inbox(self, old_name: str, new_name: str):
        old_folder = inbox_for(self._inbox_root(), old_name)
        new_folder = inbox_for(self._inbox_root(), new_name)
        if old_folder.exists() and not new_folder.exists():
            try:
                old_folder.rename(new_folder)
                self._log(f"Renamed drop folder to {new_folder}")
            except Exception as e:
                self._log(f"Could not rename the drop folder: {e}")

    def _delete_profile(self):
        p = self._current_profile()
        if not p:
            return
        if QMessageBox.question(
            self, APP_NAME, f'Remove the saved details for "{p.name}"?'
        ) == QMessageBox.Yes:
            self.profiles.remove(p)
            save_profiles(self.profiles)
            vault_delete_credential(p.credential_id)
            self._refresh_profile_list()
            self._apply_watch_config()
            folder = inbox_for(self._inbox_root(), p.name)
            if folder.exists():
                self._log(f"Its drop folder was left in place: {folder}")

    def _update_buttons(self):
        busy = self.worker is not None
        has_profile = self._current_profile() is not None
        self.test_btn.setEnabled(has_profile and not busy)
        self.deploy_btn.setEnabled(has_profile and bool(self.zip_path) and not busy)
        self.edit_btn.setEnabled(has_profile)

    # -- settings / history ------------------------------------------------

    def _open_settings(self):
        dlg = SettingsDialog(self, self.settings)
        if dlg.exec() != QDialog.Accepted:
            return
        new = dlg.result_settings()
        err = set_start_with_windows(new.get("start_with_windows", False))
        if err:
            QMessageBox.warning(self, APP_NAME,
                                f"Could not change the Windows startup setting:\n{err}")
        appearance_changed = new.get("appearance") != self.settings.get("appearance")
        self.settings = new
        save_settings(self.settings)
        if appearance_changed:
            QMessageBox.information(
                self, APP_NAME,
                "The new appearance will be used next time you open Site Deploy.")
        self.act_pause.blockSignals(True)
        self.act_pause.setChecked(not self.settings.get("watching_enabled", True))
        self.act_pause.blockSignals(False)
        self._sync_inbox_folders()
        self._apply_watch_config()

    def _show_history(self):
        dlg = HistoryDialog(self)
        if dlg.exec() == QDialog.Accepted and dlg.restore_path:
            self._start_restore(dlg.restore_path)

    def _show_restore(self):
        p = self._current_profile()
        if not p:
            QMessageBox.information(self, APP_NAME, "Pick a website on the left first.")
            return
        dlg = RestoreDialog(self, p, self._archive_dir_for(p))
        if dlg.exec() == QDialog.Accepted and dlg.chosen:
            self._start_restore(dlg.chosen)

    def _show_plugins(self):
        p = self._current_profile()
        if not p:
            QMessageBox.information(self, APP_NAME, "Pick a website on the left first.")
            return
        PluginsDialog(self, p, self._get_password, self._archive_dir_for(p)).exec()

    def _start_restore(self, backup_zip: str):
        """Putting a version back is just an update using the saved zip — same
        engine, same folder matching, and it backs up the current state too."""
        p = self._current_profile()
        if not p:
            return
        try:
            info = inspect_zip(backup_zip)
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, f"That saved copy couldn't be read:\n\n{e}")
            return
        when = datetime.fromtimestamp(os.path.getmtime(backup_zip))
        if QMessageBox.question(
            self, APP_NAME,
            f"Put back the version of {info.label} saved on "
            f"{when.strftime('%d %b %Y at %H:%M')}?\n\n"
            f"What's on your site now will be saved first, so you can undo this too."
        ) != QMessageBox.Yes:
            return
        self.zip_path = backup_zip
        self.zip_info = info
        self.zip_label.setText(os.path.basename(backup_zip))
        self.restoring = True
        note = read_backup_note(backup_zip)
        self.restore_note = note
        self._event("history", f"Putting back {os.path.basename(backup_zip)}")
        self._refresh_status()
        self._deploy()

    # -- zip selection ------------------------------------------------------

    def _browse_zip(self):
        start = str(Path.home() / "Desktop")
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose what to upload", start, FILE_DIALOG_FILTER)
        if path:
            self._use_chosen_path(path)

    def _use_chosen_path(self, path: str):
        """The one place a zip/file becomes 'what step 2 chose' — used by the
        file picker and by dropping a file onto the window."""
        ext = Path(path).suffix.lower()
        if ext not in SUPPORTED_EXTS and QMessageBox.question(
            self, APP_NAME,
            f"“{ext or 'that'}” isn't a file type Site Deploy knows about.\n\n"
            f"It can still upload it as a single file. Continue?"
        ) != QMessageBox.Yes:
            return
        try:
            info = inspect_source(path)
        except Exception as e:
            QMessageBox.warning(self, APP_NAME,
                                f"That file couldn't be read:\n\n{e}")
            return
        self.zip_path = path
        self.zip_info = info
        self.zip_label.setText(os.path.basename(path))
        self.zip_label.setStyleSheet("")
        self.restoring = False
        self.restore_note = {}
        self._event("info", f"Chose {os.path.basename(path)} — {info.label}")
        self._update_buttons()
        self._refresh_status()

    # -- drag and drop --------------------------------------------------------

    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasUrls():
            paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            if any(os.path.isfile(p) for p in paths):
                event.acceptProposedAction()
                return
        elif md.hasText() and md.text().strip():
            event.acceptProposedAction()
            return
        event.ignore()

    # A dropped file this small, with this extension, is worth reading as
    # text before assuming it's something to upload. A theme zip or a
    # multi-megabyte asset never matches — this only ever opens files that
    # could plausibly be a saved note of connection details.
    CONNECTION_FILE_EXTS = (".txt", ".md", ".ini", ".cfg", ".conf", ".log", "")
    CONNECTION_FILE_MAX_BYTES = 200_000

    def dropEvent(self, event):
        # Dropping directly onto a website in the sidebar targets THAT site
        # specifically, instead of whichever one happened to be selected —
        # switch to it first, then handle the drop exactly as normal.
        target = self._profile_at_global_pos(event.globalPosition().toPoint())
        if target is not None:
            row = self.profiles.index(target)
            if self.profile_list.currentRow() != row:
                self.profile_list.setCurrentRow(row)

        md = event.mimeData()
        if md.hasUrls():
            paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            files = [p for p in paths if os.path.isfile(p)]
            if files:
                event.acceptProposedAction()
                self._handle_dropped_files(files)
                return
        if md.hasText() and md.text().strip():
            event.acceptProposedAction()
            self._handle_dropped_text(md.text())
            return
        event.ignore()

    def _profile_at_global_pos(self, global_pos) -> Profile | None:
        """If a drop lands on a specific row in the website list, return
        that website — lets a file be dropped straight onto a site in the
        sidebar to target it directly, rather than needing to click it
        first and drop somewhere else."""
        local = self.profile_list.mapFromGlobal(global_pos)
        if not self.profile_list.rect().contains(local):
            return None
        item = self.profile_list.itemAt(local)
        if item is None:
            return None
        row = self.profile_list.row(item)
        if 0 <= row < len(self.profiles):
            return self.profiles[row]
        return None

    def _handle_dropped_files(self, paths: list[str]):
        """One or many files dropped together. Anything that reads like a
        note of website connection details goes to Add Website (with a
        picker if several sites turn up); everything else — zips, plugin
        files, whatever — goes into the upload queue for the currently
        selected website. A single upload file behaves exactly as before
        (it's staged in step 2, waiting for you to press Update my site);
        two or more run through automatically, one at a time, each still
        gated by its own confirm-before-changing-anything step."""
        connection_entries = []
        upload_candidates = []
        for path in paths:
            ext = Path(path).suffix.lower()
            entries = self._read_connection_entries(path)
            if entries:
                connection_entries.extend(entries)
            else:
                upload_candidates.append(path)
                if ext in self.CONNECTION_FILE_EXTS:
                    # Explicit, not silent: say the scan was tried and found
                    # nothing, rather than quietly treating it as an upload.
                    self._event("info", f"{os.path.basename(path)} didn't look like "
                                        f"website login details — treating it as a "
                                        f"file to upload. Use “Import website from a "
                                        f"file” in the sidebar for a clearer check.")

        if connection_entries:
            self._offer_add_websites(connection_entries, source="file")

        if not upload_candidates:
            return
        if len(upload_candidates) == 1 and not self.upload_queue and self.worker is None:
            self._use_chosen_path(upload_candidates[0])
            return
        self._queue_uploads(upload_candidates)

    def _read_connection_entries(self, path: str) -> list[dict]:
        """A dropped .txt/.ini/etc. file might be a saved note of FTP
        details rather than something meant for the site — possibly
        several sites' worth. Empty list means: treat it as an upload."""
        ext = Path(path).suffix.lower()
        if ext not in self.CONNECTION_FILE_EXTS:
            return []
        try:
            if os.path.getsize(path) > self.CONNECTION_FILE_MAX_BYTES:
                return []
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return []
        return parse_connection_entries_smart(text)

    def _handle_dropped_text(self, text: str) -> bool:
        """Dragging a block of text — from an email, a hosting panel, a
        note — that reads like FTP details, possibly for more than one
        site. Never saved silently: this only ever pre-fills the Add
        Website dialog for you to check and confirm. Returns whether
        anything connection-shaped was recognized at all."""
        entries = parse_connection_entries_smart(text)
        if not entries:
            return False
        self._offer_add_websites(entries, source="text")
        return True

    def _offer_add_websites(self, entries: list[dict], source: str):
        if len(entries) == 1:
            self._offer_add_website(entries[0], source)
            return
        self._event("info", f"Found {len(entries)} sets of website details in the "
                            f"{source} — pick which to add")
        dlg = ConnectionEntriesDialog(self, entries)
        if dlg.exec() == QDialog.Accepted:
            for info in dlg.checked_entries():
                self._offer_add_website(info, source)

    def _import_connection_file(self):
        """Explicit and unambiguous: you're telling Site Deploy this file
        IS login details, so it's read as such directly — no guessing about
        whether it was meant as something to upload instead. Never a dead
        end: if nothing could be read from it, this still opens Add Website
        (blank, or with whatever little was found) instead of just stopping
        with an error — there's always a way to keep going."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a file with website login details", str(Path.home() / "Desktop"),
            "Text files (*.txt *.md *.ini *.cfg *.conf *.log);;All files (*)")
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, f"Couldn't read that file:\n\n{e}")
            return
        entries = parse_connection_entries_smart(text)
        if entries:
            self._offer_add_websites(entries, source=f"file “{os.path.basename(path)}”")
            return
        QMessageBox.information(
            self, APP_NAME,
            f"Couldn't find anything that looks like website login details in "
            f"“{os.path.basename(path)}”.\n\n"
            f"It works best with one clear address, username and password — "
            f"either labeled (\"Host:\", \"Username:\", \"Password:\") or one "
            f"value per line with nothing else on that line.\n\n"
            f"Use Add a website when you're ready to enter it yourself.")

    def _offer_add_website(self, info: dict, source: str):
        """Pre-fill the Add Website dialog from one parsed entry. Nothing is
        saved until the dialog is confirmed — same as adding one by hand."""
        profile = Profile()
        profile.host = info.get("host", "")
        if info.get("port"):
            profile.port = info["port"]
        profile.username = info.get("username", "")
        profile.remote_path = info.get("remote_path", "")
        profile.name = info.get("name") or info.get("host", "")

        dlg = ProfileDialog(self, profile)
        if info.get("password"):
            dlg.password_edit.setText(info["password"])
        found = ", ".join(k for k in ("host", "username", "password", "port", "remote_path")
                          if info.get(k))
        self._event("info", f"Found connection details in the dropped {source} ({found}) — "
                            f"check and save to add it")
        if dlg.exec() == QDialog.Accepted:
            p = dlg.result_profile()
            self.profiles.append(p)
            save_profiles(self.profiles)
            folder = ensure_inbox(self._inbox_root(), p.name)
            self._refresh_profile_list()
            self.profile_list.setCurrentRow(len(self.profiles) - 1)
            self._apply_watch_config()
            self._event("folder", f"Drop folder ready for {p.name}")

    # -- multi-file upload queue ---------------------------------------------

    def _queue_uploads(self, paths: list[str]):
        p = self._current_profile()
        if not p:
            QMessageBox.information(
                self, APP_NAME,
                "Pick a website on the left first, then drop your files again.")
            return
        new_items = [path for path in paths if path not in self.upload_queue]
        self.upload_queue.extend(new_items)
        self._event("info", f"Queued {len(new_items)} file(s) to update "
                           f"{p.name} with, one at a time.")
        if not self._queue_active:
            self._queue_active = True
            self._advance_upload_queue()

    def _advance_upload_queue(self):
        """Loads and deploys the next queued file, if nothing else is in
        progress. Called once when queuing starts, and again every time a
        deploy finishes (success, failure, or cancel) while the queue is
        active — a bad file in the batch doesn't stop the rest."""
        if not self._queue_active or self.worker is not None:
            return
        if not self.upload_queue:
            self._queue_active = False
            return
        next_path = self.upload_queue.pop(0)
        self.zip_path = ""
        self.zip_info = None
        self._use_chosen_path(next_path)
        if self.zip_info is not None and self.zip_path:
            left = len(self.upload_queue)
            self._event("info", f"Updating with {os.path.basename(next_path)}"
                               f"{f' — {left} more queued' if left else ''}")
            self._deploy()
        else:
            self._advance_upload_queue()

    # -- password prompt ----------------------------------------------------

    def _get_password(self, profile: Profile) -> str | None:
        if profile.password_enc:
            try:
                return _dpapi_decrypt(profile.password_enc)
            except Exception:
                QMessageBox.warning(
                    self, APP_NAME,
                    "Saved password could not be read (it may have been saved on a "
                    "different Windows account). Please edit this website and re-enter it."
                )
                return None
        QMessageBox.warning(self, APP_NAME, "No password saved for this website. Edit it first.")
        return None

    # -- actions ------------------------------------------------------------

    def list_remote_dir(self, profile: Profile, path: str) -> list:
        """One-shot listing for the folder browser. Short-lived connection,
        so nothing is held open while the dialog sits there."""
        backend = None
        try:
            password = _dpapi_decrypt(profile.password_enc)
            backend, _desc = connect_backend(profile, password, lambda _m: None)
            return sorted(e for e in backend.list_dir(path) if e not in (".", ".."))
        except Exception as e:
            self._log(f"Could not list {path}: {e}")
            return []
        finally:
            if backend is not None:
                backend.close()

    def _handle_missing_folder(self, profile: Profile, probe: dict):
        dlg = FolderNotFoundDialog(self, profile, probe)
        if dlg.exec() != QDialog.Accepted or not dlg.chosen:
            return
        if dlg.chosen != profile.remote_path:
            profile.remote_path = dlg.chosen
            save_profiles(self.profiles)
            self._event("check", f"Folder to update set to {dlg.chosen}")
            self._log(f"Folder to update changed to {dlg.chosen}")
            if QMessageBox.question(
                self, APP_NAME,
                f"Folder set to:\n\n{dlg.chosen}\n\nTest the connection again now?"
            ) == QMessageBox.Yes:
                self._test_connection()

    def _mark_tested(self, profile: Profile | None):
        """Once a site has connected successfully, step 2 is behind you."""
        if profile is not None and not profile.tested_ok:
            profile.tested_ok = True
            save_profiles(self.profiles)

    def _log(self, msg):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_box.appendPlainText(f"[{stamp}] {msg}")

    def _event(self, kind, text):
        self.timeline.add_event(kind, text)

    def _test_connection(self):
        p = self._current_profile()
        if not p:
            return
        password = self._get_password(p)
        if password is None:
            return
        self._run_worker(p, password, "", test_only=True)

    def _deploy(self):
        """Step 3. No path to confirm up front — it works that out on the
        server and asks once it knows, in _on_plan_ready."""
        p = self._current_profile()
        if not p or not self.zip_path:
            return
        password = self._get_password(p)
        if password is None:
            return
        note = getattr(self, "restore_note", {}) if self.restoring else {}
        self._run_worker(p, password, self.zip_path, test_only=False,
                         needs_approval=True,
                         forced_target=note.get("target", ""),
                         forced_kind=note.get("kind", ""))

    def _archive_dir_for(self, profile: Profile) -> Path:
        folder = ensure_inbox(self._inbox_root(), profile.name) / "Archived"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _on_plan_ready(self, plan: dict):
        """It has looked at the server and knows where this goes. Ask, once."""
        worker = self.worker
        if worker is None or not worker._needs_approval:
            return
        info = plan.get("info")
        target = plan["target"]
        name = os.path.basename(self.zip_path)

        if plan.get("risky"):
            body = (f"Careful — {target} looks like your whole website folder, not a "
                    f"theme or plugin folder.\n\n"
                    f"Site Deploy couldn't tell what {name} is (no theme or plugin "
                    f"header inside it), so it fell back to the folder saved in this "
                    f"website's details. Uploading here would write files across your "
                    f"site.\n\nOnly continue if that is really what you want.")
            answer = QMessageBox.warning(self, APP_NAME, body,
                                         QMessageBox.Yes | QMessageBox.No,
                                         QMessageBox.No)
            worker.approve(answer == QMessageBox.Yes)
            if answer != QMessageBox.Yes:
                self._event("info", "Stopped — nothing was changed")
            return

        single = bool(plan.get("is_file"))
        thing = "file" if single else "folder"

        if plan["exists"]:
            matched = plan.get("matched")
            headline = f"Found where {info.label if info else name} goes:\n\n{target}\n"
            if matched:
                headline += f"\n(matched by {matched})\n"
            what = ("that file already exists, so it will be replaced" if single
                    else "that folder already exists, so this is an update")
            body = (f"{headline}\n{what.capitalize()}:\n"
                    f"  • a copy of what's there now is saved first, into\n"
                    f"    {self._archive_dir_for(worker.profile)}\n"
                    f"  • then the new version goes up\n\n"
                    f"The saved copy is a zip on this PC — nothing extra is left on "
                    f"your website, so it can't show up or be switched on there.\n\n"
                    f"Go ahead?")
        else:
            body = (f"{info.label if info else name} isn't on your site yet.\n\n"
                    f"It would be added as:\n{target}\n\n"
                    f"({plan.get('how', '')})\n\nGo ahead?")

        box = QMessageBox(QMessageBox.Question, APP_NAME, body,
                          QMessageBox.NoButton, self)
        yes = box.addButton("Yes, go ahead", QMessageBox.YesRole)
        pick = None
        if single or plan.get("needs_location") or not plan["exists"]:
            pick = box.addButton(f"Choose the {thing}...", QMessageBox.ActionRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.setDefaultButton(yes)
        box.exec()
        clicked = box.clickedButton()

        if clicked is yes:
            worker.approve(True)
            return
        if pick is not None and clicked is pick:
            chosen = self._pick_remote_location(worker.profile, plan, info)
            if chosen:
                worker.approve(True, retarget=chosen)
            else:
                worker.approve(False)
                self._event("info", "Cancelled — nothing was changed")
            return
        worker.approve(False)
        self._event("info", "Cancelled — nothing was changed")

    def _pick_remote_location(self, profile: Profile, plan: dict,
                              info: ZipInfo | None) -> str | None:
        """Browse the server and choose where this should go."""
        start = plan.get("target_dir") or posixpath.dirname(plan.get("target", "")) or "/"
        probe = {"deepest": start if start else "/",
                 "entries": self.list_remote_dir(profile, start or "/"),
                 "missing": ""}
        dlg = FolderNotFoundDialog(self, profile, probe)
        dlg.setWindowTitle("Choose where it goes")
        if dlg.exec() != QDialog.Accepted or not dlg.chosen:
            return None
        folder = dlg.chosen
        if plan.get("is_file") and info is not None:
            return posixpath.join(folder, info.filename)
        return folder

    def _run_worker(self, profile, password, zip_path, test_only, automatic=False,
                    needs_approval=False, forced_target="", forced_kind=""):
        if not test_only:
            self.progress_bar.setRange(0, 0)     # indeterminate until files start moving
            self.progress_bar.setVisible(True)
        if automatic:
            self._event("info", f"Updating {profile.name} with {os.path.basename(zip_path)}")
            self._log(f"Auto-deploying {os.path.basename(zip_path)} to {profile.name}...")
        archive_dir = None if test_only else self._archive_dir_for(profile)
        self.worker = DeployWorker(profile, password, zip_path, test_only, automatic,
                                   archive_dir=archive_dir, needs_approval=needs_approval,
                                   forced_target=forced_target, forced_kind=forced_kind)
        self.worker.logged.connect(self._log)
        self.worker.event.connect(self._event)
        self.worker.plan_ready.connect(self._on_plan_ready)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished_ok.connect(self._on_finished_ok)
        self.worker.finished_err.connect(self._on_finished_err)
        self.worker.start()
        self._update_buttons()
        self._refresh_status()

    def _on_progress(self, done, total):
        if self.progress_bar.maximum() != total:
            self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(done)

    def _finish_worker(self):
        self.worker = None
        self.progress_bar.setVisible(False)
        self.progress_bar.setRange(0, 100)
        self._update_buttons()
        QTimer.singleShot(0, self._pump_queue)
        QTimer.singleShot(0, self._advance_upload_queue)

    def _on_finished_ok(self, message):
        worker = self.worker
        automatic = bool(worker and worker.automatic)
        test_only = bool(worker and worker.test_only)
        profile = worker.profile if worker else None
        zip_path = worker.zip_path if worker else ""
        self._log(message)
        self._finish_worker()

        if automatic and profile is not None:
            self._mark_tested(profile)
            name = os.path.basename(zip_path)
            archived = self._archive_zip(profile, zip_path)
            plan = worker.plan or {}
            append_history(profile.name, name, True, message,
                           action="update" if plan.get("exists") else "create",
                           target=plan.get("target", ""),
                           backup=worker.archived_to or "")
            self._event("check", f"{profile.name} is live · {message}")
            self._log(f"Moved to {archived}")
            version = detect_version(name)
            detail = f"{profile.name}{(' ' + version) if version else ''} is live."
            self.tray.showMessage(f"{APP_NAME}: update deployed",
                                  f"{detail}\n{message}", tray_icon("idle"), 5000)
            self.current_auto_zip = None
        elif test_only:
            probe = worker.probe if worker else None
            append_history(profile.name if profile else "-", "-", True,
                           "Connection test" if not probe else "Connected, folder not found",
                           action="test")
            self._mark_tested(profile)      # the login itself is proven either way
            if probe:
                self._event("error", f"{profile.remote_path} isn't on the server yet")
                self._refresh_status()
                self._handle_missing_folder(profile, probe)
                return
            self._event("check", "Connection test succeeded")
            QMessageBox.information(
                self, APP_NAME,
                "That worked — Site Deploy can reach your website.\n\n"
                "You're set up. From now on, put your zip in this site's folder "
                "and the update happens on its own.")
        else:
            cancelled = message.startswith("Cancelled")
            restoring, self.restoring = self.restoring, False
            if profile is not None and not cancelled:
                plan = (worker.plan or {}) if worker else {}
                append_history(profile.name, os.path.basename(zip_path), True, message,
                               action=("restore" if restoring else
                                       "update" if plan.get("exists") else "create"),
                               target=plan.get("target", ""),
                               backup=(worker.archived_to or "") if worker else "")
                self._mark_tested(profile)
            self._event("info" if cancelled else "check", message)
            if cancelled:
                QMessageBox.information(self, APP_NAME, message)
            else:
                target = (worker.plan or {}).get("target", "") if worker else ""
                backup = worker.archived_to if worker else None
                detail = f"{message}\n\n{target}" if target else message
                if backup:
                    detail += (f"\n\nThe version that was there before is saved as:\n"
                               f"{backup}\n\nTo put it back, choose that zip in step 2 "
                               f"and update again.")
                QMessageBox.information(self, APP_NAME, detail)
                self.zip_path = ""          # done — back to step 2 for the next one
                self.zip_info = None
                self.zip_label.setText("No file chosen.")
        self._refresh_status()

    def _on_finished_err(self, message):
        worker = self.worker
        automatic = bool(worker and worker.automatic)
        profile = worker.profile if worker else None
        zip_path = worker.zip_path if worker else ""
        self._log(f"ERROR: {message}")
        self._finish_worker()

        if automatic and profile is not None:
            self._auto_failed(profile, zip_path, message)
            self.current_auto_zip = None
        else:
            if profile is not None:
                plan = (worker.plan or {}) if worker else {}
                append_history(profile.name, os.path.basename(zip_path) or "-", False,
                               message.split("\n")[0], action="failed",
                               target=plan.get("target", ""),
                               backup=(worker.archived_to or "") if worker else "")
            self._event("error", message.split("\n")[0])
            QMessageBox.critical(self, APP_NAME, f"That didn't work.\n\n{message}")
        self._refresh_status()


def detect_version(filename: str) -> str:
    """Pull something like 1.4.2 out of a zip filename for the toast text."""
    m = re.search(r"(\d+\.\d+(?:\.\d+)*)", filename)
    return f"v{m.group(1)}" if m else ""


# ─────────────────────────────────────────────────────────────────────────
# One copy running at a time
# ─────────────────────────────────────────────────────────────────────────
#
# A named Windows mutex, checked before anything else opens. Two copies
# sharing one profiles.json with no coordination is exactly how a site can
# look "not saved" — one window holding a stale, older list can overwrite
# a newer save from another window. Rather than trying to make concurrent
# writers safe, this makes concurrent writers impossible: launching a
# second copy signals the first one to come to the front and quits.

# "Global\" forces these into the system-wide kernel object namespace rather
# than the caller's logon session. Without it, two processes launched from
# different sessions (e.g. one from a terminal service session, one from the
# interactive console — or, as found while testing this, two launches kicked
# off by separate automation calls) can each get their own private mutex and
# never see each other, defeating the whole point of a single-instance lock.
SINGLE_INSTANCE_MUTEX_NAME = r"Global\SiteDeploy_SingleInstance_Mutex_v1"
SHOW_WINDOW_EVENT_NAME = r"Global\SiteDeploy_ShowWindow_Event_v1"


def acquire_single_instance_lock():
    """Returns (mutex_handle, already_running). Windows releases a named
    mutex automatically when the owning process exits — even if it
    crashes — so there is nothing to explicitly clean up."""
    try:
        import win32event
        import win32api
        import winerror
    except Exception:
        return None, False    # pywin32 missing: don't block launching over it
    mutex = win32event.CreateMutex(None, False, SINGLE_INSTANCE_MUTEX_NAME)
    already_running = win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
    return mutex, already_running


def signal_running_instance():
    """Ask the copy that's already running to raise its window."""
    try:
        import win32event
        import win32api
        evt = win32event.OpenEvent(win32event.EVENT_MODIFY_STATE, False,
                                   SHOW_WINDOW_EVENT_NAME)
        win32event.SetEvent(evt)
        win32api.CloseHandle(evt)
    except Exception:
        pass


def main():
    mutex, already_running = acquire_single_instance_lock()
    if already_running:
        signal_running_instance()
        sys.exit(0)

    try:
        import win32event
        show_event = win32event.CreateEvent(None, False, False, SHOW_WINDOW_EVENT_NAME)
    except Exception:
        show_event = None

    app = QApplication(sys.argv)
    app._single_instance_mutex = mutex   # keep the handle alive for the process
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setQuitOnLastWindowClosed(False)   # tray app: closing the window keeps it running
    theme.set_mode(load_settings().get("appearance", "light"))
    app.setFont(theme.ui_font())
    app.setStyleSheet(theme.stylesheet())
    app.setWindowIcon(tray_icon("idle"))

    win = MainWindow()

    if show_event is not None:
        import win32event

        def check_show_event():
            if win32event.WaitForSingleObject(show_event, 0) == win32event.WAIT_OBJECT_0:
                win._show_window()

        poll_timer = QTimer()
        poll_timer.timeout.connect(check_show_event)
        poll_timer.start(400)
        win._instance_poll_timer = poll_timer   # keep the timer alive

    # Launching goes straight to the tray. First run (nothing configured yet)
    # shows the window, otherwise there'd be nothing to see.
    if "--window" in sys.argv or not win.profiles:
        win.show()
    else:
        win.tray.showMessage(
            APP_NAME, "Running in the tray and watching your drop folders.",
            tray_icon("idle"), 4000,
        )
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
