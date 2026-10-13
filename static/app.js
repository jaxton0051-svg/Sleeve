/* =====================================================================
   Sleeve — shared browser behavior
   Owns cookie setup, download queue/file views, library interactions, and
   initializes page-specific scripts by body[data-active-tab]. */
(function () {
    "use strict";

    // ----------------------------- helpers -----------------------------

    const $ = (sel, root) => (root || document).querySelector(sel);
    const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

    const esc = (s) =>
        String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
            ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
        );

    function fmtDuration(ms) {
        if (!ms || ms <= 0) return "";
        const total = Math.round(ms / 1000);
        const m = Math.floor(total / 60);
        const s = total % 60;
        return `${m}:${String(s).padStart(2, "0")}`;
    }

    const toastEl = $("#toast");
    let toastTimer = null;
    function toast(msg, kind) {
        if (!toastEl) return;
        toastEl.textContent = msg;
        toastEl.className = "toast" + (kind ? " " + kind : "");
        toastEl.hidden = false;
        if (toastTimer) clearTimeout(toastTimer);
        toastTimer = setTimeout(() => { toastEl.hidden = true; }, 3200);
    }
    window.toast = toast;

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
            const msg = (data && data.error) || res.statusText || "Request failed";
            throw new Error(msg);
        }
        return data;
    }

    // ----------------------------- cookies -----------------------------

    async function loadBrowsers() {
        try {
            const r = await api("/api/cookies");
            const sel = $("#browser");
            if (sel) {
                sel.innerHTML = r.browsers.map((b) => `<option value="${esc(b)}">${esc(b)}</option>`).join("");
                sel.value = "chrome";
            }
            renderCookieStatus(r);
        } catch (e) {
            console.error(e);
        }
    }

    function renderCookieStatus(r) {
        const el = $("#ck-status");
        if (!el) return;
        if (r && r.exists) {
            el.className = "status ok";
            el.innerHTML = `&#9679; cookie.txt OK &middot; ${r.count} cookies`;
        } else {
            el.className = "status err";
            el.textContent = "● no cookie.txt yet";
        }
    }

    async function saveCookies() {
        const browser = $("#browser").value;
        const el = $("#ck-status");
        if (el) {
            el.className = "status";
            el.textContent = "Exporting from " + browser + "…";
        }
        try {
            const r = await api("/api/cookies", { method: "POST", body: { browser } });
            if (r.ok) {
                renderCookieStatus(r);
                toast("Cookies exported (" + r.count + ")", "ok");
            } else {
                if (el) { el.className = "status err"; el.textContent = r.error || "Export failed."; }
                toast(r.error || "Export failed", "err");
            }
        } catch (e) {
            if (el) { el.className = "status err"; el.textContent = e.message; }
            toast(e.message, "err");
        }
    }

    async function cleanCookies() {
        const el = $("#ck-status");
        if (el) {
            el.className = "status";
            el.textContent = "Cleaning cookie.txt…";
        }
        try {
            const r = await api("/api/cookies/clean", { method: "POST" });
            if (r.ok) {
                const repaired = r.repaired ? ` (repaired ${r.dropped_lines} bad rows, kept ${r.valid_lines})` : " (already clean)";
                if (el) {
                    el.className = "status ok";
                    el.textContent = `cookie.txt valid: ${r.valid_lines} rows` + repaired;
                }
                toast(`Cookie file cleaned${repaired}`, "ok");
            } else {
                if (el) { el.className = "status err"; el.textContent = r.error || "Clean failed."; }
                toast(r.error || "Clean failed", "err");
            }
        } catch (e) {
            if (el) { el.className = "status err"; el.textContent = e.message; }
            toast(e.message, "err");
        }
    }

    // ----------------------------- shared: jobs + files -----------------------------

    function setJobStatus(text, kind) {
        const el = $("#job-status");
        if (!el) return;
        el.className = "status job-status" + (kind ? " " + kind : "");
        el.textContent = text;
    }

    async function startDownload(query) {
        setJobStatus("Working… " + query);
        try {
            const r = await api("/api/download", { method: "POST", body: { query } });
            if (!r.ok) { setJobStatus(r.error || "Failed to start.", "err"); return; }
            pollJob(r.job_id, query);
        } catch (e) {
            setJobStatus(e.message, "err");
        }
    }

    async function startAlbumDownload(album) {
        const label = `${album.title} — ${album.artist}`;
        setJobStatus("Working… " + label);
        try {
            const r = await api("/api/album-download", { method: "POST", body: album });
            if (!r.ok) { setJobStatus(r.error || "Failed to start.", "err"); return; }
            pollJob(r.job_id, label);
        } catch (e) {
            setJobStatus(e.message, "err");
        }
    }

    function pollJob(jobId, originalQuery) {
        const t = setInterval(async () => {
            try {
                const r = await api("/api/download/" + jobId);
                const job = r.job || {};
                const kind = job.status === "error" ? "err" : job.status === "done" ? "ok" : "";
                let msg;
                const prog = job.progress;
                if (prog && prog.total) {
                    const cur = prog.current ? ` — ${prog.current}` : "";
                    msg = `${job.status}: ${prog.done} / ${prog.total}${cur}`;
                } else {
                    msg = `${job.status}: ${job.title || originalQuery}`;
                }
                if (job.error) msg += " — " + job.error;
                setJobStatus(msg, kind);
                if (job.status === "done") {
                    clearInterval(t);
                    toast("Done: " + (job.title || originalQuery), "ok");
                    refreshFiles();
                }
                if (job.status === "error") {
                    clearInterval(t);
                    toast(job.error || "Download failed", "err");
                    refreshFiles();
                }
            } catch (e) {
                clearInterval(t);
                setJobStatus(e.message, "err");
            }
        }, 1500);
    }

    async function refreshFiles() {
        try {
            const r = await api("/api/files");
            const list = $("#files-list");
            if (!list) return;
            if (!r.files.length) {
                list.innerHTML = `<p class="muted small">No MP3s yet.</p>`;
                return;
            }
        list.innerHTML = r.files
                .map((f) => `<a href="/downloads/${f.split("/").map(encodeURIComponent).join("/")}" download>${esc(f)}</a>`)
                .join("");
        } catch (e) {
            console.error("files:", e);
        }
    }

    // ----------------------------- jobs panel -----------------------------

    const ACTIVE_STATUSES = new Set(["queued", "searching", "downloading",
                                       "loading tracks", "cancelling"]);

    function jobStateClass(status) {
        if (ACTIVE_STATUSES.has(status)) return "is-active";
        if (status === "done") return "is-done";
        if (status === "error") return "is-error";
        if (status === "cancelled") return "is-cancelled";
        return "";
    }

    function renderJobCard(job) {
        const cls = jobStateClass(job.status);
        const prog = job.progress || null;
        const pct = prog && prog.total ? Math.round((prog.done / prog.total) * 100) : null;
        const progText = prog && prog.total
            ? `${prog.done} / ${prog.total}${prog.current ? ` — ${prog.current}` : ""}`
            : (job.title || job.query || job.status);
        const isActive = ACTIVE_STATUSES.has(job.status) || job.cancel_requested;
        const cancelBtn = isActive
            ? `<button class="job-cancel-btn" data-cancel-job="${esc(job.id)}" type="button">Stop</button>`
            : "";
        return `
            <article class="job-card ${cls}" data-job-id="${esc(job.id)}">
                <div class="job-card-head">
                    <div class="job-card-meta">
                        <div class="job-card-title">${esc(job.title || job.query || "Untitled job")}</div>
                        <div class="job-card-status">
                            ${isActive ? '<span class="status-dot"></span>' : ""}
                            ${esc(job.status)}${job.error ? " — " + esc(job.error) : ""}
                        </div>
                    </div>
                    ${cancelBtn}
                </div>
                ${pct !== null ? `
                    <div class="job-progress"><div class="job-progress-bar" style="width:${pct}%"></div></div>
                    <div class="job-progress-meta">
                        <span>${esc(progText)}</span>
                        <span>${pct}%</span>
                    </div>
                ` : ""}
            </article>
        `;
    }

    async function refreshJobs() {
        const list = $("#jobs-list");
        const badge = $("#jobs-count");
        if (!list) return;
        try {
            const r = await api("/api/jobs");
            const active = r.active || [];
            const recent = r.recent || [];
            const showCompleted = $("#show-completed");
            const items = [...active, ...(showCompleted && showCompleted.checked ? recent : [])];
            if (!items.length) {
                list.innerHTML = `<p class="muted small">${showCompleted ? "No downloads to show. Completed jobs stay hidden until you enable them." : "No active or recent downloads."}</p>`;
                if (badge) {
                    badge.textContent = "0";
                    badge.classList.remove("is-active");
                }
                return;
            }
            list.innerHTML = items.map(renderJobCard).join("");
            if (badge) {
                badge.textContent = active.length;
                badge.classList.toggle("is-active", active.length > 0);
            }
        } catch (e) {
            // Backoff so a restarting server doesn't flood the console
            // with 30 identical errors per minute. Log first occurrence,
            // then mute until the connection is healthy again.
            const now = Date.now();
            const last = refreshJobs._lastErr || 0;
            if (now - last > 30000) {
                console.error("jobs:", e);
                refreshJobs._lastErr = now;
            }
        }
    }

    async function cancelJob(jobId, btn) {
        if (!jobId) return;
        if (btn) { btn.disabled = true; btn.textContent = "Stopping…"; }
        try {
            await api(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
            toast("Stopping job…", "ok");
            refreshJobs();
        } catch (e) {
            toast(e.message || "Cancel failed", "err");
            if (btn) { btn.disabled = false; btn.textContent = "Stop"; }
        }
    }

    function initJobsPanel() {
        const list = $("#jobs-list");
        if (!list) return;
        // Delegated click handler for cancel buttons
        list.addEventListener("click", (e) => {
            const btn = e.target.closest("[data-cancel-job]");
            if (!btn) return;
            cancelJob(btn.dataset.cancelJob, btn);
        });
        const completedToggle = $("#show-completed");
        if (completedToggle) completedToggle.addEventListener("change", refreshJobs);
        // Initial + polling
        refreshJobs();
        setInterval(refreshJobs, 2000);
    }

    function openModal(content, id) {
        const previous = document.getElementById(id);
        if (previous) previous.remove();
        const modal = document.createElement("div");
        modal.className = "modal";
        modal.id = id;
        modal.setAttribute("role", "dialog");
        modal.setAttribute("aria-modal", "true");
        modal.setAttribute("aria-labelledby", "album-detail-title");
        modal.innerHTML = `<div class="modal-backdrop" data-modal-close></div><div class="modal-card modal-card-lg" role="document">${content}</div>`;
        modal.addEventListener("click", (event) => {
            if (!event.target.closest("[data-modal-close]")) return;
            modal.hidden = true;
            document.body.style.overflow = "";
            window.setTimeout(() => modal.remove(), 250);
        });
        document.body.appendChild(modal);
        document.body.style.overflow = "hidden";
    }

    // ===================================================================
    //   GALLERY (R1 redux: just your Jellyfin library)
    // ===================================================================

    function initGalleryPage() {
        // ------- cart helpers (local) -------
        const CART_KEY = "aaa.automator.cart.v1";
        function readCart() {
            try { return JSON.parse(localStorage.getItem(CART_KEY)) || { albums: [] }; }
            catch { return { albums: [] }; }
        }
        function writeCart(c) {
            try { localStorage.setItem(CART_KEY, JSON.stringify(c)); } catch {}
        }
        function addToCart(album) {
            const c = readCart();
            if (c.albums.some((a) => a.id === album.id)) return false;
            c.albums.push(album);
            writeCart(c);
            return true;
        }

        // ------- state -------
        const PAGE_SIZE = 200;
        let _offset = 0;
        let _total = Infinity;
        let _allAlbums = [];
        let _filter = "";

        const grid = $("#gallery-grid");
        const meta = $("#gallery-meta");
        const pager = $("#gallery-pager");
        const loadMoreBtn = $("#gallery-load-more");

        // ------- album tile renderer -------
        function renderAlbumCard(album) {
            const card = document.createElement("article");
            card.className = "album-card";
            card.dataset.albumId = album.id || "";
            card.dataset.title = album.title || "";
            card.dataset.artist = album.artist || "";
            card.dataset.artwork = album.artwork || "";
            card.dataset.year = album.year || "";
            card.dataset.trackCount = album.track_count || 0;
            card.tabIndex = 0;
            card.setAttribute("role", "button");
            const meta2 = [];
            if (album.year) meta2.push(String(album.year));
            if (album.track_count) meta2.push(`${album.track_count} tracks`);
            const metaText = meta2.join(" \u00b7 ");
            card.innerHTML = `
                <div class="album-cover-wrap">
                    <div class="album-cover-placeholder" aria-hidden="true">&#9836;</div>
                    <img class="album-cover" alt="" loading="lazy" decoding="async"
                         src="${esc(album.artwork || "")}"
                         onload="this.classList.add('loaded')"
                         onerror="this.remove();">
                </div>
                <div class="album-body">
                    <h3 class="album-title">${esc(album.title || "")}</h3>
                    <p class="album-artist">${esc(album.artist || "")}</p>
                    ${metaText ? `<p class="muted small">${esc(metaText)}</p>` : ""}
                </div>
            `;
            return card;
        }

        // ------- filtered rendering -------
        function applyFilter() {
            if (!_filter) {
                grid.innerHTML = "";
                _allAlbums.forEach((a) => grid.appendChild(renderAlbumCard(a)));
                return;
            }
            const q = _filter.toLowerCase();
            const matches = _allAlbums.filter((a) => {
                return (a.title || "").toLowerCase().includes(q)
                    || (a.artist || "").toLowerCase().includes(q);
            });
            grid.innerHTML = "";
            matches.forEach((a) => grid.appendChild(renderAlbumCard(a)));
            if (!matches.length) {
                grid.innerHTML = `<p class="muted">No albums match "${esc(_filter)}".</p>`;
            }
        }

        // ------- fetch + paginate -------
        async function fetchPage() {
            try {
                const r = await api(`/api/automator/library/albums?limit=${PAGE_SIZE}&start_index=${_offset}`);
                if (!r || !r.ok) {
                    if (meta) meta.textContent = (r && r.error) || "Could not load library.";
                    grid.innerHTML = `<p class="muted">Could not load your Jellyfin library. Open Automator &gt; Settings to verify the server URL and API key.</p>`;
                    grid.removeAttribute("aria-busy");
                    return;
                }
                _total = r.total || 0;
                _allAlbums = _allAlbums.concat(r.albums || []);
                _offset += (r.albums || []).length;
                if (meta) {
                    const filtered = _filter
                        ? _allAlbums.filter((a) =>
                              (a.title || "").toLowerCase().includes(_filter.toLowerCase())
                              || (a.artist || "").toLowerCase().includes(_filter.toLowerCase())).length
                        : _allAlbums.length;
                    meta.textContent = _filter
                        ? `${filtered} of ${_total} albums match "${_filter}"`
                        : `${_total} album${_total === 1 ? "" : "s"} in your Jellyfin library`;
                }
                applyFilter();
                if (_offset < _total) {
                    pager.hidden = false;
                    loadMoreBtn.disabled = false;
                    loadMoreBtn.textContent = `Load more (${_total - _offset} remaining)`;
                } else {
                    pager.hidden = true;
                }
            } catch (e) {
                if (meta) meta.textContent = `Error: ${e.message || e}`;
                grid.innerHTML = `<p class="muted">Network error: ${esc(e.message || String(e))}</p>`;
            } finally {
                grid.removeAttribute("aria-busy");
            }
        }

        // ------- click on a tile: open track modal OR add all to cart -------
        async function showAlbumDetail(album) {
            // Try to fetch the tracks so we can show them and offer "Add all".
            let tracks = [];
            let trackError = "";
            try {
                const tracksResp = await api(`/api/automator/library/albums/${encodeURIComponent(album.id)}/tracks`);
                tracks = (tracksResp && tracksResp.ok) ? (tracksResp.tracks || []) : [];
                trackError = (tracksResp && tracksResp.error) || "";
            } catch (error) {
                trackError = error.message || "Unable to load album tracks.";
            }
            const html = `
                <div class="modal-head"><h2 id="album-detail-title" class="panel-title">${esc(album.title)}</h2><button class="btn btn-ghost btn-sm" data-modal-close type="button" aria-label="Close">&times;</button></div>
                <p class="muted">${esc(album.artist)}${album.year ? " \u00b7 " + album.year : ""}${album.track_count ? " \u00b7 " + album.track_count + " tracks" : ""}</p>
                ${tracks.length
                    ? `<ol class="album-detail-track-list">${tracks.map((t) => `<li>${esc(t.title || "")}${t.duration_ms ? " <span class='muted small'>" + fmtDuration(t.duration_ms) + "</span>" : ""}</li>`).join("")}</ol>`
                    : `<p class="muted">${esc(trackError || "Could not load the album tracklist.")}</p>`}
                <div class="modal-foot" style="display:flex; justify-content:flex-end; gap:.5rem;">
                    <button class="btn btn-ghost" data-modal-close type="button">Close</button>
                    <button class="btn btn-primary" id="album-detail-add-all" type="button"${tracks.length ? "" : " disabled"}>Add all to Automator</button>
                </div>
            `;
            openModal(html, "album-detail-modal");
            const addBtn = document.getElementById("album-detail-add-all");
            if (addBtn && tracks.length) {
                addBtn.addEventListener("click", () => {
                    const cartAlbum = {
                        id: `jf-${album.id}`,
                        title: album.title || "",
                        artist: album.artist || "",
                        artwork: album.artwork || "",
                        trackCount: tracks.length,
                        alreadyOnServer: true,        // we know it's on the server
                        kind: "album",
                        fetchTracksOnStart: false,    // tracks already present
                        tracks: tracks,
                        expanded: false,
                    };
                    const ok = addToCart(cartAlbum);
                    addBtn.textContent = ok ? "\u2713 Added" : "Already in cart";
                    addBtn.disabled = !ok;
                    setTimeout(() => {
                        addBtn.textContent = ok ? "Add all to Automator" : "Already in cart";
                        addBtn.disabled = !ok;
                    }, 1200);
                    if (typeof window.toast === "function") {
                        window.toast(ok ? `Added "${album.title}" to cart` : `"${album.title}" is already in cart`, ok ? "ok" : "info");
                    }
                });
            }
        }

        if (grid) {
            grid.addEventListener("click", (e) => {
                const card = e.target.closest(".album-card");
                if (!card) return;
                const album = {
                    id: card.dataset.albumId,
                    title: card.dataset.title,
                    artist: card.dataset.artist,
                    artwork: card.dataset.artwork,
                    year: card.dataset.year ? Number(card.dataset.year) : null,
                    track_count: Number(card.dataset.trackCount || 0) || 0,
                };
                if (!album.id) return;
                showAlbumDetail(album);
            });
            grid.addEventListener("keydown", (e) => {
                if (e.key !== "Enter" && e.key !== " ") return;
                const card = e.target.closest(".album-card");
                if (!card) return;
                e.preventDefault();
                grid.dispatchEvent(new MouseEvent("click", { target: card }));
            });
        }

        // ------- filter input -------
        const filterInput = $("#gallery-search-input");
        if (filterInput) {
            let t = null;
            filterInput.addEventListener("input", () => {
                clearTimeout(t);
                t = setTimeout(() => {
                    _filter = filterInput.value.trim();
                    applyFilter();
                    if (meta) {
                        const matches = _filter
                            ? _allAlbums.filter((a) =>
                                (a.title || "").toLowerCase().includes(_filter.toLowerCase())
                                || (a.artist || "").toLowerCase().includes(_filter.toLowerCase())).length
                            : _allAlbums.length;
                        meta.textContent = _filter
                            ? `${matches} of ${_total} albums match "${_filter}"`
                            : `${_total} album${_total === 1 ? "" : "s"} in your Jellyfin library`;
                    }
                }, 120);
            });
        }

        // ------- Load more button -------
        if (loadMoreBtn) {
            loadMoreBtn.addEventListener("click", () => {
                loadMoreBtn.disabled = true;
                loadMoreBtn.textContent = "Loading\u2026";
                fetchPage();
            });
        }

        // ------- YouTube quick-add (R4) -------
        async function quickAddFromUrl(url) {
            const fb = $("#quick-add-feedback");
            if (fb) fb.textContent = "Resolving\u2026";
            try {
                const r = await api("/api/automator/youtube/single", {
                    method: "POST",
                    body: { url },
                });
                if (!r || !r.ok) throw new Error((r && r.error) || "request failed");
                const ok = addToCart(r.album);
                if (fb) {
                    fb.textContent = ok
                        ? `Added "${r.album.title}" \u2014 ${r.album.artist}`
                        : `"${r.album.title}" is already in cart`;
                }
                $("#quick-add-url").value = "";
                if (typeof window.toast === "function") {
                    window.toast(ok ? `Added "${r.album.title}" to cart` : `"${r.album.title}" is already in cart`, ok ? "ok" : "info");
                }
            } catch (e) {
                if (fb) fb.textContent = `Failed: ${e.message || e}`;
                if (typeof window.toast === "function") {
                    window.toast(`Quick-add failed: ${e.message || e}`, "err");
                }
            }
        }
        const quickAddBtn = $("#quick-add-go");
        if (quickAddBtn) {
            quickAddBtn.addEventListener("click", () => {
                const url = ($("#quick-add-url").value || "").trim();
                if (!url) {
                    if (typeof window.toast === "function") window.toast("Paste a YouTube URL first.", "info");
                    return;
                }
                if (!/^https?:\/\//.test(url)) {
                    if (typeof window.toast === "function") window.toast("URL must start with http:// or https://", "err");
                    return;
                }
                quickAddFromUrl(url);
            });
        }
        const quickAddInput = $("#quick-add-url");
        if (quickAddInput) {
            quickAddInput.addEventListener("keydown", (e) => {
                if (e.key === "Enter") { e.preventDefault(); quickAddBtn && quickAddBtn.click(); }
            });
        }

        // ------- initial load -------
        fetchPage();
    }



    // ----------------------------- init -----------------------------

    document.addEventListener("DOMContentLoaded", () => {
        if ($("#browser")) loadBrowsers();
        const exportButton = $("#export-cookies");
        if (exportButton) exportButton.addEventListener("click", saveCookies);
        const cleanBtn = $("#clean-cookies");
        if (cleanBtn) cleanBtn.addEventListener("click", cleanCookies);
        refreshFiles();
        initJobsPanel();

        const tab = document.body.dataset.activeTab || "gallery";
        if (tab === "automator") {
            if (typeof window.initAutomatorPage === "function") {
                window.initAutomatorPage();
            }
        } else if (tab === "gallery") {
            initGalleryPage();
        } else if (tab === "repair") {
            if (typeof window.initRepairPage === "function") {
                window.initRepairPage();
            }
        }
    });
})();
