/**
 * Tests for lib/clipboard.ts — the clipboard write used by the one-shot token
 * reveal dialog on /profile/tokens.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §API tokens (`/profile/tokens`): "the
 *     clipboard copy button is the primary action — the user must transfer the
 *     token to wherever it will be used before closing the dialog. Closing
 *     without copy means the user must revoke and re-mint." The copy path is the
 *     only way to keep a minted credential, so it has to work where the app is
 *     served, and it must report its own failure rather than fail silently.
 *   - spec/TESTING.md §Assertion Discipline — "Absence assertions require
 *     injection": every "did not use the fallback" assertion below installs the
 *     fallback first, so it can distinguish "not used" from "not available".
 *
 * Why both globals are stubbed in every test:
 *   jsdom implements neither `navigator.clipboard` nor `document.execCommand`.
 *   Left alone, `copyToClipboard` short-circuits through both guards and only
 *   the total-failure branch is ever reached — a suite that asserted `false` on
 *   an unstubbed environment would prove nothing about the fallback the module
 *   exists to provide. Each test therefore declares which of the two mechanisms
 *   exists, and the failure branch is reached by removing both on purpose.
 *
 * Global hygiene: the two globals are singletons of the test environment. Every
 * test restores the descriptor captured at module load, and the restore is
 * asserted in `afterEach` so a botched restore fails the test that caused it
 * rather than silently changing the environment of the next one.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { copyToClipboard } from "./clipboard";

// ── Pristine descriptors, captured before any test mutates them ───────────────

const ORIGINAL_CLIPBOARD = Object.getOwnPropertyDescriptor(navigator, "clipboard");
const ORIGINAL_EXEC_COMMAND = Object.getOwnPropertyDescriptor(document, "execCommand");

type ExecCommandFn = (commandId: string, showUI?: boolean, value?: string) => boolean;

/**
 * Install `navigator.clipboard`, or shadow it with `undefined` to model a
 * non-secure context. An own property is defined either way, so a Clipboard
 * that some jsdom build exposes on the prototype cannot leak into the
 * "unavailable" cases.
 */
function stubClipboard(value: { writeText?: unknown } | undefined): void {
  Object.defineProperty(navigator, "clipboard", {
    value,
    configurable: true,
    writable: true,
  });
}

/** Install `document.execCommand`, or shadow it with `undefined`. */
function stubExecCommand(fn: ExecCommandFn | undefined): void {
  Object.defineProperty(document, "execCommand", {
    value: fn,
    configurable: true,
    writable: true,
  });
}

/** Put both globals back exactly as the environment had them. */
function restoreGlobals(): void {
  Reflect.deleteProperty(navigator, "clipboard");
  if (ORIGINAL_CLIPBOARD) Object.defineProperty(navigator, "clipboard", ORIGINAL_CLIPBOARD);
  Reflect.deleteProperty(document, "execCommand");
  if (ORIGINAL_EXEC_COMMAND) Object.defineProperty(document, "execCommand", ORIGINAL_EXEC_COMMAND);
}

/**
 * An `execCommand` stub that records what the DOM looked like at the moment the
 * copy was issued — the only instant at which the fallback's textarea, its
 * value and its parent are observable, since the module removes the node again
 * before returning.
 */
function recordingExecCommand(result: boolean | (() => never)) {
  const seen = {
    commands: [] as string[],
    value: null as string | null,
    host: null as Element | null,
    focused: null as Element | null,
    connected: false,
  };
  const fn = vi.fn(((commandId: string) => {
    seen.commands.push(commandId);
    const textarea = document.querySelector("textarea");
    seen.value = textarea?.value ?? null;
    seen.host = textarea?.parentElement ?? null;
    seen.connected = textarea?.isConnected ?? false;
    seen.focused = document.activeElement;
    if (typeof result === "function") return result();
    return result;
  }) as ExecCommandFn);
  return { fn, seen };
}

beforeEach(() => {
  document.body.innerHTML = "";
});

afterEach(() => {
  restoreGlobals();
  // The restore is asserted, not assumed.
  expect(Object.getOwnPropertyDescriptor(navigator, "clipboard")).toEqual(ORIGINAL_CLIPBOARD);
  expect(Object.getOwnPropertyDescriptor(document, "execCommand")).toEqual(ORIGINAL_EXEC_COMMAND);
  vi.restoreAllMocks();
});

// ── 1. The async Clipboard API path ───────────────────────────────────────────

describe("copyToClipboard — secure context, Clipboard API available", () => {
  it("writes through navigator.clipboard and reports success", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    stubClipboard({ writeText });
    const { fn: exec } = recordingExecCommand(true);
    stubExecCommand(exec);

    await expect(copyToClipboard("dsk_secret_value")).resolves.toBe(true);

    expect(writeText).toHaveBeenCalledWith("dsk_secret_value");
    // The fallback is installed and would have succeeded — so "not called" here
    // means the preferred path was taken, not that no path existed.
    expect(exec).not.toHaveBeenCalled();
    expect(document.querySelector("textarea")).toBeNull();
  });
});

// ── 2. The selection fallback ─────────────────────────────────────────────────

describe("copyToClipboard — the execCommand fallback", () => {
  it("falls back when the Clipboard API is absent, the plain-HTTP case", async () => {
    // A deployment served over plain HTTP is not a secure context, so the
    // property does not exist at all. This is the state the fix exists for.
    stubClipboard(undefined);
    const { fn: exec, seen } = recordingExecCommand(true);
    stubExecCommand(exec);

    await expect(copyToClipboard("dsk_plain_http")).resolves.toBe(true);

    expect(seen.commands).toEqual(["copy"]);
    expect(seen.value).toBe("dsk_plain_http");
    expect(seen.connected).toBe(true);
  });

  it("falls back when the Clipboard API exists but rejects the write", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("NotAllowedError"));
    stubClipboard({ writeText });
    const { fn: exec, seen } = recordingExecCommand(true);
    stubExecCommand(exec);

    await expect(copyToClipboard("dsk_denied_then_copied")).resolves.toBe(true);

    expect(writeText).toHaveBeenCalledOnce();
    expect(seen.commands).toEqual(["copy"]);
    expect(seen.value).toBe("dsk_denied_then_copied");
  });

  it("falls back when the Clipboard API exists but carries no writeText function", async () => {
    // The guard is a type check, not a presence check: an object standing where
    // the Clipboard should be must not be called as if it were one.
    stubClipboard({});
    const { fn: exec, seen } = recordingExecCommand(true);
    stubExecCommand(exec);

    await expect(copyToClipboard("dsk_no_write_text")).resolves.toBe(true);
    expect(seen.value).toBe("dsk_no_write_text");
  });

  it("selects the text before issuing the copy, so the command has something to take", async () => {
    stubClipboard(undefined);
    const selection = { start: -1, end: -1, focused: false };
    const exec = vi.fn(((commandId: string) => {
      const textarea = document.querySelector("textarea");
      if (textarea) {
        selection.start = textarea.selectionStart;
        selection.end = textarea.selectionEnd;
        selection.focused = document.activeElement === textarea;
      }
      return commandId === "copy";
    }) as ExecCommandFn);
    stubExecCommand(exec);

    await expect(copyToClipboard("dsk_selected")).resolves.toBe(true);

    expect(selection.focused).toBe(true);
    expect(selection.start).toBe(0);
    expect(selection.end).toBe("dsk_selected".length);
  });

  it("reports failure when the copy command itself declines", async () => {
    stubClipboard(undefined);
    const { fn: exec } = recordingExecCommand(false);
    stubExecCommand(exec);

    await expect(copyToClipboard("dsk_declined")).resolves.toBe(false);
    expect(exec).toHaveBeenCalledOnce();
  });
});

// ── 3. Parenting inside the Radix focus trap ──────────────────────────────────

describe("copyToClipboard — the fallback node is parented inside the open dialog", () => {
  // The fixtures below mirror what Radix renders for an open DialogContent:
  // `role="dialog"` plus its own `data-state="open"` marker.
  it("mounts the textarea inside the open dialog, not on document.body", async () => {
    // Radix traps focus inside the dialog's content: a body-parented textarea
    // has focus pulled off it before the command runs, and the copy silently
    // takes nothing. The reveal dialog is the only place this helper is used
    // from, so the dialog case is the real one.
    document.body.innerHTML = `
      <div role="dialog" data-state="open" id="reveal">
        <code>dsk_in_dialog</code>
        <button id="copy-btn" aria-label="Copy token">copy</button>
      </div>`;
    const copyButton = document.getElementById("copy-btn") as HTMLButtonElement;
    copyButton.focus();
    expect(document.activeElement).toBe(copyButton);

    stubClipboard(undefined);
    const { fn: exec, seen } = recordingExecCommand(true);
    stubExecCommand(exec);

    await expect(copyToClipboard("dsk_in_dialog")).resolves.toBe(true);

    const dialog = document.getElementById("reveal");
    expect(seen.host).toBe(dialog);
    expect(seen.host).not.toBe(document.body);
    expect(seen.focused).not.toBe(copyButton);
  });

  it("mounts inside the open dialog even when nothing in it holds focus", async () => {
    // Not every browser focuses a <button> on click, so the open dialog has to
    // be found without going through `document.activeElement`. With focus left
    // on the body, a focus-derived lookup would parent the node on the body and
    // the trap would take focus off it again.
    document.body.innerHTML = `
      <div role="dialog" data-state="open" id="reveal">
        <code>dsk_unfocused_dialog</code>
        <button id="copy-btn" aria-label="Copy token">copy</button>
      </div>`;
    expect(document.activeElement).toBe(document.body);

    stubClipboard(undefined);
    const { fn: exec, seen } = recordingExecCommand(true);
    stubExecCommand(exec);

    await expect(copyToClipboard("dsk_unfocused_dialog")).resolves.toBe(true);

    expect(seen.host).toBe(document.getElementById("reveal"));
  });

  it("skips a dialog Radix has marked closed and mounts inside the open one", async () => {
    // Radix keeps a closing dialog mounted for the length of its exit
    // animation, marking it `data-state="closed"`. It comes later in document
    // order than the one that stays open, so a lookup that took the last
    // `[role="dialog"]` outright would parent the textarea in a node on its way
    // out of the document — and the copy would take nothing.
    document.body.innerHTML = `
      <div role="dialog" data-state="open" id="reveal">
        <code>dsk_two_dialogs</code>
      </div>
      <div role="dialog" data-state="closed" id="closing">
        <code>stale</code>
      </div>`;

    stubClipboard(undefined);
    const { fn: exec, seen } = recordingExecCommand(true);
    stubExecCommand(exec);

    await expect(copyToClipboard("dsk_two_dialogs")).resolves.toBe(true);

    expect(seen.host).toBe(document.getElementById("reveal"));
    expect(seen.host).not.toBe(document.getElementById("closing"));
  });

  it("restores focus to the element that had it, so the dialog is left as it was found", async () => {
    document.body.innerHTML = `
      <div role="dialog" data-state="open" id="reveal">
        <button id="copy-btn" aria-label="Copy token">copy</button>
      </div>`;
    const copyButton = document.getElementById("copy-btn") as HTMLButtonElement;
    copyButton.focus();

    stubClipboard(undefined);
    const { fn: exec } = recordingExecCommand(true);
    stubExecCommand(exec);

    await copyToClipboard("dsk_focus_restored");

    expect(document.activeElement).toBe(copyButton);
  });

  it("removes the textarea afterwards, leaving neither the dialog nor the body changed", async () => {
    document.body.innerHTML = `
      <div role="dialog" data-state="open" id="reveal">
        <button id="copy-btn">copy</button>
      </div>`;
    (document.getElementById("copy-btn") as HTMLButtonElement).focus();
    const dialogHtmlBefore = document.getElementById("reveal")!.innerHTML;

    stubClipboard(undefined);
    const { fn: exec, seen } = recordingExecCommand(true);
    stubExecCommand(exec);

    await copyToClipboard("dsk_cleanup");

    // The node existed at copy time (backstop) and is gone now.
    expect(seen.connected).toBe(true);
    expect(document.querySelectorAll("textarea")).toHaveLength(0);
    expect(document.getElementById("reveal")!.innerHTML).toBe(dialogHtmlBefore);
  });

  it("removes the textarea even when the copy command throws", async () => {
    stubClipboard(undefined);
    const { fn: exec, seen } = recordingExecCommand(() => {
      throw new Error("execCommand blew up");
    });
    stubExecCommand(exec);

    await expect(copyToClipboard("dsk_throwing_copy")).resolves.toBe(false);

    expect(seen.connected).toBe(true);
    expect(document.querySelectorAll("textarea")).toHaveLength(0);
  });
});

// ── 4. Total failure and the never-throws contract ────────────────────────────

describe("copyToClipboard — returns false rather than throwing, on every path", () => {
  it("returns false when neither the Clipboard API nor execCommand exists", async () => {
    stubClipboard(undefined);
    stubExecCommand(undefined);

    await expect(copyToClipboard("dsk_nowhere_to_go")).resolves.toBe(false);
    // Nothing was mounted, so nothing needs cleaning up.
    expect(document.querySelector("textarea")).toBeNull();
  });

  it("returns false when the Clipboard API throws synchronously and no fallback exists", async () => {
    stubClipboard({
      writeText: () => {
        throw new Error("synchronous explosion");
      },
    });
    stubExecCommand(undefined);

    await expect(copyToClipboard("dsk_sync_throw")).resolves.toBe(false);
  });

  it("returns false when creating the textarea throws", async () => {
    stubClipboard(undefined);
    const { fn: exec } = recordingExecCommand(true);
    stubExecCommand(exec);
    const createElement = vi.spyOn(document, "createElement").mockImplementation(() => {
      throw new Error("createElement blew up");
    });

    await expect(copyToClipboard("dsk_no_element")).resolves.toBe(false);

    expect(createElement).toHaveBeenCalledWith("textarea");
    expect(exec).not.toHaveBeenCalled();
  });

  it("returns false when mounting the textarea throws", async () => {
    stubClipboard(undefined);
    const { fn: exec } = recordingExecCommand(true);
    stubExecCommand(exec);
    const appendChild = vi.spyOn(document.body, "appendChild").mockImplementation(() => {
      throw new Error("appendChild blew up");
    });

    await expect(copyToClipboard("dsk_no_mount")).resolves.toBe(false);

    expect(appendChild).toHaveBeenCalledOnce();
    expect(exec).not.toHaveBeenCalled();
    // The node never joined the document, so the `isConnected` guard in the
    // cleanup must not try to remove it — and must not throw doing so.
    expect(document.querySelector("textarea")).toBeNull();
  });

  it("still returns true when restoring focus throws, after the copy already succeeded", async () => {
    // Cleanup is best-effort, but it runs in a `finally` — a throw there would
    // escape past the return value the caller is waiting on.
    document.body.innerHTML = `<div role="dialog" data-state="open"><button id="copy-btn">copy</button></div>`;
    const copyButton = document.getElementById("copy-btn") as HTMLButtonElement;
    copyButton.focus();
    vi.spyOn(copyButton, "focus").mockImplementation(() => {
      throw new Error("focus blew up");
    });

    stubClipboard(undefined);
    const { fn: exec } = recordingExecCommand(true);
    stubExecCommand(exec);

    // The copy did happen, so the caller is told so; the cleanup throw is swallowed.
    await expect(copyToClipboard("dsk_focus_throws")).resolves.toBe(true);
    expect(document.querySelectorAll("textarea")).toHaveLength(0);
  });
});
