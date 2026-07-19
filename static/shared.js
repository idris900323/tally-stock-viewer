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
