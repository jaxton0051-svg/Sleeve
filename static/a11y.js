/* Shared keyboard and focus behavior for the app's custom dialogs. */
(function () {
    "use strict";

    const dialogs = new Map();
    const focusableSelector = [
        "a[href]", "button:not([disabled])", "input:not([disabled]):not([type=hidden])",
        "select:not([disabled])", "textarea:not([disabled])", "[tabindex]:not([tabindex='-1'])",
    ].join(",");

    function focusables(dialog) {
        return Array.from(dialog.querySelectorAll(focusableSelector)).filter((el) =>
            !el.hidden && el.getAttribute("aria-hidden") !== "true" && el.getClientRects().length);
    }

    function syncDialog(dialog) {
        if (!dialog.hidden && !dialogs.has(dialog)) {
            dialogs.set(dialog, document.activeElement);
            requestAnimationFrame(() => {
                const first = focusables(dialog)[0] || dialog;
                if (!dialog.hasAttribute("tabindex")) dialog.setAttribute("tabindex", "-1");
                first.focus({ preventScroll: true });
            });
        } else if (dialog.hidden && dialogs.has(dialog)) {
            const previous = dialogs.get(dialog);
            dialogs.delete(dialog);
            if (previous && previous.isConnected && typeof previous.focus === "function") {
                requestAnimationFrame(() => previous.focus({ preventScroll: true }));
            }
        }
    }

    document.addEventListener("DOMContentLoaded", () => {
        document.querySelectorAll(".modal[role='dialog']").forEach((dialog) => {
            new MutationObserver(() => syncDialog(dialog)).observe(dialog, { attributes: true, attributeFilter: ["hidden"] });
            syncDialog(dialog);
        });

        document.addEventListener("keydown", (event) => {
            const open = Array.from(document.querySelectorAll(".modal[role='dialog']:not([hidden])"));
            const dialog = open[open.length - 1];
            if (!dialog) return;
            if (event.key === "Escape") {
                const close = dialog.querySelector("[data-modal-close]");
                if (close) { event.preventDefault(); close.click(); }
                return;
            }
            if (event.key !== "Tab") return;
            const items = focusables(dialog);
            if (!items.length) { event.preventDefault(); dialog.focus(); return; }
            const first = items[0];
            const last = items[items.length - 1];
            if (event.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
                event.preventDefault(); last.focus();
            } else if (!event.shiftKey && (document.activeElement === last || !dialog.contains(document.activeElement))) {
                event.preventDefault(); first.focus();
            }
        });
    });

    // Observe the page so dialogs added by the album-detail UI get the same behavior.
    new MutationObserver((records) => {
        for (const record of records) {
            for (const node of record.addedNodes) {
                if (!(node instanceof Element)) continue;
                if (node.matches(".modal[role='dialog']")) {
                    new MutationObserver(() => syncDialog(node)).observe(node, { attributes: true, attributeFilter: ["hidden"] });
                    syncDialog(node);
                }
                node.querySelectorAll?.(".modal[role='dialog']").forEach((dialog) => {
                    new MutationObserver(() => syncDialog(dialog)).observe(dialog, { attributes: true, attributeFilter: ["hidden"] });
                    syncDialog(dialog);
                });
            }
        }
    }).observe(document.documentElement, { childList: true, subtree: true });
})();
