"""
Paylocity → GAS Horarios scraper (cloud, GitHub Actions).

Flow:
1. Restaurar storage_state.json desde cache GH Actions (si existe) → tiene trust device cookie
2. Lanzar Playwright Chromium con ese state
3. Navegar a EmployeeSearch:
   - Si carga el grid → ya logueado, seguir
   - Si redirect a login → hacer login fresco con Company ID + Username + Password
     * Si Paylocity exige MFA SMS → Whapi alert + exit 1 (necesita bootstrap manual con trust device)
     * Si trust device cookie aún sirve → entra directo
4. Extraer empleados del Kendo grid
5. POST a GAS_URL action=ingestPaylocityRoster
6. Guardar storage_state.json actualizado (siguiente run usa cookies frescos)
"""
import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

GAS_URL = os.environ.get("GAS_URL", "")
SCRAPE_URL = "https://login.paylocity.com/Escher/Escher_WebUI/EmployeeSearch/home/index?uniquecode=csEmployeeSearch&area=multico&view=EmployeeSearch"
LOGIN_URL = "https://access.paylocity.com/"
WHAPI_TOKEN = os.environ.get("WHAPI_TOKEN", "")
ADMIN_PHONE = os.environ.get("ADMIN_PHONE", "")
COMPANY_ID = os.environ.get("PAYLOCITY_COMPANY_ID", "")
USERNAME = os.environ.get("PAYLOCITY_USERNAME", "")
PASSWORD = os.environ.get("PAYLOCITY_PASSWORD", "")

STATE_PATH = Path("storage_state.json")  # persisted via actions/cache

CO_MAP = {
    "103204": "doniphan", "103206": "zaragoza", "115148": "airway",
    "169447": "casino", "169448": "weso", "169450": "lubbock",
}


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


def whapi_alert(msg):
    if not WHAPI_TOKEN or not ADMIN_PHONE:
        log("(no whapi creds, skip alert)")
        return
    try:
        requests.post(
            "https://gate.whapi.cloud/messages/text",
            headers={"Authorization": f"Bearer {WHAPI_TOKEN}", "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            json={"to": ADMIN_PHONE, "body": msg},
            timeout=20,
        )
    except Exception as e:
        log(f"whapi err: {e}")


def load_state():
    """Try restore state from B64 secret OR existing file. Returns path or None."""
    # 1) Existing file (from cache)
    if STATE_PATH.exists() and STATE_PATH.stat().st_size > 100:
        log(f"Using cached state: {STATE_PATH} ({STATE_PATH.stat().st_size}b)")
        return str(STATE_PATH)
    # 2) Bootstrap from B64 secret
    b64 = os.environ.get("PAYLOCITY_STORAGE_STATE_B64", "")
    if b64 and len(b64) > 100:
        try:
            data = base64.b64decode(b64)
            STATE_PATH.write_bytes(data)
            log(f"Bootstrapped state from secret B64 ({len(data)}b)")
            return str(STATE_PATH)
        except Exception as e:
            log(f"B64 decode failed: {e}")
    log("No state available — fresh login needed")
    return None


import re
import time


def whapi_request_mfa_code():
    """Send WA asking user to forward SMS code. Poll Whapi inbox for 6-digit reply."""
    if not WHAPI_TOKEN or not ADMIN_PHONE:
        log("No Whapi — cannot relay MFA")
        return None
    sent_at = int(time.time())
    try:
        requests.post(
            "https://gate.whapi.cloud/messages/text",
            headers={"Authorization": f"Bearer {WHAPI_TOKEN}", "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            json={"to": ADMIN_PHONE, "body": (
                "🔐 *Paylocity necesita código MFA*\n\n"
                "Reenvíame el código de 6 dígitos que llegó por SMS.\n"
                "Solo el número (ej: 123456) — tienes 8 minutos."
            )},
            timeout=30,
        )
    except Exception as e:
        log(f"whapi send err: {e}")
        return None
    # Poll for incoming
    log("Esperando código MFA por WhatsApp (8 min max)...")
    chat_id = ADMIN_PHONE + "@s.whatsapp.net"
    deadline = sent_at + 8 * 60
    while time.time() < deadline:
        time.sleep(10)
        try:
            r = requests.get(
                f"https://gate.whapi.cloud/messages/list/{ADMIN_PHONE}",
                headers={"Authorization": f"Bearer {WHAPI_TOKEN}", "User-Agent": "Mozilla/5.0"},
                params={"count": 10},
                timeout=20,
            )
            data = r.json()
            messages = data.get("messages", [])
            for m in messages:
                if m.get("from_me"):
                    continue
                ts = m.get("timestamp", 0)
                if ts < sent_at:
                    continue
                body = (m.get("text", {}) or {}).get("body", "") if isinstance(m.get("text"), dict) else str(m.get("text", ""))
                if not body:
                    body = m.get("body", "") or m.get("caption", "")
                # Extract 6-digit code
                match = re.search(r"\b(\d{6})\b", body)
                if match:
                    code = match.group(1)
                    log(f"MFA code received via WA: {code[:2]}****")
                    return code
        except Exception as e:
            log(f"poll err: {e}")
    log("Timeout waiting for MFA code")
    return None


def handle_mfa_page(page):
    """When on MFA challenge: request code via WA, input it, check trust device, submit."""
    log("Handling MFA page...")
    # Sometimes the page first asks which method to use; click "Send/Text/SMS" option if present
    for sel in [
        'button:has-text("Texto")', 'button:has-text("SMS")', 'button:has-text("Text")',
        'a:has-text("Texto")', 'a:has-text("SMS")', 'input[value*="SMS"]', 'input[value*="Text"]'
    ]:
        try:
            page.click(sel, timeout=2000)
            log(f"Clicked SMS option: {sel}")
            page.wait_for_load_state("networkidle", timeout=10000)
            break
        except Exception:
            continue
    code = whapi_request_mfa_code()
    if not code:
        return False
    # Fill code
    filled = False
    for sel in ['input[name*="Code"]', 'input[name*="code"]', 'input[type="tel"]', 'input[name*="otp"]', 'input[name*="Otp"]', 'input[id*="Code"]']:
        try:
            page.fill(sel, code, timeout=3000)
            filled = True
            log(f"Filled code in: {sel}")
            break
        except Exception:
            continue
    if not filled:
        log("Could not find MFA code input")
        try: page.screenshot(path="screenshot_mfa_input.png")
        except Exception: pass
        return False
    # Try check "Trust this device" checkbox if present
    for sel in [
        'input[type="checkbox"][name*="Trust"]', 'input[type="checkbox"][name*="trust"]',
        'input[type="checkbox"][id*="Trust"]', 'input[type="checkbox"][id*="Remember"]',
        'label:has-text("Recordar")', 'label:has-text("Trust")', 'label:has-text("Remember")'
    ]:
        try:
            el = page.locator(sel).first
            if el and el.count() > 0:
                el.check(timeout=2000)
                log(f"Checked trust device: {sel}")
                break
        except Exception:
            continue
    # Submit
    for sel in ['button[type="submit"]', 'input[type="submit"]', 'button:has-text("Verify")',
                'button:has-text("Acceder")', 'button:has-text("Verificar")', 'button:has-text("Submit")',
                'button:has-text("Continue")', 'button:has-text("Continuar")']:
        try:
            page.click(sel, timeout=3000)
            break
        except Exception:
            continue
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass
    cur = page.url.lower()
    if "mfa" in cur or "challenge" in cur:
        log(f"Still on MFA: {page.url}")
        return False
    log(f"MFA passed → {page.url}")
    return True


def perform_login(page):
    """Returns True on successful EmployeeSearch entry, False if MFA blocked."""
    if not (COMPANY_ID and USERNAME and PASSWORD):
        log("Missing creds in env")
        return False
    log("Going to login page...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    for sel in ['input[name="CompanyId"]', 'input[name="companyId"]', 'input[id*="Company"]']:
        try:
            page.wait_for_selector(sel, timeout=15000)
            page.fill(sel, COMPANY_ID)
            break
        except PWTimeoutError:
            continue
    for sel in ['input[name="Username"]', 'input[name="username"]']:
        try:
            page.fill(sel, USERNAME, timeout=8000); break
        except Exception:
            continue
    for sel in ['input[name="Password"]', 'input[name="password"]', 'input[type="password"]']:
        try:
            page.fill(sel, PASSWORD, timeout=8000); break
        except Exception:
            continue
    for sel in ['button[type="submit"]', 'input[type="submit"]', 'button:has-text("Login")', 'button:has-text("Acceder")']:
        try:
            page.click(sel, timeout=5000); break
        except Exception:
            continue
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass
    cur = page.url.lower()
    if "/mfa" in cur or "challenge" in cur or "verify" in cur:
        log(f"MFA detected at {page.url} — attempting Whapi relay")
        if not handle_mfa_page(page):
            return False
    log(f"After login URL: {page.url}")
    return True


def scrape_employees():
    state_path = load_state()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx_kwargs = {
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
            "viewport": {"width": 1366, "height": 800},
        }
        if state_path:
            ctx_kwargs["storage_state"] = state_path
        context = browser.new_context(**ctx_kwargs)
        page = context.new_page()
        log("Navigating to EmployeeSearch...")
        page.goto(SCRAPE_URL, wait_until="domcontentloaded", timeout=90000)

        # Detect if redirected to login
        try:
            page.wait_for_selector("#EmployeeSearchGrid", timeout=12000)
            logged_in = True
        except PWTimeoutError:
            cur = page.url.lower()
            log(f"No grid yet. URL: {page.url}")
            if "access.paylocity" in cur or "login" in cur or "challenge" in cur:
                log("Detected login page — attempting fresh login")
                logged_in = perform_login(page)
                if logged_in:
                    page.goto(SCRAPE_URL, wait_until="domcontentloaded", timeout=60000)
                    try:
                        page.wait_for_selector("#EmployeeSearchGrid", timeout=20000)
                    except Exception:
                        logged_in = False
            else:
                logged_in = False

        if not logged_in:
            log("Login failed / MFA challenge")
            whapi_alert(
                "⚠️ Paylocity cloud login bloqueado (MFA SMS).\n"
                "Hay que bootstrap: corre export_state_local.py local marcando 'Trust device' "
                "y sube el storage_state.b64 fresco."
            )
            try:
                page.screenshot(path="screenshot_login_fail.png")
            except Exception:
                pass
            browser.close()
            sys.exit(1)

        log("Logged in OK, loading grid rows...")
        try:
            employees = page.evaluate("""
                async () => {
                  const $ = window.jQuery || window.$;
                  if (!$) throw 'jQuery missing';
                  const g = $('#EmployeeSearchGrid').data('kendoGrid');
                  if (!g) throw 'Kendo grid missing';
                  g.dataSource.pageSize(2000);
                  await g.dataSource.read();
                  const data = g.dataSource.data().toJSON();
                  return data.map(r => ({
                    co: r.co || r.companyId || '',
                    first: r.firstName || r.first || '',
                    last: r.lastName || r.last || '',
                    display: r.displayName || '',
                    status: r.empStatus || r.status || '',
                    hireDate: r.hireDate || r.hire || '',
                    employeeId: r.employeeId || r.empId || '',
                    jobTitle: r.jobTitle || r.job || ''
                  }));
                }
            """)
        except Exception as e:
            log(f"Kendo extract error: {e}")
            browser.close()
            sys.exit(2)

        # Persist fresh state for next run
        try:
            context.storage_state(path=str(STATE_PATH))
            log(f"Persisted state to {STATE_PATH} ({STATE_PATH.stat().st_size}b)")
        except Exception as e:
            log(f"Save state err: {e}")
        browser.close()
        return employees


def post_to_gas(employees):
    payload_emps = []
    for e in employees:
        status_raw = (e.get("status") or "").strip()
        status = "active" if status_raw.upper().startswith("A") else status_raw.lower()
        payload_emps.append({
            "co": (e.get("co") or "").strip(),
            "firstName": (e.get("first") or "").strip(),
            "lastName": (e.get("last") or "").strip(),
            "hireDate": (e.get("hireDate") or "").strip(),
            "employeeId": (e.get("employeeId") or "").strip(),
            "jobTitle": (e.get("jobTitle") or "").strip(),
            "status": status,
        })
    active = sum(1 for x in payload_emps if x["status"] == "active")
    log(f"Posting {active} active / {len(payload_emps)} total → GAS")
    payload = {
        "action": "ingestPaylocityRoster",
        "scrapedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "employees": payload_emps,
    }
    try:
        r = requests.post(GAS_URL, json=payload, timeout=120, headers={"Content-Type": "application/json"})
        log(f"GAS response ({r.status_code}): {r.text[:400]}")
        try:
            j = r.json()
            if j.get("success"):
                d = j.get("data", {})
                log(f"OK ingested={d.get('totalIn')} perStore={d.get('perStore')}")
                return True
            log(f"GAS error: {j.get('error')}")
            return False
        except Exception:
            return r.status_code == 200
    except Exception as e:
        log(f"POST exc: {e}")
        return False


def main():
    log("Starting Paylocity scrape (cloud)")
    employees = scrape_employees()
    log(f"Scraped {len(employees)} raw employees")
    if not employees:
        whapi_alert("⚠️ Paylocity scrape: 0 employees.")
        return 2
    by_co = {}
    for e in employees:
        by_co[e.get("co", "?")] = by_co.get(e.get("co", "?"), 0) + 1
    for co, cnt in sorted(by_co.items()):
        log(f"  CO {co} ({CO_MAP.get(co, '?')}): {cnt}")
    if not post_to_gas(employees):
        whapi_alert("⚠️ Paylocity scrape OK pero POST a GAS falló.")
        return 3
    log("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
