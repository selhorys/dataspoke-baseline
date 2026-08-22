/**
 * formatDatasetFilter — the Auto-indent formatter behind DatasetFilterEditor.
 *
 * Purely **lexical**: it tokenizes the clause and re-emits the same tokens
 * verbatim with different whitespace. It holds no grammar knowledge, never
 * validates, never rejects, and never repairs — the backend owns the grammar
 * (`src/shared/dataset_filter.py`, surfaced as `422 INVALID_DATASET_FILTER`),
 * so text this formatter cannot make sense of is passed through rather than
 * rewritten. Token text (including keyword case) is preserved exactly; only
 * whitespace changes.
 *
 * Layout produced — the canonical form `format_filter()` renders server-side:
 *   - one operand per line, the boolean operator leading each continuation line
 *   - a grouping `(` opens at the end of its line, its body indented one level,
 *     and its `)` sits alone at the parent's indent
 *   - an `IN (…)` value list stays on one line
 *
 * The only lookbehind is one token: a `(` directly after the `IN` keyword is a
 * value list, any other `(` is a group. That is a lexical distinction (which
 * token precedes which), not a parse. `NOT IN (…)` needs no second case — `IN`
 * is still the token immediately before the `(` — and `NOT` is not a
 * line-breaking keyword, so `NOT IN` is laid out as one unbroken run rather
 * than a `NOT` stranded from its `IN`.
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §Shared component notes (DatasetFilterEditor),
 *       spec/API.md §`dataset_filter` grammar.
 */

const INDENT = "    ";

type TokenKind = "string" | "word" | "punct";

interface Token {
  kind: TokenKind;
  /** Source text, verbatim. */
  text: string;
}

const WORD_START = /[A-Za-z_]/;
const WORD_CHAR = /[A-Za-z0-9_]/;

/**
 * Splits the clause into string literals, bare words, and punctuation.
 * Whitespace is dropped (the formatter re-emits its own). Punctuation is one
 * character except `!=`, which is lexed whole so the grammar's only
 * not-equals operator is never split across a line break or spaced apart.
 * Anything unrecognised becomes a one-character `punct` token so it survives
 * the round trip instead of being swallowed — a lone `!` reaches the backend
 * to be reported rather than disappearing here.
 */
export function tokenizeDatasetFilter(text: string): Token[] {
  const tokens: Token[] = [];
  let i = 0;

  while (i < text.length) {
    const ch = text[i];

    if (/\s/.test(ch)) {
      i += 1;
      continue;
    }

    if (ch === "'") {
      // Single-quoted literal; '' is an escaped quote. An unterminated literal
      // takes the rest of the input — the backend will report the error.
      let j = i + 1;
      let closed = false;
      while (j < text.length) {
        if (text[j] === "'") {
          if (text[j + 1] === "'") {
            j += 2;
            continue;
          }
          j += 1;
          closed = true;
          break;
        }
        j += 1;
      }
      if (!closed) j = text.length;
      tokens.push({ kind: "string", text: text.slice(i, j) });
      i = j;
      continue;
    }

    if (WORD_START.test(ch)) {
      let j = i + 1;
      while (j < text.length && WORD_CHAR.test(text[j])) j += 1;
      tokens.push({ kind: "word", text: text.slice(i, j) });
      i = j;
      continue;
    }

    if (ch === "!" && text[i + 1] === "=") {
      tokens.push({ kind: "punct", text: "!=" });
      i += 2;
      continue;
    }

    tokens.push({ kind: "punct", text: ch });
    i += 1;
  }

  return tokens;
}

function isKeyword(token: Token | undefined, keyword: string): boolean {
  return token?.kind === "word" && token.text.toUpperCase() === keyword;
}

/** Paren context: a boolean group breaks across lines, an IN list stays inline. */
type Frame = "group" | "list";

export function formatDatasetFilter(text: string): string {
  const tokens = tokenizeDatasetFilter(text);
  if (tokens.length === 0) return "";

  const lines: string[] = [];
  const stack: Frame[] = [];
  let indent = 0;
  let current = "";
  let glueNext = false;

  const flush = () => {
    if (current.trim().length > 0) lines.push(current.replace(/\s+$/, ""));
    current = "";
  };
  const startLine = () => {
    current = INDENT.repeat(indent);
  };
  const append = (piece: string, glue = false) => {
    const attached = glue || glueNext;
    glueNext = false;
    const needsSpace = current.length > 0 && !current.endsWith(" ") && !attached;
    current += (needsSpace ? " " : "") + piece;
  };

  for (let i = 0; i < tokens.length; i += 1) {
    const token = tokens[i];
    const previous = tokens[i - 1];

    if (token.kind === "punct" && token.text === "(") {
      // Covers `NOT IN (…)` as well: `IN` is still the token immediately
      // before the `(`, so the negated value list stays inline like the
      // affirmative one.
      if (isKeyword(previous, "IN")) {
        stack.push("list");
        append("(");
        glueNext = true;
      } else {
        stack.push("group");
        append("(");
        flush();
        indent += 1;
        startLine();
      }
      continue;
    }

    if (token.kind === "punct" && token.text === ")") {
      // An unbalanced `)` (nothing on the stack) is left inline: the formatter
      // does not judge the text, it only lays it out.
      const frame = stack.pop();
      if (frame === "group") {
        flush();
        indent = Math.max(0, indent - 1);
        startLine();
        append(")", true);
      } else {
        append(")", true);
      }
      continue;
    }

    if (token.kind === "punct" && token.text === ",") {
      append(",", true);
      continue;
    }

    const insideList = stack[stack.length - 1] === "list";
    // `AND` / `OR` join operands, so each starts a line. `NOT` is deliberately
    // not in this set: it appears only inside a predicate, as the first half of
    // `NOT IN`, and breaking there would strand it from its `IN`.
    if (!insideList && (isKeyword(token, "AND") || isKeyword(token, "OR"))) {
      flush();
      startLine();
      append(token.text, true);
      continue;
    }

    append(token.text);
  }

  flush();
  return lines.join("\n");
}
