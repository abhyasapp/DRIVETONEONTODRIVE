/* Browser copy of the identity helpers from dedupe/content_identity.py.
 *
 * The console edits and merges rows straight from the browser, so it must
 * derive exactly the same identity hashes the Python pipeline stores. Anything
 * that drifts here would make an edited question look like a brand new one on
 * the next sync. Keep this file in step with content_identity.py:
 *
 *   canonical()          -> _canonical()
 *   identity()           -> identity()
 *   sourceLocationKey()  -> source_location_key()
 *   answerKey()          -> answer_identity()
 *   qualityScore()       -> compute_quality_score()
 *
 * Exposes global.AbhyasIdentity.
 */
(function (global) {
  'use strict';

  const INVISIBLE = /[\u00ad\u200b\u200c\u200d\u200e\u200f\u2060\ufeff]/g;
  const WHITESPACE = /\s+/g;
  const KEEP_SYMBOLS = '+-*/=<>^%~';

  const PUNCTUATION = {
    '\u2018': "'", '\u2019': "'", '\u201a': "'", '\u201b': "'",
    '\u2032': "'", '\u2035': "'",
    '\u201c': '"', '\u201d': '"', '\u201e': '"', '\u201f': '"',
    '\u2033': '"', '\u2036': '"',
    '\u2010': '-', '\u2011': '-', '\u2012': '-', '\u2013': '-',
    '\u2014': '-', '\u2015': '-', '\u2212': '-',
    '\u2026': '...',
    '\u00a0': ' ', '\u2007': ' ', '\u202f': ' '
  };

  const ALNUM = /^[\p{L}\p{N}]$/u;

  function canonical(value) {
    if (value === null || value === undefined) return '';
    let text = String(value).normalize('NFC').replace(INVISIBLE, '');
    let mapped = '';
    for (const ch of text) mapped += PUNCTUATION[ch] !== undefined ? PUNCTUATION[ch] : ch;
    mapped = mapped.toLowerCase();
    let out = '';
    for (const ch of mapped) {
      if (/\s/.test(ch)) out += ' ';
      else if (ALNUM.test(ch) || KEEP_SYMBOLS.includes(ch)) out += ch;
    }
    return out.replace(WHITESPACE, ' ').trim();
  }

  function stripQuestionNumber(stem) {
    const text = String(stem === null || stem === undefined ? '' : stem);
    const match = /^\s*\p{Nd}+\s*[.)]\s*/u.exec(text);
    return match ? text.slice(match[0].length) : text;
  }

  /* json.dumps(value, ensure_ascii=False, sort_keys=True) for plain data. */
  function pythonJson(value, compact) {
    const itemSeparator = compact ? ',' : ', ';
    const keySeparator = compact ? ':' : ': ';
    if (value === null) return 'null';
    if (typeof value === 'string') return JSON.stringify(value);
    if (typeof value === 'number') return Number.isFinite(value) ? String(value) : 'NaN';
    if (Array.isArray(value)) return '[' + value.map(item => pythonJson(item, compact)).join(itemSeparator) + ']';
    const keys = Object.keys(value).sort();
    return '{' + keys.map(key => JSON.stringify(key) + keySeparator + pythonJson(value[key], compact))
      .join(itemSeparator) + '}';
  }

  /* Python sorts str by code point; JS sort() compares UTF-16 code units. */
  function compareCodePoints(a, b) {
    const left = Array.from(a, ch => ch.codePointAt(0));
    const right = Array.from(b, ch => ch.codePointAt(0));
    const length = Math.min(left.length, right.length);
    for (let i = 0; i < length; i += 1) {
      if (left[i] !== right[i]) return left[i] - right[i];
    }
    return left.length - right.length;
  }

  async function sha256Hex(text) {
    const subtle = global.crypto && global.crypto.subtle;
    if (!subtle) throw new Error('This browser cannot compute SHA-256 outside a secure (https) page.');
    const bytes = new TextEncoder().encode(text);
    const digest = await subtle.digest('SHA-256', bytes);
    return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
  }

  async function identity(stem, options) {
    let canonicalStem = canonical(stripQuestionNumber(stem));
    canonicalStem = canonicalStem.replace(/\s+([,.;:!?%])/g, '$1');
    const canonicalOptions = (Array.isArray(options) ? options : []).map(canonical)
      .sort(compareCodePoints);
    return sha256Hex(pythonJson({ stem: canonicalStem, opts: canonicalOptions }, false));
  }

  async function sourceLocationKey(bookName, subtopic) {
    return sha256Hex(pythonJson([canonical(bookName), canonical(subtopic)], true));
  }

  function canonicalAnswer(value) {
    // A hyphen joining two letters is typography ("back-sight" == "back sight");
    // a numeric hyphen is kept because "5-10%" is a range, not 510%.
    return canonical(value)
      .replace(/\s*-\s*/g, '-')
      .replace(/([a-z])-(?=[a-z])/g, '$1')
      .replace(/ /g, '');
  }

  function answerKey(correctText, correctIndex, options) {
    const hasText = correctText !== null && correctText !== undefined
      && String(correctText).trim() !== '';
    const hasIndex = correctIndex !== null && correctIndex !== undefined;
    const list = Array.isArray(options) ? options : null;
    const validIndex = Number.isInteger(correctIndex) && list !== null
      && correctIndex >= 0 && correctIndex < list.length;
    if (hasIndex && !validIndex) return '__answer_conflict__';
    const indexed = validIndex ? canonicalAnswer(list[correctIndex]) : null;
    if (hasText) {
      const textAnswer = canonicalAnswer(correctText);
      if (indexed !== null && textAnswer !== indexed) return '__answer_conflict__';
      return textAnswer;
    }
    if (indexed !== null) return indexed;
    return '__unanswered__';
  }

  function qualityScore(explanation, options, textLength) {
    let score = 0;
    if (explanation && String(explanation).trim()) score += 20;
    const count = Array.isArray(options) ? options.length : 0;
    if (count === 4) score += 10;
    else if (count === 3) score += 5;
    else if (count >= 5) score += 8;
    const length = textLength || 0;
    if (length >= 40 && length <= 200) score += 3;
    return score;
  }

  global.AbhyasIdentity = {
    canonical,
    identity,
    sourceLocationKey,
    answerKey,
    qualityScore,
    pythonJson
  };
})(typeof window !== 'undefined' ? window : globalThis);
