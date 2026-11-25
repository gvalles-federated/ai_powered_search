# ai_powered_search

Prototype of an AI search bar for parts lookup: type a search the way you'd say it, for example
`altenator 2021 dodge ram 5.7`, and get the parts that fit.

## How it works

1. **The model reads the search.** A local model (Ollama) turns the text into fields: year, make, model, engine,
   part type, position, part brand, vehicle details (trim, drive type…) and part features ("160 amp", "ceramic").
   It fixes typos and casual names, but its output is never trusted directly.
2. **VCdb / PCdb check it.** The vehicle, engine and part type are matched against the Auto Care tables:
   - Typos are fuzzy-matched to real names.
   - Known naming differences are corrected (Chevy → Chevrolet, Dodge Ram → Ram from 2011).
   - Anything that doesn't exist or is ambiguous comes back as a question with valid options. For example,
     *"The 2021 Ram 1500 has no 5.3L engine. Which one?"*
3. **The catalog returns the parts.** The search goes vehicle + part type → ACES applications → qualifiers → SKUs →
   PIES attributes. It runs read-only on the catalog read replica, and every step uses indexed columns. Filters,
   in order:
   - **Engine and position**, from the ACES qualifiers (`catalog.acesattrload`).
   - **Vehicle details**, such as submodel, drive type, fuel or body: typed values are matched to the qualifiers on
     the results, and every qualifier present is offered as clickable chips. An application without that kind of
     qualifier fits every value.
   - **Brand**, fuzzy-matched to the `skumaster` brand names (AAIA and FAP).
   - **Part features**, matched against the PIES attributes in `catalog.skuitemrel`; "160 amp" matches
     "Amperage Rating: 160A".
   - **SKUs**: unblocked, with `record_origin` in pies / aces / duplicate_strip.

The page always shows what was understood, any corrections made, and anything typed that couldn't be matched, plus
the time each step took.

## Setup (macOS, Linux, Windows)

### Prerequisites

- **Python 3.10 or newer.**
- **[Ollama](https://ollama.com)** with a model pulled. `ollama list` should show it. The install differs by OS
  (app for macOS and Windows, install script for Linux), but the `ollama` commands are the same everywhere.
- **Network access to the catalog read replica**, for example on the office network or VPN.

Run every command below from the project folder.

### 1. Install

**macOS / Linux**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```
On Debian/Ubuntu, if `venv` is missing: `sudo apt install python3-venv`.

**Windows (PowerShell)**
```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```
If PowerShell refuses to run the activate script, allow local scripts once:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
In Command Prompt, activate with `.venv\Scripts\activate.bat`; in Git Bash, `source .venv/Scripts/activate`.

### 2. Connection settings (`.env`)

The app reads its database and model settings from a `.env` file in the project folder (`app/config.py` loads it
with `python-dotenv`). `.env` is git-ignored. **Never commit it, and never put real credentials in `.env.example`,
this README or any other tracked file.**

**Create the file from the template and restrict who can read it:**

| OS | Commands |
|---|---|
| macOS | `cp .env.example .env`<br>`chmod 600 .env`<br>`open -e .env` (or `nano .env`, or open it in PyCharm) |
| Linux | `cp .env.example .env`<br>`chmod 600 .env`<br>`nano .env` (or any editor) |
| Windows (PowerShell) | `Copy-Item .env.example .env`<br>`icacls .env /inheritance:r /grant:r "${env:USERNAME}:F"`<br>`notepad .env` |

On Windows, the `icacls` line removes inherited permissions so only your user can read the file. Files under your
user folder are usually private already, so this step is optional there.

**Fill in the values.** Use no quotes and no spaces around `=`:

```
DB_HOST=<catalog read replica host>
DB_PORT=3306
DB_USER=<read-only username>
DB_PASSWORD=<password>

OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=<exact name from `ollama list`>

MAX_APPS=500
```

| Setting | What to put |
|---|---|
| `DB_HOST`, `DB_PORT` | The **catalog read replica** endpoint. If you have it as `host:port`, split it into the two settings. |
| `DB_USER`, `DB_PASSWORD` | A **read-only** account on the read replica. The app only runs SELECTs and sets its session to READ ONLY. |
| `OLLAMA_URL` | Where Ollama is listening. The default is the local Ollama install. |
| `OLLAMA_MODEL` | The NAME column from `ollama list`, copied exactly, including the tag after the colon. |
| `MAX_APPS` | Safety cap on fitment applications fetched per search. |

The database account needs SELECT on the `vcdb`, `pcdb` and `catalog` schemas
(`catalog.acesappsload`, `catalog.acesattrload`, `catalog.skuacesrel`, `catalog.skumaster`).

**Check that git ignores the file.** `.env` must **not** be listed by:

```
git status
```

**Shell variables instead of `.env`.** Variables set in the shell override `.env`:
- **macOS / Linux:** `export DB_HOST=...`
- **PowerShell:** `$env:DB_HOST = "..."`
- **Command Prompt:** `set DB_HOST=...`

That's handy for a one-off run, but they vanish when the terminal closes and the password ends up in your shell
history, so `.env` is the better choice.

### 3. Run

Make sure Ollama is running (`ollama list` should answer), then, with the virtual environment active:

```
streamlit run streamlit_app.py
```

Streamlit prints a local URL (usually http://localhost:8501) and opens it in your browser.

The sidebar shows the model and database host the app picked up, which confirms `.env` was read. The first page
load reads the VCdb / PCdb reference tables into memory, which takes a few seconds.

### Troubleshooting

| Symptom | Check |
|---|---|
| Connection error or timeout on page load | Network access to the read replica (VPN?), and `DB_HOST` / `DB_PORT` |
| `Access denied` | `DB_USER` / `DB_PASSWORD`, and that the account has SELECT on `vcdb`, `pcdb` and `catalog` |
| "OLLAMA_MODEL is not set" | `.env` is in the project folder and `OLLAMA_MODEL` is filled in |
| Model errors (404, connection refused) | Ollama is running, and `OLLAMA_MODEL` matches `ollama list` exactly |
| Sidebar shows "not set" for everything | `.env` must be in the project root, next to `streamlit_app.py`. On Windows, check it isn't named `.env.txt`: Notepad can add `.txt`; turn on file extensions in Explorer to see. |

## Layout

| Path | What |
|---|---|
| `streamlit_app.py` | The UI |
| `app/llm.py` | Prompt and JSON schema for the local model |
| `app/reference.py` | VCdb makes / models / base vehicles and PCdb part types + aliases, kept in memory |
| `app/resolve.py` | Validation, typo matching and follow-up questions |
| `app/search.py` | Fitment lookup and SKU details |
| `app/db.py` / `app/config.py` | Read-only DB access and settings from `.env` |
