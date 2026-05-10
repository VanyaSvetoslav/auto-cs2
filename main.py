"""CS2 Toolkit — local FastAPI app with two tools.

1. Steam Avatar Fetcher — resolves SteamID64s, vanity URLs, profile URLs and
   fetches avatars / persona names from the Steam Web API.
2. CS2 .dem Multitool — parses a Counter-Strike 2 demo with `demoparser2` and
   returns header info, the round-by-round result, and a per-player K/D +
   crosshair code summary.

Run with:

    pip install -r requirements.txt
    # Either: export STEAM_API_KEY=...   (https://steamcommunity.com/dev/apikey)
    # Or:     drop a .env file next to main.py with STEAM_API_KEY=...
    python main.py

Environment variables (also read from .env):

    STEAM_API_KEY  Steam Web API key. Required for /api/steam/avatars.
    HOST           Bind interface, default 0.0.0.0. Use 127.0.0.1 to
                   restrict to localhost.
    PORT           Bind port, default 8080.
"""

from __future__ import annotations

import asyncio
import errno
import math
import os
import re
import secrets
import socket
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
UPLOAD_DIR = ROOT / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Load `.env` from the project root if present. Existing process env wins.
load_dotenv(ROOT / ".env", override=False)

STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "").strip()
MAX_DEMO_BYTES = 300 * 1024 * 1024  # 300 MB
ALLOWED_IMAGE_HOSTS = {
    "avatars.akamaihd.net",
    "avatars.steamstatic.com",
    "steamcdn-a.akamaihd.net",
    "media.steampowered.com",
    "community.cloudflare.steamstatic.com",
    "community.akamai.steamstatic.com",
    "cdn.akamai.steamstatic.com",
    "cdn.cloudflare.steamstatic.com",
}

STEAMID64_RE = re.compile(r"^7656119\d{10}$")
PROFILES_RE = re.compile(
    r"steamcommunity\.com/profiles/(7656119\d{10})", re.IGNORECASE
)
VANITY_RE = re.compile(r"steamcommunity\.com/id/([A-Za-z0-9_-]+)", re.IGNORECASE)

MAX_PROXY_REDIRECTS = 5


def _is_allowed_image_host(host: str) -> bool:
    """True if `host` is one of `ALLOWED_IMAGE_HOSTS` or a subdomain."""
    if not host:
        return False
    h = host.lower().split(":", 1)[0]
    if h in ALLOWED_IMAGE_HOSTS:
        return True
    return any(h.endswith("." + a) for a in ALLOWED_IMAGE_HOSTS)


app = FastAPI(title="CS2 Toolkit", version="1.0.0")


class AvatarRequest(BaseModel):
    input: str


def _split_inputs(raw: str) -> list[str]:
    """Split on commas / whitespace / newlines; drop empties."""
    return [p.strip() for p in re.split(r"[\s,]+", raw) if p.strip()]


def _classify(token: str) -> tuple[str, str]:
    """Return (kind, value) where kind is 'steamid' or 'vanity'."""
    if STEAMID64_RE.match(token):
        return "steamid", token

    if "steamcommunity.com" in token.lower() or token.startswith("http"):
        m = PROFILES_RE.search(token)
        if m:
            return "steamid", m.group(1)
        m = VANITY_RE.search(token)
        if m:
            return "vanity", m.group(1)
        # Fallback: take last path segment
        try:
            path = urlparse(token).path.strip("/")
            last = path.rsplit("/", 1)[-1]
            if STEAMID64_RE.match(last):
                return "steamid", last
            if last:
                return "vanity", last
        except Exception:
            pass

    return "vanity", token


async def _resolve_vanity(client: httpx.AsyncClient, vanity: str) -> str | None:
    url = "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"
    try:
        r = await client.get(
            url,
            params={"key": STEAM_API_KEY, "vanityurl": vanity},
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json().get("response", {})
        if data.get("success") == 1 and data.get("steamid"):
            return str(data["steamid"])
    except Exception:
        return None
    return None


async def _fetch_summaries(
    client: httpx.AsyncClient, steamids: list[str]
) -> list[dict[str, Any]]:
    if not steamids:
        return []
    out: list[dict[str, Any]] = []
    url = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/"
    for i in range(0, len(steamids), 100):
        chunk = steamids[i : i + 100]
        r = await client.get(
            url,
            params={"key": STEAM_API_KEY, "steamids": ",".join(chunk)},
            timeout=20.0,
        )
        r.raise_for_status()
        players = r.json().get("response", {}).get("players", [])
        out.extend(players)
    return out


@app.post("/api/steam/avatars")
async def fetch_avatars(payload: AvatarRequest) -> JSONResponse:
    if not STEAM_API_KEY:
        raise HTTPException(
            status_code=500,
            detail=(
                "STEAM_API_KEY is not set. Get one at "
                "https://steamcommunity.com/dev/apikey and export it before "
                "starting the server."
            ),
        )

    tokens = _split_inputs(payload.input or "")
    if not tokens:
        raise HTTPException(status_code=400, detail="No input provided.")
    if len(tokens) > 500:
        raise HTTPException(
            status_code=400,
            detail="Too many inputs (max 500 per request).",
        )

    direct_ids: list[str] = []
    vanity_tokens: list[str] = []
    for t in tokens:
        kind, value = _classify(t)
        if kind == "steamid":
            direct_ids.append(value)
        else:
            vanity_tokens.append(value)

    async with httpx.AsyncClient() as client:
        resolved: list[str | None] = []
        if vanity_tokens:
            resolved = await asyncio.gather(
                *[_resolve_vanity(client, v) for v in vanity_tokens]
            )

        all_ids: list[str] = []
        seen: set[str] = set()
        for sid in direct_ids:
            if sid not in seen:
                seen.add(sid)
                all_ids.append(sid)
        for sid in resolved:
            if sid and sid not in seen:
                seen.add(sid)
                all_ids.append(sid)

        unresolved = [
            v for v, sid in zip(vanity_tokens, resolved or []) if not sid
        ]

        try:
            players = await _fetch_summaries(client, all_ids)
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=502, detail=f"Steam API error: {e}"
            ) from e

    by_id = {p.get("steamid"): p for p in players}
    results: list[dict[str, Any]] = []
    for sid in all_ids:
        p = by_id.get(sid)
        if p:
            results.append(
                {
                    "steamid": p.get("steamid"),
                    "personaname": p.get("personaname") or "",
                    "avatarfull": p.get("avatarfull") or "",
                    "profileurl": p.get("profileurl") or "",
                }
            )
        else:
            results.append(
                {
                    "steamid": sid,
                    "personaname": "",
                    "avatarfull": "",
                    "profileurl": "",
                    "error": "not_found_or_private",
                }
            )

    return JSONResponse(
        {
            "results": results,
            "unresolved_vanities": unresolved,
            "count": len(results),
        }
    )


@app.get("/api/proxy/image")
async def proxy_image(url: str) -> Response:
    try:
        parsed = urlparse(url)
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid URL") from e

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Invalid URL")

    if not _is_allowed_image_host(parsed.netloc):
        raise HTTPException(status_code=400, detail="Host not allowed.")

    # Follow redirects manually so the host whitelist is re-checked on every
    # hop. This blocks SSRF where an allowed Steam CDN host redirects to
    # 127.0.0.1, 169.254.169.254 (cloud metadata), or any other internal
    # address.
    current_url = url
    async with httpx.AsyncClient(follow_redirects=False) as client:
        for _ in range(MAX_PROXY_REDIRECTS + 1):
            try:
                r = await client.get(current_url, timeout=20.0)
            except httpx.HTTPError as e:
                raise HTTPException(status_code=502, detail=str(e)) from e

            if r.is_redirect:
                next_url = r.headers.get("location", "")
                if not next_url:
                    raise HTTPException(
                        status_code=502, detail="Redirect with no Location header."
                    )
                next_url = str(httpx.URL(current_url).join(next_url))
                next_parsed = urlparse(next_url)
                if next_parsed.scheme not in {"http", "https"}:
                    raise HTTPException(
                        status_code=502,
                        detail="Redirect to unsupported scheme.",
                    )
                if not _is_allowed_image_host(next_parsed.netloc):
                    raise HTTPException(
                        status_code=400,
                        detail="Redirect to non-allowed host blocked.",
                    )
                current_url = next_url
                continue

            try:
                r.raise_for_status()
            except httpx.HTTPError as e:
                raise HTTPException(status_code=502, detail=str(e)) from e

            content_type = r.headers.get("content-type", "image/jpeg")
            return Response(content=r.content, media_type=content_type)

    raise HTTPException(status_code=502, detail="Too many redirects.")


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    try:
        if isinstance(value, float) and math.isnan(value):
            return ""
    except Exception:
        pass
    return str(value)


def _parse_demo_sync(path: Path) -> dict[str, Any]:
    """CPU-bound parsing routine. Run inside a thread."""
    from demoparser2 import DemoParser  # type: ignore

    parser = DemoParser(str(path))

    # ---- Header --------------------------------------------------------
    header: dict[str, Any] = {}
    try:
        raw_header = parser.parse_header() or {}
        header = {k: _safe_str(v) for k, v in raw_header.items()}
    except Exception as e:
        header = {"error": f"header parse failed: {e}"}

    # ---- Player roster (steamid -> name, last seen team) --------------
    roster: dict[str, dict[str, Any]] = {}
    try:
        info = parser.parse_player_info()
        for _, row in info.iterrows():
            sid = _safe_str(row.get("steamid"))
            if not sid or sid == "0":
                continue
            roster.setdefault(
                sid,
                {
                    "steamid": sid,
                    "name": _safe_str(row.get("name")),
                    "team": "",
                    "kills": 0,
                    "deaths": 0,
                    "crosshair_code": "",
                },
            )
    except Exception:
        pass

    # ---- Crosshair codes ----------------------------------------------
    try:
        candidate_ticks = [128, 256, 512, 1024, 2048, 64, 32, 16, 8, 1]
        cdf = None
        for t in candidate_ticks:
            try:
                cdf = parser.parse_ticks(["crosshair_code"], ticks=[t])
                if cdf is not None and len(cdf) > 0:
                    break
            except Exception:
                cdf = None
        if cdf is None or len(cdf) == 0:
            try:
                cdf = parser.parse_ticks(["crosshair_code"])
            except Exception:
                cdf = None
        if cdf is not None and len(cdf) > 0:
            for _, row in cdf.iterrows():
                sid = _safe_str(row.get("steamid"))
                code = _safe_str(row.get("crosshair_code"))
                name = _safe_str(row.get("name"))
                if not sid or sid == "0":
                    continue
                entry = roster.setdefault(
                    sid,
                    {
                        "steamid": sid,
                        "name": name,
                        "team": "",
                        "kills": 0,
                        "deaths": 0,
                        "crosshair_code": "",
                    },
                )
                if code and code.lower() != "nan":
                    entry["crosshair_code"] = code
                if name and not entry.get("name"):
                    entry["name"] = name
    except Exception:
        pass

    # ---- Round results -------------------------------------------------
    ct_wins = 0
    t_wins = 0
    total_rounds = 0
    try:
        rdf = parser.parse_event(
            "round_end", other=["winner", "reason", "total_rounds_played"]
        )
        if rdf is not None and len(rdf) > 0:
            for _, row in rdf.iterrows():
                w = row.get("winner")
                try:
                    w_int = int(w) if w is not None else None
                except Exception:
                    w_int = None
                if w_int == 3:
                    ct_wins += 1
                elif w_int == 2:
                    t_wins += 1
            total_rounds = ct_wins + t_wins
    except Exception:
        pass

    # ---- Kills / deaths ------------------------------------------------
    try:
        kdf = parser.parse_event(
            "player_death",
            player=["team_name"],
            other=["total_rounds_played"],
        )
        if kdf is not None and len(kdf) > 0:
            kill_counts: dict[str, int] = defaultdict(int)
            death_counts: dict[str, int] = defaultdict(int)
            last_team: dict[str, str] = {}
            for _, row in kdf.iterrows():
                attacker = _safe_str(row.get("attacker_steamid"))
                attacker_name = _safe_str(row.get("attacker_name"))
                victim = _safe_str(row.get("user_steamid"))
                victim_name = _safe_str(row.get("user_name"))
                victim_team = _safe_str(row.get("user_team_name")) or _safe_str(
                    row.get("team_name")
                )

                if attacker and attacker != "0" and attacker != victim:
                    kill_counts[attacker] += 1
                    if attacker_name:
                        roster.setdefault(
                            attacker,
                            {
                                "steamid": attacker,
                                "name": attacker_name,
                                "team": "",
                                "kills": 0,
                                "deaths": 0,
                                "crosshair_code": "",
                            },
                        )
                if victim and victim != "0":
                    death_counts[victim] += 1
                    if victim_name:
                        roster.setdefault(
                            victim,
                            {
                                "steamid": victim,
                                "name": victim_name,
                                "team": "",
                                "kills": 0,
                                "deaths": 0,
                                "crosshair_code": "",
                            },
                        )
                    if victim_team:
                        last_team[victim] = victim_team

            for sid, n in kill_counts.items():
                roster.setdefault(
                    sid,
                    {
                        "steamid": sid,
                        "name": "",
                        "team": "",
                        "kills": 0,
                        "deaths": 0,
                        "crosshair_code": "",
                    },
                )["kills"] = n
            for sid, n in death_counts.items():
                roster.setdefault(
                    sid,
                    {
                        "steamid": sid,
                        "name": "",
                        "team": "",
                        "kills": 0,
                        "deaths": 0,
                        "crosshair_code": "",
                    },
                )["deaths"] = n
            for sid, team in last_team.items():
                if team and not roster[sid].get("team"):
                    roster[sid]["team"] = team
    except Exception:
        pass

    players = sorted(
        roster.values(), key=lambda p: (-int(p.get("kills") or 0), p.get("name") or "")
    )

    if ct_wins > t_wins:
        winner_side = "CT"
    elif t_wins > ct_wins:
        winner_side = "T"
    else:
        winner_side = "DRAW" if total_rounds else ""

    return {
        "header": header,
        "match_result": {
            "ct_wins": ct_wins,
            "t_wins": t_wins,
            "total_rounds": total_rounds,
            "winner_side": winner_side,
        },
        "players": players,
    }


@app.post("/api/demo/parse")
async def parse_demo(file: UploadFile = File(...)) -> JSONResponse:
    if not file.filename or not file.filename.lower().endswith(".dem"):
        raise HTTPException(status_code=400, detail="Expected a .dem file.")

    safe_id = secrets.token_hex(8)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", file.filename)
    dest = UPLOAD_DIR / f"{safe_id}_{safe_name}"

    written = 0
    try:
        with dest.open("wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_DEMO_BYTES:
                    fh.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail="Demo too large (max 300 MB).",
                    )
                fh.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to save upload: {e}"
        ) from e

    try:
        result = await asyncio.to_thread(_parse_demo_sync, dest)
    except Exception as e:
        return JSONResponse(
            status_code=500, content={"error": f"Demo parsing failed: {e}"}
        )
    finally:
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass

    return JSONResponse(result)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "steam_api_key_set": bool(STEAM_API_KEY),
        "version": app.version,
    }


# Serve the SPA from /static and the index from /
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index(request: Request) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)


def _resolve_host_port() -> tuple[str, int]:
    host = os.environ.get("HOST", "0.0.0.0").strip() or "0.0.0.0"
    port_raw = os.environ.get("PORT", "8080").strip() or "8080"
    try:
        port = int(port_raw)
    except ValueError:
        print(f"Invalid PORT={port_raw!r}; falling back to 8080.")
        port = 8080
    if not (0 < port < 65536):
        print(f"PORT {port} out of range; falling back to 8080.")
        port = 8080
    return host, port


def _port_in_use(host: str, port: int) -> bool:
    """Return True if (host, port) cannot be bound right now."""
    candidates: list[tuple[int, str]] = []
    if host == "0.0.0.0":
        candidates.append((socket.AF_INET, host))
    elif host == "::":
        candidates.append((socket.AF_INET6, host))
    else:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            seen: set[tuple[int, str]] = set()
            for info in infos:
                key = (info[0], info[4][0])
                if key not in seen:
                    seen.add(key)
                    candidates.append(key)
        except socket.gaierror:
            return False

    for family, addr in candidates:
        try:
            s = socket.socket(family, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((addr, port))
            except OSError as e:
                if e.errno == errno.EADDRINUSE:
                    return True
                # Other errors (e.g. EACCES) — let uvicorn surface them.
                return False
            finally:
                s.close()
        except OSError:
            continue
    return False


def main() -> None:
    host, port = _resolve_host_port()
    display_host = "localhost" if host in {"0.0.0.0", "::"} else host

    if _port_in_use(host, port):
        print(
            f"Port {port} on {host} is already in use. Free it or pick another:\n"
            f"  sudo ss -tlnp | grep ':{port}'\n"
            f"  sudo lsof -iTCP:{port} -sTCP:LISTEN -n -P\n"
            f"  PORT={port + 1} python main.py"
        )
        sys.exit(1)

    print(f"CS2 Toolkit running at http://{display_host}:{port}")
    if host == "0.0.0.0":
        print("  (bound to all interfaces — set HOST=127.0.0.1 to restrict to localhost)")
    if STEAM_API_KEY:
        print("Steam API Key: SET")
    else:
        print("Steam API Key: NOT SET — avatar fetcher will return 500 errors.")
        print("  Get one at https://steamcommunity.com/dev/apikey, then either")
        print("  export STEAM_API_KEY=your_key_here   or put it in a `.env` file.")

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
