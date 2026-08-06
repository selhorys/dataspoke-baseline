/**
 * Clipboard write that works outside a secure context.
 *
 * `navigator.clipboard` is exposed only to secure contexts — HTTPS or
 * `localhost`. A deployment served over plain HTTP therefore has no async
 * Clipboard API at all, so the write falls back to a selection over an
 * off-screen `<textarea>` driven by `document.execCommand("copy")`, which
 * carries no such requirement.
 *
 * Never throws: callers get `false` when the text did not reach the clipboard
 * and can surface a "copy it manually" message.
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  if (typeof navigator !== "undefined" && typeof navigator.clipboard?.writeText === "function") {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // A rejected write means a denied permission or a document that is not
      // focused; the selection path below can still succeed.
    }
  }
  return copyViaSelection(text);
}

/** Copy `text` by selecting it in a throwaway off-screen textarea. */
function copyViaSelection(text: string): boolean {
  if (typeof document === "undefined" || typeof document.execCommand !== "function") {
    return false;
  }

  const previouslyFocused =
    document.activeElement instanceof HTMLElement ? document.activeElement : null;
  let textarea: HTMLTextAreaElement | null = null;

  // Element construction, mounting and the copy all sit inside the try so that
  // no step can escape as a rejection — the contract is a boolean.
  try {
    textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.tabIndex = -1;
    // Fixed and transparent rather than `display: none` — a non-rendered node
    // holds no selection, so the copy command would find nothing to take. The
    // node is focused below, so it carries no `aria-hidden`.
    textarea.style.position = "fixed";
    textarea.style.top = "0";
    textarea.style.left = "0";
    textarea.style.width = "1px";
    textarea.style.height = "1px";
    textarea.style.padding = "0";
    textarea.style.border = "none";
    textarea.style.opacity = "0";
    textarea.style.pointerEvents = "none";

    // A Radix dialog traps focus inside its content, so a textarea parented to
    // `document.body` would have focus pulled back off it before the command
    // runs. Mounting it inside the open dialog keeps it within the trap.
    //
    // The dialog is found from the document rather than from
    // `document.activeElement`: Safari does not focus a <button> on click, so a
    // focus-derived lookup returns nothing on precisely the browsers this path
    // exists for. Radix marks a closed dialog `data-state="closed"`; the last
    // remaining one in document order is the topmost.
    const host =
      Array.from(document.querySelectorAll<HTMLElement>('[role="dialog"]'))
        .filter((el) => el.dataset.state !== "closed")
        .at(-1) ?? document.body;
    host.appendChild(textarea);

    textarea.focus({ preventScroll: true });
    textarea.select();
    textarea.setSelectionRange(0, text.length);
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    try {
      if (textarea?.isConnected) textarea.remove();
      previouslyFocused?.focus({ preventScroll: true });
    } catch {
      // Cleanup is best-effort; a throw here would escape past the return.
    }
  }
}
