# 2026-06-27 — Chat code-block: copy button + light-theme legibility

User report (paraphrased from Vietnamese): the **Copy** button on code blocks in
`/chat` doesn't work, and switching to the **light** theme makes the code block
render in colors you can't read.

## Bug 1 — Copy button does nothing

`chat.html` `md()` rendered the copy button with an inline handler:

```js
<button ... onclick="(function(b){const t=...;navigator.clipboard.writeText(t);...})(this)">Copy</button>
```

`navigator.clipboard` only exists in a **secure context** (HTTPS or
`localhost`/`127.0.0.1`). On the dev box reached over plain `http://<ip>:8787`
(off the VPN tunnel) it's `undefined`, so `navigator.clipboard.writeText` throws
a `TypeError`, the handler dies, and the button text never changes — looks dead.

**Fix:** drop the inline handler. Alpine `@click` can't bind to markup injected
via `x-html`, so we attach one **delegated** click listener on `$refs.scroll` in
`init()` (runs before the lock-gate return so it works pre/post unlock):

```js
this.$refs.scroll?.addEventListener("click", (e) => {
  const btn = e.target.closest(".chat-copy");
  if (btn) this.copyCode(btn);
});
```

`copyText()` tries the async Clipboard API when `window.isSecureContext`, else
falls back to a hidden `<textarea>` + `document.execCommand("copy")` — which works
over plain http. `copyCode()` reads the sibling `<pre>` via
`btn.closest(".chat-code").querySelector("pre")` (robust vs the old
`parentElement.nextElementSibling` chain).

## Bug 2 — Light theme: block code is invisible

CSS selector leak. The inline-code rule was:

```css
.prose-chat :not(.chat-code) > code { background: #0a0a0a; ... }
html.theme-light .prose-chat :not(.chat-code) > code { background: #eef0f2; }
```

`:not(.chat-code) > code` *also* matches **block** code, because block code is
`.chat-code > pre > code` — the `<code>`'s direct parent is the `<pre>`, which is
not `.chat-code`. So the block `<code>` picked up the inline background. In dark
theme both backgrounds are dark, so nothing showed; in **light** theme it got
`#eef0f2` (light) while keeping its light text `#e6edf3` → light-on-light.

**Fix:** scope the inline rule to `:not(pre) > code` (block code can no longer
match — its parent *is* a `pre`), and give block code explicit light-theme colors:

```css
html.theme-light .chat-code { border-color: #d6dade; }
html.theme-light .chat-code pre { background: #f6f8fa; }
html.theme-light .chat-code code { color: #1f2328; }
```

Now: dark theme = dark block + light text; light theme = light block + dark text.
The change touches **only** block code — every inline `<code>` matches the old and
new selectors identically (verified).

## Verification

Headless-browser deps aren't installable in this sandbox (no passwordless sudo for
`playwright install-deps`; `libatk` et al. missing), so the full
`scripts/verify_chat_ui.py` render must run on the host. Instead verified the exact
failure modes with a real DOM engine (jsdom) against the **real** extracted
`md()`/`copyCode`/`copyText`:

- copy button has no inline `onclick`; renders `class="chat-copy"`.
- `blockCode.matches(".prose-chat :not(.chat-code) > code")` → **true** (bug
  reproduced); `.matches(".prose-chat :not(pre) > code")` → **false** (fixed).
- every non-block `<code>` matches old-selector iff new-selector (no inline
  regression); a nested inline `<code>` still matches the new rule.
- `copyCode` traversal returns the `<pre>` text (`print('hi')\nx = 1`), not the bar.
- `copyText` falls back to `execCommand` and returns true when `navigator.clipboard`
  is absent.

Plus regression guards in `tests/test_app.py`
(`test_chat_code_block_light_theme_is_legible`,
`test_chat_copy_button_uses_delegated_handler`). `uv run ruff check` clean;
`uv run python -m pytest -q` all pass.

Users must **hard-refresh** (Ctrl+Shift+R) to drop the cached old CSS/JS.
