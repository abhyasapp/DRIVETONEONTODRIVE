/* Split a SQL script into single statements.
 *
 * The hosted console talks to Neon over an HTTPS SQL endpoint that takes one
 * query per request, so a multi-statement script (functions included) has to be
 * split without breaking `$$`-quoted bodies, string literals or comments.
 * Exposes global.AbhyasSql.split(sql).
 */
(function (global) {
  'use strict';

  function split(text) {
    const statements = [];
    let start = 0;
    let i = 0;
    const n = text.length;

    while (i < n) {
      const ch = text[i];

      if (ch === '-' && text[i + 1] === '-') {
        while (i < n && text[i] !== '\n') i += 1;
        continue;
      }

      if (ch === '/' && text[i + 1] === '*') {
        i += 2;
        while (i < n && !(text[i] === '*' && text[i + 1] === '/')) i += 1;
        i += 2;
        continue;
      }

      if (ch === "'") {
        i += 1;
        while (i < n) {
          if (text[i] === "'" && text[i + 1] === "'") { i += 2; continue; }
          if (text[i] === "'") { i += 1; break; }
          i += 1;
        }
        continue;
      }

      if (ch === '"') {
        i += 1;
        while (i < n && text[i] !== '"') i += 1;
        i += 1;
        continue;
      }

      if (ch === '$') {
        const rest = text.slice(i);
        const tag = /^\$[A-Za-z_][A-Za-z0-9_]*\$|^\$\$/.exec(rest);
        if (tag) {
          const closer = tag[0];
          const end = text.indexOf(closer, i + closer.length);
          i = end === -1 ? n : end + closer.length;
          continue;
        }
      }

      if (ch === ';') {
        const statement = text.slice(start, i).trim();
        if (statement) statements.push(statement);
        start = i + 1;
      }

      i += 1;
    }

    const tail = text.slice(start).trim();
    if (tail) statements.push(tail);
    return statements;
  }

  global.AbhyasSql = { split };
})(typeof window !== 'undefined' ? window : globalThis);
