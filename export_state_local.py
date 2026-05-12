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

PROFILE_DIR = r"C:\Users\Javo\paylocity_login_profile"  # profile dedicado para este flow
STATE_OUT = Path(__file__).parent / "storage_state.json"
B64_OUT = Path(__file__).parent / "storage_state.b64"
PAYLOCITY_URL = "https://login.paylocity.com/Escher/Escher_WebUI/EmployeeSearch/home/index?uniquecode=csEmployeeSearch&area=multico&view=EmployeeSearch"


def main():
    print(f"Profile: {PROFILE_DIR}")
    print("Abriendo Chromium VISIBLE. Loguéate en Paylocity con tu MFA.")
    print("Cuando veas la lista de employees (EmployeeSearch grid), CIERRA la ventana.")
    print()
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(PAYLOCITY_URL)
        print("Esperando que cierres la ventana de Chrome...")
        # Wait until user closes browser window
        try:
            page.wait_for_event("close", timeout=600000)  # 10 min max
        except Exception:
            pass
        # Save state (works even if context still open)
        try:
            ctx.storage_state(path=str(STATE_OUT))
        except Exception as e:
            print(f"WARN saving state: {e}")
        try:
            ctx.close()
        except Exception:
            pass
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
