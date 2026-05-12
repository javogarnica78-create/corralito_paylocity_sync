"""
Extracts Paylocity cookies directly from your installed Chrome profiles (DPAPI decrypt).
No need to log in fresh — uses your existing logged-in session.

Scans all Chrome profiles, decrypts cookies, finds paylocity.com domains,
outputs Playwright-compatible storage_state.json + .b64.
"""
import base64
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import win32crypt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CHROME_BASE = Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data"
OUT_DIR = Path(__file__).parent
STATE_OUT = OUT_DIR / "storage_state.json"
B64_OUT = OUT_DIR / "storage_state.b64"


def get_master_key():
    local_state = json.loads((CHROME_BASE / "Local State").read_text(encoding="utf-8"))
    key_b64 = local_state["os_crypt"]["encrypted_key"]
    key = base64.b64decode(key_b64)
    # Strip "DPAPI" prefix (5 bytes)
    if key[:5] != b"DPAPI":
        raise RuntimeError("Key doesn't start with DPAPI prefix")
    encrypted = key[5:]
    decrypted = win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)[1]
    return decrypted


def decrypt_value(encrypted_value, master_key):
    """Decrypt Chrome cookie value. Returns plaintext str or None."""
    if not encrypted_value:
        return ""
    # AES-GCM v10/v11
    if encrypted_value[:3] in (b"v10", b"v11"):
        try:
            nonce = encrypted_value[3:15]
            ciphertext_tag = encrypted_value[15:]
            aesgcm = AESGCM(master_key)
            plain = aesgcm.decrypt(nonce, ciphertext_tag, None)
            return plain.decode("utf-8", errors="ignore")
        except Exception as e:
            return None
    # Legacy DPAPI
    try:
        plain = win32crypt.CryptUnprotectData(encrypted_value, None, None, None, 0)[1]
        return plain.decode("utf-8", errors="ignore")
    except Exception:
        return None


def cdp_samesite_to_pw(s):
    if not s or s == 0 or s == -1:
        return "None"
    # Chrome SQLite uses: 0=None, 1=Lax, 2=Strict, -1=unspecified
    if isinstance(s, int):
        return {0: "None", 1: "Lax", 2: "Strict"}.get(s, "None")
    s = s.lower() if isinstance(s, str) else "none"
    return {"none": "None", "lax": "Lax", "strict": "Strict"}.get(s, "None")


def _copy_locked(src, dst):
    """Copy file even if locked by another process (FILE_SHARE_READ)."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.windll.kernel32
    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = -1
    h = kernel32.CreateFileW(
        str(src), GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None, OPEN_EXISTING, 0, None,
    )
    if h == INVALID_HANDLE_VALUE:
        raise OSError(f"CreateFileW failed (err={ctypes.GetLastError()})")
    try:
        # Read in chunks
        with open(dst, "wb") as f:
            buf = ctypes.create_string_buffer(65536)
            bytes_read = wintypes.DWORD(0)
            while True:
                ok = kernel32.ReadFile(h, buf, 65536, ctypes.byref(bytes_read), None)
                if not ok or bytes_read.value == 0:
                    break
                f.write(buf.raw[:bytes_read.value])
    finally:
        kernel32.CloseHandle(h)


def extract_from_db(db_path, master_key):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        try:
            shutil.copyfile(db_path, tmp.name)
        except PermissionError:
            _copy_locked(db_path, tmp.name)
        conn = sqlite3.connect(f"file:{tmp.name}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("""
            SELECT host_key, name, encrypted_value, path, expires_utc, is_secure, is_httponly, samesite
            FROM cookies
            WHERE host_key LIKE '%paylocity.com%'
               OR host_key LIKE '%b2clogin.com%'
        """)
        rows = cur.fetchall()
        conn.close()
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass

    out = []
    for host, name, enc, path, expires_utc, is_secure, is_httponly, samesite in rows:
        plain = decrypt_value(enc, master_key)
        if plain is None:
            continue
        # Chrome expires_utc = microseconds since 1601-01-01. Convert to UNIX epoch (sec since 1970)
        if expires_utc and expires_utc > 0:
            expires = (expires_utc / 1_000_000) - 11644473600
        else:
            expires = -1
        out.append({
            "name": name,
            "value": plain,
            "domain": host,
            "path": path or "/",
            "expires": expires,
            "httpOnly": bool(is_httponly),
            "secure": bool(is_secure),
            "sameSite": cdp_samesite_to_pw(samesite),
        })
    return out


def main():
    print(f"Chrome base: {CHROME_BASE}")
    master = get_master_key()
    print(f"Master key OK ({len(master)} bytes)")

    profiles = ["Default"] + [d.name for d in CHROME_BASE.iterdir() if d.is_dir() and d.name.startswith("Profile ")]
    all_cookies = {}  # key = (domain, name, path) → cookie (keep latest by expires)
    per_profile = {}
    for prof in profiles:
        db = CHROME_BASE / prof / "Network" / "Cookies"
        if not db.exists():
            continue
        try:
            ck = extract_from_db(db, master)
        except Exception as e:
            print(f"  {prof}: ERROR {e}")
            continue
        per_profile[prof] = len(ck)
        for c in ck:
            key = (c["domain"], c["name"], c["path"])
            existing = all_cookies.get(key)
            if not existing or (c["expires"] > existing["expires"]):
                all_cookies[key] = c
        print(f"  {prof}: {len(ck)} cookies paylocity/b2clogin")

    final = list(all_cookies.values())
    print(f"\nTotal unique cookies: {len(final)}")
    domains = {}
    for c in final:
        domains[c["domain"]] = domains.get(c["domain"], 0) + 1
    for d, n in sorted(domains.items(), key=lambda x: -x[1]):
        print(f"  {d}: {n}")

    # Sanity: look for session-y cookies
    session_y = [c["name"] for c in final if any(k in c["name"].lower() for k in ["session", "auth", "asp", "tgt", "ad", "sid", "token", "csrf"])]
    print(f"Session-y cookies: {session_y[:15]}")

    if len(final) < 3:
        print("WARN: Pocas cookies. Paylocity no aparece en ningun profile.")
        sys.exit(1)

    state = {"cookies": final, "origins": []}
    STATE_OUT.write_text(json.dumps(state), encoding="utf-8")
    b64 = base64.b64encode(STATE_OUT.read_bytes()).decode("ascii")
    B64_OUT.write_text(b64, encoding="ascii")
    print(f"\n✓ Wrote {STATE_OUT} ({STATE_OUT.stat().st_size} bytes)")
    print(f"✓ Wrote {B64_OUT} ({len(b64)} chars)")
    print(f"\nUpload to GitHub Secret:")
    print(f"  Get-Content storage_state.b64 | gh secret set PAYLOCITY_STORAGE_STATE_B64")


if __name__ == "__main__":
    main()
