#!/usr/bin/env python3
import json, os, sys, urllib.request


PR_NUMBER = 3775
REPO = "nesquena/hermes-webui"


def get_token():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        cfg = os.path.expanduser("~/.git-credentials")
        if os.path.exists(cfg):
            with open(cfg, "r") as f:
                first = f.readline() or ""
            if first.startswith("https://") and "oauth2" in first:
                token = first.split("oauth2")[-1].strip("\n")
    return token


def api(url, token):
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "hermes-cron",
    })
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    token = get_token()
    if not token:
        print(json.dumps({"error": "no_token"}))
        sys.exit(1)
    data = api(f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}", token)
    data2 = api(f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}/comments", token)
    data3 = api(f"https://api.github.com/repos/{REPO}/issues/{PR_NUMBER}/comments", token)
    print(json.dumps({
        "last_updated": data.get("updated_at"),
        "head_sha": data.get("head", {}).get("sha"),
        "review_comments": len(data2),
        "issue_comments": len(data3),
    }))


if __name__ == "__main__":
    main()
