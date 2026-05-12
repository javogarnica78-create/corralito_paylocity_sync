# corralito_paylocity_sync

GitHub Actions cloud workflow que diario (5 AM MT) scrapea Paylocity y postea a GAS Horarios endpoint `ingestPaylocityRoster`. La PC del admin **no necesita estar prendida**.

## Architecture

```
GitHub Actions (5 AM UTC daily)
  → Playwright Chromium con storage_state.json (cookies)
    → https://login.paylocity.com/.../EmployeeSearch
      → extrae Kendo grid (co, name, hireDate, etc.)
        → POST → GAS Horarios action=ingestPaylocityRoster
          → tab PaylocityRoster del Sheet (snapshot fresco)
            → dailyDualSystemCheck (1 PM MT) auto-alta unknowns
```

## Setup (UNA vez)

### 1. Crear el repo en GitHub
```bash
cd C:/Users/Javo/corralito_paylocity_sync
gh repo create corralito_paylocity_sync --private --source=. --push
```

### 2. Extraer storage_state desde una sesión Paylocity logged-in (local, una vez)
```bash
python export_state_local.py
# Se abre Chrome visible. Logueate manualmente en Paylocity (con MFA).
# Una vez que veas la lista de employees, cierra la ventana.
# Genera storage_state.json + storage_state.b64.
```

### 3. Subir como GitHub Secrets
```bash
gh secret set PAYLOCITY_STORAGE_STATE_B64 < storage_state.b64
gh secret set WHAPI_TOKEN --body "<TU_TOKEN_WHAPI>"
gh secret set ADMIN_PHONE --body "19156679319"
gh secret set GAS_URL --body "https://script.google.com/macros/s/AKfycbxsUvNqY-Uih5_-AtVzsDSHJW9OXjJpxtsjIrJtSXee5gwo5YJSQ3LSt-Z-fWF6sSLP/exec"
```

### 4. Dispara manualmente para validar
```bash
gh workflow run daily.yml
gh run watch
```

## Refresh session (cuando expire — Whapi te avisa)

Cuando llegue WhatsApp "⚠️ Paylocity session expirada":
1. Repite step 2 + 3 de arriba (login fresco + secret update)
2. Frecuencia esperada: cada 30-90 días

## Files

- `scrape.py` — Playwright scraper que corre en GitHub Actions
- `export_state_local.py` — helper local para extraer cookies (interactivo)
- `.github/workflows/daily.yml` — cron 11:00 UTC + workflow_dispatch
- `requirements.txt` — playwright + requests

## NO commitear

- `storage_state.json`
- `storage_state.b64`
(ambos en `.gitignore`)
