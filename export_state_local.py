"""
ONE-TIME helper (runs locally, not in cloud).

Lee el paylocity_pw_profile existente y exporta storage_state.json + base64.
Output: pega el base64 como GitHub Secret PAYLOCITY_STORAGE_STATE_B64.

Si la sesión cloud expira (Paylocity invalida cookies), corre este script otra vez
después de hacer login fresco una vez en Chrome con el profile.

Uso:
  python export_state_local.py
  → imprime base64 + comando gh secret set listo para copy-paste
"""
import base64
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE_DIR = r"C:\Users\Javo\Jg Dropbox\javier garnica\Z-Claude\paylocity_pw_profile"
STATE_OUT = Path(__file__).parent / "storage_state.json"
B64_OUT = Path(__file__).parent / "storage_state.b64"


def main():
    print(f"Opening Playwright persistent context: {PROFILE_DIR}")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,  # visible so user can verify session if needed
        )
        # Just save state and close
        ctx.storage_state(path=str(STATE_OUT))
        ctx.close()
    print(f"Wrote {STATE_OUT}")

    raw = STATE_OUT.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    B64_OUT.write_text(b64, encoding="ascii")
    print(f"Wrote {B64_OUT} ({len(b64)} chars)")
    print()
    print("=" * 70)
    print("Now set as GitHub Secret:")
    print(f"  gh secret set PAYLOCITY_STORAGE_STATE_B64 < {B64_OUT}")
    print("=" * 70)


if __name__ == "__main__":
    sys.exit(main() or 0)
