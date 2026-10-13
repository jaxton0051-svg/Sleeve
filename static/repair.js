/* =====================================================================
   Sleeve — Repair tab (lyrics .lrc fixer)

   Public entry: initRepairPage() — dispatched by app.js when active_tab
   is "repair". Scans the user's music folder, shows broken .lrc files,
   re-fetches from LRCLIB on demand.
   ===================================================================== */
(function () {
    "use strict";

    const $ = (sel, root) => (root || document).querySelector(sel);
    const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
    const esc = (s) =>
        String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
            ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

    function fmtBytes(n) {
        if (!n) return "0 B";
        const u = ["B", "KB", "MB", "GB"];
        let i = 0; let v = n;
        while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
        return v.toFixed(v >= 10 ? 0 : 1) + " " + u[i];
    }

    async function api(path, opts) {
        opts = opts || {};
        if (opts.body && typeof opts.body !== "string") {
            opts.body = JSON.stringify(opts.body);
            opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
        }
        const res = await fetch(path, opts);
        const ct = res.headers.get("content-type") || "";
        const data = ct.includes("application/json") ? await res.json() : await res.text();
        if (!res.ok && (!data || data.ok === false)) {
            throw new Error((data && data.error) || res.statusText || "Request failed");
        }
        return data;
    }

    // ----------------------------- state -----------------------------

    let _scanState = null;     // last scan result
    let _busy = false;         // true while a scan / fix is in flight

    // State for the Manual lyrics modal.
    const _manual = {
        active: false,
        mp3: null,
        title: "",
        artist: "",
        picked: null,             // picked LRCLIB search hit
        uploadText: "",           // text read from the uploaded file
    };

    // State for the Deduper sub-panel.
    const _dedupe = {
        busy: false,
        scan: null,               // last scan response
        picked: new Set(),        // mp3 paths selected for bulk Delete
        lastResults: [],          // flattened list of (group, song) for skip/delete callbacks
    };

    // ----------------------------- entry -----------------------------

    function initRepairPage() {
        bindRepairTabs();
        const scanBtn = $("#repair-scan-btn");
        if (scanBtn && !scanBtn.dataset.wired) {
            scanBtn.dataset.wired = "1";
            scanBtn.addEventListener("click", runScan);
        }
        const bulkBtn = $("#repair-bulk-fix-btn");
        if (bulkBtn && !bulkBtn.dataset.wired) {
            bulkBtn.dataset.wired = "1";
            bulkBtn.addEventListener("click", runBulkFix);
        }
        bindManualModal();
        bindDedupe();
        bindRenamer();
        wireModalDismissers();
    }

    function bindRepairTabs() {
        const tabs = $$(".repair-tab[role='tab']");
        if (!tabs.length || tabs[0].dataset.wired) return;
        tabs.forEach((tab, index) => {
            tab.dataset.wired = "1";
            tab.addEventListener("click", () => activateRepairTab(tab));
            tab.addEventListener("keydown", (event) => {
                let next = null;
                if (event.key === "ArrowRight" || event.key === "ArrowDown") next = tabs[(index + 1) % tabs.length];
                if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = tabs[(index - 1 + tabs.length) % tabs.length];
                if (event.key === "Home") next = tabs[0];
                if (event.key === "End") next = tabs[tabs.length - 1];
                if (!next) return;
                event.preventDefault();
                activateRepairTab(next);
                next.focus();
            });
        });
    }

    function activateRepairTab(active) {
        $$(".repair-tab[role='tab']").forEach((tab) => {
            const selected = tab === active;
            tab.setAttribute("aria-selected", String(selected));
            tab.tabIndex = selected ? 0 : -1;
            tab.classList.toggle("is-active", selected);
            const panel = $("#" + tab.getAttribute("aria-controls"));
            if (panel) panel.hidden = !selected;
        });
    }

    // Close any open modal when its [data-modal-close] trigger is clicked.
    // Shared Escape and focus behavior lives in a11y.js. Wired once on Repair init because the Repair
    // tab can be opened without first visiting Automator (which is where
    // the automator.js copy of this handler lives).
    function wireModalDismissers() {
        if (wireModalDismissers._wired) return;
        wireModalDismissers._wired = true;
        document.addEventListener("click", (e) => {
            const closer = e.target.closest("[data-modal-close]");
            if (!closer) return;
            const m = closer.closest(".modal");
            if (m) closeModal("#" + m.id);
        });
    }

    // ----------------------------- scan ------------------------------

    async function runScan() {
        if (_busy) return;
        const scanBtn = $("#repair-scan-btn");
        const folderLine = $("#repair-folder-line");
        const results = $("#repair-results");
        const summary = $("#repair-summary");
        const bulkBtn = $("#repair-bulk-fix-btn");
        if (scanBtn) scanBtn.disabled = true;
        bulkBtn.disabled = true;
        _busy = true;
        summary.textContent = "Scanning\u2026";
        folderLine.textContent = "";
        results.innerHTML = '<p class="muted small">Scanning music folder\u2026</p>';
        try {
            const r = await api("/api/repair/scan");
            _scanState = r;
            if (!r.ok) {
                summary.textContent = "";
                results.innerHTML = `
                    <div class="repair-error">
                        <p>${esc(r.error || "scan failed")}</p>
                        <p class="muted small">Configure the music folder in Automator &rarr; Edit settings.</p>
                    </div>`;
                folderLine.textContent = r.music_folder ? `Scanned: ${esc(r.music_folder)}` : "";
                return;
            }
            folderLine.textContent = `Scanned: ${esc(r.music_folder)}`;
            renderResults(r);
        } catch (e) {
            summary.textContent = "";
            results.innerHTML = `<div class="repair-error"><p>${esc(e.message)}</p></div>`;
        } finally {
            _busy = false;
            if (scanBtn) scanBtn.disabled = false;
            updateBulkButton();
        }
    }

    function renderResults(r) {
        const summary = $("#repair-summary");
        const results = $("#repair-results");
        const c = r.counts || {};
        const noLrc = c.no_lrc || 0;
        summary.innerHTML =
            `<strong>${c.total || 0}</strong> MP3 file${(c.total || 0) === 1 ? "" : "s"} scanned &middot; ` +
            `<span class="repair-stat-broken">${c.broken || 0} broken</span> &middot; ` +
            `<span class="repair-stat-healthy">${c.healthy || 0} healthy</span>` +
            (noLrc ? ` &middot; <span class="muted small">${noLrc} no .lrc hidden</span>` : "");

        // Per user preference: hide songs with no .lrc entirely from the
        // Repair list. They are still scanned (so the count is accurate)
        // and still visible in the Files tab.
        const entries = (r.entries || []).filter((e) => (e.status || "") !== "no_lrc");
        if (!entries.length) {
            results.innerHTML = "<p class=\"muted small\">No MP3 files need lyric attention.</p>";
            return;
        }

        const rows = entries.map((e) => {
            const status = e.status || "error";
            const badgeClass = ({
                broken: "repair-badge-broken",
                no_lrc: "repair-badge-nolrc",
                healthy: "repair-badge-healthy",
                error: "repair-badge-error",
            })[status] || "repair-badge-error";
            const statusLabel = ({
                broken: "Broken",
                no_lrc: "No .lrc",
                healthy: "Healthy",
                error: "Error",
            })[status] || status;

            const isFixable = status === "broken";
            const cb = isFixable
                ? `<label class="repair-row-check"><input type="checkbox" data-repair-pick="${esc(e.mp3)}" checked></label>`
                : `<span class="repair-row-check repair-row-check-na">\u2014</span>`;
            const title = e.title ? esc(e.title) : "<em>no id3 title</em>";
            const artist = e.artist ? esc(e.artist) : "<em>no id3 artist</em>";
            const reason = e.reason ? `<span class="muted small">${esc(e.reason)}</span>` : "";
            const lrcInfo = e.lrc
                ? `${fmtBytes(e.size_lrc || 0)} &middot; ${esc(e.lrc_relpath || "")}`
                : "<em>missing</em>";

            return `
                <li class="repair-row repair-row-${status}" data-mp3="${esc(e.mp3)}" data-lrc="${esc(e.lrc)}">
                    ${cb}
                    <div class="repair-row-main">
                        <div class="repair-row-title"><strong>${title}</strong> &mdash; ${artist}</div>
                        <div class="repair-row-path muted small">${esc(e.mp3_relpath || "")}</div>
                        <div class="repair-row-meta muted small">.lrc: ${lrcInfo}${reason ? " &middot; " + reason : ""}</div>
                    </div>
                    <div class="repair-row-action">
                        <span class="repair-badge ${badgeClass}">${statusLabel}</span>
                        ${isFixable ? `
                            <button type="button" class="btn btn-sm btn-primary" data-repair-fix="${esc(e.mp3)}">Repair</button>
                            <button type="button" class="btn btn-sm btn-ghost" data-repair-manual="${esc(e.mp3)}" title="Provide your own lyrics">Manual\u2026</button>
                        ` : ""}
                        <span class="repair-row-status muted small" data-repair-status></span>
                    </div>
                </li>`;
        }).join("");

        results.innerHTML = `
            <ul class="repair-list">${rows}</ul>
            <p class="muted small" style="margin-top:.5rem;">Showing ${entries.length} entr${entries.length === 1 ? "y" : "ies"}. Broken files are pre-selected; Repair re-fetches from LRCLIB; Manual\u2026 lets you paste/upload/look up your own.</p>
        `;

        results.querySelectorAll("[data-repair-fix]").forEach((btn) => {
            btn.addEventListener("click", () => fixOne(btn.dataset.repairFix, btn));
        });
        results.querySelectorAll("[data-repair-manual]").forEach((btn) => {
            btn.addEventListener("click", () => openManualModal(btn.dataset.repairManual, btn));
        });
        results.querySelectorAll("[data-repair-pick]").forEach((cb) => {
            cb.addEventListener("change", updateBulkButton);
        });
        updateBulkButton();
    }

    function updateBulkButton() {
        const btn = $("#repair-bulk-fix-btn");
        if (!btn) return;
        if (_busy) {
            btn.disabled = true;
            return;
        }
        const checked = document.querySelectorAll("[data-repair-pick]:checked");
        btn.disabled = checked.length === 0;
        btn.textContent = checked.length > 1
            ? `Repair ${checked.length} selected`
            : "Repair selected";
    }

    // ----------------------------- fix one ----------------------------

    async function fixOne(mp3Path, btn) {
        if (_busy) return;
        _busy = true;
        btn.disabled = true;
        const original = btn.textContent;
        btn.textContent = "Repairing\u2026";
        const row = btn.closest(".repair-row");
        const statusEl = row && row.querySelector("[data-repair-status]");
        if (statusEl) statusEl.textContent = "";
        try {
            const r = await api("/api/repair/fix", { method: "POST",
                body: { paths: [mp3Path] } });
            const item = (r.results || [])[0] || {};
            if (item.ok) {
                btn.textContent = "\u2713 Fixed";
                if (statusEl) statusEl.textContent = "lrclib \u2014 " + (formatBytes(item.size_lrc || 0) || "written");
                btn.classList.remove("btn-primary");
                btn.classList.add("btn-ghost");
                if (row) {
                    row.classList.remove("repair-row-broken");
                    row.classList.add("repair-row-healthy");
                    const badge = row.querySelector(".repair-badge");
                    if (badge) {
                        badge.className = "repair-badge repair-badge-healthy";
                        badge.textContent = "Fixed";
                    }
                    const cb = row.querySelector("[data-repair-pick]");
                    if (cb) cb.checked = false;
                    const manualBtn = row.querySelector("[data-repair-manual]");
                    if (manualBtn) manualBtn.remove();
                }
            } else {
                btn.disabled = false;
                btn.textContent = original;
                if (statusEl) {
                    statusEl.textContent = item.error || "failed";
                    statusEl.style.color = "var(--danger, #c00)";
                }
            }
        } catch (e) {
            btn.disabled = false;
            btn.textContent = original;
            if (statusEl) {
                statusEl.textContent = e.message;
                statusEl.style.color = "var(--danger, #c00)";
            }
        } finally {
            _busy = false;
            updateBulkButton();
        }
    }

    // ----------------------------- bulk fix ----------------------------

    async function runBulkFix() {
        if (_busy) return;
        const checked = [...document.querySelectorAll("[data-repair-pick]:checked")];
        if (!checked.length) return;
        const paths = checked.map((c) => c.dataset.repairPick);
        _busy = true;
        const bulkBtn = $("#repair-bulk-fix-btn");
        bulkBtn.disabled = true;
        const originalText = bulkBtn.textContent;
        bulkBtn.textContent = `Repairing 0 / ${paths.length}\u2026`;
        let done = 0, ok = 0;
        try {
            for (const p of paths) {
                done++;
                bulkBtn.textContent = `Repairing ${done} / ${paths.length}\u2026`;
                try {
                    const r = await api("/api/repair/fix", { method: "POST",
                        body: { paths: [p] } });
                    const item = (r.results || [])[0] || {};
                    const row = document.querySelector(`.repair-row[data-mp3="${cssEscape(p)}"]`);
                    if (item.ok) {
                        ok++;
                        if (row) {
                            row.classList.remove("repair-row-broken");
                            row.classList.add("repair-row-healthy");
                            const badge = row.querySelector(".repair-badge");
                            if (badge) {
                                badge.className = "repair-badge repair-badge-healthy";
                                badge.textContent = "Fixed";
                            }
                            const cb = row.querySelector("[data-repair-pick]");
                            if (cb) cb.checked = false;
                            const fixBtn = row.querySelector("[data-repair-fix]");
                            if (fixBtn) {
                                fixBtn.textContent = "\u2713 Fixed";
                                fixBtn.classList.remove("btn-primary");
                                fixBtn.classList.add("btn-ghost");
                            }
                            const manualBtn = row.querySelector("[data-repair-manual]");
                            if (manualBtn) manualBtn.remove();
                            const statusEl = row.querySelector("[data-repair-status]");
                            if (statusEl) statusEl.textContent = "lrclib \u2014 written";
                        }
                    } else if (row) {
                        const statusEl = row.querySelector("[data-repair-status]");
                        if (statusEl) {
                            statusEl.textContent = item.error || "failed";
                            statusEl.style.color = "var(--danger, #c00)";
                        }
                    }
                } catch (e) {
                    const row = document.querySelector(`.repair-row[data-mp3="${cssEscape(p)}"]`);
                    if (row) {
                        const statusEl = row.querySelector("[data-repair-status]");
                        if (statusEl) {
                            statusEl.textContent = e.message;
                            statusEl.style.color = "var(--danger, #c00)";
                        }
                    }
                }
            }
        } finally {
            _busy = false;
            bulkBtn.textContent = `${ok}/${paths.length} fixed`;
            updateBulkButton();
            setTimeout(() => { bulkBtn.textContent = originalText; }, 4000);
        }
    }

    // ───────────────── Manual lyrics modal ────────────────────────

    function bindManualModal() {
        const modal = $("#repair-manual-modal");
        if (!modal || modal.dataset.wired) return;
        modal.dataset.wired = "1";

        // Tab buttons
        modal.querySelectorAll("[data-manual-tab]").forEach((btn) => {
            btn.addEventListener("click", () => {
                const tab = btn.dataset.manualTab;
                modal.querySelectorAll("[data-manual-tab]").forEach((b) =>
                    b.classList.toggle("is-active", b === btn));
                modal.querySelectorAll(".manual-pane").forEach((p) => {
                    p.hidden = (p.id !== `manual-pane-${tab}`);
                });
            });
        });

        // Paste save
        const pasteSave = $("#manual-paste-save");
        if (pasteSave) {
            pasteSave.addEventListener("click", () => saveManualLyrics("paste"));
        }
        // Upload save
        const uploadInput = $("#manual-upload-file");
        const uploadSave = $("#manual-upload-save");
        if (uploadInput) {
            uploadInput.addEventListener("change", async () => {
                const f = uploadInput.files && uploadInput.files[0];
                if (!f) {
                    if (uploadSave) uploadSave.disabled = true;
                    $("#manual-upload-info").textContent = "";
                    _manual.uploadText = "";
                    return;
                }
                try {
                    _manual.uploadText = await f.text();
                    $("#manual-upload-info").textContent =
                        `File: ${esc(f.name)} (${fmtBytes(f.size)})`;
                    if (uploadSave) uploadSave.disabled = false;
                } catch (e) {
                    $("#manual-upload-info").textContent = `Read error: ${esc(e.message)}`;
                    if (uploadSave) uploadSave.disabled = true;
                }
            });
        }
        if (uploadSave) {
            uploadSave.addEventListener("click", () => {
                if (!_manual.uploadText) return;
                saveManualLyrics("upload", _manual.uploadText);
            });
        }
        // Search
        const searchBtn = $("#manual-search-btn");
        const searchInput = $("#manual-search-q");
        if (searchBtn) {
            searchBtn.addEventListener("click", runManualSearch);
        }
        if (searchInput) {
            searchInput.addEventListener("keydown", (e) => {
                if (e.key === "Enter") {
                    e.preventDefault();
                    runManualSearch();
                }
            });
        }
        const searchSave = $("#manual-search-save");
        if (searchSave) {
            searchSave.addEventListener("click", () => {
                if (!_manual.picked || !_manual.picked.lrc_text) return;
                saveManualLyrics("manual_search", _manual.picked.lrc_text);
            });
        }
    }

    function openManualModal(mp3Path, btn) {
        if (!mp3Path) return;
        _manual.active = true;
        _manual.mp3 = mp3Path;
        _manual.picked = null;
        _manual.uploadText = "";

        // Read ID3 from the row context for the modal header.
        const row = btn && btn.closest(".repair-row");
        _manual.title = row ? row.querySelector(".repair-row-title strong").textContent : "";
        _manual.artist = (() => {
            if (!row) return "";
            const t = row.querySelector(".repair-row-title").textContent;
            const idx = t.indexOf("\u2014");
            return idx >= 0 ? t.slice(idx + 1).trim() : "";
        })();

        const fb = $("#manual-feedback");
        if (fb) fb.textContent = "";
        const heading = $("#repair-manual-h");
        if (heading) {
            const t = _manual.title || "this song";
            const a = _manual.artist ? ` &mdash; ${_manual.artist}` : "";
            heading.innerHTML = `<span class="panel-emoji" aria-hidden="true">&#127908;</span> Manual lyrics for ${esc(t)}${esc(a)}`;
        }
        // Reset tabs to Paste.
        const modal = $("#repair-manual-modal");
        if (modal) {
            modal.querySelectorAll("[data-manual-tab]").forEach((b) =>
                b.classList.toggle("is-active", b.dataset.manualTab === "paste"));
            modal.querySelectorAll(".manual-pane").forEach((p) => {
                p.hidden = (p.id !== "manual-pane-paste");
            });
        }
        const pasteText = $("#manual-paste-text");
        if (pasteText) pasteText.value = "";
        const uploadInfo = $("#manual-upload-info");
        if (uploadInfo) uploadInfo.textContent = "";
        const uploadInput = $("#manual-upload-file");
        if (uploadInput) uploadInput.value = "";
        const uploadSave = $("#manual-upload-save");
        if (uploadSave) uploadSave.disabled = true;
        const searchInput = $("#manual-search-q");
        if (searchInput) searchInput.value = "";
        const searchResults = $("#manual-search-results");
        if (searchResults) searchResults.innerHTML = "";
        const searchSave = $("#manual-search-save");
        if (searchSave) searchSave.disabled = true;

        openModal("#repair-manual-modal");
    }

    function openModal(sel) {
        const el = $(sel);
        if (!el) return;
        el.hidden = false;
    }
    function closeModal(sel) {
        const el = $(sel);
        if (!el) return;
        el.hidden = true;
    }

    async function saveManualLyrics(source, lrcText) {
        if (!_manual.mp3) return;
        let text = lrcText;
        if (!text) {
            text = ($("#manual-paste-text") || {}).value || "";
        }
        text = (text || "").trim();
        if (!text) {
            const fb = $("#manual-feedback");
            if (fb) fb.textContent = "Lyrics text is empty.";
            return;
        }
        const fb = $("#manual-feedback");
        if (fb) fb.textContent = "Saving\u2026";
        try {
            const r = await api("/api/repair/upload", {
                method: "POST",
                body: { mp3_path: _manual.mp3, lrc_text: text, source: source },
            });
            if (!r.ok) {
                if (fb) fb.textContent = `Save failed: ${esc(r.error || "unknown")}`;
                return;
            }
            if (fb) fb.textContent = `Saved \u2014 ${fmtBytes(r.size_lrc)} written (source: ${esc(r.source)}).`;
            // Update the row in place.
            const row = document.querySelector(`.repair-row[data-mp3="${cssEscape(_manual.mp3)}"]`);
            if (row) {
                row.classList.remove("repair-row-broken");
                row.classList.add("repair-row-healthy");
                const badge = row.querySelector(".repair-badge");
                if (badge) {
                    badge.className = "repair-badge repair-badge-healthy";
                    badge.textContent = "Fixed";
                }
                const fixBtn = row.querySelector("[data-repair-fix]");
                if (fixBtn) fixBtn.remove();
                const manualBtn = row.querySelector("[data-repair-manual]");
                if (manualBtn) manualBtn.remove();
                const cb = row.querySelector("[data-repair-pick]");
                if (cb) cb.checked = false;
                const statusEl = row.querySelector("[data-repair-status]");
                if (statusEl) statusEl.textContent = `manual (${esc(source)}) \u2014 written`;
            }
            // Close modal after a short delay so the user sees the success.
            setTimeout(() => closeModal("#repair-manual-modal"), 700);
        } catch (e) {
            if (fb) fb.textContent = `Save error: ${esc(e.message)}`;
        }
    }

    async function runManualSearch() {
        const q = ($("#manual-search-q") || {}).value || "";
        if (!q.trim()) {
            $("#manual-search-results").innerHTML =
                `<p class="muted small">Type something to search LRCLIB.</p>`;
            return;
        }
        $("#manual-search-results").innerHTML =
            `<p class="muted small">Searching LRCLIB for &quot;${esc(q)}&quot;\u2026</p>`;
        $("#manual-search-save").disabled = true;
        _manual.picked = null;
        try {
            const r = await api("/api/repair/manual_search", {
                method: "POST", body: { q: q, limit: 20 },
            });
            if (!r.ok || !r.results || !r.results.length) {
                $("#manual-search-results").innerHTML =
                    `<p class="muted small">No LRCLIB results for &quot;${esc(q)}&quot;. Try a different query.</p>`;
                return;
            }
            const rows = r.results.map((hit, i) => {
                const dur = hit.duration_ms ? formatDur(hit.duration_ms) : "";
                const tags = [];
                if (hit.albumName) tags.push(esc(hit.albumName));
                if (dur) tags.push(dur);
                if (hit.synced) tags.push("synced");
                return `
                    <li class="manual-result-item">
                        <label>
                            <input type="radio" name="manual-pick" data-idx="${i}">
                            <strong>${esc(hit.trackName || hit.name)}</strong>
                            &mdash; ${esc(hit.artistName)}
                            <span class="muted small">${tags.join(" &middot; ")}</span>
                        </label>
                    </li>`;
            }).join("");
            $("#manual-search-results").innerHTML =
                `<ul class="manual-result-list">${rows}</ul>`;
            $("#manual-search-results").querySelectorAll("input[name='manual-pick']").forEach((r2) => {
                r2.addEventListener("change", () => {
                    const idx = parseInt(r2.dataset.idx, 10);
                    _manual.picked = r.results[idx];
                    $("#manual-search-save").disabled = false;
                });
            });
        } catch (e) {
            $("#manual-search-results").innerHTML =
                `<p class="muted small" style="color: var(--danger, #c00);">Search failed: ${esc(e.message)}</p>`;
        }
    }

    // ───────────────── Deduper sub-panel ─────────────────────────

    function bindDedupe() {
        const scanBtn = $("#dedupe-scan-btn");
        if (scanBtn && !scanBtn.dataset.wired) {
            scanBtn.dataset.wired = "1";
            scanBtn.addEventListener("click", runDedupeScan);
        }
    }

    async function runDedupeScan() {
        if (_dedupe.busy) return;
        _dedupe.busy = true;
        const btn = $("#dedupe-scan-btn");
        if (btn) btn.disabled = true;
        const summary = $("#dedupe-summary");
        const folderLine = $("#dedupe-folder-line");
        const results = $("#dedupe-results");
        summary.textContent = "Scanning\u2026";
        folderLine.textContent = "";
        results.innerHTML = '<p class="muted small">Scanning music folder for duplicate songs\u2026</p>';
        try {
            const r = await api("/api/repair/dedupe");
            _dedupe.scan = r;
            if (!r.ok) {
                summary.textContent = "";
                results.innerHTML = `<div class="repair-error"><p>${esc(r.error || "scan failed")}</p></div>`;
                folderLine.textContent = r.music_folder ? `Scanned: ${esc(r.music_folder)}` : "";
                return;
            }
            folderLine.textContent = `Scanned: ${esc(r.music_folder)}`;
            renderDedupeResults(r);
        } catch (e) {
            summary.textContent = "";
            results.innerHTML = `<div class="repair-error"><p>${esc(e.message)}</p></div>`;
        } finally {
            _dedupe.busy = false;
            if (btn) btn.disabled = false;
        }
    }

    function renderDedupeResults(r) {
        const summary = $("#dedupe-summary");
        const results = $("#dedupe-results");
        const c = r.counts || {};
        summary.innerHTML =
            `<strong>${c.groups || 0}</strong> duplicate group${(c.groups || 0) === 1 ? "" : "s"} found &middot; ` +
            `<strong>${c.songs || 0}</strong> songs flagged across ` +
            `<strong>${c.albums_with_dupes || 0}</strong> album${(c.albums_with_dupes || 0) === 1 ? "" : "s"}`;
        if (!r.send2trash_available) {
            summary.innerHTML +=
                ` &middot; <span style="color: var(--danger, #c00);">send2trash not installed</span> ` +
                `<span class="muted small">(pip install send2trash)</span>`;
        }

        const albums = r.albums || [];
        if (!albums.length) {
            results.innerHTML = "<p class=\"muted small\">No duplicate songs found within any album folder.</p>";
            return;
        }

        const blocks = albums.map((a, ai) => {
            const groupBlocks = a.groups.map((g, gi) => {
                const reasonLabel = g.reason === "exact_title_match"
                    ? "exact-title match"
                    : "near-title match";
                const songRows = g.songs.map((s, si) => {
                    const checked = (si === 0) ? "" : "checked";
                    return `
                        <li class="dedupe-song" data-mp3="${esc(s.mp3)}">
                            <label class="dedupe-song-check">
                                <input type="checkbox" data-dedupe-pick="${esc(s.mp3)}" ${checked}>
                            </label>
                            <div class="dedupe-song-meta">
                                <span class="track-title">${esc(s.title)}</span>
                                <span class="muted small">${esc(s.mp3_relpath)}</span>
                            </div>
                        </li>`;
                }).join("");
                const isFirstSong = g.songs[0] ? cssEscape(g.songs[0].mp3) : "";
                return `
                    <div class="dedupe-group" data-album-idx="${ai}" data-group-idx="${gi}"
                         data-norm-title="${esc(g.norm_title)}">
                        <div class="dedupe-group-head">
                            <strong>${esc(g.norm_title)}</strong>
                            <span class="muted small">${g.songs.length} songs &middot; ${reasonLabel}</span>
                        </div>
                        <ul class="dedupe-songs">${songRows}</ul>
                        <div class="dedupe-group-actions">
                            <button type="button" class="btn btn-sm btn-primary"
                                    data-dedupe-delete-group="${ai}:${gi}">
                                Delete selected
                            </button>
                            <button type="button" class="btn btn-sm btn-ghost"
                                    data-dedupe-skip-group="${ai}:${gi}">
                                Skip group
                            </button>
                        </div>
                    </div>`;
            }).join("");
            return `
                <div class="dedupe-album" data-album-idx="${ai}">
                    <h3 class="dedupe-album-head">${esc(a.album_display)}</h3>
                    ${groupBlocks}
                </div>`;
        }).join("");

        results.innerHTML = `<div class="dedupe-albums">${blocks}</div>
            <p class="muted small" style="margin-top:.5rem;">
                Within-album only. Checked rows are duplicates the user is likely to remove.
                Delete moves the MP3 (and its .lrc) to the Recycle Bin. Skip hides the
                group from future scans (file <code>dedupe_skipped.json</code>).
            </p>`;

        results.querySelectorAll("[data-dedupe-delete-group]").forEach((btn) => {
            btn.addEventListener("click", () => {
                const [ai, gi] = btn.dataset.dedupeDeleteGroup.split(":").map(Number);
                const group = r.albums[ai].groups[gi];
                deleteGroup(r, ai, gi, group);
            });
        });
        results.querySelectorAll("[data-dedupe-skip-group]").forEach((btn) => {
            btn.addEventListener("click", () => {
                const [ai, gi] = btn.dataset.dedupeSkipGroup.split(":").map(Number);
                const group = r.albums[ai].groups[gi];
                skipGroup(r, ai, gi, group);
            });
        });
    }

    async function deleteGroup(scanResp, ai, gi, group) {
        if (_dedupe.busy) return;
        // Collect checked songs (skip the first — the user's primary).
        const container = document.querySelector(
            `.dedupe-group[data-album-idx="${ai}"][data-group-idx="${gi}"]`);
        const checked = container
            ? [...container.querySelectorAll("[data-dedupe-pick]:checked")].map((c) => c.dataset.dedupePick)
            : group.songs.slice(1).map((s) => s.mp3);
        if (!checked.length) {
            // Nothing selected: take all songs except the first one.
            checked.push(...group.songs.slice(1).map((s) => s.mp3));
        }
        if (!checked.length) {
            toast("Nothing selected \u2014 nothing to delete.");
            return;
        }
        _dedupe.busy = true;
        try {
            const r = await api("/api/repair/dedupe/action", {
                method: "POST", body: { action: "delete", paths: checked },
            });
            if (!r.ok) {
                toast(`Delete failed: ${r.error || "unknown"}`);
                return;
            }
            const ok = r.summary && r.summary.deleted;
            const failed = r.summary && r.summary.failed;
            toast(`Moved ${ok} to Recycle Bin${failed ? " (" + failed + " failed)" : ""}.`);
            // Remove the album block (or group block) from the UI.
            if (container) container.remove();
        } catch (e) {
            toast(`Delete error: ${e.message}`);
        } finally {
            _dedupe.busy = false;
        }
    }

    async function skipGroup(scanResp, ai, gi, group) {
        if (_dedupe.busy) return;
        _dedupe.busy = true;
        try {
            const r = await api("/api/repair/dedupe/action", {
                method: "POST", body: { action: "skip", paths: group.songs.map((s) => s.mp3) },
            });
            if (!r.ok) {
                toast(`Skip failed: ${r.error || "unknown"}`);
                return;
            }
            toast("Group hidden from future scans.");
            const container = document.querySelector(
                `.dedupe-group[data-album-idx="${ai}"][data-group-idx="${gi}"]`);
            if (container) container.remove();
        } catch (e) {
            toast(`Skip error: ${e.message}`);
        } finally {
            _dedupe.busy = false;
        }
    }

    function toast(msg) {
        if (typeof window.toast === "function") { window.toast(msg); return; }
        // Fallback: short banner at top of the panel.
        const old = document.querySelector(".repair-toast");
        if (old) old.remove();
        const t = document.createElement("div");
        t.className = "repair-toast";
        t.textContent = msg;
        Object.assign(t.style, {
            position: "fixed", bottom: "16px", right: "16px",
            padding: "8px 12px", borderRadius: "6px",
            background: "rgba(0,0,0,.85)", color: "#fff",
            fontSize: ".9em", zIndex: 9999,
        });
        document.body.appendChild(t);
        setTimeout(() => t.remove(), 3500);
    }

    // ----------------------------- utils ----------------------------

    function formatDur(ms) {
        ms = ms || 0;
        if (!ms) return "";
        const s = Math.round(ms / 1000);
        const m = Math.floor(s / 60);
        const r = s % 60;
        return `${m}:${String(r).padStart(2, "0")}`;
    }

    function cssEscape(s) {
        return String(s).replace(/[\\]/g, "\\\\").replace(/["]/g, '\\"');
    }

    // ───────────────── Renamer sub-panel ─────────────────────────

    // State for the Renamer sub-panel. `approved` tracks user-chosen
    // renames and renumberings that will be sent to the apply endpoint
    // when the user clicks "Apply all approved".
    const _rename = {
        busy: false,
        scan: null,
    };

    function bindRenamer() {
        const scanBtn = $("#rename-scan-btn");
        if (scanBtn && !scanBtn.dataset.wired) {
            scanBtn.dataset.wired = "1";
            scanBtn.addEventListener("click", runRenameScan);
        }
        const applyAll = $("#rename-apply-all-btn");
        if (applyAll && !applyAll.dataset.wired) {
            applyAll.dataset.wired = "1";
            applyAll.addEventListener("click", applyAllRenames);
        }
    }

    async function runRenameScan() {
        if (_rename.busy) return;
        _rename.busy = true;
        const btn = $("#rename-scan-btn");
        const applyAll = $("#rename-apply-all-btn");
        const summary = $("#rename-summary");
        const folderLine = $("#rename-folder-line");
        const results = $("#rename-results");
        if (btn) btn.disabled = true;
        if (applyAll) applyAll.disabled = true;
        summary.textContent = "Scanning\u2026";
        folderLine.textContent = "";
        results.innerHTML = '<p class="muted small">Scanning music folder for misnamed files and duplicate track numbers\u2026</p>';
        try {
            const r = await api("/api/repair/rename/scan");
            _rename.scan = r;
            if (!r.ok) {
                summary.textContent = "";
                results.innerHTML = `<div class="repair-error"><p>${esc(r.error || "scan failed")}</p></div>`;
                folderLine.textContent = r.music_folder ? `Scanned: ${esc(r.music_folder)}` : "";
                return;
            }
            folderLine.textContent = `Scanned: ${esc(r.music_folder)}`;
            renderRenameResults(r);
        } catch (e) {
            summary.textContent = "";
            results.innerHTML = `<div class="repair-error"><p>${esc(e.message)}</p></div>`;
        } finally {
            _rename.busy = false;
            if (btn) btn.disabled = false;
            if (applyAll) applyAll.disabled = false;
            updateRenameApplyButton();
        }
    }

    function renderRenameResults(r) {
        const summary = $("#rename-summary");
        const results = $("#rename-results");
        const applyAll = $("#rename-apply-all-btn");
        const c = r.counts || {};
        const mis = r.misnamed || [];
        const col = r.collisions || [];
        const nothing = mis.length === 0 && col.length === 0;
        summary.innerHTML = nothing
            ? `<strong>0</strong> issues found &middot; library looks clean`
            : `<strong>${mis.length}</strong> misnamed &middot; ` +
              `<strong>${col.length}</strong> album${col.length === 1 ? "" : "s"} with track-number collisions`;

        if (nothing) {
            results.innerHTML = "";
            if (applyAll) applyAll.disabled = true;
            return;
        }

        const blocks = [];
        if (mis.length) {
            const rows = mis.map((m, i) => {
                const checked = m.title ? "checked" : "";
                const enabled = m.title ? "" : "disabled";
                const titleMissing = !m.title;
                return `
                    <li class="rename-row" data-mp3="${esc(m.mp3)}" data-row-idx="${i}">
                        <label class="rename-row-check">
                            <input type="checkbox" class="rename-row-pick" data-pick="${esc(m.mp3)}" ${checked} ${enabled}>
                        </label>
                        <div class="rename-row-main">
                            <div class="rename-row-old">
                                <span class="muted small">current:</span>
                                <code>${esc(m.current)}</code>
                            </div>
                            <div class="rename-row-arrow" aria-hidden="true">&rarr;</div>
                            <div class="rename-row-new">
                                <span class="muted small">suggested:</span>
                                <code>${esc(m.suggested)}</code>
                                ${titleMissing ? `<span class="muted small"> (no ID3 title — can't suggest)</span>` : ""}
                                ${m.reason ? `<span class="muted small"> &middot; ${esc(m.reason)}</span>` : ""}
                            </div>
                            <div class="rename-row-meta muted small">${esc(m.album_key || "")}${m.artist ? " &middot; " + esc(m.artist) : ""}</div>
                        </div>
                        <div class="rename-row-actions">
                            <button type="button" class="btn btn-sm btn-primary"
                                    data-rename-one="${esc(m.mp3)}" ${titleMissing ? "disabled" : ""}>
                                Rename
                            </button>
                        </div>
                    </li>`;
            }).join("");
            blocks.push(`
                <section class="rename-section">
                    <h3 class="rename-section-head">
                        Misnamed files
                        <span class="muted small">${mis.length} file${mis.length === 1 ? "" : "s"} &middot; check the ones you want renamed, then "Apply all approved"</span>
                    </h3>
                    <ul class="rename-list">${rows}</ul>
                </section>`);
        }

        if (col.length) {
            const albumBlocks = col.map((album, ai) => {
                const dupRows = album.duplicate_tracks.map((d) => {
                    const names = d.current_names.map((n) => `<code>${esc(n)}</code>`).join(", ");
                    return `
                        <li class="rename-collision-dup">
                            <span class="rename-collision-track">track <strong>${esc(d.track_number)}</strong></span>
                            <span class="muted small">&times; ${d.count}</span>
                            <span class="muted small">${names}</span>
                        </li>`;
                }).join("");
                const proposedRows = album.proposed.map((p, pi) => {
                    const willRename = !_basenameEqLoose(p.current, p.new_basename);
                    return `
                        <li class="rename-proposed-row ${willRename ? "" : "rename-proposed-noop"}"
                            data-mp3="${esc(p.mp3)}">
                            <span class="rename-proposed-old"><code>${esc(p.current)}</code></span>
                            <span class="rename-proposed-arrow" aria-hidden="true">&rarr;</span>
                            <span class="rename-proposed-new">
                                <code>${esc(p.new_basename)}</code>
                                ${willRename ? "" : `<span class="muted small">(no change)</span>`}
                            </span>
                            <span class="muted small">track ${esc(p.track_old || "?")} &rarr; ${esc(p.track_new)}</span>
                        </li>`;
                }).join("");
                return `
                    <div class="rename-album" data-album-idx="${ai}">
                        <h4 class="rename-album-head">${esc(album.album_key)}</h4>
                        <div class="rename-collision-dups">
                            <p class="muted small">Duplicates found:</p>
                            <ul class="rename-collision-dup-list">${dupRows}</ul>
                        </div>
                        <p class="muted small" style="margin-top:.5rem;">Proposed renumbering (1..${album.proposed.length}, sorted by current track then filename):</p>
                        <ul class="rename-proposed-list">${proposedRows}</ul>
                        <div class="rename-album-actions">
                            <button type="button" class="btn btn-sm btn-primary"
                                    data-rename-collide="${ai}">
                                Apply renumber to this album
                            </button>
                        </div>
                    </div>`;
            }).join("");
            blocks.push(`
                <section class="rename-section" style="margin-top:1.5rem;">
                    <h3 class="rename-section-head">
                        Track-number collisions
                        <span class="muted small">${col.length} album${col.length === 1 ? "" : "s"} &middot; one button applies the entire proposed renumbering to that album</span>
                    </h3>
                    ${albumBlocks}
                </section>`);
        }

        results.innerHTML = blocks.join("");

        // Wire per-row and per-album buttons.
        results.querySelectorAll("[data-rename-one]").forEach((btn) => {
            btn.addEventListener("click", () => {
                const mp3 = btn.dataset.renameOne;
                const row = mis.find((m) => m.mp3 === mp3);
                if (row) applyRenames([{ mp3: row.mp3, new_basename: row.suggested }]);
            });
        });
        results.querySelectorAll("[data-rename-collide]").forEach((btn) => {
            btn.addEventListener("click", () => {
                const ai = parseInt(btn.dataset.renameCollide, 10);
                const album = col[ai];
                if (!album) return;
                const actions = album.proposed
                    .filter((p) => !_basenameEqLoose(p.current, p.new_basename))
                    .map((p) => ({ mp3: p.mp3, new_basename: p.new_basename }));
                if (!actions.length) {
                    toast("No changes needed for this album.");
                    return;
                }
                applyRenames(actions);
            });
        });

        // Wire per-row checkbox change -> enable/disable Apply all.
        results.querySelectorAll(".rename-row-pick").forEach((cb) => {
            cb.addEventListener("change", updateRenameApplyButton);
        });

        updateRenameApplyButton();
    }

    function _basenameEqLoose(a, b) {
        if (!a || !b) return false;
        if (String(a).toLowerCase() === String(b).toLowerCase()) return true;
        const strip = (s) => String(s).replace(/^\d+\s*-\s*/, "");
        return strip(a).toLowerCase() === strip(b).toLowerCase();
    }

    function updateRenameApplyButton() {
        const applyAll = $("#rename-apply-all-btn");
        if (!applyAll) return;
        const checked = $$(".rename-row-pick:checked:not(:disabled)");
        applyAll.disabled = checked.length === 0;
        applyAll.textContent = checked.length
            ? `Apply ${checked.length} rename${checked.length === 1 ? "" : "s"}`
            : "Apply all approved";
    }

    async function applyAllRenames() {
        if (_rename.busy) return;
        const checked = $$(".rename-row-pick:checked:not(:disabled)");
        if (!checked.length) return;
        const r = _rename.scan;
        if (!r || !r.ok) return;
        const actions = [];
        for (const cb of checked) {
            const mp3 = cb.dataset.pick;
            const m = (r.misnamed || []).find((x) => x.mp3 === mp3);
            if (m && m.suggested) {
                actions.push({ mp3: m.mp3, new_basename: m.suggested });
            }
        }
        if (!actions.length) return;
        await applyRenames(actions);
    }

    async function applyRenames(actions) {
        if (_rename.busy) return;
        if (!actions.length) return;
        _rename.busy = true;
        try {
            const r = await api("/api/repair/rename/apply", {
                method: "POST",
                body: { actions },
            });
            if (!r || !r.ok) {
                toast(`Rename failed: ${(r && r.error) || "unknown"}`);
                return;
            }
            const ok = (r.summary && r.summary.renamed) || 0;
            const failed = (r.summary && r.summary.failed) || 0;
            toast(`Renamed ${ok} file${ok === 1 ? "" : "s"}${failed ? ` (${failed} failed)` : ""}.`);
            // Re-scan so the list reflects the new state.
            await runRenameScan();
        } catch (e) {
            toast(`Rename error: ${e.message}`);
        } finally {
            _rename.busy = false;
        }
    }

    window.initRepairPage = initRepairPage;
})();
