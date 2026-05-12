"""
Paylocity → GAS Horarios scraper (cloud, GitHub Actions).

Flujo:
1. Carga storage_state.json (cookies) desde env var PAYLOCITY_STORAGE_STATE_B64
2. Playwright Chromium navega a EmployeeSearch multi-co
3. Extrae employees (co, first, last, hireDate, employeeId, jobTitle, status) del Kendo grid
4. POST a GAS_URL endpoint action=ingestPaylocityRoster
5. Si session expirada → WhatsApp alerta vía Whapi y exit 1

Exit codes:
  0 = success
  1 = session expired
  2 = scrape parser error
  3 = POST failed
"""
import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

GAS_URL = os.environ.get(
    "GAS_URL",
    "https://script.google.com/macros/s/AKfycbxsUvNqY-Uih5_-AtVzsDSHJW9OXjJpxtsjIrJtSXee5gwo5YJSQ3LSt-Z-fWF6sSLP/exec",
)
SCRAPE_URL = "https://login.paylocity.com/Escher/Escher_WebUI/EmployeeSearch/home/index?uniquecode=csEmployeeSearch&area=multico&view=EmployeeSearch"
WHAPI_TOKEN = os.environ.get("WHAPI_TOKEN", "")
ADMIN_PHONE = os.environ.get("ADMIN_PHONE", "")

CO_MAP = {
    "103204": "doniphan",
    "103206": "zaragoza",
    "115148": "airway",
    "169447": "casino",
    "169448": "weso",
    "169450": "lubbock",
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
            headers={
                "Authorization": f"Bearer {WHAPI_TOKEN}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
            json={"to": ADMIN_PHONE, "body": msg},
            timeout=20,
        )
    except Exception as e:
        log(f"whapi err: {e}")


def load_storage_state():
    """Decode storage_state from env var (base64 of JSON) and write to temp file."""
    b64 = os.environ.get("PAYLOCITY_STORAGE_STATE_B64", "")
    if not b64:
        raise RuntimeError("Missing PAYLOCITY_STORAGE_STATE_B64 env var")
    state = base64.b64decode(b64).decode("utf-8")
    p = Path("/tmp/storage_state.json") if os.name != "nt" else Path(os.environ.get("TEMP", ".")) / "storage_state.json"
    p.write_text(state, encoding="utf-8")
    return str(p)


def scrape_employees(state_path):
    """Run Playwright, navigate, return list[dict]."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(storage_state=state_path, user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
        ))
        page = context.new_page()
        log(f"Navigating to EmployeeSearch...")
        page.goto(SCRAPE_URL, wait_until="domcontentloaded", timeout=90000)

        # Detect session expiration
        try:
            page.wait_for_selector("#EmployeeSearchGrid", timeout=30000)
        except Exception:
            cur = page.url
            if "access.paylocity.com" in cur or "login" in cur.lower():
                log(f"Session expired → {cur}")
                whapi_alert(
                    "⚠️ Paylocity session expirada en GitHub Action.\n"
                    "Hay que refrescar storage_state. Avísale al admin."
                )
                browser.close()
                sys.exit(1)
            log(f"Grid no apareció. URL actual: {cur}")
            html = page.content()[:2000]
            log(f"HTML head: {html}")
            browser.close()
            sys.exit(2)

        # Configure page size to 2000 + read all rows
        log("Loading all rows from Kendo grid...")
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
            log(f"Kendo extraction failed: {e}")
            browser.close()
            sys.exit(2)
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
        r = requests.post(GAS_URL, json=payload, timeout=120,
                          headers={"Content-Type": "application/json"})
        body = r.text
        log(f"GAS response ({r.status_code}): {body[:500]}")
        try:
            j = r.json()
            if j.get("success"):
                d = j.get("data", {})
                log(f"OK ingested={d.get('totalIn')} perStore={d.get('perStore')}")
                return True
            log(f"GAS error: {j.get('error')}")
            return False
        except Exception:
            log("Respuesta no JSON")
            return r.status_code == 200
    except Exception as e:
        log(f"POST exc: {e}")
        return False


def main():
    log("Starting Paylocity scrape (cloud)")
    state_path = load_storage_state()
    employees = scrape_employees(state_path)
    log(f"Scraped {len(employees)} raw employees")
    if not employees:
        whapi_alert("⚠️ Paylocity scrape: 0 employees. Algo cambió en el portal.")
        return 2

    # Breakdown por CO
    by_co = {}
    for e in employees:
        by_co.setdefault(e.get("co", "?"), 0)
        by_co[e["co"]] += 1
    for co, cnt in sorted(by_co.items()):
        store = CO_MAP.get(co, "?")
        log(f"  CO {co} ({store}): {cnt}")

    if not post_to_gas(employees):
        whapi_alert("⚠️ Paylocity scrape OK pero POST a GAS falló.")
        return 3

    log("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
