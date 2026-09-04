# GoodParts test assignment

## Diagnostic setup

Requires Python 3.12 or newer.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item .env.example .env
```

Open `.env` and manually add the real `OZON_CLIENT_ID` and `OZON_API_KEY` values.
Do not commit this file.

Run the diagnostic from the project root:

```powershell
python scripts/diagnose_ozon.py
```
