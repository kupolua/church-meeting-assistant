/* Click a timestamp → play the recording from there.
 *
 * Shared by the meeting detail page (#meeting-audio) and the speaker review
 * page (#speaker-audio); the seek logic was duplicated in both templates
 * before it moved out of them.
 *
 * Lives in a file rather than a <script> block because the CSP is
 * script-src 'self' (web/headers.py) — an inline block is not executed, and
 * the failure is silent: the markup renders, the timestamps just stop doing
 * anything.
 *
 * Anything with class .ts-link and a data-seconds attribute is a seek target.
 * Handled by delegation from the document, so links that appear later — the
 * ones meeting-detail.js builds out of topic text, or anything htmx swaps in —
 * work without re-binding.
 */
(function () {
    "use strict";

    var audio = document.querySelector("#meeting-audio, #speaker-audio");
    if (!audio) return;          // page has no recording; nothing to wire up

    function seekTo(seconds) {
        function doSeek() {
            try { audio.currentTime = seconds; } catch (e) { /* not seekable yet */ }
            var played = audio.play();
            // Autoplay can be refused (no user gesture on this element); the
            // seek still happened, so the user can press play themselves.
            if (played && played.catch) played.catch(function () {});
        }

        // readyState < HAVE_METADATA means duration is unknown and assigning
        // currentTime is discarded — wait for metadata, then seek once.
        if (audio.readyState >= 1) {
            doSeek();
        } else {
            audio.addEventListener("loadedmetadata", doSeek, { once: true });
            audio.load();
        }

        audio.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }

    function activate(target, event) {
        var link = target && target.closest ? target.closest(".ts-link") : null;
        if (!link) return;
        event.preventDefault();
        event.stopPropagation();
        var seconds = parseFloat(link.dataset.seconds);
        if (!isNaN(seconds)) seekTo(seconds);
    }

    document.addEventListener("click", function (e) { activate(e.target, e); });
    document.addEventListener("keydown", function (e) {
        // Transcript timestamps are <span tabindex=0>, not links — they need
        // Enter/Space handled explicitly to stay keyboard-reachable.
        if (e.key === "Enter" || e.key === " ") activate(e.target, e);
    });
})();
