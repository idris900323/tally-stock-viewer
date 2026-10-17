// "Stock updated, tap to refresh" -- keeps a long-open page honest.
//
// Polls /last_update (the poll the pages already make), which carries
// data_version (and admin_version for admins). When it differs from what the
// page last loaded, a small non-blocking notice appears; tapping it runs the
// page's own in-place refresh. The view itself is never touched automatically.
//
// Polling stops while the tab is hidden and resumes with an immediate check
// on visibilitychange / pageshow / online, because browsers throttle timers
// in background tabs. No logging, no extra threads or storage.
//
// Served as its own file (not folded into shared.js) because the service
// worker caches shared.js cache-first; this file is fetched network-first.
(function () {
    "use strict";

    let opts = null;
    let applied = { data: "", admin: "" };
    let latest = { data: "", admin: "" };
    let timer = null;
    let inFlight = false;
    let noticeEl = null;
    let noticeTextEl = null;
    let flashTimer = null;

    // Each page says which version(s) it shows: "data" (stock, categories,
    // mappings, images) and/or "admin" (the accounts list; admin sessions only).
    function isStale() {
        const watch = opts.watch || ["data"];
        return watch.some(kind => Boolean(latest[kind] && applied[kind] && latest[kind] !== applied[kind]));
    }

    function ensureNotice() {
        if (noticeEl) {
            return;
        }
        const style = document.createElement("style");
        style.textContent =
            "#dataSyncNotice{position:fixed;left:50%;bottom:16px;transform:translateX(-50%);z-index:2147483000;" +
            "max-width:calc(100vw - 24px);display:none}" +
            "#dataSyncNotice button{font:600 14px Arial,sans-serif;background:#1f6feb;color:#fff;border:0;" +
            "border-radius:999px;padding:10px 18px;box-shadow:0 4px 14px rgba(0,0,0,.25);cursor:pointer}";
        document.head.appendChild(style);
        noticeEl = document.createElement("div");
        noticeEl.id = "dataSyncNotice";
        noticeEl.setAttribute("role", "status");
        noticeEl.setAttribute("aria-live", "polite");
        const button = document.createElement("button");
        button.type = "button";
        noticeTextEl = button;
        button.addEventListener("click", onNoticeClick);
        noticeEl.appendChild(button);
        document.body.appendChild(noticeEl);
    }

    function noticeText() {
        return (opts && opts.label) || "Stock updated · tap to refresh";
    }

    function render() {
        if (!opts) {
            return;
        }
        if (isStale()) {
            ensureNotice();
            if (!flashTimer) {
                noticeTextEl.textContent = noticeText();
            }
            noticeEl.style.display = "block";
        } else if (noticeEl) {
            noticeEl.style.display = "none";
        }
    }

    function busy() {
        try {
            return Boolean(opts.isBusy && opts.isBusy());
        } catch (error) {
            return false;
        }
    }

    async function onNoticeClick() {
        if (busy()) {
            // Don't touch the view; say why, then restore the normal text.
            noticeTextEl.textContent = "Finish what you're doing, then tap again";
            clearTimeout(flashTimer);
            flashTimer = setTimeout(() => {
                flashTimer = null;
                render();
            }, 2500);
            return;
        }
        const target = { data: latest.data, admin: latest.admin };
        noticeTextEl.disabled = true;
        try {
            await opts.refresh();
            applied = target;
        } catch (error) {
            // Leave the notice up so the user can retry.
        } finally {
            noticeTextEl.disabled = false;
            render();
        }
    }

    function observe(data) {
        if (!opts || !data || !data.data_version) {
            return;
        }
        latest = { data: data.data_version, admin: data.admin_version || latest.admin };
        if (!applied.data) {
            applied = { data: latest.data, admin: latest.admin };
        } else if (!applied.admin && latest.admin) {
            applied.admin = latest.admin;
        }
        render();
    }

    async function defaultPoll() {
        const res = await fetch("/last_update", { cache: "no-store" });
        if (res.ok) {
            observe(await res.json());
        }
    }

    async function poll() {
        if (inFlight || document.hidden) {
            return;
        }
        inFlight = true;
        try {
            await (opts.poll || defaultPoll)();
        } catch (error) {
            // Best-effort; the next tick catches up.
        } finally {
            inFlight = false;
        }
    }

    function schedule() {
        clearTimeout(timer);
        timer = null;
        if (document.hidden) {
            return;
        }
        timer = setTimeout(async () => {
            await poll();
            schedule();
        }, opts.intervalMs);
    }

    async function checkNow() {
        if (document.hidden) {
            return;
        }
        clearTimeout(timer);
        await poll();
        schedule();
    }

    function onVisibility() {
        if (document.hidden) {
            clearTimeout(timer);
            timer = null;
        } else {
            checkNow();
        }
    }

    window.DataSync = {
        // opts: { version, adminVersion, watch: ["data"] | ["admin"], intervalMs, isBusy(), refresh(),
        //         poll() (optional: replaces the default /last_update fetch --
        //         must call DataSync.observe(json) itself), label }
        init(options) {
            opts = options;
            applied = { data: options.version || "", admin: options.adminVersion || "" };
            latest = { data: applied.data, admin: applied.admin };
            document.addEventListener("visibilitychange", onVisibility);
            window.addEventListener("pageshow", checkNow);
            window.addEventListener("online", checkNow);
            schedule();
        },
        observe,
        // Call right after a normal load of the data, with the version that
        // was current BEFORE the fetch started (current()).
        current() {
            return { data: latest.data, admin: latest.admin };
        },
        markApplied(version) {
            if (!opts || !version) {
                return;
            }
            applied = { data: version.data || applied.data, admin: version.admin || applied.admin };
            render();
        },
        checkNow,
    };
})();
