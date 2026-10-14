/* =====================================================================
   AUTOMATOR \u2014 frontend logic for the third tab.

   State:
     - profile (from GET /api/automator/profile, cached)
     - cart (localStorage aaa.automator.cart.v1)
     - active jobs polled from /api/automator/jobs every 2s

   Public entry: initAutomatorPage() \u2014 dispatched by app.js.
   ===================================================================== */
(function () {
    "use strict";

    // ----------------------------- helpers -----------------------------

    const $ = (sel, root) => (root || document).querySelector(sel);
    const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

    const esc = (s) =>
        String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
            ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

    function fmtDuration(ms) {
        if (!ms || ms <= 0) return "";
        const total = Math.round(ms / 1000);
        const m = Math.floor(total / 60);
        const s = total % 60;
        return `${m}:${String(s).padStart(2, "0")}`;
    }

    // Reuse the global toast if available, otherwise a tiny local one.
    function toast(msg, kind) {
        if (typeof window.toast === "function") {
            window.toast(msg, kind);
            return;
        }
        const el = $("#toast");
        if (!el) { console.log("[toast]", msg); return; }
        el.textContent = msg;
        el.className = "toast" + (kind ? " " + kind : "");
        el.hidden = false;
        setTimeout(() => { el.hidden = true; }, 3200);
    }

    async function api(path, opts) {
        opts = opts || {};
        if (opts.body && typeof opts.body !== "string") {
            opts.body = JSON.stringify(opts.body);
            opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
        }
        const res = await fetch(path, opts);
        const ct = res.headers.get("content-type") || "";
        let data;
        if (ct.includes("application/json")) {
            try { data = await res.json(); } catch { data = null; }
        } else {
            data = { ok: res.ok, _text: await res.text() };
        }
        if (!res.ok && (!data || data.ok === undefined)) {
            throw new Error(`HTTP ${res.status} on ${path}`);
        }
        return data;
    }

    // ----------------------------- state -------------------------------

    const CART_KEY = "aaa.automator.cart.v1";
    let profile = null;
    let cart = loadCart();
    const cartListeners = [];

    function withUniqueTrackIds(album) {
        // Older carts and provider responses may have no `id` at all. These
        // keys are only for this album's lyric choices; `source_id` is kept.
        return {
            ...album,
            tracks: (album.tracks || []).map((track, index) => ({
                ...track,
                id: `${album.id}:${index + 1}`,
            })),
        };
    }

    function loadCart() {
        try {
            const raw = localStorage.getItem(CART_KEY);
            if (!raw) return { albums: [] };
            const parsed = JSON.parse(raw);
            if (!parsed || !Array.isArray(parsed.albums)) return { albums: [] };
            return { ...parsed, albums: parsed.albums.map(withUniqueTrackIds) };
        } catch {
            return { albums: [] };
        }
    }
    function saveCart() {
        try {
            localStorage.setItem(CART_KEY, JSON.stringify(cart));
        } catch {}
        cartListeners.forEach((fn) => { try { fn(cart); } catch {} });
    }

    function addToCart(album, tracks) {
        // De-dupe by collectionId
        if (cart.albums.some((a) => String(a.id) === String(album.id))) return false;
        cart.albums.push(withUniqueTrackIds({
            id: album.id,
            title: album.title || album.collectionName || "",
            artist: album.artist || album.artistName || "",
            artwork: album.artwork || album.artworkUrl100 || "",
            trackCount: (tracks && tracks.length) || album.track_count || album.trackCount || 0,
            alreadyOnServer: !!album.alreadyOnServer,
            tracks: tracks || [],
            expanded: false,
        }));
        saveCart();
        return true;
    }

    function removeFromCart(id) {
        cart.albums = cart.albums.filter((a) => String(a.id) !== String(id));
        saveCart();
    }

    function clearCart() {
        cart = { albums: [] };
        saveCart();
    }

    function totalTracks() {
        return cart.albums.reduce((n, a) => n + (a.tracks && a.tracks.length ? a.tracks.length : (a.trackCount || 0)), 0);
    }

    // ----------------------------- profile -----------------------------

    async function loadProfile() {
        try {
            const r = await api("/api/automator/profile");
            if (r && r.ok) {
                profile = r;
                renderProfile();
                return profile;
            }
        } catch (e) {
            console.error(e);
        }
        profile = { ok: false };
        renderProfile();
        return profile;
    }

    function renderProfile() {
        const status = $("#automator-profile-status");
        const detail = $("#automator-profile-detail");
        if (!status || !detail) return;
        if (!profile) {
            status.textContent = "Automator";
            detail.innerHTML = "&nbsp;";
            return;
        }
        if (!profile.jellyfin_url || !profile.api_key_masked) {
            status.textHTML = "Automator &mdash; not configured";
            status.className = "profile-state-warn";
            detail.innerHTML = `<span>Click <strong>Edit settings</strong> to connect a Jellyfin server.</span>`;
            return;
        }
        status.textContent = `Connected: ${esc(profile.server_name || "Jellyfin")}`;
        status.className = "profile-state-ok";
        const backupLine = profile.backup && profile.backup.enabled
            ? `<span>Backup: <strong>${esc(profile.backup.mode)}</strong> &rarr; <span class="profile-key">${esc(profile.backup.path || "")}</span></span>`
            : `<span>Backup: <strong>off</strong></span>`;
        detail.innerHTML = `
            <span>URL: <span class="profile-key">${esc(profile.jellyfin_url)}</span></span>
            <span>Music folder: <span class="profile-key">${esc(profile.music_folder || "")}</span></span>
            <span>Whisper: <strong>${esc(profile.whisper_model)}</strong> / ${esc(profile.whisper_device)}</span>
            ${backupLine}
        `;
    }

    // ----------------------------- modals ------------------------------

    function openModal(id) {
        const m = $(id);
        if (!m) return;
        m.hidden = false;
        document.body.style.overflow = "hidden";
        // Move focus to first focusable element
        const focusable = m.querySelector("input, select, textarea, button:not([data-modal-close])");
        if (focusable) setTimeout(() => focusable.focus(), 30);
    }
    function closeModal(id) {
        const m = $(id);
        if (!m) return;
        m.hidden = true;
        document.body.style.overflow = "";
    }
    function wireModalDismissers() {
        document.addEventListener("click", (e) => {
            const closer = e.target.closest("[data-modal-close]");
            if (!closer) return;
            const m = closer.closest(".modal");
            if (m) closeModal("#" + m.id);
        });
    }

    // ----------------------------- settings form -----------------------

    function openSettings() {
        const f = $("#automator-settings-form");
        if (!f) return;
        const p = profile || {};
        $("#ap-server-name").value = p.server_name || "";
        $("#ap-jellyfin-url").value = p.jellyfin_url || "";
        $("#ap-api-key").value = ""; // never echo the masked key back
        $("#ap-api-key").placeholder = p.api_key_masked ? `current: ${p.api_key_masked} (paste to change)` : "X-Emby-Token";
        $("#ap-music-folder").value = p.music_folder || "";
        $("#ap-whisper-model").value = p.whisper_model || "turbo";
        $("#ap-whisper-device").value = p.whisper_device || "auto";
        const bk = p.backup || {};
        $("#ap-backup-enabled").checked = !!bk.enabled;
        $("#ap-backup-mode").value = bk.mode || "skip";
        $("#ap-backup-path").value = bk.path || "";
        $("#automator-settings-feedback").textContent = "";
        _trackProviderOrder = null;
        // Discover search providers are loaded async — fetch them now so
        // the modal renders populated checkboxes when it appears.
        loadDiscoverProviders().then(populateDiscoverProviderChoices);
        openModal("#automator-settings-modal");
    }

    // ----------------------------- discover providers --------------------

    let _discoverState = null;

    async function loadDiscoverProviders() {
        try {
            const r = await api("/api/settings/discover");
            if (r && r.ok) {
                _discoverState = r;
                return r;
            }
        } catch (e) { /* fall through */ }
        _discoverState = { providers: ["all"], track_providers: ["all"], available: [], active: [], active_track_providers: [] };
        return _discoverState;
    }

    function populateDiscoverProviderChoices(state) {
        const list = $("#ap-discover-providers-list");
        if (!list || !state) return;
        list.innerHTML = "";
        const all = state.available && state.available.length ? state.available : [];
        const isAll = !state.providers || state.providers.includes("all") || state.providers.length === 0;
        const initialSelected = new Set(isAll ? ["all"] : state.providers.filter((n) => n !== "all"));

        function makeChip(value, label, checked) {
            const wrap = document.createElement("label");
            wrap.className = "checkbox-row ap-discover-chip";
            wrap.dataset.value = value;
            wrap.style.padding = ".25rem .5rem";
            wrap.style.border = "1px solid var(--border)";
            wrap.style.borderRadius = "6px";
            wrap.style.cursor = "pointer";
            wrap.style.background = checked ? "var(--bg-elevated)" : "transparent";
            const cb = document.createElement("input");
            cb.type = "checkbox";
            cb.value = value;
            cb.checked = !!checked;
            cb.dataset.discoverProvider = value;
            cb.style.marginRight = "6px";
            const span = document.createElement("span");
            span.textContent = label;
            wrap.appendChild(cb);
            wrap.appendChild(span);
            // Native <label> already toggles the input on click; we don't
            // need a JS click handler. Just style the chip when its
            // checkbox state changes.
            cb.addEventListener("change", () => {
                wrap.style.background = cb.checked ? "var(--bg-elevated)" : "transparent";
            });
            return wrap;
        }

        list.appendChild(makeChip("all", "All", isAll));
        for (const name of all) {
            const label = ({
                itunes: "iTunes",
                musicbrainz: "MusicBrainz",
                deezer: "Deezer",
                audiodb: "TheAudioDB",
                spotify: "Spotify",
            })[name] || name;
            list.appendChild(makeChip(name, label, initialSelected.has(name)));
        }

        setupDiscoverProviderListeners(list);
        populateTracklistProviderChoices(state);
    }

    let _trackProviderOrder = null;

    function providerLabel(name) {
        return ({
            itunes: "iTunes",
            musicbrainz: "MusicBrainz",
            deezer: "Deezer",
            audiodb: "TheAudioDB",
            spotify: "Spotify",
        })[name] || name;
    }

    function populateTracklistProviderChoices(state) {
        const list = $("#ap-track-providers-list");
        if (!list || !state) return;
        const available = state.available || [];
        const saved = state.track_providers || ["all"];
        if (_trackProviderOrder === null) {
            _trackProviderOrder = (!saved.length || saved.includes("all"))
                ? available.slice()
                : saved.filter((name) => available.includes(name));
            if (!_trackProviderOrder.length) _trackProviderOrder = available.slice();
        }

        list.innerHTML = "";
        const reset = document.createElement("button");
        reset.type = "button";
        reset.className = "btn btn-ghost btn-sm track-provider-reset";
        reset.textContent = "Use all defaults";
        reset.addEventListener("click", () => {
            _trackProviderOrder = available.slice();
            populateTracklistProviderChoices(state);
        });
        list.appendChild(reset);

        const orderHeading = document.createElement("p");
        orderHeading.className = "muted small track-provider-heading";
        orderHeading.textContent = "Selected order (fallback priority)";
        list.appendChild(orderHeading);

        const names = [..._trackProviderOrder, ...available.filter((name) => !_trackProviderOrder.includes(name))];
        names.forEach((name) => {
            const selectedIndex = _trackProviderOrder.indexOf(name);
            const row = document.createElement("div");
            row.className = "track-provider-row" + (selectedIndex >= 0 ? " is-selected" : "");

            const label = document.createElement("label");
            label.className = "checkbox-row track-provider-choice";
            const checkbox = document.createElement("input");
            checkbox.type = "checkbox";
            checkbox.checked = selectedIndex >= 0;
            checkbox.dataset.trackProvider = name;
            checkbox.setAttribute("aria-label", `Use ${providerLabel(name)} for album tracklists`);
            const nameText = document.createElement("span");
            nameText.textContent = providerLabel(name);
            label.append(checkbox, nameText);
            row.appendChild(label);

            const priority = document.createElement("span");
            priority.className = "track-provider-priority muted small";
            priority.textContent = selectedIndex >= 0 ? `#${selectedIndex + 1}` : "Not used";
            row.appendChild(priority);

            if (selectedIndex >= 0) {
                const actions = document.createElement("div");
                actions.className = "track-provider-order-actions";
                [["up", "Move up"], ["down", "Move down"]].forEach(([direction, labelText]) => {
                    const button = document.createElement("button");
                    button.type = "button";
                    button.className = "btn btn-ghost btn-sm";
                    button.dataset.moveTrackProvider = name;
                    button.dataset.direction = direction;
                    button.textContent = direction === "up" ? "↑" : "↓";
                    button.setAttribute("aria-label", `${labelText} ${providerLabel(name)} in fallback order`);
                    button.title = labelText;
                    button.disabled = direction === "up" ? selectedIndex === 0 : selectedIndex === _trackProviderOrder.length - 1;
                    actions.appendChild(button);
                });
                row.appendChild(actions);
            }
            list.appendChild(row);
        });

        if (list.dataset.wired === "1") return;
        list.dataset.wired = "1";
        list.addEventListener("change", (event) => {
            const checkbox = event.target.closest("input[data-track-provider]");
            if (!checkbox) return;
            if (checkbox.checked) {
                if (!_trackProviderOrder.includes(checkbox.dataset.trackProvider)) _trackProviderOrder.push(checkbox.dataset.trackProvider);
            } else if (_trackProviderOrder.length > 1) {
                _trackProviderOrder = _trackProviderOrder.filter((name) => name !== checkbox.dataset.trackProvider);
            } else {
                checkbox.checked = true;
                return;
            }
            populateTracklistProviderChoices(_discoverState || state);
            list.querySelector(`input[data-track-provider="${checkbox.dataset.trackProvider}"]`)?.focus();
        });
        list.addEventListener("click", (event) => {
            const button = event.target.closest("button[data-move-track-provider]");
            if (!button) return;
            const index = _trackProviderOrder.indexOf(button.dataset.moveTrackProvider);
            const next = button.dataset.direction === "up" ? index - 1 : index + 1;
            if (index < 0 || next < 0 || next >= _trackProviderOrder.length) return;
            const provider = button.dataset.moveTrackProvider;
            [_trackProviderOrder[index], _trackProviderOrder[next]] = [_trackProviderOrder[next], _trackProviderOrder[index]];
            populateTracklistProviderChoices(_discoverState || state);
            const nextFocus = list.querySelector(`button[data-move-track-provider="${provider}"][data-direction="${button.dataset.direction}"]`);
            (nextFocus && !nextFocus.disabled ? nextFocus : list.querySelector(`input[data-track-provider="${provider}"]`))?.focus();
        });
    }

    function collectTracklistProviders() {
        const list = $("#ap-track-providers-list");
        if (!list) return ["all"];
        const selected = [...list.querySelectorAll("input[data-track-provider]:checked")]
            .map((checkbox) => checkbox.dataset.trackProvider);
        if (!selected.length) return ["all"];
        const available = _discoverState?.available || [];
        const defaultsMatch = selected.length === available.length
            && selected.every((name, index) => name === available[index]);
        return defaultsMatch ? ["all"] : selected;
    }

    function setupDiscoverProviderListeners(list) {
        if (!list || list.dataset.wired === "1") return;
        list.dataset.wired = "1";
        list.addEventListener("change", (e) => {
            const target = e.target;
            if (!(target instanceof HTMLInputElement) || target.type !== "checkbox") return;
            const allCb = list.querySelector("input[value='all']");
            if (!allCb) return;
            const others = [...list.querySelectorAll("input[type=checkbox]")]
                .filter((c) => c !== allCb);
            if (target === allCb) {
                // All toggled: if now checked, clear every individual pick.
                if (allCb.checked) {
                    others.forEach((c) => {
                        c.checked = false;
                        const chip = c.closest(".ap-discover-chip");
                        if (chip) chip.style.background = "transparent";
                    });
                }
            } else {
                // Individual toggled: drop the "All" selection so the
                // user is free to multi-select individuals.
                if (allCb.checked) {
                    allCb.checked = false;
                    const allChip = allCb.closest(".ap-discover-chip");
                    if (allChip) allChip.style.background = "transparent";
                }
                // If nothing is selected, snap back to "All".
                if (!others.some((c) => c.checked)) {
                    allCb.checked = true;
                    const allChip = allCb.closest(".ap-discover-chip");
                    if (allChip) allChip.style.background = "var(--bg-elevated)";
                }
            }
        });
    }

    function collectDiscoverProviders() {
        const list = $("#ap-discover-providers-list");
        if (!list) return ["all"];
        const cbs = list.querySelectorAll("input[type=checkbox]:checked");
        const out = [];
        cbs.forEach((c) => out.push(c.value));
        return out.length ? out : ["all"];
    }

    async function saveSettings(e) {
        e.preventDefault();
        const payload = {
            server_name: $("#ap-server-name").value.trim(),
            jellyfin_url: $("#ap-jellyfin-url").value.trim(),
            jellyfin_api_key: $("#ap-api-key").value.trim(), // empty = keep existing
            music_folder: $("#ap-music-folder").value.trim(),
            whisper_model: $("#ap-whisper-model").value,
            whisper_device: $("#ap-whisper-device").value,
            backup: {
                enabled: $("#ap-backup-enabled").checked,
                mode: $("#ap-backup-mode").value,
                path: $("#ap-backup-path").value.trim(),
            },
        };
        try {
            const r = await api("/api/automator/profile", { method: "POST", body: payload });
            if (r && r.ok) {
                profile = r;
                renderProfile();
                // Save discover providers independently — failure here is
                // not fatal, just shown in the feedback line.
                const disc = await api("/api/settings/discover", {
                    method: "POST", body: {
                        providers: collectDiscoverProviders(),
                        track_providers: collectTracklistProviders(),
                    },
                });
                if (!disc || !disc.ok) {
                    $("#automator-settings-feedback").textContent =
                        "Saved Jellyfin settings, but discover providers failed: " +
                        (disc && disc.error || "unknown");
                    return;
                }
                $("#automator-settings-feedback").textContent = "Saved.";
                setTimeout(() => closeModal("#automator-settings-modal"), 600);
            } else {
                $("#automator-settings-feedback").textContent = "Save failed: " + (r && r.error || "unknown");
            }
        } catch (err) {
            $("#automator-settings-feedback").textContent = "Save error: " + err.message;
        }
    }

    async function testConnection(intoEl) {
        intoEl = intoEl || $("#automator-settings-feedback") || document.body;
        intoEl.textContent = "Testing\u2026";
        try {
            const r = await api("/api/automator/test-connection", { method: "POST" });
            if (r && r.ok) {
                intoEl.innerHTML = `<span style="color:var(--success);">Connected to <strong>${esc(r.server_name)}</strong> v${esc(r.version)}</span>`;
            } else {
                intoEl.innerHTML = `<span style="color:var(--danger);">${esc(r && r.error || "Connection failed")}</span>`;
            }
            return r;
        } catch (e) {
            intoEl.innerHTML = `<span style="color:var(--danger);">${esc(e.message)}</span>`;
            return null;
        }
    }

    // ----------------------------- search ------------------------------

    async function runSearch(q) {
        const grid = $("#automator-search-grid");
        const meta = $("#automator-search-meta");
        if (!grid || !meta) return;
        if (!q || !q.trim()) {
            grid.innerHTML = "";
            meta.textContent = "Type a query and press Search.";
            return;
        }
        grid.setAttribute("aria-busy", "true");
        grid.innerHTML = placeholderCards(8);
        meta.textContent = "Searching\u2026";
        try {
            const r = await api("/api/automator/search/albums?q=" + encodeURIComponent(q) + "&limit=25");
            if (!r || !r.ok) {
                grid.innerHTML = "";
                meta.textContent = r && r.error || "Search failed.";
                return;
            }
            const albums = r.albums || [];
            if (!albums.length) {
                grid.innerHTML = "";
                meta.textContent = `No album or song-title matches for "${esc(q)}".`;
                return;
            }
            const songMatches = albums.filter((album) => album.matched_song).length;
            meta.textContent = `${albums.length} album${albums.length === 1 ? "" : "s"}${songMatches ? ` · ${songMatches} found from song search` : ""}`;
            renderAlbumGrid(grid, albums);
        } catch (e) {
            meta.textContent = "Error: " + e.message;
            grid.innerHTML = "";
        } finally {
            grid.removeAttribute("aria-busy");
        }
    }

    function placeholderCards(n) {
        const cards = [];
        for (let i = 0; i < n; i++) cards.push('<div class="ph-card" aria-hidden="true"></div>');
        return cards.join("");
    }

    function renderAlbumGrid(grid, albums) {
        grid.innerHTML = albums.map((a) => {
            const id = a.id;
            const title = esc(a.title || "");
            const artist = esc(a.artist || "");
            const art = esc(a.artwork || "");
            const year = (a.release_date || "").slice(0, 4);
            const genre = esc(a.genre || "");
            const onserver = !!a.alreadyOnServer;
            const addLabel = onserver ? "In library" : "Add album";
            const addDisabled = onserver ? " disabled" : "";
            const addTitle = onserver
                ? "This album is already in your Jellyfin library"
                : "Add album to queue";
            const yearLine = year ? `<p class="album-meta-line muted small">${esc(year)}${genre ? " &middot; " + genre : ""}</p>` : "";
            const songMatchLine = a.matched_song
                ? `<p class="album-meta-line song-match-line">Song match · ${esc(a.matched_song)}</p>`
                : "";
            return `
            <article class="album-card${onserver ? " is-onserver" : ""}" data-album-id="${id}"
                     data-title="${esc(a.title || '')}"
                     data-artist="${esc(a.artist || '')}"
                     data-year="${esc(year)}"
                     tabindex="0" role="button"
                     aria-label="Open ${title} \u2014 ${artist}">
                <div class="album-cover-wrap">
                    <div class="album-cover-placeholder" aria-hidden="true">&#9836;</div>
                    <img class="album-cover" alt="" loading="lazy" decoding="async" hidden>
                </div>
                <div class="album-body">
                    <h3 class="album-title">${title}</h3>
                    <p class="album-artist">${artist}</p>
                    ${yearLine}
                    ${songMatchLine}
                </div>
                <div class="album-card-actions">
                    <button class="album-preview-btn" type="button"
                            data-preview-album="${id}"
                            title="Show this album's songs">Show songs</button>
                    <button class="album-add-btn" type="button"
                            data-add-album="${id}"${addDisabled}
                            title="${addTitle}" aria-label="${addTitle}: ${title} by ${artist}">${addLabel}</button>
                </div>
            </article>`;
        }).join("");
        // Match Albums page pattern: store artwork on the card, observer sets img.src.
        albums.forEach((a, i) => {
            const card = grid.children[i];
            if (!card) return;
            card.dataset.artwork = a.artwork || "";
            card.dataset.coverLoaded = "";
        });
        setupCoverObserver(grid);
    }

    let _coverObserver = null;
    function setupCoverObserver(grid) {
        // Match Albums page pattern: observe the CARD, set img.src from
        // card.dataset.artwork when it scrolls into view. This way hidden imgs
        // never trigger a fetch, which avoids the "image never loads" issue
        // when IntersectionObserver roots behave unexpectedly.
        const cards = $$(".album-card", grid);
        if (!("IntersectionObserver" in window)) {
            cards.forEach(loadCoverForCard);
            return;
        }
        if (!_coverObserver) {
            _coverObserver = new IntersectionObserver((entries) => {
                for (const entry of entries) {
                    if (!entry.isIntersecting) continue;
                    loadCoverForCard(entry.target);
                    _coverObserver.unobserve(entry.target);
                }
            }, { root: null, rootMargin: "300px 0px", threshold: 0.01 });
        }
        cards.forEach((c) => _coverObserver.observe(c));
    }

    function loadCoverForCard(card) {
        if (!card || card.dataset.coverLoaded) return;
        card.dataset.coverLoaded = "1";
        const cover = card.dataset.artwork || card.dataset.cover || "";
        if (!cover) return;
        const img = card.querySelector(".album-cover");
        if (!img) return;
        img.hidden = false;
        img.onload = () => img.classList.add("loaded");
        img.onerror = () => { img.hidden = true; };
        img.src = cover;
    }

    // Click delegation: add to cart from search / recent grid
    function wireAlbumGridClicks() {
        ["#automator-search-grid", "#automator-recent-grid"].forEach((sel) => {
            const grid = $(sel);
            if (!grid) return;
            grid.addEventListener("click", (e) => {
                const addBtn = e.target.closest("[data-add-album]");
                if (addBtn) {
                    e.stopPropagation();
                    const card = addBtn.closest(".album-card");
                    handleAddToCart(card, String(card.dataset.albumId || ""), addBtn);
                    return;
                }
                const previewBtn = e.target.closest("[data-preview-album]");
                if (previewBtn) {
                    e.stopPropagation();
                    const card = previewBtn.closest(".album-card");
                    toggleTracklistPreview(card, previewBtn);
                    return;
                }
                // Click on the card body (not the buttons) also opens preview.
                const card = e.target.closest(".album-card");
                if (card) {
                    const previewBtn = card.querySelector("[data-preview-album]");
                    if (previewBtn) toggleTracklistPreview(card, previewBtn);
                }
            });
            grid.addEventListener("keydown", (e) => {
                if (e.key !== "Enter" && e.key !== " ") return;
                const addBtn = e.target.closest("[data-add-album]");
                if (addBtn) {
                    e.preventDefault();
                    const card = addBtn.closest(".album-card");
                    handleAddToCart(card, String(card.dataset.albumId || ""), addBtn);
                    return;
                }
                const previewBtn = e.target.closest("[data-preview-album]");
                if (previewBtn) {
                    e.preventDefault();
                    const card = previewBtn.closest(".album-card");
                    toggleTracklistPreview(card, previewBtn);
                }
            });
        });
    }

    // Fetch + render the tracklist inline below the album card. Idempotent:
    // clicking again collapses the open panel.
    async function toggleTracklistPreview(card, btn) {
        const panel = card.querySelector(".album-tracklist");
        if (panel) {
            const open = !panel.hidden;
            panel.hidden = open;
            btn.textContent = open ? "Show songs" : "Hide songs";
            return;
        }
        btn.disabled = true;
        const original = btn.textContent;
        btn.textContent = "Loading songs\u2026";
        try {
            const title = card.dataset.title || "";
            const artist = card.dataset.artist || "";
            const id = String(card.dataset.albumId || "");
            const params = new URLSearchParams();
            if (title) params.set("title", title);
            if (artist) params.set("artist", artist);
            const qs = params.toString();
            const r = await api("/api/automator/album/" + encodeURIComponent(id) + "/tracks" + (qs ? "?" + qs : ""));
            if (!r || !r.ok) {
                toast("Could not load tracks: " + (r && r.error || "unknown"), "err");
                btn.textContent = original;
                btn.disabled = false;
                return;
            }
            renderTracklist(card, r);
            btn.textContent = "Hide songs";
            btn.disabled = false;
        } catch (e) {
            toast("Could not load tracks: " + e.message, "err");
            btn.textContent = original;
            btn.disabled = false;
        }
    }

    function renderTracklist(card, payload) {
        const tracks = payload.tracks || [];
        const album = payload.album || {};
        const okProviders = payload.providers
            ? Object.values(payload.providers).filter((p) => p && p.ok).length
            : 0;
        const sourceLine = payload.source
            ? `<span class="muted small">via ${esc(payload.source)}${okProviders ? " (" + okProviders + " sources)" : ""}</span>`
            : "";
        const items = tracks.map((t) =>
            `<li class="album-tracklist-item">` +
            `<span class="track-num">${esc(String(t.track_number || ""))}</span>` +
            `<span class="track-title">${esc(t.title || "")}</span>` +
            `<span class="track-meta muted small">${formatDur(t.duration_ms)}${t.source ? " &middot; " + esc(t.source) : ""}</span>` +
            `</li>`
        ).join("");
        const listHtml = tracks.length
            ? `<ol class="album-tracklist-list">${items}</ol>`
            : `<p class="muted small">No tracks listed by any source for this album.</p>`;
        const panel = document.createElement("div");
        panel.className = "album-tracklist";
        panel.innerHTML =
            `<div class="album-tracklist-head">` +
            `<strong>${esc(album.title || "")}</strong>` +
            sourceLine +
            `</div>` +
            listHtml;
        card.appendChild(panel);
    }

    function formatDur(ms) {
        ms = ms || 0;
        if (!ms) return "";
        const s = Math.round(ms / 1000);
        const m = Math.floor(s / 60);
        const r = s % 60;
        return `${m}:${String(r).padStart(2, "0")}`;
    }

    async function handleAddToCart(card, id, btn) {
        const title = card.dataset.title || "";
        const artist = card.dataset.artist || "";
        if (btn.disabled || card.classList.contains("is-onserver")) {
            toast(`"${title}" is already in your Jellyfin library.`);
            return;
        }
        btn.disabled = true;
        btn.textContent = "Loading\u2026";
        try {
            // Pass title + artist so the server can fall back to TheAudioDB
            // when iTunes lookup is unreachable.
            const params = new URLSearchParams();
            if (title) params.set("title", title);
            if (artist) params.set("artist", artist);
            const qs = params.toString();
            const r = await api("/api/automator/album/" + id + "/tracks" + (qs ? "?" + qs : ""));
            if (!r || !r.ok) {
                toast("Could not load album: " + (r && r.error || "unknown"), "err");
                btn.disabled = false;
                btn.textContent = "Add album";
                return;
            }
            const album = r.album || { id, title, artist };
            const tracks = r.tracks || [];
            if (!tracks.length) {
                toast("Album has no tracks.", "err");
                btn.disabled = false;
                btn.textContent = "Add album";
                return;
            }
            const ok = addToCart(album, tracks);
            if (ok) {
                btn.textContent = "Queued";
                toast(`Added "${title}" (${tracks.length} tracks) to cart.`);
            } else {
                btn.textContent = "Already in cart";
                toast(`"${title}" is already in your cart.`);
            }
            renderCart();
        } catch (e) {
            btn.disabled = false;
            btn.textContent = "Add album";
            toast("Add failed: " + e.message, "err");
        }
    }

    // ----------------------------- recent on server --------------------

    async function loadRecent() {
        const grid = $("#automator-recent-grid");
        const meta = $("#automator-recent-meta");
        if (!grid || !meta) return;
        if (!profile || !profile.jellyfin_url) {
            grid.innerHTML = "";
            meta.textContent = "Configure a Jellyfin server first.";
            return;
        }
        grid.setAttribute("aria-busy", "true");
        grid.innerHTML = placeholderCards(6);
        meta.textContent = "Loading\u2026";
        try {
            const r = await api("/api/automator/library/recent?limit=24");
            if (!r || !r.ok) {
                grid.innerHTML = "";
                meta.innerHTML = `<span style="color:var(--danger);">${esc(r && r.error || "Could not reach Jellyfin")}</span>`;
                return;
            }
            const albums = r.albums || [];
            if (!albums.length) {
                grid.innerHTML = "";
                meta.textContent = "No recent albums on the server yet.";
                return;
            }
            meta.textContent = `${albums.length} recently added to Jellyfin`;
            // Reuse the same grid renderer — these are view-only, no Add button.
            grid.innerHTML = albums.map((a) => `
                <article class="album-card is-onserver" data-album-id="${esc(a.id || '')}"
                         data-title="${esc(a.title)}" data-artist="${esc(a.artist)}"
                         data-artwork="${esc(a.artwork || '')}"
                         tabindex="0">
                    <span class="album-onserver" title="Already on Jellyfin">&#10003; on server</span>
                    <div class="album-cover-wrap">
                        <div class="album-cover-placeholder" aria-hidden="true">&#9836;</div>
                        <img class="album-cover" alt="" loading="lazy" decoding="async" hidden>
                    </div>
                    <div class="album-body">
                        <h3 class="album-title">${esc(a.title)}</h3>
                        <p class="album-artist">${esc(a.artist)}</p>
                    </div>
                </article>
            `).join("");
            setupCoverObserver(grid);
        } catch (e) {
            meta.textContent = "Error: " + e.message;
            grid.innerHTML = "";
        } finally {
            grid.removeAttribute("aria-busy");
        }
    }

    // ----------------------------- cart render -------------------------

    function renderCart() {
        const list = $("#automator-cart-list");
        const count = $("#automator-cart-count");
        const review = $("#automator-cart-review");
        const clear = $("#automator-cart-clear");
        if (!list || !count) return;
        if (!cart.albums.length) {
            list.innerHTML = `<p class="muted small">Your queue is empty. Search for an album to get started.</p>`;
            count.textContent = "0";
            review.disabled = true;
            clear.disabled = true;
            return;
        }
        count.textContent = String(cart.albums.length);
        review.disabled = false;
        clear.disabled = false;
        list.innerHTML = cart.albums.map((a) => {
            const trackN = (a.tracks && a.tracks.length) || a.trackCount || 0;
            const art = a.artwork
                ? `<img class="cart-cover" alt="" loading="lazy" decoding="async" src="${esc(a.artwork)}">`
                : `<div class="cart-cover placeholder" aria-hidden="true">&#127926;</div>`;
            const onserver = a.alreadyOnServer
                ? `<span class="cart-onserver">&#10003; on server</span>`
                : "";
            const releaseYear = (a.release_date || "").slice(0, 4);
            const yearStr = releaseYear ? `<span class="cart-year">${esc(releaseYear)}</span>` : "";
            return `
            <div class="cart-item" data-cart-id="${esc(a.id)}">
                ${art}
                <div class="cart-meta">
                    <div class="cart-title">${esc(a.title)}</div>
                    <div class="cart-sub">${esc(a.artist)}${yearStr ? " &middot; " + yearStr : ""}
                        <span class="cart-count">${trackN} track${trackN === 1 ? "" : "s"}</span>
                        ${onserver}
                    </div>
                </div>
                <button class="cart-remove" type="button"
                        aria-label="Remove ${esc(a.title)} from cart"
                        data-remove-cart="${esc(a.id)}">&times;</button>
            </div>`;
        }).join("");
    }

    function wireCartClicks() {
        const list = $("#automator-cart-list");
        if (!list) return;
        list.addEventListener("click", (e) => {
            const rem = e.target.closest("[data-remove-cart]");
            if (!rem) return;
            const id = rem.dataset.removeCart;
            removeFromCart(id);
            renderCart();
        });
        $("#automator-cart-clear").addEventListener("click", () => {
            if (!cart.albums.length) return;
            clearCart();
            renderCart();
        });
        $("#automator-cart-review").addEventListener("click", openDupReview);
    }

    // ----------------------------- duplicate review ----------------------

    // Walk the cart and collect every track that the aggregator flagged.
    function collectFlaggedTracks() {
        const out = []; // { albumIndex, trackIndex, track, reason }
        cart.albums.forEach((a, ai) => {
            (a.tracks || []).forEach((t, ti) => {
                const flag = t.flag;
                if (!flag) return;
                if (flag.type === "renumbered") return; // not user-actionable
                out.push({ albumIndex: ai, trackIndex: ti, track: t, reason: flag });
            });
        });
        return out;
    }

    function openDupReview() {
        if (!cart.albums.length) return;
        if (!profile || !profile.jellyfin_url) {
            toast("Configure Jellyfin first (Edit settings).", "err");
            return;
        }
        const flagged = collectFlaggedTracks();
        const list = $("#automator-dupreview-list");
        const summary = $("#automator-dupreview-summary");
        if (!list || !summary) return;

        if (!flagged.length) {
            summary.innerHTML =
                "<strong>No duplicates flagged.</strong> Auto-dedup already removed exact matches. Continuing to the run-confirmation step.";
            list.innerHTML = "";
            const btn = $("#automator-dupreview-continue");
            if (btn) btn.textContent = "Continue";
            bindDupReviewActions();
            openModal("#automator-dupreview-modal");
            return;
        }

        summary.innerHTML =
            `<strong>${flagged.length}</strong> flagged track${flagged.length === 1 ? "" : "s"} across ${new Set(flagged.map((f) => f.albumIndex)).size} album${new Set(flagged.map((f) => f.albumIndex)).size === 1 ? "" : "s"}. ` +
            `Click <strong>Remove</strong> to drop the duplicate, or <strong>Keep</strong> to add it as-is.`;

        const byAlbum = new Map();
        flagged.forEach((f) => {
            if (!byAlbum.has(f.albumIndex)) byAlbum.set(f.albumIndex, []);
            byAlbum.get(f.albumIndex).push(f);
        });
        const rows = [];
        byAlbum.forEach((items, ai) => {
            const album = cart.albums[ai];
            rows.push(`<div class="dupreview-album-head"><strong>${esc(album.title || "")}</strong> &mdash; ${esc(album.artist || "")}</div>`);
            rows.push('<ul class="dupreview-list">');
            items.forEach((f) => {
                const t = f.track;
                const reason = f.reason.type === "near_duplicate"
                    ? `same title as <code>${esc(f.reason.primary_source || "?")}</code> track but duration differs (${formatDur(t.duration_ms)} vs primary ${formatDur(f.reason.duration_ms_primary)})`
                    : JSON.stringify(f.reason);
                rows.push(`
                    <li class="dupreview-item" data-album="${ai}" data-track="${f.trackIndex}">
                        <div class="dupreview-meta">
                            <span class="track-num">${esc(String(t.track_number || ""))}</span>
                            <span class="track-title">${esc(t.title || "")}</span>
                            <span class="muted small">${esc(reason)}</span>
                        </div>
                        <div class="dupreview-actions">
                            <button type="button" class="btn btn-sm btn-ghost" data-dup="keep">Keep</button>
                            <button type="button" class="btn btn-sm btn-primary" data-dup="remove">Remove</button>
                        </div>
                    </li>`);
            });
            rows.push("</ul>");
        });
        list.innerHTML = rows.join("");
        bindDupReviewActions();
        openModal("#automator-dupreview-modal");
    }

    function bindDupReviewActions() {
        const list = $("#automator-dupreview-list");
        if (!list) return;
        list.addEventListener("click", (e) => {
            const btn = e.target.closest("[data-dup]");
            if (!btn) return;
            const item = btn.closest(".dupreview-item");
            if (!item) return;
            const ai = parseInt(item.dataset.album, 10);
            const ti = parseInt(item.dataset.track, 10);
            const action = btn.dataset.dup;
            if (action === "remove") {
                if (!cart.albums[ai] || !cart.albums[ai].tracks[ti]) return;
                cart.albums[ai].tracks.splice(ti, 1);
                if (cart.albums[ai].trackCount != null) {
                    cart.albums[ai].trackCount = cart.albums[ai].tracks.length;
                }
                saveCart();
                item.remove();
                renderCart();
                toast("Removed.");
            } else {
                item.classList.add("is-kept");
                item.querySelectorAll("button").forEach((b) => b.disabled = true);
                toast("Kept.");
            }
            if (!list.querySelector(".dupreview-item")) {
                $("#automator-dupreview-summary").innerHTML =
                    "<strong>All flagged tracks reviewed.</strong> Continuing to the run-confirmation step.";
            }
        });
        const cont = $("#automator-dupreview-continue");
        if (cont && !cont.dataset.wired) {
            cont.dataset.wired = "1";
            cont.addEventListener("click", () => {
                closeModal("#automator-dupreview-modal");
                openConfirm();
            });
        }
    }

    // ----------------------------- confirm modal -----------------------

    function openConfirm() {
        if (!cart.albums.length) return;
        if (!profile || !profile.jellyfin_url) {
            toast("Configure Jellyfin first (Edit settings).", "err");
            return;
        }
        const list = $("#automator-confirm-list");
        const summary = $("#automator-confirm-summary");
        const estimate = $("#automator-confirm-estimate");
        if (!list || !summary || !estimate) return;

        const trackTotal = totalTracks();
        list.innerHTML = cart.albums.map((a) => {
            const tn = (a.tracks && a.tracks.length) || a.trackCount || 0;
            return `
            <li>
                <span><strong>${esc(a.title)}</strong> \u2014 ${esc(a.artist)}</span>
                <span class="confirm-count">${tn} track${tn === 1 ? "" : "s"}</span>
            </li>`;
        }).join("");

        const whisperModel = profile.whisper_model || "small.en";
        const secondsEst = trackTotal * 240; // ~4 min avg track
        const minutes = Math.ceil(secondsEst / 60);
        summary.innerHTML = `<strong>${cart.albums.length}</strong> album${cart.albums.length === 1 ? "" : "s"} &middot; <strong>${trackTotal}</strong> track${trackTotal === 1 ? "" : "s"} will be:`;
        estimate.innerHTML = `
            <ul style="margin:.25rem 0 .25rem 1.25rem;">
                <li>downloaded from YouTube via yt-dlp</li>
                <li>verified (ffprobe) and tagged (mutagen) with correct title/artist/album/track #</li>
                <li>transcribed with Whisper (<strong>${esc(whisperModel)}</strong>) &rarr; <code>.lrc</code> sidecar</li>
                <li>saved to <code>${esc(profile.music_folder || "")}</code></li>
                <li>Jellyfin library refresh triggered at the end</li>
            </ul>
            <div class="hint">Estimated time: <strong>~${minutes} min</strong> for downloads + transcription. The jobs panel stays live throughout.</div>
        `;

        const cb = $("#automator-confirm-backup");
        const hint = $("#automator-confirm-backup-hint");
        const bk = profile.backup || {};
        if (bk.enabled && bk.mode !== "skip") {
            cb.disabled = false;
            cb.checked = true;
            hint.textContent = `(${esc(bk.mode)} \u2192 ${esc(bk.path || "")})`;
        } else {
            cb.disabled = true;
            cb.checked = false;
            hint.textContent = "(backup not configured in settings)";
        }
        openModal("#automator-confirm-modal");
    }

    async function runConfirmed() {
        const cb = $("#automator-confirm-backup");
        const backup = cb.checked;
        const albums = cart.albums.map((a) => ({
            id: a.id,
            title: a.title,
            artist: a.artist,
            release_date: a.release_date || "",
            tracks: a.tracks || [],
        }));
        const btn = $("#automator-confirm-go");
        btn.disabled = true;
        // Flatten tracks across all cart albums for the batch lyrics lookup
        const allTracks = [];
        albums.forEach((a) => {
            (a.tracks || []).forEach((t) => {
                allTracks.push({
                    id: t.id,
                    title: t.title,
                    artist: t.artist || a.artist,
                    album: a.title || "",
                    duration_ms: t.duration_ms || 0,
                });
            });
        });
        let lyricsResults = [];
        if (allTracks.length) {
            try {
                lyricsResults = await pollLyricsLookup(allTracks, (done, total, current) => {
                    const cur = current ? ` — ${trunc(current, 36)}` : "";
                    btn.textContent = `Looking up lyrics… ${done}/${total}${cur}`;
                });
            } catch (e) {
                console.warn("lyrics lookup failed; will fall back to Whisper for all", e);
            }
        }
        closeModal("#automator-confirm-modal");
        await runLyricsReview(albums, lyricsResults, backup);
        btn.disabled = false;
        btn.textContent = "Review lyrics";
    }

    // Kick off the lyrics lookup and poll until it finishes. Calls
    // onProgress(done,total,current) every ~800ms so the UI can show
    // "Looking up lyrics… X/Y — track name". On failure, returns [].
    async function pollLyricsLookup(tracks, onProgress) {
        let startResp;
        try {
            startResp = await api("/api/automator/lookup-lyrics",
                                  { method: "POST", body: { tracks } });
        } catch (e) {
            console.warn("lookup-lyrics start failed:", e);
            return [];
        }
        if (!startResp || !startResp.ok || !startResp.lookup_id) {
            console.warn("lookup-lyrics start returned no lookup_id:", startResp);
            return [];
        }
        const lookupId = startResp.lookup_id;
        const total = startResp.total || tracks.length;
        const POLL_MS = 800;
        const MAX_POLLS = Math.ceil((total * 5 + 30) * (1000 / POLL_MS)); // ~5s/track worst case + 30s
        for (let i = 0; i < MAX_POLLS; i++) {
            await sleep(POLL_MS);
            let st;
            try {
                st = await api(`/api/automator/lookup-lyrics/status/${lookupId}`);
            } catch (e) {
                console.warn("lookup-lyrics poll failed:", e);
                continue;
            }
            if (!st || !st.ok) continue;
            if (typeof onProgress === "function") {
                try { onProgress(st.done || 0, st.total || total, st.current || ""); } catch (_) {}
            }
            if (st.status === "done") return st.results || [];
            if (st.status === "error") {
                console.warn("lookup-lyrics finished with error:", st.error);
                return [];
            }
        }
        console.warn("lookup-lyrics polling timed out after", MAX_POLLS, "ticks");
        return [];
    }

    function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
    function trunc(s, n) {
        s = String(s || "");
        return s.length <= n ? s : s.slice(0, n - 1) + "\u2026";
    }

    // ----------------------------- lyrics review ----------------------

    let _lyricsReviewState = null;

    async function runLyricsReview(albums, lyricsResults, backup) {
        const decisions = {};   // track_id -> "lyrics" | "whisper" | "manual" | "none"
        const manual_lyrics = {}; // track_id -> {lrc_text, language, source} (set by manual search modal)
        let withLyrics = 0, instrumental = 0;
        lyricsResults.forEach((r) => {
            const k = String(r.track_id);
            if (r.lyrics && r.lyrics.instrumental) {
                decisions[k] = "none";
                instrumental++;
            } else if (r.lyrics && r.lyrics.available && r.lyrics.lrc_text) {
                decisions[k] = "lyrics";
                withLyrics++;
            } else {
                decisions[k] = "whisper";
            }
        });

        const list = $("#automator-subs-list");
        const progress = $("#automator-subs-progress");
        const count = $("#automator-subs-count");
        const go = $("#automator-subs-go");
        const allSubs = $("#automator-subs-all-subs");
        const allWhisper = $("#automator-subs-all-whisper");
        if (!list || !go) return;

        // Build rows
        const rows = [];
        lyricsResults.forEach((r) => {
            const hasLyrics = !!(r.lyrics && r.lyrics.available && r.lyrics.lrc_text);
            const isInstr = !!(r.lyrics && r.lyrics.instrumental);
            const synced = !!(r.lyrics && r.lyrics.synced);
            const lang = (r.lyrics && r.lyrics.language) || "";
            const preview = (r.lyrics && r.lyrics.preview) || "";
            const errMsg = (r.lyrics && r.lyrics.error) || "";
            let badge, badgeTitle;
            if (isInstr) {
                badge = "instrumental"; badgeTitle = "LRCLIB marks this as an instrumental track";
            } else if (hasLyrics && synced) {
                badge = "synced"; badgeTitle = "Synced lyrics from LRCLIB";
            } else if (hasLyrics) {
                badge = "plain"; badgeTitle = "Plain (unsynced) lyrics from LRCLIB";
            } else if (errMsg) {
                badge = "no match"; badgeTitle = errMsg;
            } else {
                badge = "no match"; badgeTitle = "No lyrics found in LRCLIB";
            }
            const tn = String(r.track_number || "").padStart(2, "0");
            const initialDec = decisions[String(r.track_id)] || "whisper";
            rows.push(`
            <li class="subs-row" data-track-id="${esc(r.track_id)}"
                data-has-subs="${hasLyrics ? "1" : "0"}"
                data-artist="${esc(r.artist || "")}"
                data-title="${esc(r.title || "")}"
                data-album="${esc(r.album || "")}"
                data-duration-ms="${esc(r.duration_ms || 0)}">
                <div class="subs-row-head">
                    <div class="subs-row-title">
                        <strong>${tn}</strong> &middot; ${esc(r.title || "(untitled)")}
                        <span class="muted small">&middot; ${esc(r.artist || "")}</span>
                    </div>
                    <div class="subs-toggle">
                        <span class="subs-lang-badge" title="${esc(badgeTitle)}">${esc(badge)}</span>
                        <label class="subs-radio">
                            <input type="radio" name="lyrics-${esc(r.track_id)}" value="lyrics"
                                   ${hasLyrics ? "" : "disabled"}${initialDec === "lyrics" ? " checked" : ""}>
                            <span>Lyrics</span>
                        </label>
                        <label class="subs-radio">
                            <input type="radio" name="lyrics-${esc(r.track_id)}" value="whisper"
                                   ${initialDec === "whisper" ? " checked" : ""}>
                            <span>Whisper</span>
                        </label>
                        <label class="subs-radio">
                            <input type="radio" name="lyrics-${esc(r.track_id)}" value="manual"
                                   ${initialDec === "manual" ? " checked" : ""}>
                            <span>Manual</span>
                        </label>
                        <label class="subs-radio">
                            <input type="radio" name="lyrics-${esc(r.track_id)}" value="none"
                                   ${initialDec === "none" ? " checked" : ""}>
                            <span>No lyrics</span>
                        </label>
                    </div>
                </div>
                <div class="subs-row-foot">
                    <button class="btn btn-ghost btn-xs subs-manual-btn" type="button"
                            data-track-id="${esc(r.track_id)}"
                            ${initialDec === "manual" ? "" : "hidden"}>
                        ${manual_lyrics[String(r.track_id)] ? "Change pick" : "Search manually"}
                    </button>
                    ${initialDec === "manual" && manual_lyrics[String(r.track_id)]
                        ? `<span class="muted small subs-manual-summary">picked (${(manual_lyrics[String(r.track_id)].lrc_text || "").split("\n").length} lines)</span>`
                        : ""}
                </div>
                ${isInstr
                    ? `<p class="muted small">LRCLIB marks this track as instrumental, so no lyrics are selected.</p>`
                    : hasLyrics
                    ? `<pre class="subs-preview">${esc(preview || "(empty)")}</pre>`
                    : `<p class="muted small">No lyrics in LRCLIB for this track${errMsg ? ` \u2014 ${esc(errMsg)}` : ""}. Pick <em>Whisper</em> to transcribe the audio, <em>Manual</em> to search LRCLIB yourself, or <em>No lyrics</em> to skip.</p>`}
            </li>`);
        });
        list.innerHTML = rows.join("");

        const updateSelectionCount = () => {
            const picks = Object.values(decisions);
            if (count) count.textContent = `${picks.filter((pick) => pick === "lyrics").length} lyrics, ${picks.filter((pick) => pick === "whisper").length} whisper, ${picks.filter((pick) => pick === "manual").length} manual, ${picks.filter((pick) => pick === "none").length} skipped`;
        };
        list.onchange = (e) => {
            const radio = e.target.closest("input[type=radio]");
            if (!radio) return;
            const row = radio.closest(".subs-row");
            if (!row) return;
            const k = String(row.dataset.trackId);
            decisions[k] = radio.value;
            // If user switched away from manual, clear their pick so it can't leak.
            if (radio.value !== "manual") {
                delete manual_lyrics[k];
            }
            refreshManualButton(row, k);
            updateSelectionCount();
        };
        allSubs.disabled = withLyrics === 0;
        allWhisper.disabled = false;
        allSubs.onclick = () => {
            $$("input[type=radio][value=lyrics]", list).forEach((r) => {
                if (!r.disabled) { r.checked = true; decisions[String(r.closest(".subs-row").dataset.trackId)] = "lyrics"; }
            });
            updateSelectionCount();
        };
        allWhisper.onclick = () => {
            $$("input[type=radio][value=whisper]", list).forEach((r) => {
                r.checked = true;
                const k = String(r.closest(".subs-row").dataset.trackId);
                decisions[k] = "whisper";
                delete manual_lyrics[k];
                refreshManualButton(r.closest(".subs-row"), k);
            });
            updateSelectionCount();
        };

        // Per-track manual-search button (delegated)
        list.onclick = (e) => {
            const btn = e.target.closest(".subs-manual-btn");
            if (!btn) return;
            const row = btn.closest(".subs-row");
            if (!row) return;
            openManualSearchModal(row);
        };

        if (progress) progress.textContent = withLyrics
            ? `Found lyrics for ${withLyrics} of ${lyricsResults.length} tracks. ${instrumental ? `${instrumental} instrumental track${instrumental === 1 ? "" : "s"} set to No lyrics. ` : ""}LRCLIB lyrics are selected where available.`
            : `No lyrics found in LRCLIB. ${instrumental ? `${instrumental} instrumental track${instrumental === 1 ? "" : "s"} set to No lyrics. ` : ""}Whisper is selected for the other tracks by default.`;
        updateSelectionCount();
        go.disabled = false;
        _lyricsReviewState = { albums, decisions, backup, lyricsResults, manual_lyrics };
        openModal("#automator-subs-modal");
    }

    // ----------------------- manual LRCLIB search modal -----------------------

    let _manualSearchRow = null;

    // Refresh the per-track manual-search button visibility + summary text.
    // Reads from _lyricsReviewState so it can be called from outside runLyricsReview
    // (e.g. from useManualResult when a pick is made via the modal).
    function refreshManualButton(row, k) {
        if (!row || !k) return;
        const btn = row.querySelector(".subs-manual-btn");
        const sum = row.querySelector(".subs-manual-summary");
        const state = _lyricsReviewState || {};
        const dec = (state.decisions || {})[k];
        const picks = state.manual_lyrics || {};
        if (dec === "manual") {
            if (btn) { btn.hidden = false; btn.textContent = picks[k] ? "Change pick" : "Search manually"; }
            if (picks[k]) {
                const lineCount = (picks[k].lrc_text || "").split("\n").length;
                if (!sum) {
                    const s = document.createElement("span");
                    s.className = "muted small subs-manual-summary";
                    s.textContent = `picked (${lineCount} lines)`;
                    row.querySelector(".subs-row-foot").appendChild(s);
                } else {
                    sum.textContent = `picked (${lineCount} lines)`;
                }
            } else if (sum) {
                sum.remove();
            }
        } else {
            if (btn) btn.hidden = true;
            if (sum) sum.remove();
        }
    }

    async function openManualSearchModal(row) {
        if (!row) return;
        _manualSearchRow = row;
        const k = String(row.dataset.trackId);
        const state = _lyricsReviewState || {};
        const existing = (state.manual_lyrics || {})[k];
        const artistInp = $("#manual-search-artist");
        const titleInp = $("#manual-search-title");
        const resultsEl = $("#manual-search-results");
        const statusEl = $("#manual-search-status");
        if (!artistInp || !titleInp || !resultsEl) return;
        artistInp.value = row.dataset.artist || "";
        titleInp.value = row.dataset.title || "";
        statusEl.textContent = existing
            ? "You already picked a result. Searching will replace it."
            : "Type a query, then press Search LRCLIB.";
        resultsEl.innerHTML = "";
        if (existing) {
            renderManualResult(existing, true);
        }
        openModal("#automator-manual-search-modal");
        setTimeout(() => $("#manual-search-go") && $("#manual-search-go").focus(), 30);
    }

    function renderManualResult(item, isPicked) {
        const resultsEl = $("#manual-search-results");
        if (!resultsEl) return;
        const id = `manual-result-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
        const lineCount = (item.lrc_text || "").split("\n").filter((l) => l.trim()).length;
        const syncBadge = item.synced
            ? `<span class="subs-lang-badge" title="Synced timestamps">synced</span>`
            : `<span class="subs-lang-badge" title="Plain (no timestamps)">plain</span>`;
        const langBadge = item.language
            ? `<span class="subs-lang-badge">${esc(item.language)}</span>`
            : "";
        const card = document.createElement("div");
        card.className = "manual-result-card";
        card.id = id;
        card.innerHTML = `
            <div class="manual-result-head">
                <strong>${esc(item.track_name || item.title || "?")}</strong>
                <span class="muted small"> &middot; ${esc(item.artist_name || item.artist || "")}</span>
                <span class="manual-result-badges">${syncBadge}${langBadge}</span>
            </div>
            <div class="manual-result-meta muted small">
                album: ${esc(item.album_name || item.album || "")}${item.duration ? ` &middot; ${Math.floor(item.duration/60)}:${String(Math.floor(item.duration%60)).padStart(2,"0")}` : ""} &middot; ${lineCount} lines
            </div>
            <pre class="manual-result-preview">${esc((item.lrc_text || "").slice(0, 400))}${(item.lrc_text || "").length > 400 ? "\u2026" : ""}</pre>
            <div class="manual-result-foot">
                ${isPicked ? `<span class="manual-picked-tag">&#10003; current pick</span>` : `<button class="btn btn-primary btn-xs manual-use-btn" type="button" data-result-id="${id}">Use this</button>`}
            </div>
        `;
        resultsEl.appendChild(card);
        const useBtn = card.querySelector(".manual-use-btn");
        if (useBtn) {
            useBtn.addEventListener("click", () => {
                useManualResult(item);
            });
        }
    }

    function useManualResult(item) {
        if (!_manualSearchRow || !_lyricsReviewState) return;
        const k = String(_manualSearchRow.dataset.trackId);
        const state = _lyricsReviewState;
        state.decisions[k] = "manual";
        state.manual_lyrics = state.manual_lyrics || {};
        state.manual_lyrics[k] = {
            lrc_text: item.lrc_text || "",
            language: item.language || "",
            source: "lrclib",
            track_name: item.track_name || item.title || "",
            artist_name: item.artist_name || item.artist || "",
        };
        // Make sure the radio in the parent modal reflects the new state.
        const radio = _manualSearchRow.querySelector('input[type="radio"][value="manual"]');
        if (radio) radio.checked = true;
        refreshManualButton(_manualSearchRow, k);
        const resultsEl = $("#manual-search-results");
        if (resultsEl) {
            resultsEl.innerHTML = "";
            renderManualResult(state.manual_lyrics[k], true);
        }
        const statusEl = $("#manual-search-status");
        if (statusEl) statusEl.textContent = "Picked. Close this modal to keep going.";
        toast("Manual lyrics picked. Approve & start jobs when ready.", "ok");
    }

    async function runManualSearch() {
        const artist = ($("#manual-search-artist").value || "").trim();
        const title = ($("#manual-search-title").value || "").trim();
        const statusEl = $("#manual-search-status");
        const resultsEl = $("#manual-search-results");
        const go = $("#manual-search-go");
        if (!artist && !title) {
            if (statusEl) statusEl.textContent = "Enter at least an artist or title.";
            return;
        }
        if (statusEl) statusEl.textContent = "Searching LRCLIB\u2026";
        if (go) { go.disabled = true; go.textContent = "Searching\u2026"; }
        const k = _manualSearchRow ? String(_manualSearchRow.dataset.trackId) : "manual";
        const tracks = [{
            id: k,
            title: title || "(unknown)",
            artist: artist || "",
            album: _manualSearchRow ? (_manualSearchRow.dataset.album || "") : "",
            duration_ms: _manualSearchRow ? parseInt(_manualSearchRow.dataset.durationMs || "0", 10) || 0 : 0,
        }];
        try {
            const r = await api("/api/automator/lookup-lyrics", {
                method: "POST",
                body: { tracks },
            });
            if (!r || !r.ok) {
                if (statusEl) statusEl.textContent = "Search failed: " + ((r && r.error) || "unknown");
                if (resultsEl) resultsEl.innerHTML = "";
                return;
            }
            const lookupId = r.lookup_id;
            const maxAttempts = 30;
            for (let i = 0; i < maxAttempts; i++) {
                await new Promise((res) => setTimeout(res, 700));
                const sr = await api(`/api/automator/lookup-lyrics/status/${lookupId}`);
                if (!sr) break;
                if (sr.status === "error") {
                    if (statusEl) statusEl.textContent = "Search error: " + (sr.error || "unknown");
                    return;
                }
                if (sr.status === "done") {
                    const results = sr.results || [];
                    const found = results.find((x) => x && x.lyrics && x.lyrics.available && x.lyrics.lrc_text);
                    if (!found) {
                        if (statusEl) statusEl.textContent = `No lyrics found for "${artist} - ${title}". Try a different query.`;
                        if (resultsEl) resultsEl.innerHTML = "";
                        return;
                    }
                    const item = found.lyrics;
                    if (statusEl) statusEl.textContent = `1 result found. Pick one or refine the query.`;
                    if (resultsEl) resultsEl.innerHTML = "";
                    renderManualResult(item, false);
                    return;
                }
                if (statusEl) statusEl.textContent = `Searching\u2026 (${sr.done || 0}/${sr.total || 1})`;
            }
            if (statusEl) statusEl.textContent = "Search timed out. Try again.";
        } catch (e) {
            if (statusEl) statusEl.textContent = "Search error: " + (e.message || e);
        } finally {
            if (go) { go.disabled = false; go.textContent = "Search LRCLIB"; }
        }
    }

    function wireManualSearchModal() {
        const go = $("#manual-search-go");
        if (go) go.addEventListener("click", runManualSearch);
        ["manual-search-artist", "manual-search-title"].forEach((id) => {
            const inp = document.getElementById(id);
            if (inp) inp.addEventListener("keydown", (e) => {
                if (e.key === "Enter") { e.preventDefault(); runManualSearch(); }
            });
        });
    }

    const LANG_LABELS = {
        "ko": "Korean", "ja": "Japanese", "zh": "Chinese", "zh-Hans": "Chinese",
        "zh-Hant": "Chinese", "en": "English", "en-US": "English", "en-GB": "English",
        "es": "Spanish", "fr": "French", "de": "German", "pt": "Portuguese",
        "it": "Italian", "ru": "Russian", "id": "Indonesian", "vi": "Vietnamese",
        "th": "Thai", "ar": "Arabic", "hi": "Hindi",
    };

    async function startJobsFromSubsReview() {
        const state = _lyricsReviewState;
        if (!state) return;
        const go = $("#automator-subs-go");
        go.disabled = true;
        go.textContent = "Starting\u2026";
        // Build track_lyrics map for tracks the user picked "lyrics", "manual", or "none"
        const track_lyrics = {};
        const manual = state.manual_lyrics || {};
        state.lyricsResults.forEach((r) => {
            const k = String(r.track_id);
            const pick = state.decisions[k] || "whisper";
            if (pick === "lyrics" && r.lyrics && r.lyrics.lrc_text) {
                track_lyrics[k] = {
                    source: "lyrics",
                    lrc_text: r.lyrics.lrc_text,
                    language: r.lyrics.language || "",
                };
            } else if (pick === "manual") {
                const m = manual[k];
                if (m && m.lrc_text) {
                    track_lyrics[k] = {
                        source: "lyrics",
                        lrc_text: m.lrc_text,
                        language: m.language || "",
                    };
                }
                // If no pick yet, fall through to whisper (the backend will run Whisper).
            } else if (pick === "none") {
                track_lyrics[k] = { source: "none" };
            }
        });
        const payload = {
            albums: state.albums,
            backup: state.backup,
            track_lyrics,
        };
        try {
            const r = await api("/api/automator/cart/process", { method: "POST", body: payload });
            if (r && r.ok) {
                const n = (r.job_ids || []).length;
                toast(`Started ${n} job${n === 1 ? "" : "s"}. Watch the Automator jobs panel.`);
                clearCart();
                renderCart();
                closeModal("#automator-subs-modal");
                _lyricsReviewState = null;
                refreshAutomatorJobs();
            } else {
                toast("Failed to start: " + (r && r.error || "unknown"), "err");
            }
        } catch (e) {
            toast("Start failed: " + e.message, "err");
        } finally {
            go.disabled = false;
            go.textContent = "Approve & start jobs";
        }
    }

    // ----------------------------- jobs panel --------------------------

    let _jobsTimer = null;
    const COLLAPSE_AFTER_MS = 5_000;
    const HIDE_AFTER_MS = 60_000;
    const _collapseTimers = new Map();    // jobId -> setTimeout handle
    const _collapseAt = new Map();        // jobId -> ms timestamp when first collapsed
    const _showCompleted = (() => {
        try { return localStorage.getItem("aaa.automator.showCompleted") === "1"; }
        catch { return false; }
    })();

    async function refreshAutomatorJobs() {
        const list = $("#automator-jobs-list");
        const badge = $("#automator-jobs-count");
        if (!list || !badge) return;
        try {
            const r = await api("/api/automator/jobs");
            if (!r || !r.ok) {
                list.innerHTML = `<p class="muted small">Could not load jobs.</p>`;
                badge.textContent = "0";
                return;
            }
            const active = r.active || [];
            const recent = r.recent || [];
            const total = active.length + recent.length;
            badge.textContent = String(total);
            if (!total) {
                list.innerHTML = `<p class="muted small">No automator jobs yet.</p>`;
                return;
            }
            const blocks = [];
            active.forEach((j) => blocks.push(renderJobCard(j, true)));
            // R3: completed jobs are filtered by showCompleted + age.
            const now = Date.now();
            recent.slice(0, 5).forEach((j) => {
                if (!_showCompleted && _isTooOld(j, now)) return;
                blocks.push(renderJobCard(j, false));
            });
            list.innerHTML = blocks.join("");
            _armCollapseTimers();
        } catch (e) {
            list.innerHTML = `<p class="muted small">Error: ${esc(e.message)}</p>`;
        }
    }

    function _isTerminal(j) {
        const s = (j.status || "");
        return s === "done" || s === "error" || s === "cancelled" || s === "partial";
    }

    function _isTooOld(j, nowMs) {
        if (!_isTerminal(j)) return false;
        const ts = Date.parse(j.finished || j.started || "") || 0;
        if (!ts) return false;
        return (nowMs - ts) > HIDE_AFTER_MS;
    }

    function _armCollapseTimers() {
        const list = $("#automator-jobs-list");
        if (!list) return;
        list.querySelectorAll(".job-card.is-finished").forEach((card) => {
            const id = card.dataset.jobId;
            if (!id) return;
            if (_collapseTimers.has(id)) return;
            const t = setTimeout(() => {
                card.classList.add("is-collapsed");
                _collapseAt.set(id, Date.now());
            }, COLLAPSE_AFTER_MS);
            _collapseTimers.set(id, t);
        });
    }

    function renderJobCard(j, isActive) {
        const st = j.status || "queued";
        const fin = _isTerminal(j);
        const cls = [isActive ? "is-active" : ("is-" + st), fin ? "is-finished" : ""]
            .filter(Boolean).join(" ");
        const title = esc(j.title || "");
        const p = j.progress || { done: 0, total: 0 };
        const pct = p.total ? Math.round((p.done / p.total) * 100) : (fin ? 100 : 0);
        const statusLabel = st === "partial" ? "partial" : st;
        // Tally tracks that finished without a .lrc so the user can see
        // at a glance how many of the downloaded songs need manual
        // lyrics. Excludes tracks the user explicitly skipped ("none").
        const noLyricsCount = (j.files || []).filter((f) =>
            f && f.status !== "failed" && !f.lyrics &&
            f.lyrics_status && f.lyrics_status !== "user skipped"
        ).length;
        const noLyricsLine = (fin && noLyricsCount > 0)
            ? `<p class="job-card-meta" style="color:var(--warning, #b03e1c);">${noLyricsCount} of ${(j.files || []).length} tracks downloaded without lyrics &mdash; open Repair tab to add them manually</p>`
            : "";
        const cancelBtn = isActive
            ? `<button class="btn btn-ghost btn-sm" type="button" data-cancel-job="${esc(j.id)}">Stop</button>`
            : "";
        // A2: "Retry N failed" button on finished jobs with failed tracks
        const failedCount = (j.files || []).filter((f) => f && f.status === "failed").length;
        const retryBtn = (!isActive && fin && failedCount > 0)
            ? `<button class="btn btn-primary btn-sm" type="button" data-retry-failed="${esc(j.id)}">Retry ${failedCount} failed</button>`
            : "";
        const errLine = j.error ? `<p class="job-card-meta" style="color:var(--danger);">${esc(j.error)}</p>` : "";
        const currentLine = p.current ? `<p class="job-card-meta muted small">Current: ${esc(p.current)}</p>` : "";
        const tracks = Array.isArray(j.files) ? j.files : [];
        const tracksHtml = tracks.length ? renderTrackList(tracks) : "";
        // R3: collapsible chevron. Click on header to toggle; expanded by default
        // for active jobs, collapsed for finished jobs (auto-set 5s after done).
        const expandBtn = fin
            ? `<button class="btn btn-ghost btn-sm job-expand-btn" type="button" aria-expanded="true" aria-label="Toggle details">&#9662;</button>`
            : "";
        return `
        <article class="job-card ${cls}" data-job-id="${esc(j.id)}">
            <div class="job-card-head">
                <div>
                    <h3 class="job-card-title">\u26ef ${title}</h3>
                    <div class="job-card-status">
                        <span class="status-dot"></span>
                        <span>${esc(statusLabel)}</span>
                        <span class="muted small">&middot; ${p.done}/${p.total || "?"} ${fin ? "" : "tracks"}</span>
                    </div>
                </div>
                <div class="job-card-actions">
                    ${retryBtn}
                    ${expandBtn}
                    ${cancelBtn}
                </div>
            </div>
            <div class="job-progress" aria-hidden="true">
                <div class="job-progress-bar" style="width:${pct}%;"></div>
            </div>
            ${currentLine}
            ${errLine}
            ${noLyricsLine}
            ${tracksHtml}
        </article>`;
    }

    function renderTrackList(files) {
        const li = files.slice(0, 50).map((f, i) => {
            const tn = String(f.track_number || "").padStart(2, "0");
            const title = esc(f.title || "(untitled)");
            const status = f.status || "pending";
            const cls = "track-status-" + status;
            const glyph = {
                done: "\u2713",
                failed: "\u2717",
                downloading: "\u21bb",
                verifying: "\u21bb",
                tagging: "\u21bb",
                transcribing: "\u21bb",
                saving: "\u21bb",
                queued: "\u25cb",
                skipped: "\u2014",
                partial: "!",
            }[status] || "\u25cb";
            const lyrics = f.lyrics ? ' <span class="muted small">(+ lyrics)</span>' : "";
            // `lyrics_status` is a short human-readable string set by
            // the download worker explaining what happened to this
            // track's .lrc ("saved (lrclib)", "saved (whisper)",
            // "no LRCLIB match; whisper failed: ...", "user skipped",
            // etc). Surface it so the user can see WHY a track is
            // missing its .lrc next to the .mp3 on disk.
            const lyricsStatus = (!f.lyrics && f.lyrics_status)
                ? ` <span class="muted small">— ${esc(f.lyrics_status)}</span>`
                : "";
            const err = f.error ? `<span class="track-error">${esc(f.error)}</span>` : "";
            // C1: per-track expandable log. Hidden by default; user clicks to expand.
            const logLines = Array.isArray(f.log) ? f.log : [];
            const logHtml = logLines.length
                ? `<pre class="track-log" hidden>${logLines.map((l) => esc(l)).join("\n")}</pre>`
                : `<pre class="track-log muted small" hidden>(no log entries yet)</pre>`;
            const logBtn = `<button class="btn btn-ghost btn-sm track-log-btn" type="button" aria-expanded="false">Show log</button>`;
            return `<li class="track-row" data-idx="${i}">
                <div class="track-row-head">
                    <span><span class="${cls}">${glyph}</span> ${tn} &middot; ${title}${lyrics}${lyricsStatus}</span>
                    <span class="${cls}">${esc(status)}</span>
                    ${logBtn}
                </div>
                ${err}
                ${logHtml}
            </li>`;
        }).join("");
        const more = files.length > 50 ? `<li class="muted small">+${files.length - 50} more</li>` : "";
        return `<ol class="job-card-track-list">${li}${more}</ol>`;
    }

    function wireJobsPanel() {
        const list = $("#automator-jobs-list");
        if (!list) return;
        list.addEventListener("click", async (e) => {
            // C1: per-track log toggle
            const logBtn = e.target.closest(".track-log-btn");
            if (logBtn) {
                e.preventDefault();
                e.stopPropagation();
                const row = logBtn.closest(".track-row");
                if (row) {
                    const pre = row.querySelector(".track-log");
                    if (pre) {
                        const hidden = pre.hasAttribute("hidden");
                        if (hidden) pre.removeAttribute("hidden");
                        else pre.setAttribute("hidden", "");
                        logBtn.textContent = hidden ? "Hide log" : "Show log";
                        logBtn.setAttribute("aria-expanded", hidden ? "true" : "false");
                    }
                }
                return;
            }
            // R3: expand/collapse toggle on the chevron
            const expandBtn = e.target.closest(".job-expand-btn");
            if (expandBtn) {
                e.preventDefault();
                e.stopPropagation();
                const card = expandBtn.closest(".job-card");
                if (card) {
                    card.classList.toggle("is-collapsed");
                    expandBtn.setAttribute("aria-expanded",
                        card.classList.contains("is-collapsed") ? "false" : "true");
                }
                return;
            }
            // A2: retry-failed button handler
            const retryBtn = e.target.closest("[data-retry-failed]");
            if (retryBtn) {
                e.preventDefault();
                e.stopPropagation();
                const jobId = retryBtn.dataset.retryFailed;
                const originalText = retryBtn.textContent;
                retryBtn.disabled = true;
                retryBtn.textContent = "Retrying\u2026";
                try {
                    const r = await api("/api/automator/jobs/" + jobId + "/retry-failed", { method: "POST" });
                    if (!r || !r.ok) {
                        toast("Retry failed: " + ((r && r.error) || "unknown"), "err");
                        retryBtn.disabled = false;
                        retryBtn.textContent = originalText;
                        return;
                    }
                    toast(`Retry job ${r.retry_job_id} started (${r.failed_count} track(s))`, "ok");
                    setTimeout(refreshAutomatorJobs, 600);
                } catch (err) {
                    toast("Retry failed: " + err.message, "err");
                    retryBtn.disabled = false;
                    retryBtn.textContent = originalText;
                }
                return;
            }
            const btn = e.target.closest("[data-cancel-job]");
            if (!btn) return;
            btn.disabled = true;
            btn.textContent = "Stopping\u2026";
            try {
                await api("/api/automator/jobs/" + btn.dataset.cancelJob + "/cancel", { method: "POST" });
                setTimeout(refreshAutomatorJobs, 600);
            } catch (err) {
                toast("Cancel failed: " + err.message, "err");
                btn.disabled = false;
                btn.textContent = "Stop";
            }
        });

        // R3: "Show completed" toggle (persists across page reloads)
        const showToggle = document.getElementById("show-completed");
        if (showToggle) {
            showToggle.checked = _showCompleted;
            showToggle.addEventListener("change", () => {
                const v = !!showToggle.checked;
                try { localStorage.setItem("aaa.automator.showCompleted", v ? "1" : "0"); } catch {}
                // Update the in-memory flag via a small escape hatch:
                // We don't expose _showCompleted externally, so refresh re-reads it.
                location.reload(); // simplest correct behaviour; cheap refresh.
            });
        }
    }

    // ----------------------------- sub-tabs ----------------------------

    function showSubTab(id) {
        $$(".sub-tab", $("#panel-automator")).forEach((t) => {
            const on = t.dataset.subtab === id;
            t.classList.toggle("is-active", on);
            t.setAttribute("aria-selected", on ? "true" : "false");
        });
        $$(".sub-panel", $("#panel-automator")).forEach((p) => {
            p.classList.toggle("is-active", p.id === id);
        });
    }
    function wireSubTabs() {
        const tabs = $$(".sub-tab", $("#panel-automator"));
        tabs.forEach((t) => {
            t.addEventListener("click", () => {
                const id = t.dataset.subtab;
                showSubTab(id);
                if (id === "automator-recent") loadRecent();
            });
        });
    }

    // ----------------------------- init --------------------------------

    function initAutomatorPage() {
        // Settings
        $("#automator-edit-settings").addEventListener("click", openSettings);
        $("#automator-test-connection").addEventListener("click", () => testConnection($("#automator-profile-detail")));
        $("#automator-settings-form").addEventListener("submit", saveSettings);
        $("#ap-api-key-show").addEventListener("click", (e) => {
            e.preventDefault();
            const inp = $("#ap-api-key");
            inp.type = inp.type === "password" ? "text" : "password";
            e.target.textContent = inp.type === "password" ? "Show" : "Hide";
        });
        $("#ap-test").addEventListener("click", (e) => { e.preventDefault(); testConnection(); });

        wireModalDismissers();
        wireSubTabs();
        wireAlbumGridClicks();
        wireCartClicks();
        wireJobsPanel();
        wireManualSearchModal();

        // Search form
        $("#automator-search-form").addEventListener("submit", (e) => {
            e.preventDefault();
            const inp = $("#automator-search-input");
            runSearch(inp.value);
            $("#automator-search-clear").hidden = !inp.value;
            showSubTab("automator-search");
        });
        $("#automator-search-clear").addEventListener("click", () => {
            $("#automator-search-input").value = "";
            $("#automator-search-clear").hidden = true;
            $("#automator-search-grid").innerHTML = "";
            $("#automator-search-meta").textContent = "Search iTunes to find albums.";
        });

        // Confirm modal run button
        $("#automator-confirm-go").addEventListener("click", runConfirmed);
        // Subs review modal approve button
        $("#automator-subs-go").addEventListener("click", startJobsFromSubsReview);

        // Initial render
        cartListeners.push(renderCart);
        renderCart();
        loadProfile().then(() => {
            // Once profile loads, attempt to load recent too
            if (profile && profile.jellyfin_url) loadRecent();
        });
        refreshAutomatorJobs();
        if (_jobsTimer) clearInterval(_jobsTimer);
        _jobsTimer = setInterval(refreshAutomatorJobs, 2000);

        // beforeunload nudge if cart is non-empty
        window.addEventListener("beforeunload", (e) => {
            if (cart.albums.length) {
                e.preventDefault();
                e.returnValue = "You have albums in your cart that haven't been processed. Leave anyway?";
            }
        });
    }

    // Expose for app.js dispatch
    window.initAutomatorPage = initAutomatorPage;
})();
