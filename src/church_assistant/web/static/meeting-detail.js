/* Meeting detail page behaviour: turning timestamps into seek targets, and the
 * "change speaker" dialog.
 *
 * External file because the CSP is script-src 'self' (web/headers.py). Note
 * what the first half does — it CREATES the clickable timestamps. Inline, and
 * therefore blocked, the topics and transcript simply render without any
 * playback links at all, which reads as a missing feature rather than an error.
 *
 * The actual seeking lives in audio-seek.js; this file only marks up targets.
 */
(function () {
    "use strict";

    function tsToSeconds(str) {
        var parts = str.trim().split(":").map(function (p) { return parseInt(p, 10); });
        if (parts.some(function (x) { return isNaN(x); })) return null;
        if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
        if (parts.length === 2) return parts[0] * 60 + parts[1];
        return null;
    }

    // ── Стенограма ───────────────────────────────────────────────────────
    // Each .turn-ts span IS exactly a timestamp, so it can be marked directly.
    // No role="button": Pico.css would paint it as a filled button.
    function markTranscriptTimestamps() {
        document.querySelectorAll(".transcript-list .turn-ts").forEach(function (span) {
            var seconds = tsToSeconds(span.textContent);
            if (seconds === null) return;
            span.classList.add("ts-link");
            span.dataset.seconds = seconds;
            span.tabIndex = 0;
            span.title = "Слухати з " + span.textContent.trim();
        });
    }

    // ── Теми ─────────────────────────────────────────────────────────────
    // Topic bodies are prose, so timestamps are only linkified inside a
    // parenthetical made up PURELY of timestamps — "(00:21)", "(24:11, 28:16)",
    // "(31:30; 33:52)". That restriction is what keeps a Bible reference like
    // "Псалом 84:6" from being turned into a seek link.
    var TS = "\\d{1,2}:\\d{2}(?::\\d{2})?";
    var PAREN_LIST = "\\(\\s*" + TS + "\\s*(?:[;,]\\s*" + TS + "\\s*)*\\)";

    function buildParenFragment(parenStr) {
        var frag = document.createDocumentFragment();
        var re = new RegExp(TS, "g");
        var last = 0, m;
        while ((m = re.exec(parenStr))) {
            if (m.index > last) {
                frag.appendChild(document.createTextNode(parenStr.slice(last, m.index)));
            }
            var link = document.createElement("a");
            link.className = "ts-link";
            link.href = "#";
            link.dataset.seconds = tsToSeconds(m[0]);
            link.textContent = m[0];
            link.title = "Слухати з " + m[0];
            frag.appendChild(link);
            last = m.index + m[0].length;
        }
        if (last < parenStr.length) {
            frag.appendChild(document.createTextNode(parenStr.slice(last)));
        }
        return frag;
    }

    function linkifyParens(root) {
        if (!root) return;
        var test = new RegExp(PAREN_LIST);

        // Collect first, replace after: replacing while walking would invalidate
        // the TreeWalker's position.
        var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
            acceptNode: function (node) {
                return test.test(node.nodeValue || "")
                    ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
            }
        });
        var targets = [], node;
        while ((node = walker.nextNode())) targets.push(node);

        targets.forEach(function (textNode) {
            var text = textNode.nodeValue;
            var re = new RegExp(PAREN_LIST, "g");
            var frag = document.createDocumentFragment();
            var last = 0, match, replaced = false;
            while ((match = re.exec(text))) {
                replaced = true;
                if (match.index > last) {
                    frag.appendChild(document.createTextNode(text.slice(last, match.index)));
                }
                frag.appendChild(buildParenFragment(match[0]));   // incl. the parens
                last = match.index + match[0].length;
            }
            if (!replaced) return;
            if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
            textNode.parentNode.replaceChild(frag, textNode);
        });
    }

    // ── "Change speaker" dialog ──────────────────────────────────────────
    function wireSpeakerDialog() {
        var dialog = document.getElementById("speaker-dialog");
        if (!dialog || typeof dialog.showModal !== "function") return;

        var fLabel = document.getElementById("dlg-label");
        var fLabelTxt = document.getElementById("dlg-label-txt");
        var fName = document.getElementById("dlg-name");
        var cancel = document.getElementById("dlg-cancel");

        document.addEventListener("click", function (e) {
            var link = e.target.closest ? e.target.closest(".turn-speaker-link") : null;
            if (!link) return;
            e.preventDefault();
            fLabel.value = link.dataset.label || "";
            fLabelTxt.textContent = link.dataset.label || "";
            fName.value = link.dataset.name || "";
            dialog.showModal();
            fName.focus();
            fName.select();
        });

        if (cancel) cancel.addEventListener("click", function () { dialog.close(); });

        // htmx swaps the pending-changes panel after submit → close the dialog.
        var form = dialog.querySelector("form");
        if (form) {
            form.addEventListener("htmx:afterRequest", function (evt) {
                if (evt.detail && evt.detail.successful) dialog.close();
            });
        }
    }

    // Only the audio-dependent half is skipped without a recording; the speaker
    // dialog works either way.
    if (document.getElementById("meeting-audio")) {
        markTranscriptTimestamps();
        linkifyParens(document.querySelector(".topics-list"));
    }
    wireSpeakerDialog();
})();
