"""Re-runnable benchmark: `python bench/bench.py` (from a checkout).

Measures, with fixed seeds and no network:
  1. grouping accuracy - which of feat/fix/refactor/docs/test/chore/other a
     commit subject belongs to - for the 0.2.0 reader (a conventional-commit
     prefix and nothing else) and the 0.3.0 one;
  2. ticket / pull-request / issue extraction, precision and recall, against
     a naive `[A-Z]+-\\d+|#\\d+` regex and the 0.2.0 merge-only PR number;
  3. noise folding: does it fold only what is noise;
  4. redaction: how many planted secrets survive, how many ordinary strings
     are changed (false positives);
  5. speed: commits classified + refs extracted per second.

Two corpora: `holdout.tsv` (99 subjects in the styles real repositories use,
labelled by hand BEFORE the classifier was scored) and a generated one
(seeded templates, thousands of subjects). Both were written by the authors
of the tool: they show regressions and relative gain, not the accuracy you
will see on your own history. Secrets are assembled at run time.

Writes bench/results.json when run with --write.
"""

from __future__ import annotations

import json
import os
import random
import re
import string
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from standupguy import digest, gitlog, redact, refs, themes   # noqa: E402

GROUPS = themes.GROUPS


# ---- corpora --------------------------------------------------------------

def load_holdout():
    rows = []
    with open(os.path.join(HERE, "holdout.tsv"), encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#") or not line.strip():
                continue
            g, subject, t, p, i = (line.rstrip("\n").split("\t") + [""] * 5)[:5]
            rows.append({
                "group": g, "subject": subject,
                "tickets": set(filter(None, t.split(","))),
                "prs": {int(x) for x in filter(None, p.split(","))},
                "issues": {int(x) for x in filter(None, i.split(","))}})
    return rows


_OBJ = ["login form", "billing page", "search index", "user export",
        "webhook handler", "session cache", "image resizer", "audit log",
        "retry queue", "report builder", "invite flow", "settings panel"]
_VERBS = {
    "feat": ["Add", "Implement", "Introduce", "Create", "Support", "Enable"],
    "fix": ["Fix", "Resolve", "Correct", "Prevent", "Repair", "Handle"],
    "refactor": ["Refactor", "Simplify", "Extract", "Rename", "Rework",
                 "Restructure"],
    "docs": ["Document", "Update README for", "Explain"],
    "test": ["Add tests for", "Cover", "Mock"],
    "chore": ["Bump", "Upgrade", "Pin", "Release"],
}
_SCOPES = ["api", "ui", "auth", "core", "db", "cli"]
_KEYS = ["ENG", "PLAT", "OPS", "BUG", "PAY", "WEB", "APP"]
_CONV = {"feat": "feat", "fix": "fix", "refactor": "refactor", "docs": "docs",
         "test": "test", "chore": "chore"}


def generated(n=3000, seed=20261107):
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        g = rng.choice(list(_VERBS))
        obj = rng.choice(_OBJ)
        style = rng.random()
        if style < 0.30:
            scope = f"({rng.choice(_SCOPES)})" if rng.random() < .5 else ""
            subject = f"{_CONV[g]}{scope}: {rng.choice(_VERBS[g]).lower()} {obj}"
        else:
            subject = f"{rng.choice(_VERBS[g])} {obj}"
        tickets, prs, issues = set(), set(), set()
        r = rng.random()
        if r < .25:
            k = f"{rng.choice(_KEYS)}-{rng.randint(1, 9999)}"
            tickets.add(k)
            form = rng.randrange(4)
            subject = [f"{k}: {subject}", f"[{k}] {subject}",
                       f"{subject} ({k})", f"{subject} [{k}]"][form]
        if rng.random() < .25:
            n_ = rng.randint(1, 9999)
            prs.add(n_)
            subject += f" (#{n_})"
        elif rng.random() < .12:
            n_ = rng.randint(1, 9999)
            issues.add(n_)
            subject += rng.choice([f", closes #{n_}", f" (fixes #{n_})",
                                   f" see #{n_}"])
        rows.append({"group": g, "subject": subject, "tickets": tickets,
                     "prs": prs, "issues": issues})
    # merge commits: the group is the branch prefix
    for _ in range(n // 10):
        g = rng.choice(["feat", "fix", "refactor", "docs", "chore"])
        prefix = {"feat": "feature", "fix": "bugfix", "refactor": "refactor",
                  "docs": "docs", "chore": "chore"}[g]
        n_ = rng.randint(1, 9999)
        rows.append({"group": g, "tickets": set(), "issues": set(),
                     "prs": {n_},
                     "subject": f"Merge pull request #{n_} from acme/"
                                f"{prefix}/{rng.choice(_OBJ).replace(' ', '-')}"})
    # decoys: look like keys or issues, are neither
    decoys = ["Support UTF-8 names", "Switch to SHA-256", "Handle ISO-8601",
              "Patch CVE-2024-4417", "Add HTTP-2", "Use TLS-1.3", "Bump GPT-4",
              "Parse X-Forwarded-For", "Use color #abc", "Add AES-256 option",
              "Fix RFC-3339 parsing", "Support RSA-4096 keys"]
    for _ in range(n // 10):
        rows.append({"group": None, "subject": rng.choice(decoys),
                     "tickets": set(), "prs": set(), "issues": set()})
    return rows


# ---- scoring --------------------------------------------------------------

def old_group(subject):
    return digest.classify(subject)[0]


def new_group(subject):
    return themes.classify_smart(subject)


def accuracy(rows, fn):
    graded = [r for r in rows if r["group"] is not None]
    ok = sum(fn(r["subject"]) == r["group"] for r in graded)
    return ok / len(graded)


def per_group(rows, fn):
    out = {}
    for g in GROUPS:
        graded = [r for r in rows if r["group"] == g]
        if graded:
            out[g] = round(sum(fn(r["subject"]) == g for r in graded)
                           / len(graded), 3)
    return out


_NAIVE_KEY = re.compile(r"\b[A-Z]+-\d+\b")
_NAIVE_NUM = re.compile(r"#(\d+)")


def prf(pairs):
    tp = fp = fn = 0
    for truth, found in pairs:
        tp += len(truth & found)
        fp += len(found - truth)
        fn += len(truth - found)
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return {"precision": round(p, 3), "recall": round(r, 3), "f1": round(f, 3)}


def ref_sets(rows, extractor):
    """Score tickets, PR numbers and issue numbers together as one set of
    labelled references per commit."""
    pairs = []
    for r in rows:
        truth = ({("t", t) for t in r["tickets"]}
                 | {("p", n) for n in r["prs"]}
                 | {("i", n) for n in r["issues"]})
        pairs.append((truth, extractor(r["subject"])))
    return prf(pairs)


def new_refs(subject):
    x = refs.extract(subject)
    return ({("t", t) for t in x.tickets} | {("p", n) for n in x.prs}
            | {("i", n) for n in x.issues})


def naive_refs(subject):
    # what a quick regex does: every KEY-N and every #N is "an issue/ticket"
    # (the PR is not told apart from an issue)
    return ({("t", t) for t in _NAIVE_KEY.findall(subject)}
            | {("i", int(n)) for n in _NAIVE_NUM.findall(subject)})


def old_refs(subject):
    # 0.2.0: only "Merge pull request #N" was understood
    m = gitlog._MERGE_RE.search(subject)
    return {("p", int(m.group(1)))} if m else set()


# ---- noise ---------------------------------------------------------------

NOISE = ["wip", "tmp", "save", "checkpoint", "asdf", "misc", "update", "fix",
         "more", ".", "stuff", "lint", "oops", "Merge branch 'main' into dev",
         "Merge remote-tracking branch 'origin/main'",
         "Merge branch 'dev' of github.com:acme/app"]
KEEP = ["wip: login form", "Fix typo in README", "Update billing totals",
        "Fix flaky test in scheduler", "Add save button", "Temp files are now"
        " cleaned on exit", "Merge pull request #5 from acme/feature/x",
        "Update dependencies", "Misc fixes for the importer (#3)",
        "ABC-1 wip", "Add checkpoint support", "Lint the YAML files in CI"]


class _C:
    def __init__(self, subject, i):
        self.subject, self.body, self.files = subject, "", []
        self.date, self.pr_number, self.sha = "2026-10-01", None, f"{i:040x}"


def noise_scores():
    commits = [_C(s, i) for i, s in enumerate(NOISE + KEEP)]
    kept = {it.commit.subject for it in themes.fold(commits).items}
    folded_noise = sum(s not in kept for s in NOISE)
    folded_keep = sum(s not in kept for s in KEEP)
    return {"noise_folded": f"{folded_noise}/{len(NOISE)}",
            "real_work_folded_by_mistake": f"{folded_keep}/{len(KEEP)}"}


# ---- redaction ------------------------------------------------------------

def _rand(rng, n, alphabet=string.ascii_letters + string.digits):
    return "".join(rng.choice(alphabet) for _ in range(n))


def secret_cases(seed=7):
    """(kind, text, secret-substring) - every secret built at run time."""
    rng = random.Random(seed)
    up = string.ascii_uppercase + string.digits
    out = []
    for _ in range(10):
        tok = "AKIA" + _rand(rng, 16, up)
        out.append(("aws-key", f"fix: use {tok} for the bucket", tok))
        tok = "ghp_" + _rand(rng, 36)
        out.append(("github-token", f"ci: rotate {tok}", tok))
        tok = "sk_live_" + _rand(rng, 24)
        out.append(("stripe-key", f"test with {tok} in staging", tok))
        tok = "xoxb-" + "-".join(_rand(rng, 11, string.digits)
                                 for _ in range(2)) + "-" + _rand(rng, 24)
        out.append(("slack-token", f"post via {tok}", tok))
        tok = "sk-" + _rand(rng, 40)
        out.append(("api-key", f"switch to {tok}", tok))
        tok = ".".join(["eyJ" + _rand(rng, 20), "eyJ" + _rand(rng, 20),
                        _rand(rng, 30)])
        out.append(("jwt", f"debug token {tok}", tok))
        pw = _rand(rng, 10) + "9!"
        out.append(("assignment", f"set password={pw} in the fixture", pw))
        tok = _rand(rng, 40)
        out.append(("bare-high-entropy", f"new key {tok} added", tok))
        host = rng.choice(["wiki", "jira", "build"]) + ".corp"
        out.append(("internal-url", f"see https://{host}/page?id=5 for notes",
                    host))
        ip = f"10.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"
        out.append(("private-ip", f"point db at {ip}", ip))
        mail = f"{_rand(rng, 6).lower()}@example.com"
        out.append(("email", f"ask {mail} about it", mail))
        q = _rand(rng, 12)
        out.append(("url-query-token", f"docs: https://example.com/x?token={q}",
                    q))
    return out


def clean_cases(seed=11):
    rng = random.Random(seed)
    out = ["Add dark mode (#12)", "Fix UTF-8 handling", "Bump lodash to 4.17.21",
           "see https://github.com/acme/app/pull/12", "fix: token refresh logic",
           "docs: password reset flow", "refactor src/very/long/module/name/"
           "that/is/long_enough_here.py", "auth middleware cleanup",
           "Merge pull request #5 from acme/feature/secret-santa",
           "api key rotation docs", "update CHANGELOG for v1.2.3"]
    for _ in range(30):
        out.append("commit " + "".join(rng.choice("0123456789abcdef")
                                       for _ in range(40)))
    return out


def redaction_scores():
    r = redact.make("standard")
    by_kind, leaked = {}, 0
    cases = secret_cases()
    for kind, text, secret in cases:
        survived = secret in r.text(text)
        by_kind.setdefault(kind, [0, 0])
        by_kind[kind][0] += 1
        by_kind[kind][1] += survived
        leaked += survived
    clean = clean_cases()
    changed = sum(r.text(t) != t for t in clean)
    return {"planted": len(cases), "leaked": leaked,
            "recall": round(1 - leaked / len(cases), 3),
            "leaked_by_kind": {k: v[1] for k, v in by_kind.items() if v[1]},
            "ordinary_strings": len(clean), "changed_by_mistake": changed}


# ---- tags -----------------------------------------------------------------

def tag_repeats(rows):
    """On how many digest lines does a ticket or issue tag repeat a key the
    line text already shows (`add login [ABC-1] [ABC-1]`)? Round 2 always
    appended the tags; round 3 leaves out the ones the text names."""
    before = after = lines = 0
    for r in rows:
        text = themes.clean_subject(r["subject"])
        found = refs.extract(r["subject"])
        if not (found.tickets or found.issues):
            continue
        lines += 1
        # round 2: every ticket/issue tag was appended
        before += any(t in text for t in found.tickets) or \
            any(f"#{n}" in text for n in found.issues)
        shown = refs.tags(found, text)
        after += any(f"[{t}]" in shown for t in found.tickets
                     if t in text)
    return {"lines_with_a_ticket_or_issue": lines,
            "repeat_before": before, "repeat_after": after}


# ---- speed ----------------------------------------------------------------

def speed(rows, repeat=5):
    subjects = [r["subject"] for r in rows]
    best = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        for s in subjects:
            themes.classify_smart(s)
            refs.extract(s)
        dt = time.perf_counter() - t0
        best = dt if best is None or dt < best else best
    return {"subjects": len(subjects),
            "per_second": int(len(subjects) / best)}


def run():
    hold, gen = load_holdout(), generated()
    result = {
        "corpora": {"holdout": len(hold), "generated": len(gen)},
        "grouping_accuracy": {
            "holdout": {"0.2.0 prefix only": round(accuracy(hold, old_group), 3),
                        "0.3.0": round(accuracy(hold, new_group), 3)},
            "generated": {"0.2.0 prefix only": round(accuracy(gen, old_group), 3),
                          "0.3.0": round(accuracy(gen, new_group), 3)},
            "holdout_by_group_0.3.0": per_group(hold, new_group),
        },
        "refs": {
            "holdout": {"0.2.0 merge only": ref_sets(hold, old_refs),
                        "naive regex": ref_sets(hold, naive_refs),
                        "0.3.0": ref_sets(hold, new_refs)},
            "generated": {"0.2.0 merge only": ref_sets(gen, old_refs),
                          "naive regex": ref_sets(gen, naive_refs),
                          "0.3.0": ref_sets(gen, new_refs)},
        },
        "noise_folding": noise_scores(),
        "redaction": redaction_scores(),
        "tag_repeats": {"holdout": tag_repeats(hold),
                        "generated": tag_repeats(gen)},
    }
    return result


if __name__ == "__main__":
    res = run()
    res_speed = speed(generated())
    print(json.dumps(res, indent=2))
    print("speed (this machine, not committed):", res_speed)
    if "--write" in sys.argv:
        with open(os.path.join(HERE, "results.json"), "w") as fh:
            json.dump(res, fh, indent=2, sort_keys=True)
            fh.write("\n")
