/* Church Meeting Assistant — the small amount of JS that isn't htmx.
 *
 * Exists because a Content-Security-Policy of script-src 'self' blocks inline
 * handlers (onsubmit="...") outright. htmx's hx-confirm covers htmx-driven
 * actions, but a few controls are deliberately PLAIN form posts — on the
 * ingestion detail page, cancelling has to redirect back to that page rather
 * than swap in the job-list panel htmx would return. Those forms mark
 * themselves with data-confirm and are handled here.
 *
 * Delegated from the document and registered in the capture phase, so it also
 * covers forms htmx swaps in later — there is no re-binding to remember.
 */
(function () {
    "use strict";

    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (!form || typeof form.getAttribute !== "function") return;

        var message = form.getAttribute("data-confirm");
        if (message && !window.confirm(message)) {
            event.preventDefault();
            event.stopPropagation();
        }
    }, true);
})();
