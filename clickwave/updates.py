"""Update check against the project's GitHub releases.

Only asks GitHub for the latest release's version number - nothing about
the user or their setup is sent.
"""

import json
import re
import urllib.error
import urllib.request

from .common import APP_NAME, APP_VERSION, API_LATEST_URL, RELEASES_URL


class UpdateInfo:
    def __init__(self, latest, url, newer):
        self.latest, self.url, self.newer = latest, url, newer


def _parse(version):
    nums = re.findall(r"\d+", version or "")
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(remote, local=APP_VERSION):
    a, b = _parse(remote), _parse(local)
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def check(timeout=6):
    """Returns UpdateInfo. Raises OSError with a readable message offline."""
    req = urllib.request.Request(API_LATEST_URL, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{APP_NAME}/{APP_VERSION}",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise OSError("no releases have been published yet")
        if e.code == 403:
            raise OSError("GitHub is rate-limiting update checks - try again in a few minutes")
        raise OSError(f"GitHub answered with an error ({e.code})")
    except (urllib.error.URLError, TimeoutError) as e:
        raise OSError("couldn't reach GitHub - check your internet connection") from e
    except ValueError as e:
        raise OSError("GitHub sent an unexpected answer") from e
    latest = (data.get("tag_name") or data.get("name") or "").lstrip("vV")
    url = data.get("html_url") or RELEASES_URL
    return UpdateInfo(latest, url, is_newer(latest))
