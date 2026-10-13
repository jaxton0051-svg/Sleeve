(function () {
    "use strict";

    const $ = (selector) => document.querySelector(selector);
    const escapeHtml = (value) => String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);

    async function request(path, options = {}) {
        const response = await fetch(path, options);
        const data = await response.json();
        if (!response.ok || data.ok === false) throw new Error(data.error || response.statusText);
        return data;
    }

    function setStatus(message, kind = "") {
        const status = $("#discover-status");
        status.textContent = message;
        status.className = `status ${kind}`.trim();
    }

    function albumCard(album) {
        const title = escapeHtml(album.title || "Untitled album");
        const artist = escapeHtml(album.artist || "Unknown artist");
        const art = escapeHtml(album.artwork || "");
        const count = album.track_count ? `${album.track_count} tracks` : escapeHtml(album.source || "Music database");
        return `<article class="album-card discover-album">
            <div class="album-cover-wrap"><div class="album-cover-placeholder" aria-hidden="true">♫</div>
            ${art ? `<img class="album-cover loaded" alt="" loading="lazy" decoding="async" src="${art}" onerror="this.remove()">` : ""}</div>
            <div class="album-body"><h3 class="album-title">${title}</h3><p class="album-artist">${artist}</p><p class="muted small">${escapeHtml(count)}</p></div>
            <div class="album-actions"><button class="btn btn-primary btn-sm" type="button" data-download-album data-title="${title}" data-artist="${artist}">Download album</button></div>
        </article>`;
    }

    function renderTracks(tracks) {
        const list = $("#trending-list");
        list.innerHTML = tracks.slice(0, 10).map((track, index) => {
            const title = escapeHtml(track.title || "Untitled");
            const artist = escapeHtml(track.artist || "Unknown artist");
            const artwork = escapeHtml(track.artwork || "");
            return `<article class="track-tile"><span class="track-rank">${escapeHtml(track.rank || index + 1)}</span>
                <span class="track-thumb" aria-hidden="true">${artwork ? `<img src="${artwork}" alt="" loading="eager" decoding="async" onerror="this.remove()">` : ""}</span>
                <div class="track-copy"><strong>${title}</strong><span>${artist}</span></div>
                <button class="btn btn-ghost btn-sm" type="button" data-download-track data-title="${title}" data-artist="${artist}" aria-label="Download ${title} by ${artist}">Download</button></article>`;
        }).join("") || '<p class="empty-state">No chart results are available right now.</p>';
        list.setAttribute("aria-busy", "false");
    }

    async function loadTrending(force = false) {
        const list = $("#trending-list");
        list.setAttribute("aria-busy", "true");
        try {
            const data = await request(`/api/trending${force ? "?refresh=1" : ""}`);
            renderTracks(data.songs || []);
            setStatus(`Charts from ${data.source || "music providers"}.`);
        } catch (error) {
            list.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}. Try refreshing in a moment.</p>`;
            list.setAttribute("aria-busy", "false");
        }
    }

    async function loadAlbums() {
        const grid = $("#discover-grid");
        grid.setAttribute("aria-busy", "true");
        try {
            const data = await request("/api/albums");
            grid.innerHTML = (data.albums || []).map(albumCard).join("") || '<p class="empty-state">No albums are available right now.</p>';
            grid.setAttribute("aria-busy", "false");
        } catch (error) {
            grid.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
            grid.setAttribute("aria-busy", "false");
        }
    }

    function renderSearch(data, kind) {
        const grid = $("#discover-grid");
        if (kind === "song") {
            const songs = data.songs || [];
            grid.innerHTML = songs.map((song) => `<article class="search-result track-tile">
                <span class="track-rank" aria-hidden="true">♫</span><div class="track-copy"><strong>${escapeHtml(song.title)}</strong><span>${escapeHtml(song.artist)} · ${escapeHtml(song.source || "Music source")}</span></div>
                <button class="btn btn-primary btn-sm" type="button" data-download-track data-title="${escapeHtml(song.title)}" data-artist="${escapeHtml(song.artist)}">Download</button></article>`).join("") || '<p class="empty-state">No matching songs found.</p>';
        } else {
            grid.innerHTML = (data.albums || []).map(albumCard).join("") || '<p class="empty-state">No matching albums found.</p>';
        }
        grid.setAttribute("aria-busy", "false");
        const providers = Object.entries(data.provider_summary || {}).map(([name, count]) => `${name}: ${count}`).join(" · ");
        setStatus(providers ? `Results by source: ${providers}` : "Search complete.");
    }

    async function search(event) {
        event.preventDefault();
        const query = $("#discover-search-input").value.trim();
        if (!query) { setStatus("Enter an artist, album, or track to search.", "err"); return; }
        const kind = $("#discover-search-kind").value;
        const grid = $("#discover-grid");
        grid.innerHTML = '<p class="empty-state">Searching enabled music sources…</p>';
        grid.setAttribute("aria-busy", "true");
        setStatus("Searching…");
        try {
            const path = kind === "song"
                ? `/api/song/search?q=${encodeURIComponent(query)}&limit=30`
                : `/api/search?q=${encodeURIComponent(query)}&kind=album&limit=30`;
            renderSearch(await request(path), kind);
        } catch (error) {
            grid.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
            grid.setAttribute("aria-busy", "false");
            setStatus("Search failed. Check your connection or provider settings.", "err");
        }
    }

    async function startDownload(button, album) {
        button.disabled = true;
        const original = button.textContent;
        button.textContent = "Starting…";
        try {
            const body = album
                ? { title: button.dataset.title, artist: button.dataset.artist }
                : { query: `${button.dataset.title} ${button.dataset.artist}` };
            const path = album ? "/api/album-download" : "/api/download";
            await request(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
            button.textContent = "Added to downloads";
            setStatus("Download queued. Track progress below.", "ok");
            window.setTimeout(() => { button.textContent = original; button.disabled = false; }, 2200);
        } catch (error) {
            button.textContent = original;
            button.disabled = false;
            setStatus(error.message, "err");
        }
    }

    document.addEventListener("DOMContentLoaded", () => {
        if (document.body.dataset.activeTab !== "discover") return;
        $("#discover-search-form").addEventListener("submit", search);
        $("#discover-refresh").addEventListener("click", () => loadTrending(true));
        document.addEventListener("click", (event) => {
            const album = event.target.closest("[data-download-album]");
            const track = event.target.closest("[data-download-track]");
            if (album) startDownload(album, true);
            if (track) startDownload(track, false);
        });
        loadTrending();
        loadAlbums();
    });
})();
