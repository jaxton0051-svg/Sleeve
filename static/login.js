(function () {
    "use strict";

    const form = document.querySelector("#jellyfin-login-form");
    if (!form) return;
    const error = document.querySelector("#login-error");
    const submit = document.querySelector("#login-submit");

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        error.hidden = true;
        error.textContent = "";
        const username = document.querySelector("#login-username").value.trim();
        const passwordField = document.querySelector("#login-password");
        const password = passwordField.value;
        const serverUrl = document.querySelector("#login-server-url").value.trim();
        submit.disabled = true;
        submit.setAttribute("aria-busy", "true");
        submit.querySelector("span:first-child").textContent = "Checking with Jellyfin…";

        try {
            const response = await fetch("/api/auth/login", {
                method: "POST",
                headers: { "Content-Type": "application/json", "Accept": "application/json" },
                body: JSON.stringify({ username, password, server_url: serverUrl }),
            });
            const result = await response.json();
            if (!response.ok || !result.ok) throw new Error(result.error || "Sign-in failed.");
            passwordField.value = "";
            const requested = document.querySelector("#login-next").value;
            const next = requested.startsWith("/") && !requested.startsWith("//") ? requested : "/gallery";
            window.location.assign(next);
        } catch (err) {
            error.textContent = err.message || "Could not sign in. Check the Jellyfin server and try again.";
            error.hidden = false;
            passwordField.value = "";
            passwordField.focus();
        } finally {
            submit.disabled = false;
            submit.removeAttribute("aria-busy");
            submit.querySelector("span:first-child").textContent = "Continue to Sleeve";
        }
    });
})();
