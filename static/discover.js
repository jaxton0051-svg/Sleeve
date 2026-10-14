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
        const id = escapeHtml(album.id || "");
        const count = album.track_count ? `${album.track_count} tracks` : escapeHtml(album.source || "Music database");
        const alreadyOnServer = !!album.alreadyOnServer;
        const buttonLabel = alreadyOnServer ? "In library" : "Download album";
        return `<article class="album-card discover-album">
            <div class="album-cover-wrap"><div class="album-cover-placeholder" aria-hidden="true">♫</div>
            ${art ? `<img class="album-cover loaded" alt="" loading="lazy" decoding="async" src="${art}" onerror="this.remove()">` : ""}</div>
            <div class="album-body"><h3 class="album-title">${title}</h3><p class="album-artist">${artist}</p><p class="muted small">${escapeHtml(count)}</p></div>
            <div class="album-actions"><button class="btn btn-primary btn-sm" type="button" data-queue-discover-album data-album-id="${id}" data-title="${title}" data-artist="${artist}" data-artwork="${art}" aria-label="${alreadyOnServer ? `${title} by ${artist} is already in your Jellyfin library` : `Add ${title} by ${artist} to Automator`}" ${alreadyOnServer ? "disabled title=\"Already in your Jellyfin library\"" : ""}>${buttonLabel}</button></div>
        </article>`;
    }

    function renderTracks(tracks) {
        const list = $("#trending-list");
        list.innerHTML = tracks.slice(0, 10).map((track, index) => {
            const title = escapeHtml(track.title || "Untitled");
            const artist = escapeHtml(track.artist || "Unknown artist");
            const artwork = escapeHtml(track.artwork || "");
            const inLibrary = !!track.trackAlreadyOnServer;
            return `<article class="track-tile"><span class="track-rank">${escapeHtml(track.rank || index + 1)}</span>
                <span class="track-thumb" aria-hidden="true">${artwork ? `<img src="${artwork}" alt="" loading="eager" decoding="async" onerror="this.remove()">` : ""}</span>
                <div class="track-copy"><strong>${title}</strong><span>${artist}</span></div>
                <button class="btn btn-ghost btn-sm" type="button" data-find-song-album data-title="${title}" data-artist="${artist}" aria-label="${inLibrary ? `${title} by ${artist} is already in your Jellyfin library` : `Find the album for ${title} by ${artist} and add it to Automator`}" ${inLibrary ? "disabled title=\"This track is already in your Jellyfin library\"" : ""}>${inLibrary ? "In library" : "Download album"}</button></article>`;
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

    let albumsRequestId = 0;
    async function loadAlbums(genre = "all") {
        const grid = $("#discover-grid");
        const requestId = ++albumsRequestId;
        grid.setAttribute("aria-busy", "true");
        grid.innerHTML = '<p class="empty-state">Loading albums…</p>';
        try {
            const data = await request(`/api/albums?genre=${encodeURIComponent(genre)}`);
            if (requestId !== albumsRequestId) return;
            grid.innerHTML = (data.albums || []).map(albumCard).join("") || '<p class="empty-state">No albums are available right now.</p>';
            if (data.source) setStatus(`Albums from ${data.source}.`);
            grid.setAttribute("aria-busy", "false");
        } catch (error) {
            if (requestId !== albumsRequestId) return;
            grid.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
            grid.setAttribute("aria-busy", "false");
            setStatus("Could not load this album chart. Try another genre or refresh later.", "err");
        }
    }

    function renderSearch(data, kind) {
        const grid = $("#discover-grid");
        if (kind === "song") {
            const songs = data.songs || [];
            grid.innerHTML = songs.map((song) => `<article class="search-result track-tile">
                <span class="track-rank" aria-hidden="true">♫</span><div class="track-copy"><strong>${escapeHtml(song.title)}</strong><span>${escapeHtml(song.artist)} · ${escapeHtml(song.source || "Music source")}</span></div>
                <button class="btn btn-primary btn-sm" type="button" data-download-track data-title="${escapeHtml(song.title)}" data-artist="${escapeHtml(song.artist)}" ${song.trackAlreadyOnServer ? "disabled title=\"This track is already in your Jellyfin library\"" : ""}>${song.trackAlreadyOnServer ? "In library" : "Download"}</button></article>`).join("") || '<p class="empty-state">No matching songs found.</p>';
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
        albumsRequestId += 1;
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

    function queueAlbumInAutomator(foundAlbum, tracks, artistFallback, artworkFallback = "") {
        const title = foundAlbum.title || "";
        const artist = foundAlbum.artist || artistFallback;
        const albumId = String(foundAlbum.id || `${foundAlbum.source || "album"}:${title}:${artist}`);
        const cartKey = "aaa.automator.cart.v1";
        let cart = { albums: [] };
        try {
            const saved = JSON.parse(localStorage.getItem(cartKey) || "null");
            if (saved && Array.isArray(saved.albums)) cart = saved;
        } catch {}
        const exists = cart.albums.some((entry) => String(entry.id) === albumId);
        if (!exists) {
            cart.albums.push({
                id: albumId,
                title,
                artist,
                artwork: foundAlbum.artwork || artworkFallback,
                trackCount: tracks.length,
                alreadyOnServer: !!foundAlbum.alreadyOnServer,
                kind: "album",
                fetchTracksOnStart: false,
                tracks,
                expanded: false,
            });
            localStorage.setItem(cartKey, JSON.stringify(cart));
        }
        return { exists, title };
    }

    async function fetchAlbumForDiscoverCard(button) {
        const title = button.dataset.title || "";
        const artist = button.dataset.artist || "";
        let albumId = button.dataset.albumId || "";
        if (!albumId) {
            const params = new URLSearchParams({ q: `${title} ${artist}`, limit: "25" });
            const matches = await request(`/api/automator/search/albums?${params}`);
            const normalize = (value) => String(value || "").normalize("NFKC").toLocaleLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
            const exact = (matches.albums || []).find((item) =>
                normalize(item.title) === normalize(title) && normalize(item.artist) === normalize(artist));
            if (!exact) throw new Error(`Could not find the exact album “${title}” by ${artist}.`);
            albumId = exact.id;
        }
        const params = new URLSearchParams({ title, artist });
        const details = await request(`/api/automator/album/${encodeURIComponent(albumId)}/tracks?${params}`);
        const tracks = Array.isArray(details.tracks) ? details.tracks : [];
        const album = details.album || {};
        if (!tracks.length || !album.title) throw new Error(`Could not load the tracklist for “${title}” by ${artist}.`);
        return { album, tracks };
    }

    async function addDiscoverAlbumToAutomator(button) {
        const title = button.dataset.title || "";
        const artist = button.dataset.artist || "";
        const original = button.textContent;
        button.disabled = true;
        button.textContent = "Loading tracklist…";
        setStatus(`Loading ${title} by ${artist} for Automator…`);
        try {
            const { album, tracks } = await fetchAlbumForDiscoverCard(button);
            if (album.alreadyOnServer) {
                button.textContent = "In library";
                button.disabled = true;
                setStatus(`${album.title || title} by ${album.artist || artist} is already in your Jellyfin library.`, "ok");
                return;
            }
            const queued = queueAlbumInAutomator(album, tracks, artist, button.dataset.artwork || "");
            button.textContent = queued.exists ? "Already in Automator" : "Added to Automator";
            setStatus(queued.exists
                ? `${queued.title} is already in your Automator queue. Opening it…`
                : `Added ${queued.title} to Automator. Opening your queue…`, "ok");
            window.location.assign("/automator#automator-queue-panel");
        } catch (error) {
            button.disabled = false;
            button.textContent = original;
            setStatus(error.message || "Could not add this album to Automator.", "err");
        }
    }

    async function addHitAlbumToAutomator(button) {
        const title = button.dataset.title || "";
        const artist = button.dataset.artist || "";
        const original = button.textContent;
        button.disabled = true;
        button.textContent = "Finding album…";
        setStatus(`Finding the album for ${title} by ${artist}…`);
        try {
            const params = new URLSearchParams({ title, artist });
            const result = await request(`/api/song/0/album?${params}`);
            const foundAlbum = result.album || {};
            const tracks = Array.isArray(result.tracks) ? result.tracks : [];
            if (!tracks.length || !foundAlbum.title) {
                throw new Error(`Could not find the full album for ${title} by ${artist}.`);
            }
            const normalize = (value) => String(value || "").normalize("NFKC").toLocaleLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
            const selectedTrackAlreadyOnServer = tracks.some((track) =>
                track.trackAlreadyOnServer
                && normalize(track.title) === normalize(title));
            if (foundAlbum.alreadyOnServer || selectedTrackAlreadyOnServer) {
                button.textContent = "In library";
                button.disabled = true;
                setStatus(selectedTrackAlreadyOnServer
                    ? `${title} by ${artist} is already in your Jellyfin library.`
                    : `${foundAlbum.title} by ${foundAlbum.artist || artist} is already in your Jellyfin library.`, "ok");
                return;
            }
            const queued = queueAlbumInAutomator(foundAlbum, tracks, artist, button.dataset.artwork || "");
            button.textContent = queued.exists ? "Already in Automator" : "Added to Automator";
            setStatus(queued.exists
                ? `${queued.title} is already in your Automator queue. Opening it…`
                : `Added ${queued.title} to Automator. Opening your queue…`, "ok");
            window.location.assign("/automator#automator-queue-panel");
        } catch (error) {
            button.textContent = original;
            button.disabled = false;
            setStatus(error.message || "Could not add this album to Automator.", "err");
        }
    }

    document.addEventListener("DOMContentLoaded", () => {
        if (document.body.dataset.activeTab !== "discover") return;
        $("#discover-search-form").addEventListener("submit", search);
        $("#discover-refresh").addEventListener("click", () => loadTrending(true));
        document.querySelectorAll("[data-album-genre]").forEach((button) => {
            button.addEventListener("click", () => {
                document.querySelectorAll("[data-album-genre]").forEach((option) => {
                    const selected = option === button;
                    option.classList.toggle("is-active", selected);
                    option.setAttribute("aria-pressed", String(selected));
                });
                loadAlbums(button.dataset.albumGenre);
            });
        });
        document.addEventListener("click", (event) => {
            const album = event.target.closest("[data-queue-discover-album]");
            const track = event.target.closest("[data-download-track]");
            const hitAlbum = event.target.closest("[data-find-song-album]");
            if (album) addDiscoverAlbumToAutomator(album);
            if (track) startDownload(track, false);
            if (hitAlbum) addHitAlbumToAutomator(hitAlbum);
        });
        loadTrending();
        loadAlbums("pop");
    });
})();
