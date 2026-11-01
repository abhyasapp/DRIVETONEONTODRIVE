/* Verifies that review/identity.js reproduces dedupe/content_identity.py exactly.
 *
 * The organiser-reviewed edits from index.html write identity hashes straight
 * into Neon, so a mismatch with the Python pipeline would make an edited
 * question look new on the next sync. The vectors are produced by the real
 * Python module, not copied by hand.
 *
 *   python tests/test_console_identity.mjs      (needs no extra packages)
 *   node tests/test_console_identity.mjs
 */
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');

const python = (() => {
  const candidates = [path.join(root, '.venv', 'Scripts', 'python.exe'),
    path.join(root, '.venv', 'bin', 'python'), 'python3', 'python'];
  return candidates.find(candidate => {
    try {
      execFileSync(candidate, ['--version'], { stdio: 'ignore' });
      return true;
    } catch { return false; }
  }) || 'python';
})();

const PY = String.raw`
import json, sys
sys.path.insert(0, ".")
from dedupe.content_identity import (identity, answer_identity, source_location_key,
                                     answers_equivalent, answers_similarity,
                                     answers_mergeable, compute_quality_score)

stems = [
    "What is the unit of force?",
    "  12. What is the unit of force? ",
    "12) Total station is used for ...",
    "What\u2019s the \u201cbest\u201d answer \u2014 A or B ?",
    "Which of the following is  NOT correct ?",
    "\u0938\u092e\u093e\u0928 \u092a\u094d\u0930\u0936\u094d\u0928 : \u092a\u094d\u0930\u0915\u093e\u0936\u0915\u0940\u092f \u0935\u093f\u0926\u094d\u092f\u0941\u0924\u094d \u0927\u093e\u0930\u093e \u0915\u094d\u092f\u093e \u0939\u094b?",
    "The pressure is 2 \u00d7 10^5 N/m2",
    "  (a) random spacing   and   INVISIBLE\u200bWORD",
    "Same words different order: load is applied slowly",
    "Slowly is applied load different: same words",
    "",
]
options = [
    ["Newton", "Joule", "Watt", "Pascal"],
    ["NEWTON", "joule", "Pascal", "watt"],
    ["A", "B"],
    [],
]
answers = [
    ("Newton", 0, ["Newton", "Joule"]),
    (None, 1, ["Newton", "Joule"]),
    ("Joule", 0, ["Newton", "Joule"]),
    (None, None, ["Newton", "Joule"]),
    (None, 5, ["Newton", "Joule"]),
    ("  newton ", None, ["Newton", "Joule"]),
    (None, None, []),
    ("back-sight", None, ["foresight", "intermediate sight", "back-sight", "any sight"]),
    (None, 2, ["foresight", "intermediate sight", "back sight", "any sight"]),
    ("back  -  sight", None, ["foresight"]),
    ("5-10%", None, ["2-3%", "5-10%"]),
    ("1-2 m", None, ["1-2 m", "12 m"]),
]

pairs = [
    ("back-sight", "back sight"),
    ("Gnatt chart", "Gantt chart"),
    ("perfomance/security money", "performance/security money"),
    ("siliceous", "silicious"),
    ("2-3% of estmated amount", "2-3% of estimated amount"),
    ("increase workable", "increase workability"),
    ("increase", "decrease"),
    ("5-10%", "10-15%"),
    ("2", "20"),
    ("2-3% of quoted amount", "3-4% of quoted amount"),
    ("< three crore", "< ten crore"),
    ("graded aggregates", "graded aggregate"),
    ("1 in 250", "1 : 250"),
    ("", ""),
    ("Newton", "Newton"),
    ("40 m", "(d) 40 m"),
    ("2-3% of estimated amount", "3-4% of estimated amount"),
    ("increase workable", "decrease workable"),
    ("i = 2/5 mr\u00b2", "i = 2/5 mr"),
    ("i = 2/5 mr\u00b2", "i = 2/5 mr\u00b2"),
    ("\u03c3cos\u00b2\u03b8", "\u03c3/2sin2\u03b8"),
]

out = {"identity": [], "answer": [], "location": [], "quality": [],
       "equivalent": [], "similarity": []}
for left, right in pairs:
    left_key = answer_identity(left, None, None)
    right_key = answer_identity(right, None, None)
    out["equivalent"].append({"a": left, "b": right, "ka": left_key, "kb": right_key,
                              "same": answers_equivalent(left_key, right_key)})
    out["similarity"].append({"a": left, "b": right, "ka": left_key, "kb": right_key,
                              "ratio": answers_similarity(left_key, right_key),
                              "mergeable": answers_mergeable(left_key, right_key)})
for stem in stems:
    for opts in options:
        out["identity"].append({"stem": stem, "options": opts,
                                "hash": identity(stem, opts)})
for text, index, opts in answers:
    out["answer"].append({"text": text, "index": index, "options": opts,
                          "key": answer_identity(text, index, opts)})
for book, topic in [("Sunil Sah ", " Highway "), ("RK SHRESTHA", "geotech"),
                    ("", ""), ("\u0938\u0928\u094d\u0926\u0947\u0936", "prakashan")]:
    out["location"].append({"book": book, "topic": topic,
                            "hash": source_location_key(book, topic)})
for explanation, opts, length in [("", ["a", "b", "c", "d"], 50),
                                  ("because", ["a", "b", "c"], 10),
                                  (None, ["a", "b", "c", "d", "e"], 250)]:
    out["quality"].append({"explanation": explanation, "options": opts,
                           "length": length,
                           "score": compute_quality_score(explanation, opts, length)})
print(json.dumps(out))
`;

const vectors = JSON.parse(execFileSync(python, ['-c', PY], {
  cwd: root,
  encoding: 'utf8',
  env: { ...process.env, PYTHONIOENCODING: 'utf-8' }
}));

vm.runInThisContext(readFileSync(path.join(root, 'review', 'identity.js'), 'utf8'));
const Identity = globalThis.AbhyasIdentity;

let checked = 0;
const failures = [];
const same = (label, actual, expected) => {
  checked += 1;
  if (actual !== expected) failures.push(`${label}\n  js:     ${actual}\n  python: ${expected}`);
};

for (const row of vectors.identity) {
  same(`identity(${JSON.stringify(row.stem).slice(0, 60)}, ${JSON.stringify(row.options)})`,
    await Identity.identity(row.stem, row.options), row.hash);
}
for (const row of vectors.answer) {
  same(`answerKey(${JSON.stringify(row.text)}, ${row.index})`,
    Identity.answerKey(row.text, row.index, row.options), row.key);
}
for (const row of vectors.location) {
  same(`sourceLocationKey(${JSON.stringify(row.book)}, ${JSON.stringify(row.topic)})`,
    await Identity.sourceLocationKey(row.book, row.topic), row.hash);
}
for (const row of vectors.equivalent) {
  same(`answersEquivalent(${JSON.stringify(row.a)}, ${JSON.stringify(row.b)})`,
    Identity.answersEquivalent(row.ka, row.kb), row.same);
}
for (const row of vectors.similarity) {
  same(`similarity(${JSON.stringify(row.a)}, ${JSON.stringify(row.b)})`,
    Math.round(Identity.answerSimilarity(row.ka, row.kb) * 1e6) / 1e6,
    Math.round(row.ratio * 1e6) / 1e6);
  same(`answersMergeable(${JSON.stringify(row.a)}, ${JSON.stringify(row.b)})`,
    Identity.answersMergeable(row.ka, row.kb), row.mergeable);
}
for (const row of vectors.quality) {
  same(`qualityScore(${JSON.stringify(row.explanation)}, ${JSON.stringify(row.options)}, ${row.length})`,
    Identity.qualityScore(row.explanation, row.options, row.length), row.score);
}

if (failures.length) {
  console.error(`identity parity FAILED (${failures.length}/${checked})`);
  for (const failure of failures.slice(0, 10)) console.error(failure);
  process.exit(1);
}
console.log(`identity parity OK: ${checked} vectors match dedupe/content_identity.py`);
