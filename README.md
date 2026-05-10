# CS2 Toolkit

A tiny local web app with two utilities for Counter-Strike 2:

- **Avatar Fetcher** — paste SteamID64s, profile URLs, or vanity names and pull
  full-quality (184&times;184) Steam avatars in bulk; download them all as a
  ZIP.
- **.dem Multitool** — drop in a CS2 demo file and get the map / server header,
  the round-by-round result, and a per-player K/D + crosshair-code summary
  (with copy buttons and lazy-loaded Steam avatars).

Everything runs on your machine. There is no database, no Docker, and no build
step — just `pip install` and `python main.py`.

## Requirements

- Python **3.11+**
- A free [Steam Web API key](https://steamcommunity.com/dev/apikey) (only
  required for the Avatar Fetcher tab; the demo parser works without it)

## Install

```bash
git clone https://github.com/VanyaSvetoslav/auto-cs2.git
cd auto-cs2
pip install -r requirements.txt

# Either drop a .env file with your key (preferred):
cp .env.example .env
# then edit .env and set STEAM_API_KEY=your_key_here

# Or export it for this shell only:
export STEAM_API_KEY=your_key_here   # https://steamcommunity.com/dev/apikey

python main.py
```

On Windows / PowerShell:

```powershell
Copy-Item .env.example .env
# edit .env and set STEAM_API_KEY=your_key_here
python main.py

# or, ad-hoc for the current shell:
$env:STEAM_API_KEY = "your_key_here"
python main.py
```

Then open <http://localhost:8080> in your browser.

`STEAM_API_KEY` is read from the process environment first; if it is not set,
`main.py` falls back to a `.env` file next to it (loaded with
[`python-dotenv`](https://pypi.org/project/python-dotenv/)). Real `.env` files
are gitignored — only `.env.example` is committed.

## How it works

- The frontend is a single `static/index.html` page (vanilla JS + Tailwind via
  CDN, JSZip via CDN). No bundler, no Node.
- The backend is a single `main.py` FastAPI app that exposes:
  - `POST /api/steam/avatars` — resolves a mixed list of SteamIDs / profile
    URLs / vanity names and returns the matching player summaries.
  - `GET /api/proxy/image` — host-whitelisted image proxy used by the ZIP
    download flow to avoid CORS.
  - `POST /api/demo/parse` — uploads a `.dem`, parses it with
    [`demoparser2`](https://github.com/LaihoE/demoparser), and returns a
    structured JSON summary.
  - `GET /api/health` — quick status check.

## Notes

- `.dem` files are written to `uploads/` only for the duration of a parse and
  are deleted immediately afterwards. The `uploads/` directory is gitignored.
- Max upload size is **300&nbsp;MB**.
- The image proxy will only fetch from the official Steam avatar CDNs.
- `STEAM_API_KEY` is read from the process environment, with `.env` as a
  fallback. If neither is set, the avatar endpoint returns a 500 with a hint,
  but the demo parser still works.
- The image proxy follows redirects manually (max 5 hops) and re-checks the
  host against the whitelist on every hop, so an upstream redirect to
  `127.0.0.1` / `169.254.169.254` / private RFC1918 ranges is rejected with
  HTTP 400.

## License

MIT — see [LICENSE](./LICENSE).
