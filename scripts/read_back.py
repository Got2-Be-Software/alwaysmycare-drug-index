#!/usr/bin/env python3
"""
read_back.py — fetch the published manifest and index the way AlwaysMyCare does, and compare them with what was built.

Usage: python3 scripts/read_back.py <repository> <built manifest.json> <built index>

Anonymous (no token, no cookies). Every redirect hop must be https to github.com or to a host ending in
.githubusercontent.com — the app's own rule — or the step fails at once: if GitHub moves its asset host outside that,
every install stops updating, and this step is where it shows. A 5xx or a network error is retried (4 tries, 15 s
apart); "latest" can serve the previous release's manifest for a short while after a publish, so a different manifest
is retried for up to about two minutes. Anything else fails at once.
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ATTEMPTS = 8          # for a stale "latest"
TRIES = 4             # for a transient failure
WAIT_SECONDS = 15


class Refused(Exception):
    pass


def allowed(url: str) -> bool:
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and (host == "github.com" or host.endswith(".githubusercontent.com"))


class AppRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed(newurl):
            raise Refused(f"redirect to a host the app refuses: {newurl}")
        print(f"  hop {code} -> {urllib.parse.urlsplit(newurl).hostname}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(AppRedirects())   # no cookie processor: nothing is kept between requests


def fetch(url: str) -> bytes:
    if not allowed(url):
        raise SystemExit(f"::error::{url} is not an address the app would ask")
    for attempt in range(1, TRIES + 1):
        try:
            with OPENER.open(urllib.request.Request(url, headers={"User-Agent": "alwaysmycare-drug-index read-back"}), timeout=120) as r:
                if r.status != 200:
                    raise SystemExit(f"::error::{url} answered {r.status}")
                return r.read()
        except Refused as e:
            raise SystemExit(f"::error::{e}")
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise SystemExit(f"::error::{url} answered {e.code}")
            reason = f"{e.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            reason = str(e)
        if attempt == TRIES:
            raise SystemExit(f"::error::{url} failed {TRIES} times; last: {reason}")
        print(f"  {url}: {reason}; trying again in {WAIT_SECONDS} s")
        time.sleep(WAIT_SECONDS)
    raise SystemExit("unreachable")


def main() -> None:
    repository, built_manifest, built_index = sys.argv[1], sys.argv[2], sys.argv[3]
    want = open(built_manifest, "rb").read()
    address = f"https://github.com/{repository}/releases/latest/download/manifest.json"
    for attempt in range(1, ATTEMPTS + 1):
        got = fetch(address)
        if got == want:
            break
        print(f"  attempt {attempt}: latest still serves another manifest; waiting {WAIT_SECONDS} s")
        time.sleep(WAIT_SECONDS)
    else:
        raise SystemExit(f"::error::after {ATTEMPTS} attempts, {address} does not serve this run's manifest")
    url = json.loads(want)["url"]
    if fetch(url) != open(built_index, "rb").read():
        raise SystemExit(f"::error::{url} does not serve the built index")
    print("Read back as the app reads it: manifest and index match.")


if __name__ == "__main__":
    main()
