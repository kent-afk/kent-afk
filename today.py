#!/usr/bin/env python3
"""Refresh the numbers inside dark_mode.svg and light_mode.svg.

The layout itself lives in make_svg.py. This script only replaces the text of
elements with known IDs, so it is safe to run from GitHub Actions once a day.

It uses the public REST API:
  * GET /users/{user}                                  -> followers
  * GET /user/repos or /users/{user}/repos             -> repositories, stars
  * GET /repos/{owner}/{repo}/stats/contributors       -> commits, additions, deletions

ACCESS_TOKEN is the personal token (fine-grained, read-only Contents +
Metadata) used for the user's own repositories, including private ones.
CONTRIBUTED_TOKEN is an optional second token used to see repositories owned
by organisations or other collaborators. It can be a classic PAT with the
`repo` scope, or a fine-grained token for that organisation. If ACCESS_TOKEN
is absent the script falls back to GITHUB_TOKEN and then to public data.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, NoReturn

import requests
from dateutil.relativedelta import relativedelta
from lxml import etree

ROOT = Path(__file__).resolve().parent
API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
DEFAULT_USER = "kent-afk"
DEFAULT_BIRTHDAY = "2006-11-26"
CACHE_FILE = ROOT / "cache" / "stats.json"
SVG_NAMES = ("dark_mode.svg", "light_mode.svg")

# Repositories whose real code lives on a non-default branch. The contributor
# stats API only looks at the default branch, so the per-commit fallback needs
# to be pointed at the right branch explicitly.
BRANCH_OVERRIDES = {
    "portfolio-site": "gh-pages",
}

USER_NAME = os.environ.get("USER_NAME", DEFAULT_USER).strip() or DEFAULT_USER
TOKEN = (os.environ.get("ACCESS_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
EXTRA_TOKEN = (os.environ.get("CONTRIBUTED_TOKEN") or "").strip()


def make_headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"{USER_NAME}-profile-readme",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


HEADERS = make_headers(TOKEN)
EXTRA_HEADERS = make_headers(EXTRA_TOKEN) if EXTRA_TOKEN else HEADERS


def fail(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def rate_limit_message(response: requests.Response) -> str:
    reset = response.headers.get("X-RateLimit-Reset")
    if reset and reset.isdigit():
        when = dt.datetime.fromtimestamp(int(reset), dt.timezone.utc)
        return f"GitHub rate limit exhausted; it resets at {when:%Y-%m-%d %H:%M:%S} UTC"
    return "GitHub rate limit exhausted"


def api_get(
    path: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> requests.Response:
    url = path if path.startswith("http") else f"{API}{path}"
    request_headers = headers or HEADERS
    response: requests.Response | None = None
    for attempt in range(4):
        try:
            response = requests.get(url, headers=request_headers, params=params, timeout=timeout)
        except requests.RequestException as exc:
            if attempt == 3:
                fail(f"request to {url} failed: {exc}")
            time.sleep(2 + attempt * 2)
            continue
        if response.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            time.sleep(2 + attempt * 2)
            continue
        break
    assert response is not None
    if response.status_code == 401:
        fail("GitHub rejected a token (401). Check ACCESS_TOKEN and CONTRIBUTED_TOKEN")
    if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
        fail(rate_limit_message(response))
    return response


def api_json(response: requests.Response, what: str) -> Any:
    if not response.ok:
        fail(f"{what} failed: HTTP {response.status_code}: {response.text[:300]}")
    try:
        return response.json()
    except ValueError:
        fail(f"{what} returned invalid JSON")


def get_pages(
    path: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Read every page of a list endpoint (100 items per page)."""
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        query = dict(params or {})
        query.update({"per_page": 100, "page": page})
        response = api_get(path, params=query, headers=headers)
        chunk = api_json(response, path)
        if not isinstance(chunk, list):
            fail(f"{path} did not return a list")
        items.extend(chunk)
        if len(chunk) < 100:
            return items
        page += 1
        if page > 20:
            fail(f"{path} has too many pages")


def token_login(headers: dict[str, str] | None = None) -> str | None:
    """Return the login that owns the token, or None for anonymous access."""
    request_headers = headers or HEADERS
    if "Authorization" not in request_headers:
        return None
    response = api_get("/user", headers=request_headers)
    if response.status_code == 200:
        return response.json().get("login")
    return None


def fetch_personal_repositories(
    username: str, headers: dict[str, str]
) -> list[dict[str, Any]]:
    """Repositories owned by the user, including private ones when authorised."""
    login = token_login(headers)
    if login and login.lower() == username.lower():
        repos = get_pages(
            "/user/repos",
            {"affiliation": "owner", "sort": "pushed", "direction": "desc"},
            headers=headers,
        )
    else:
        repos = get_pages(
            f"/users/{username}/repos",
            {"type": "owner", "sort": "pushed", "direction": "desc"},
            headers=headers,
        )
    return [
        repo
        for repo in repos
        if (repo.get("owner") or {}).get("login", "").lower() == username.lower()
    ]


def fetch_extra_repositories(
    username: str, headers: dict[str, str], known: set[str]
) -> list[dict[str, Any]]:
    """Repositories visible to the second token but not in the personal list."""
    if "Authorization" not in headers:
        return []
    login = token_login(headers)
    if not login:
        return []
    repos = get_pages(
        "/user/repos",
        {
            "affiliation": "owner,collaborator,organization_member",
            "sort": "pushed",
            "direction": "desc",
        },
        headers=headers,
    )
    return [repo for repo in repos if repo.get("full_name") not in known]


def format_uptime(birthday: str | None = None) -> str:
    """How long the user has been alive, e.g. '19 years, 11 months, 6 days'."""
    try:
        born = dt.date.fromisoformat((birthday or DEFAULT_BIRTHDAY)[:10])
    except (TypeError, ValueError):
        born = dt.date.fromisoformat(DEFAULT_BIRTHDAY)
    diff = relativedelta(dt.date.today(), born)
    parts = [
        f"{diff.years} {'year' if diff.years == 1 else 'years'}",
        f"{diff.months} {'month' if diff.months == 1 else 'months'}",
        f"{diff.days} {'day' if diff.days == 1 else 'days'}",
    ]
    return ", ".join(parts)


def contributor_stats(
    full_name: str, headers: dict[str, str] | None = None
) -> dict[str, int] | None:
    """Return this user's commits/additions/deletions in a repository.

    GitHub computes contributor statistics lazily: the first request returns
    202 while the job runs, and a later request returns the result. A 204 means
    the repository is empty.
    """
    for attempt in range(7):
        response = api_get(
            f"/repos/{full_name}/stats/contributors", headers=headers, timeout=45
        )
        if response.status_code == 202:
            time.sleep(1.5 + attempt)
            continue
        if response.status_code == 204:
            return {"commits": 0, "additions": 0, "deletions": 0}
        if response.status_code == 404:
            return None
        data = api_json(response, f"stats for {full_name}")
        mine = [
            contributor
            for contributor in (data or [])
            if (contributor.get("author") or {}).get("login", "").lower() == USER_NAME.lower()
        ]
        return {
            "commits": sum(int(contributor.get("total") or 0) for contributor in mine),
            "additions": sum(
                int(week.get("a") or 0)
                for contributor in mine
                for week in (contributor.get("weeks") or [])
            ),
            "deletions": sum(
                int(week.get("d") or 0)
                for contributor in mine
                for week in (contributor.get("weeks") or [])
            ),
        }
    raise RuntimeError(f"stats for {full_name} are still being computed")


def commit_stats_fallback(
    full_name: str,
    ref: str | None = None,
    max_commits: int = 100,
    headers: dict[str, str] | None = None,
) -> dict[str, int] | None:
    """Sum the per-commit diff stats for repositories where the stats API lags.

    GitHub's /stats/contributors endpoint sometimes returns a commit count with
    zero additions/deletions for a repository that was pushed very recently.
    The commit endpoint has the real numbers, so use it as a one-off fallback.
    """
    params: dict[str, Any] = {"author": USER_NAME, "per_page": max_commits}
    if ref:
        params["sha"] = ref
    response = api_get(f"/repos/{full_name}/commits", params=params, headers=headers)
    commits = api_json(response, f"commits for {full_name}")
    if not isinstance(commits, list) or not commits:
        return None

    additions = 0
    deletions = 0
    for commit in commits[:max_commits]:
        sha = commit.get("sha")
        if not sha:
            continue
        detail = api_get(f"/repos/{full_name}/commits/{sha}", headers=headers)
        if detail.status_code == 404:
            continue
        data = api_json(detail, f"commit {sha} in {full_name}")
        stats = data.get("stats") or {}
        additions += int(stats.get("additions") or 0)
        deletions += int(stats.get("deletions") or 0)
    return {"commits": len(commits[:max_commits]), "additions": additions, "deletions": deletions}


def load_cache() -> dict[str, Any]:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {"user": USER_NAME, "repos": {}}
    if not isinstance(data, dict) or not isinstance(data.get("repos"), dict):
        return {"user": USER_NAME, "repos": {}}
    return data


def save_cache(cache: dict[str, Any]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    cache["user"] = USER_NAME
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(CACHE_FILE)


def collect_stats(
    repos: list[dict[str, Any]],
    cache: dict[str, Any],
    force: bool,
    repo_headers: dict[str, dict[str, str]] | None = None,
) -> dict[str, dict[str, Any]]:
    cached_repos = cache.get("repos", {})
    current: dict[str, dict[str, Any]] = {}

    for index, repo in enumerate(repos, 1):
        full_name = repo["full_name"]
        pushed_at = repo.get("pushed_at") or ""
        headers = (repo_headers or {}).get(full_name, HEADERS)
        cached = cached_repos.get(full_name)
        if (
            not force
            and isinstance(cached, dict)
            and cached.get("pushed_at") == pushed_at
            and {"commits", "additions", "deletions"} <= cached.keys()
        ):
            current[full_name] = {
                "pushed_at": pushed_at,
                "commits": int(cached["commits"]),
                "additions": int(cached["additions"]),
                "deletions": int(cached["deletions"]),
            }
            print(f"  [{index:>2}/{len(repos)}] cache  {full_name}")
            continue

        if not repo.get("size") or not repo.get("default_branch"):
            current[full_name] = {
                "pushed_at": pushed_at,
                "commits": 0,
                "additions": 0,
                "deletions": 0,
            }
            print(f"  [{index:>2}/{len(repos)}] empty  {full_name}")
        else:
            try:
                result = contributor_stats(full_name, headers=headers)
            except RuntimeError as exc:
                print(f"  [{index:>2}/{len(repos)}] pending {full_name}: {exc}")
                continue
            if result is None:
                if cached and {"commits", "additions", "deletions"} <= cached.keys():
                    result = {
                        "commits": int(cached["commits"]),
                        "additions": int(cached["additions"]),
                        "deletions": int(cached["deletions"]),
                    }
                    print(f"  [{index:>2}/{len(repos)}] kept   {full_name} (HTTP 404, cached values)")
                else:
                    result = {"commits": 0, "additions": 0, "deletions": 0}
                    print(f"  [{index:>2}/{len(repos)}] skip   {full_name} (HTTP 404)")
            else:
                print(
                    f"  [{index:>2}/{len(repos)}] fetch  {full_name}: "
                    f"{result['commits']} commits, +{result['additions']:,} / -{result['deletions']:,}"
                )
                if (
                    result["commits"] > 0
                    and result["additions"] + result["deletions"] == 0
                    and int(repo.get("size") or 0) > 0
                ):
                    ref = BRANCH_OVERRIDES.get(full_name.split("/", 1)[-1])
                    detailed = commit_stats_fallback(
                        full_name, ref=ref, headers=headers
                    )
                    if detailed is not None:
                        branch = f" [{ref}]" if ref else ""
                        print(
                            f"  [{index:>2}/{len(repos)}] detail {full_name}{branch}: "
                            f"{detailed['commits']} commits, "
                            f"+{detailed['additions']:,} / -{detailed['deletions']:,}"
                        )
                        result = detailed
            current[full_name] = {"pushed_at": pushed_at, **result}

        # Save after every repository: a rate limit in the middle of a run
        # should not throw away the repositories that already succeeded.
        cache["repos"] = current
        save_cache(cache)

    cache["repos"] = current
    return current


def write_svgs(values: dict[str, str], svg_dir: Path, dry_run: bool) -> None:
    for name in SVG_NAMES:
        path = svg_dir / name
        if not path.exists():
            fail(f"missing {path}")
        tree = etree.parse(str(path))
        root = tree.getroot()
        for element_id, text in values.items():
            nodes = root.xpath(f".//*[@id='{element_id}']")
            if not nodes:
                fail(f"{path.name}: no element with id={element_id!r}")
            for node in nodes:
                node.text = text
        if dry_run:
            print(f"  dry-run: would update {path.name}")
        else:
            tree.write(str(path), encoding="utf-8", xml_declaration=True)
            print(f"  wrote {path.name}")


def fixture_values(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    add = int(data.get("loc_add", 0))
    delete = int(data.get("loc_del", 0))
    total = int(data.get("loc_total", add - delete))
    return {
        "uptime": str(data.get("uptime") or format_uptime(data.get("birthday"))),
        "repos": f"{int(data.get('repos', 0)):,}",
        "contributed": f"{int(data.get('contributed', 0)):,}",
        "stars": f"{int(data.get('stars', 0)):,}",
        "commits": f"{int(data.get('commits', 0)):,}",
        "followers": f"{int(data.get('followers', 0)):,}",
        "loc_total": f"{total:,}",
        "loc_add": f"+{add:,}",
        "loc_del": f"-{delete:,}",
    }


def main() -> None:
    global USER_NAME
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default=USER_NAME, help="GitHub login (default: %(default)s)")
    parser.add_argument("--fixture", type=Path, help="read stats from JSON instead of the API")
    parser.add_argument("--svg-dir", type=Path, default=ROOT, help="directory containing the SVGs")
    parser.add_argument("--force", action="store_true", help="ignore the local cache")
    parser.add_argument("--dry-run", action="store_true", help="parse and validate but do not write")
    args = parser.parse_args()

    USER_NAME = args.user.strip() or USER_NAME
    HEADERS["User-Agent"] = f"{USER_NAME}-profile-readme"
    EXTRA_HEADERS["User-Agent"] = f"{USER_NAME}-profile-readme"

    if args.fixture:
        values = fixture_values(args.fixture)
        print("Using fixture values:")
        for key, value in values.items():
            print(f"  {key:<12} {value}")
        write_svgs(values, args.svg_dir, args.dry_run)
        return

    user = api_json(api_get(f"/users/{USER_NAME}", headers=HEADERS), "user")
    personal = fetch_personal_repositories(USER_NAME, HEADERS)
    personal_names = {repo["full_name"] for repo in personal}
    extra_headers = EXTRA_HEADERS if EXTRA_TOKEN else HEADERS
    extra = fetch_extra_repositories(USER_NAME, extra_headers, personal_names)
    repos = personal + extra
    repo_headers = {repo["full_name"]: HEADERS for repo in personal}
    repo_headers.update({repo["full_name"]: extra_headers for repo in extra})
    owned = personal

    cache = load_cache()
    print(
        f"Repository stats for {USER_NAME} "
        f"({len(repos)} accessible, {len(owned)} owned):"
    )
    repo_stats = collect_stats(repos, cache, args.force, repo_headers)
    save_cache(cache)

    commits = sum(item["commits"] for item in repo_stats.values())
    additions = sum(item["additions"] for item in repo_stats.values())
    deletions = sum(item["deletions"] for item in repo_stats.values())
    stars = sum(int(repo.get("stargazers_count") or 0) for repo in owned)
    followers = int((user.get("followers") or 0))
    contributed = len(repos)

    values = {
        "uptime": format_uptime(),
        "repos": f"{len(owned):,}",
        "contributed": f"{contributed:,}",
        "stars": f"{stars:,}",
        "commits": f"{commits:,}",
        "followers": f"{followers:,}",
        "loc_total": f"{additions - deletions:,}",
        "loc_add": f"+{additions:,}",
        "loc_del": f"-{deletions:,}",
    }

    print("\nFinal values:")
    for key, value in values.items():
        print(f"  {key:<12} {value}")
    write_svgs(values, args.svg_dir, args.dry_run)


if __name__ == "__main__":
    main()
