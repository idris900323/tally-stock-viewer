// Shared helpers used across index.html, accounts.html, system.html,
// train.html, and bulk_match.html. Previously each template kept its own
// copy of these two functions; system.html's escapeHtml() had drifted to
// only escape & < > (missing " and '), which this consolidation fixes.

function escapeHtml(value) {
    return String(value == null ? "" : value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll("\"", "&quot;")
        .replaceAll("'", "&#39;");
}

// Every timestamp the backend sends to these pages (report/last-login/scan
// times, etc.) is a naive ISO string with no UTC offset, e.g.
// "2026-09-27T12:00:00" -- see app.py/database.py, which stamp these with
// datetime.now() and, in production, run on Render's server clock (UTC).
// That used to be the office PC's clock (IST) instead, and because the
// string carries no "Z"/offset, `new Date(...)` on it is ambiguous: per
// spec, a date-time string with no timezone designator is parsed as the
// *browser's* local time, not UTC. Moving the server from an IST machine
// to a UTC one silently changed what these pages displayed even though
// nothing about the underlying value changed. toISTDate() forces the UTC
// reading explicitly, and formatIST()/formatISTFriendly() render it in
// Asia/Kolkata regardless of the viewer's own device timezone -- this is
// purely a display fix; the ISO value itself is never altered or re-sent.
function toISTDate(isoString) {
    if (!isoString) {
        return null;
    }
    const hasOffset = /[zZ]|[+-]\d{2}:?\d{2}$/.test(isoString);
    const date = new Date(hasOffset ? isoString : `${isoString}Z`);
    return Number.isNaN(date.getTime()) ? null : date;
}

function istParts(isoString) {
    const date = toISTDate(isoString);
    if (!date) {
        return null;
    }
    const parts = new Intl.DateTimeFormat("en-GB", {
        timeZone: "Asia/Kolkata",
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hourCycle: "h23",
    }).formatToParts(date).reduce((acc, p) => { acc[p.type] = p.value; return acc; }, {});
    return parts;
}

// "27/09/2026 22:38:05 IST" -- same dd/mm/yyyy style already used across
// the app, now explicitly labelled so it can't be misread as UTC again.
function formatIST(isoString) {
    const parts = istParts(isoString);
    if (!parts) {
        return "-";
    }
    return `${parts.day}/${parts.month}/${parts.year} ${parts.hour}:${parts.minute}:${parts.second} IST`;
}

// "27 Sep 2026, 10:38 PM IST" -- for friendlier admin-facing labels
// (accounts last-login, etc.) that previously used 12-hour AM/PM text.
function formatISTFriendly(isoString) {
    const parts = istParts(isoString);
    if (!parts) {
        return null;
    }
    const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    let hours = Number(parts.hour) % 24;
    const ampm = hours >= 12 ? "PM" : "AM";
    hours = hours % 12 || 12;
    return `${parts.day} ${months[Number(parts.month) - 1]} ${parts.year}, ${hours}:${parts.minute} ${ampm} IST`;
}

// Display-only cleanup of a trailing Tally shelf-location code (e.g.
// "* M-20", "****H-4****", "***G-4)"). Mirrors utils/normalize.py's
// strip_shelf_code_for_display() -- never use this on a value sent to the
// backend, only on text shown to the user.
function stripShelfCodeForDisplay(name) {
    const text = String(name || "");
    const idx = text.indexOf("*");
    if (idx === -1) {
        return text.trim();
    }
    let before = text.slice(0, idx);
    before = before.replace(/[\s.\-,]+$/, "");
    while (before.endsWith(")") && (before.split("(").length - 1) < (before.split(")").length - 1)) {
        before = before.slice(0, -1).replace(/\s+$/, "");
    }
    const cleaned = before.trim();
    return cleaned || text.trim();
}
