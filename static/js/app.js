"use strict";

// Shared boundary for any progressive JSON mutation.
async function apiRequest(url, options = {}) {
    const target = new URL(url, window.location.origin);
    if (target.origin !== window.location.origin) throw new Error("Request failed");
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    headers.set("Content-Type", "application/json");
    headers.set("X-CSRFToken", document.querySelector('meta[name="csrf-token"]').content);
    const response = await fetch(target, {...options, headers, credentials: "same-origin", redirect: "error"});
    if (!response.ok) throw new Error("Request failed. Reload the page and try again.");
    return response.json();
}
window.apiRequest = apiRequest;

document.addEventListener("DOMContentLoaded", () => {
    const audio = document.getElementById("review-audio");
    const playbackStatus = document.getElementById("playback-status");
    if (audio) {
        document.querySelectorAll("button[data-seek]").forEach(button => {
            button.addEventListener("click", () => {
                const start = Number(button.dataset.seek);
                if (!Number.isFinite(start) || start < 0) return;
                const seek = () => {
                    if (Number.isFinite(audio.duration) && start > audio.duration) return;
                    try {
                        audio.currentTime = start;
                        const playing = audio.play();
                        if (playing) playing.catch(() => { playbackStatus.textContent = "Use the audio controls to start playback."; });
                    } catch (_) { playbackStatus.textContent = "Playback is unavailable. Use the public source timestamp."; }
                };
                if (audio.readyState >= 1) seek();
                else audio.addEventListener("loadedmetadata", seek, {once: true});
            });
        });
        audio.addEventListener("error", () => { playbackStatus.textContent = "Playback is unavailable. Use the public source timestamp."; });
    }
    document.querySelectorAll("[data-backfill-form]").forEach(form => {
        const selector = form.querySelector("[name=selector_type]");
        const update = () => form.querySelectorAll("[data-selector]").forEach(group => {
            const active = group.dataset.selector === selector.value;
            group.hidden = !active;
            group.querySelectorAll("input").forEach(input => {
                input.disabled = !active;
                input.required = active;
            });
        });
        selector.addEventListener("change", update);
        update();
    });
    document.querySelectorAll("form[data-confirm]").forEach(form => {
        form.addEventListener("submit", event => {
            if (!window.confirm(form.dataset.confirm)) event.preventDefault();
        });
    });
});
