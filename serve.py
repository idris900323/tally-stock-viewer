print("[DIAGNOSTIC] serve.py started, before any imports", flush=True)

import os

from waitress import serve
from app import app, start_background_startup_tasks
import logging

print("[DIAGNOSTIC] 'from app import app' completed (app.py module-level code finished)", flush=True)

# DIAGNOSTIC: confirm exactly what Render's environment actually provides
# for PORT, before any fallback logic is applied.
print(f"[DIAGNOSTIC] raw os.environ.get('PORT') = {os.environ.get('PORT')!r}", flush=True)

# Render (and other PaaS hosts) assign the port via $PORT and require
# binding to it; the office PC never sets this, so it keeps using 5000.
host = "0.0.0.0"
port = int(os.environ.get("PORT", "5000"))

try:
    log = logging.getLogger("waitress")
    log.info(f"Starting waitress on port {port}")
except Exception:
    pass

# app.py's data load / image scan / recurring stock export are gated behind
# `if __name__ == "__main__":`, which never runs when this module does
# `from app import app` — production (launched via serve.py) was therefore
# never starting the background export timer at all. Start it explicitly.
start_background_startup_tasks()

print(f"[DIAGNOSTIC] start_background_startup_tasks() returned (it only starts threads, "
      f"doesn't block, so this should print almost immediately)", flush=True)

# DIAGNOSTIC: unmissable, unconditional print (not routed through the
# logging module) so this is guaranteed to show up in Render's raw deploy
# logs right before the bind actually happens.
print(f"[DIAGNOSTIC] About to bind waitress to host={host!r} port={port!r}", flush=True)

# NOTE: waitress.serve() itself logs "Serving on http://{host}:{port}" via
# logging.getLogger("waitress").info(...), NOT via print() -- so with only
# a file handler attached to the root logger, that message never reaches
# this console either. serve() also never returns in the success case (it
# blocks forever running the server), so the line below normally never
# executes -- its absence, combined with every line above it having
# printed, is itself the confirmation that we got all the way into
# serve.run() and it's just blocking as expected, not stuck earlier.
# asyncore_use_poll: switches waitress's connection-handling loop from
# select() to poll(), which has no FD_SETSIZE-style ceiling (select()'s is
# 1024 -- the exact "filedescriptor out of range in select()" crash from
# the OOM/fd-exhaustion investigation this was added for). Belt and braces
# only: it raises the ceiling this specific crash mode hits, it does not
# fix whatever is actually accumulating open files/sockets in the first
# place -- see get_resource_usage()/start_resource_monitor() in app.py for
# the actual leak-finding instrumentation.
# threads: lowered from 8 to 4 (default) as part of the memory-leak
# follow-up investigation -- each waitress worker thread can independently
# be running a full-size (~3000x4000) share-image decode at once (see
# _SHARE_IMAGE_BUILD_SEMAPHORE in app.py, which now caps that specific
# work at 2 concurrent builds regardless of thread count), but every other
# request type still gets a thread of its own to run in, and this container
# only has 512MB to share across however many are live simultaneously.
# Fewer threads means a smaller worst case across the OTHER (non-share-
# image) endpoints too, at the cost of a slightly smaller ceiling on truly
# concurrent unrelated requests -- a reasonable trade on a single small
# instance that was getting OOM-killed under real daytime traffic. Made
# configurable (WAITRESS_THREADS) rather than a second hardcoded value, so
# this can be tuned per deployment (e.g. raised again if a future change to
# the share-image semaphore's own wait behavior changes this trade-off)
# without another code change.
threads = int(os.environ.get("WAITRESS_THREADS", "4"))
serve(app, host=host, port=port, threads=threads, asyncore_use_poll=True)

print("[DIAGNOSTIC] serve() returned -- this should only happen if the server stopped", flush=True)
