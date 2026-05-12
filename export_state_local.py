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
    print("Abriendo Chromium VISIBLE en Paylocity.")
    print()
    print("PASOS:")
    print("  1. Loguéate en Paylocity con MFA")
    print("  2. Espera a que cargue la lista de empleados (EmployeeSearch grid)")
    print("  3. *NO cierres la ventana de Chrome todavía*")
    print("  4. Vuelve a esta terminal y presiona ENTER")
    print()
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(PAYLOCITY_URL)
        input("\n>>> Cuando veas la lista de empleados en Chrome, PRESIONA ENTER aquí <<<\n")
        # Save state BEFORE closing context
        try:
            ctx.storage_state(path=str(STATE_OUT))
            print(f"✓ Saved {STATE_OUT}")
        except Exception as e:
            print(f"ERROR saving state: {e}")
            sys.exit(2)
        try:
            ctx.close()
        except Exception:
            pass
    # Validate
    import json as _json
    state = _json.loads(STATE_OUT.read_text(encoding="utf-8"))
    cookies = state.get("cookies", [])
    pay_cookies = [c for c in cookies if "paylocity" in c.get("domain", "")]
    print(f"Total cookies: {len(cookies)}, Paylocity cookies: {len(pay_cookies)}")
    if len(pay_cookies) < 3:
        print("⚠️ Pocas cookies Paylocity — login pudo no haberse completado. Repite el proceso.")
        sys.exit(3)

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
