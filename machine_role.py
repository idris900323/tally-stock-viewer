"""Which job this machine has. Stdlib only, no side effects on import, so every
entry point can call it FIRST -- before binding a port, creating a folder,
opening mappings.db or importing the heavy app modules.

MACHINE_ROLE comes from the environment, else from the project's .env:
  unset / "full"  the web app may run here (Render, local legacy PC) -- today's behavior
  "feeder"        this PC only runs feeder.py; every web-app entry point refuses

FAIL CLOSED: anything that is not clearly "full" or "feeder" refuses to run the
web app -- an empty value, an unknown word, a malformed quote, conflicting
duplicate MACHINE_ROLE lines, or an .env that exists but cannot be read/decoded
(e.g. saved as UTF-16). A missing .env, or no MACHINE_ROLE line in it, is "full".

Read ONCE per process (cached): after the first call the check is a plain
module-variable compare -- no .env read, no file open, per call.
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
REFUSAL = ("This machine is set to feeder-only; "
           "set MACHINE_ROLE=full in .env to run the web app")
_cached_role = None


def _parse_value(raw):
    """The value part of a MACHINE_ROLE=... line, or None if malformed."""
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        if end == -1:
            return None  # unmatched quote
        rest = raw[end + 1:].strip()
        if rest and not rest.startswith("#"):
            return None  # junk after the closing quote
        return raw[1:end]
    return raw.split(" #", 1)[0].strip()


def _role_from_env_file():
    """None (no .env / no MACHINE_ROLE line), the role word, or a marker
    string that is not 'full'/'feeder' (so it fails closed)."""
    values = []
    try:
        with open(os.path.join(_HERE, ".env"), "r", encoding="utf-8-sig") as handle:
            for line in handle:
                line = line.strip()
                if line.lower().startswith("export "):
                    line = line[7:].lstrip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, raw = line.partition("=")
                # Case-insensitive key: python-dotenv sets os.environ, which on
                # Windows is case-insensitive, so "machine_role=" would take
                # effect there and must not be missed here.
                if key.strip().upper() != "MACHINE_ROLE":
                    continue
                values.append(_parse_value(raw))
    except FileNotFoundError:
        return None
    except Exception:
        return "unreadable .env"
    if not values:
        return None
    if any(v is None for v in values):
        return "malformed MACHINE_ROLE line"
    if len({v.lower() for v in values}) > 1:
        return "conflicting MACHINE_ROLE lines"
    return values[0]


def get_role():
    """'full', 'feeder', or an invalid value/marker. Cached for the process."""
    global _cached_role
    if _cached_role is None:
        value = os.environ.get("MACHINE_ROLE")
        if value is None:
            value = _role_from_env_file()
        if value is None:
            _cached_role = "full"
        else:
            value = value.strip().lower()
            _cached_role = value if value else "empty MACHINE_ROLE"
    return _cached_role


def is_feeder():
    return get_role() == "feeder"


def _log_line(message):
    # Append to an EXISTING logs/ folder only -- never create one.
    try:
        log_dir = os.path.join(_HERE, "logs")
        if os.path.isdir(log_dir):
            with open(os.path.join(log_dir, "role_guard.log"), "a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except Exception:
        pass


def refuse_if_not_full(entry_name):
    """Call at the very top of a web-app entry point. Exits non-zero (3) with
    one clear line if this machine must not run the web app."""
    role = get_role()
    if role == "full":
        return
    if role == "feeder":
        message = REFUSAL
    else:
        message = f"Invalid MACHINE_ROLE ({role}); use exactly 'feeder' or 'full' in .env"
    line = f"{entry_name}: {message}"
    try:
        print(line, file=sys.stderr if sys.stderr else None)
    except Exception:
        pass  # pythonw has no console
    _log_line(line)
    sys.exit(3)


def refuse_db_access_if_feeder():
    """Last line of defence inside database.py. Runs on every connection, so the
    hot path is one module-variable compare (the role was read at import)."""
    if _cached_role is None:
        get_role()
    if _cached_role != "full":
        _log_line("blocked a mappings.db access on a feeder-only/invalid-role machine")
        raise RuntimeError("mappings.db access refused: this machine is not set to run the web app (MACHINE_ROLE)")
