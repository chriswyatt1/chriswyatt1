#!/usr/bin/env python3
"""Render the "languages I write" chart on the profile README.

Finds every repo you've committed to (from your contribution graph, so org
repos like nf-core and Eco-Flow count too), walks the commits you authored,
and works out each commit's languages from the file extensions it touched.
Every commit counts once, split between its languages by lines changed, so a
scaffold or vendored dump weighs no more than any other commit. It then draws
light and dark SVGs comparing all time with the last 3 months. If the last 3
months are too quiet, the recent window widens (6 months, 1 year, ...) so the
chart is never empty.

Commit details are cached in stats/commits.json, so scheduled runs only fetch
commits they haven't seen. Standard library only.

Environment:
  GITHUB_TOKEN        token for the GitHub API (required)
  STATS_LOGIN         GitHub login to report on (default: repo owner)
  INCLUDE_PRIVATE     "true" to include private repos (default: public only)
  FULL_REFRESH        "true" to re-list every repo from the start
"""

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "stats" / "commits.json"
OUT = {"light": ROOT / "stats" / "languages-light.svg",
       "dark": ROOT / "stats" / "languages-dark.svg"}

TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
LOGIN = os.environ.get("STATS_LOGIN") or os.environ.get("GITHUB_REPOSITORY_OWNER") or "chriswyatt1"
INCLUDE_PRIVATE = os.environ.get("INCLUDE_PRIVATE", "").lower() == "true"
FULL_REFRESH = os.environ.get("FULL_REFRESH", "").lower() == "true"

# Recent window: 3 months, widened step by step until it holds enough commits.
RECENT_WINDOWS = [(90, "Last 3 months"), (182, "Last 6 months"), (365, "Last year"),
                  (730, "Last 2 years"), (None, "All time")]
MIN_RECENT_COMMITS = 5
# Within a commit, one file counts for at most this many lines, so a
# vendored file doesn't drown out the code it was committed alongside.
FILE_LINE_CAP = 1000
TOP_LANGUAGES = 7
# Re-list this far behind the newest cached commit, to catch late-landing merges.
RELIST_OVERLAP = timedelta(days=90)

# Programming and markup languages only: docs, config data (YAML/JSON/CSV),
# test snapshots and sequence files are ignored, as in GitHub's language bar.
SUFFIX_LANG = {
    ".nf.test": "Nextflow", ".nf": "Nextflow", ".config": "Nextflow",
    ".groovy": "Groovy", ".py": "Python", ".pyx": "Python",
    ".ipynb": "Jupyter Notebook", ".r": "R", ".rmd": "R",
    ".pl": "Perl", ".pm": "Perl", ".sh": "Shell", ".bash": "Shell",
    ".zsh": "Shell", ".ksh": "Shell", ".tex": "TeX", ".cls": "TeX",
    ".sty": "TeX", ".html": "HTML", ".htm": "HTML", ".css": "CSS",
    ".scss": "CSS", ".sass": "CSS", ".less": "CSS", ".js": "JavaScript",
    ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".astro": "Astro",
    ".svelte": "Svelte", ".vue": "Vue", ".c": "C", ".h": "C", ".cpp": "C++",
    ".cc": "C++", ".cxx": "C++", ".hpp": "C++", ".java": "Java",
    ".kt": "Kotlin", ".scala": "Scala", ".rs": "Rust", ".go": "Go",
    ".jl": "Julia", ".rb": "Ruby", ".php": "PHP", ".awk": "Awk",
    ".smk": "Snakemake", ".wdl": "WDL", ".cwl": "CWL", ".sql": "SQL",
    ".lua": "Lua", ".mk": "Makefile", ".dockerfile": "Dockerfile",
}
FILENAME_LANG = {"dockerfile": "Dockerfile", "makefile": "Makefile",
                 "snakefile": "Snakemake", "nextflow.config": "Nextflow"}
SKIP_DIRS = {"node_modules", "vendor", "_site", "dist", "site-packages", ".venv"}


def language_of(path):
    parts = path.lower().split("/")
    name = parts[-1]
    if SKIP_DIRS.intersection(parts[:-1]) or ".min." in name:
        return None
    if name in FILENAME_LANG:
        return FILENAME_LANG[name]
    if name.startswith("dockerfile"):
        return "Dockerfile"
    for suffix, lang in SUFFIX_LANG.items():
        if name.endswith(suffix):
            return lang
    return None


# --------------------------------------------------------------------- API

def request(url, body=None):
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "language-stats"}
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(6):
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as err:
            if err.code in (404, 409, 451):  # gone, empty or blocked repo
                return None
            limited = err.code == 429 or (err.code == 403 and (
                err.headers.get("Retry-After") or err.headers.get("X-RateLimit-Remaining") == "0"))
            if not (limited or err.code >= 500) or attempt == 5:
                raise
            wait = int(err.headers.get("Retry-After") or 0)
            if not wait and err.headers.get("X-RateLimit-Remaining") == "0":
                wait = int(err.headers.get("X-RateLimit-Reset", time.time() + 60)) - int(time.time())
            time.sleep(min(max(wait, 2 ** attempt), 900))
        except (urllib.error.URLError, http.client.HTTPException, OSError):
            if attempt == 5:
                raise
            time.sleep(2 ** attempt)


def graphql(query, **variables):
    result = request("https://api.github.com/graphql", {"query": query, "variables": variables})
    if result.get("errors"):
        raise RuntimeError(result["errors"])
    return result["data"]


def contributed_repos(since_year):
    """Map repo -> first year you committed to it (default branches only)."""
    created = graphql("query($l:String!){user(login:$l){createdAt}}", l=LOGIN)["user"]["createdAt"]
    first_year = {}
    for year in range(max(int(created[:4]), since_year), datetime.now(timezone.utc).year + 1):
        data = graphql("""
            query($l:String!, $from:DateTime!, $to:DateTime!) {
              user(login:$l) { contributionsCollection(from:$from, to:$to) {
                commitContributionsByRepository(maxRepositories:100) {
                  repository { nameWithOwner isPrivate } } } } }""",
            l=LOGIN, **{"from": f"{year}-01-01T00:00:00Z", "to": f"{year}-12-31T23:59:59Z"})
        for item in data["user"]["contributionsCollection"]["commitContributionsByRepository"]:
            repo = item["repository"]
            if INCLUDE_PRIVATE or not repo["isPrivate"]:
                first_year.setdefault(repo["nameWithOwner"], year)
    return first_year


def list_commits(repo, since):
    commits, page = [], 1
    while True:
        batch = request(f"https://api.github.com/repos/{repo}/commits?author={LOGIN}"
                        f"&since={since}&per_page=100&page={page}")
        if not batch:
            return commits
        commits += [(c["sha"], c["commit"]["committer"]["date"]) for c in batch
                    if len(c["parents"]) <= 1]  # merges would re-count others' work
        if len(batch) < 100:
            return commits
        page += 1


def commit_languages(repo, sha):
    try:
        detail = request(f"https://api.github.com/repos/{repo}/commits/{sha}")
    except Exception as err:  # leave it uncached; the next run retries it
        print(f"  skipped {repo}@{sha[:7]}: {err}", file=sys.stderr)
        return None
    langs = defaultdict(int)
    for f in (detail or {}).get("files", []):
        lang = language_of(f["filename"])
        if lang:
            langs[lang] += min(f.get("changes", 0), FILE_LINE_CAP)
    return dict(langs)


# ------------------------------------------------------------------ totals

def update_cache():
    cache = json.loads(CACHE.read_text()) if CACHE.exists() and not FULL_REFRESH else {}
    newest = max((c["date"] for c in cache.values()), default=None)
    relist_from = (datetime.fromisoformat(newest.replace("Z", "+00:00")) - RELIST_OVERLAP
                   if newest else None)

    repos = contributed_repos(relist_from.year if relist_from else 1970)
    todo = {}
    for repo, year in sorted(repos.items()):
        since = relist_from.strftime("%Y-%m-%dT%H:%M:%SZ") if relist_from else f"{year}-01-01T00:00:00Z"
        for sha, date in list_commits(repo, since):
            if sha not in cache and sha not in todo:
                todo[sha] = (repo, date)
    print(f"{len(repos)} repos, {len(todo)} new commits to fetch", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = pool.map(lambda item: (item[0], item[1][1], commit_languages(item[1][0], item[0])),
                           todo.items())
        for i, (sha, date, langs) in enumerate(results, 1):
            if langs is not None:
                cache[sha] = {"date": date, "langs": langs}
            if i % 250 == 0:
                print(f"  {i}/{len(todo)}", file=sys.stderr)
                save_cache(cache)
    save_cache(cache)
    return cache


def save_cache(cache):
    CACHE.write_text(json.dumps(dict(sorted(cache.items(), key=lambda kv: kv[1]["date"])),
                                separators=(",", ":")).replace('},"', '},\n"') + "\n")


def summarise(commits):
    totals = defaultdict(float)
    for c in commits:
        lines = sum(c["langs"].values()) or 1
        for lang, n in c["langs"].items():
            totals[lang] += n / lines
    return {"shares": {lang: n / len(commits) for lang, n in totals.items()},
            "commits": len(commits)}


def build_periods(cache, now):
    commits = [dict(c, when=datetime.fromisoformat(c["date"].replace("Z", "+00:00")))
               for c in cache.values() if c["langs"]]
    if not commits:
        sys.exit("No commits with recognised code found.")
    first = min(c["when"] for c in commits)
    latest = max(c["when"] for c in commits)
    all_time = dict(summarise(commits), label="All time", note=f"since {first.year}")

    for days, label in RECENT_WINDOWS:
        window = [c for c in commits if days is None or c["when"] >= now - timedelta(days=days)]
        if len(window) >= MIN_RECENT_COMMITS or days is None:
            break
    note = ""
    if days != 90:  # say why the window is wider than 3 months
        note = f"quiet lately · last push {latest.day} {latest:%b %Y}"
    recent = dict(summarise(window), label=label, note=note)
    return all_time, recent


# -------------------------------------------------------------------- draw

# Categorical slots validated for colour-vision deficiency on each GitHub
# surface (adjacent pairs, as in a stacked bar); "Other" is a neutral grey.
THEMES = {
    "light": {"surface": "#ffffff", "border": "#d0d7de", "ink": "#1f2328", "muted": "#59636e",
              "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7"],
              "other": "#a3a29b"},
    "dark": {"surface": "#0d1117", "border": "#30363d", "ink": "#f0f6fc", "muted": "#9198a1",
             "series": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9"],
             "other": "#6b6a64"},
}
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"
W, PAD = 800, 24
BAR_W, BAR_H = W - 2 * PAD, 14


def pct(share):
    if share <= 0:
        return "–"
    if share < 0.001:
        return "<0.1%"
    return f"{share * 100:.1f}%" if share < 0.1 else f"{share * 100:.0f}%"


def pick_languages(periods):
    """Top languages across both periods, in all-time order so colours stay put."""
    everything = set().union(*(p["shares"] for p in periods))
    by_peak = sorted(everything, key=lambda l: -max(p["shares"].get(l, 0) for p in periods))
    keep = set(by_peak[:TOP_LANGUAGES])
    return sorted(keep, key=lambda l: (-periods[0]["shares"].get(l, 0), l))


def render(theme, periods, languages, updated):
    t = THEMES[theme]
    colour = dict(zip(languages, t["series"]))
    keys = languages + ["Other"]

    def share(period, key):
        if key == "Other":
            return max(0.0, 1 - sum(period["shares"].get(l, 0) for l in languages))
        return period["shares"].get(key, 0)

    rows = [(k, [share(p, k) for p in periods]) for k in keys]
    rows = [(k, s) for k, s in rows if k != "Other" or max(s) >= 0.0005]

    out, y = [], PAD
    title = "Languages I write"
    subtitle = f"Share of my commits by language, across public repos · updated {updated}"
    out.append(f'<text x="{PAD}" y="{y + 16}" class="t">{title}</text>')
    out.append(f'<text x="{PAD}" y="{y + 36}" class="s">{escape(subtitle)}</text>')
    y += 60

    for i, p in enumerate(periods):
        meta = f"{p['commits']:,} commits" + (f" · {p['note']}" if p["note"] else "")
        out.append(f'<text x="{PAD}" y="{y + 13}" class="h">{p["label"]}</text>')
        out.append(f'<text x="{W - PAD}" y="{y + 13}" class="s" text-anchor="end">{escape(meta)}</text>')
        y += 22
        out.append(f'<clipPath id="bar{i}"><rect x="{PAD}" y="{y}" width="{BAR_W}" '
                   f'height="{BAR_H}" rx="4"/></clipPath><g clip-path="url(#bar{i})">')
        segments = [(k, s[i]) for k, s in rows if s[i] * BAR_W >= 1]
        usable = BAR_W - 2 * (len(segments) - 1)
        total = sum(v for _, v in segments) or 1
        x = PAD
        for k, v in segments:
            w = v / total * usable
            fill = t["other"] if k == "Other" else colour[k]
            out.append(f'<rect x="{x:.2f}" y="{y}" width="{w:.2f}" height="{BAR_H}" fill="{fill}"/>')
            x += w + 2
        out.append("</g>")
        y += BAR_H + 22

    # Legend: two columns, each with a value per period.
    y += 4
    col_w = (W - 2 * PAD - 32) / 2
    num_x = [col_w - 70, col_w]
    heads = ["All time", "Recent" if periods[1]["label"] == "All time" else periods[1]["label"].replace("Last ", "")]
    for col in range(2):
        cx = PAD + col * (col_w + 32)
        for j, head in enumerate(heads):
            out.append(f'<text x="{cx + num_x[j]:.1f}" y="{y + 10}" class="c" text-anchor="end">{escape(head)}</text>')
    y += 20
    per_col = (len(rows) + 1) // 2
    for n, (k, s) in enumerate(rows):
        col, r = divmod(n, per_col)
        cx, cy = PAD + col * (col_w + 32), y + r * 26
        fill = t["other"] if k == "Other" else colour[k]
        out.append(f'<line x1="{cx}" y1="{cy + 22}" x2="{cx + col_w:.1f}" y2="{cy + 22}" class="g"/>')
        out.append(f'<rect x="{cx}" y="{cy + 4}" width="10" height="10" rx="2" fill="{fill}"/>')
        out.append(f'<text x="{cx + 18}" y="{cy + 14}" class="l">{escape(k)}</text>')
        for j in range(2):
            out.append(f'<text x="{cx + num_x[j]:.1f}" y="{cy + 14}" class="v" text-anchor="end">{pct(s[j])}</text>')
    y += per_col * 26 + PAD - 4

    desc = "; ".join(f"{p['label']}: " + ", ".join(f"{k} {pct(s[i])}" for k, s in rows)
                     for i, p in enumerate(periods))
    style = (f"text{{font-family:{FONT}}}"
             f".t{{font-size:16px;font-weight:600;fill:{t['ink']}}}"
             f".h{{font-size:13px;font-weight:600;fill:{t['ink']}}}"
             f".s{{font-size:12px;fill:{t['muted']}}}"
             f".c{{font-size:11px;fill:{t['muted']}}}"
             f".l{{font-size:13px;fill:{t['ink']}}}"
             f".v{{font-size:13px;fill:{t['muted']};font-variant-numeric:tabular-nums}}"
             f".g{{stroke:{t['border']};stroke-width:1;opacity:.6}}")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{y}" viewBox="0 0 {W} {y}" '
            f'role="img" aria-labelledby="title desc">\n'
            f'<title id="title">{title}</title>\n<desc id="desc">{escape(desc)}</desc>\n'
            f'<style>{style}</style>\n'
            f'<rect x="0.5" y="0.5" width="{W - 1}" height="{y - 1}" rx="6" fill="{t["surface"]}" '
            f'stroke="{t["border"]}"/>\n' + "\n".join(out) + "\n</svg>\n")


def main():
    if not TOKEN:
        sys.exit("Set GITHUB_TOKEN.")
    now = datetime.now(timezone.utc)
    cache = update_cache()
    periods = build_periods(cache, now)
    languages = pick_languages(periods)
    for theme, path in OUT.items():
        path.write_text(render(theme, periods, languages, f"{now.day} {now:%b %Y}"))
    for p in periods:
        top = sorted(p["shares"].items(), key=lambda kv: -kv[1])[:5]
        print(f"{p['label']} ({p['commits']} commits): "
              + ", ".join(f"{k} {pct(v)}" for k, v in top), file=sys.stderr)


if __name__ == "__main__":
    main()
