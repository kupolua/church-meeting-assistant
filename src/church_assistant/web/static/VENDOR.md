# Vendored front-end assets

Everything the UI loads is served from this directory. Nothing is fetched from a
CDN at runtime, for three reasons:

1. **The app claims to be local.** Church data never leaves the machine — but a
   CDN `<script>` still told a third party, on every page view, that someone was
   using it and from which address.
2. **It has to work offline.** htmx drives every interactive part of the UI: the
   dashboard poll, sidebar search, all admin actions, session revocation,
   the ingestion panel. Without it those controls fail *silently* — the page
   renders, the buttons just stop doing anything.
3. **Supply chain.** A CDN serving something else means arbitrary JavaScript in a
   page that holds an authenticated session. HttpOnly stops the cookie being
   read, not an injected script acting as the user.

Vendoring is also what makes the Content-Security-Policy in `web/headers.py`
meaningful: with a CDN, `script-src` would have to allow exactly the origin you
would most want to forbid.

## Contents

| File | Version | Source | SHA-256 |
|---|---|---|---|
| `htmx.min.js` | 1.9.10 | `https://unpkg.com/htmx.org@1.9.10/dist/htmx.min.js` | `b3bdcf5c741897a53648b1207fff0469a0d61901429ba1f6e88f98ebd84e669e` |
| `pico.min.css` | — | Pico.css (vendored earlier) | — |
| `app.css` | — | ours | — |

## Updating

Download, verify, and record the new hash here in the same commit:

```bash
cd src/church_assistant/web/static
curl -sfL -o htmx.min.js "https://unpkg.com/htmx.org@<VERSION>/dist/htmx.min.js"
shasum -a 256 htmx.min.js
grep -o 'version:"[0-9.]*"' htmx.min.js | head -1   # must match <VERSION>
```

Then re-run `tests/mt_phase3_smoke.py` and click through one HTMX action (the
dashboard refreshes itself, so a stale or broken build is obvious within 5 s).

Do NOT bump the version in the same commit as unrelated work: htmx behaviour
changes between minors, and the UI has no automated browser tests to catch it.
