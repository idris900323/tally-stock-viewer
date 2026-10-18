"""Which job this machine has. Stdlib only, no side effects on import, so every
entry point can call it FIRST -- before binding a port, creating a folder,
opening mappings.db or importing the heavy app modules.

MACHINE_ROLE comes from the environment, else from the project's .env:
  unset / "full"  the web app may run here (Render, local legacy PC) -- today's behavior
  "feeder"        this PC only runs feeder.py; every web-app entry point refuses

Anything else is treated as a configuration error and refused, rather than
guessing which way a typo should go.
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
REFUSAL = ("This machine is set to feeder-only; "
           "set MACHINE_ROLE=full in .env to run the web app")
_cached_role = None


def _role_from_env_file():
    value = None
    try:
        with open(os.path.join(_HERE, ".env"), "r", encoding="utf-8-sig") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:].lstrip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, raw = line.partition("=")
                if key.strip() != "MACHINE_ROLE":
                    continue
                raw = raw.strip()
                if raw[:1] in ("'", '"') and raw.count(raw[0]) >= 2:
                    raw = raw[1:raw.index(raw[0], 1)]
                else:
                    raw = raw.split(" #", 1)[0].strip()
                value = raw  # last one wins, like python-dotenv
    except OSError:
        pass
    return value


def get_role():
    """'full', 'feeder', or the raw (invalid) value. Cached for the process."""
    global _cached_role
    if _cached_role is None:
        value = os.environ.get("MACHINE_ROLE")
        if value is None:
            value = _role_from_env_file()
        value = (value or "").strip().lower()
        _cached_role = value or "full"
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
        message = f"Invalid MACHINE_ROLE={role!r} in the environment/.env; use 'feeder' or 'full'"
    line = f"{entry_name}: {message}"
    try:
        print(line, file=sys.stderr if sys.stderr else None)
    except Exception:
        pass  # pythonw has no console
    _log_line(line)
    sys.exit(3)


def refuse_db_access_if_feeder():
    """Last line of defence inside database.py: a feeder machine never opens
    or creates mappings.db, whatever code path got that far."""
    if is_feeder():
        _log_line("blocked a mappings.db access on a feeder-only machine")
        raise RuntimeError("mappings.db access refused: this machine is set to feeder-only (MACHINE_ROLE=feeder)")
