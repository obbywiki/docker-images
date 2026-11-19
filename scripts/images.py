#!/usr/bin/env python3
"""Rebuild base images from the upstream Dockerfiles as-is (docker-library/php + nginx/docker-nginx).

  check: compare upstream with versions.json and emit the build plan
  build: build and push one image for one platform, by digest
  merge: join the per-platform digests into one tagged multi-arch image
  record: write what was built to versions.json
  pins: print the pinned references to paste into FROM / image: lines
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "images.json").read_text())
VERSIONS = ROOT / "versions.json"
WORK = ROOT / ".work"

FROM_RE = re.compile(r"^FROM\s+(?:--\S+\s+)*(\S+)", re.M | re.I)


# HELPERS


def run(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(args, cwd=cwd, text=True, capture_output=True)

    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit(f"command failed ({proc.returncode}): {' '.join(args)}")

    return proc.stdout.strip()


def stream(*args: str) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, check=True)


def gh_output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")

    if path:
        with open(path, "a") as f:
            f.write(f"{key}={value}\n")
    else:
        print(f"{key}={value}")


def summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")

    if path:
        with open(path, "a") as f:
            f.write(text + "\n")
    else:
        print(text)


def registry() -> str:
    if reg := os.environ.get("REGISTRY"):
        return reg.rstrip("/")

    owner = os.environ.get("GITHUB_REPOSITORY_OWNER")

    if not owner:
        raise SystemExit("set REGISTRY (e.g. ghcr.io/obbywiki) or GITHUB_REPOSITORY_OWNER")

    return f"ghcr.io/{owner.lower()}"


def load_plan() -> dict:
    plan = os.environ.get("PLAN")

    if not plan:
        raise SystemExit("PLAN is not set (it comes from the check job's output)")

    return json.loads(plan)


def vtuple(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", version)[:3])


def manifest_digest(ref: str) -> str:
    manifest = run("docker", "buildx", "imagetools", "inspect", ref, "--format", "{{json .Manifest}}")
    return json.loads(manifest)["digest"]


# UPSTREAM


def fetch_upstream(name: str, cfg: dict, commit: str | None = None) -> tuple[Path, str]:
    """Shallow-clone the upstream repo at its branch head, or at `commit`."""

    dest = WORK / "upstream" / name
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    run("git", "clone", "--quiet", "--depth=1", "--branch", cfg["branch"], cfg["repo"], str(dest))

    if commit:
        run("git", "fetch", "--quiet", "--depth=1", "origin", commit, cwd=dest)
        run("git", "checkout", "--quiet", "--detach", commit, cwd=dest)

    return dest, run("git", "rev-parse", "HEAD", cwd=dest)


def from_ref(dockerfile: Path) -> str:
    refs = FROM_RE.findall(dockerfile.read_text())

    if len(refs) != 1:
        raise SystemExit(f"{dockerfile}: expected exactly one FROM, found {refs}")

    ref = refs[0]

    # BuildKit matches named contexts against the familiar name, minus :latest
    return ref.removesuffix(":latest")


def env_value(text: str, key: str) -> str | None:
    m = re.search(rf'^ENV\s+{re.escape(key)}[\s=]+"?([^"\s]+)', text, re.M)
    return m.group(1) if m else None


def describe(cfg: dict, dest: Path, commit: str) -> dict:
    dockerfiles = [dest / path / "Dockerfile" for path in cfg["chain"]]
    version = next( (v for f in dockerfiles if (v := env_value(f.read_text(), cfg["version_env"]))), None )
    if not version:
        raise SystemExit(f"{cfg['repo']}: no ENV {cfg['version_env']} in {cfg['chain']}")

    base = from_ref(dockerfiles[0])
    if "@" not in base:
        base = f"{base}@{manifest_digest(base)}"

    minor = ".".join(version.split(".")[:2])
    return {
        "version": version,
        "upstream": cfg["repo"],
        "commit": commit,
        "trees": {path: run("git", "rev-parse", f"HEAD:{path}", cwd=dest) for path in cfg["chain"]},
        "base": base,
        "tags": [t.format(version=version, minor=minor) for t in cfg["tags"]],
        "config": hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:12],
    }


def check_feed(cfg: dict, version: str) -> str:
    """Warn when the project has released something upstream hasn't picked up."""
    feed = cfg.get("release_feed")
    if not feed:
        return "–"
    try:
        req = urllib.request.Request(feed["url"], headers={"User-Agent": "docker-images-check"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            latest = json.load(resp)[feed["key"]]
    except Exception as exc:  # the feed is advisory, never fail the run on it
        print(f"::warning::could not read {feed['url']}: {exc}")
        return "feed unreachable"
    if vtuple(latest) > vtuple(version):
        print(f"::warning::{latest} is released but the upstream Dockerfile is still at {version}")
        return f"**{latest} released, upstream Dockerfile not updated yet**"
    return f"current ({latest})"


def labels(cfg: dict, info: dict) -> dict[str, str]:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    return {
        "org.opencontainers.image.source": f"https://github.com/{repo}" if repo else "",
        "org.opencontainers.image.revision": os.environ.get("GITHUB_SHA", ""),
        "org.opencontainers.image.version": info["version"],
        "org.opencontainers.image.description": (
            f"{cfg['name']} {info['version']} built from "
            f"{info['upstream'].removesuffix('.git')} at {info['commit'][:12]}"
        ),
        "org.opencontainers.image.base.name": info["base"].split("@")[0],
        "org.opencontainers.image.base.digest": info["base"].split("@")[1],
    }


# commands


def cmd_check(args: argparse.Namespace) -> None:
    recorded = json.loads(VERSIONS.read_text())
    plan: dict[str, dict] = {}
    matrix: list[dict] = []
    rows = ["| image | upstream | built | release feed | action |", "|---|---|---|---|---|"]

    for name, cfg in CONFIG["images"].items():
        dest, commit = fetch_upstream(name, cfg)
        info = describe(cfg, dest, commit)
        prev = recorded.get(name)

        reasons = []
        if args.force:
            reasons.append("forced")
        if not prev:
            reasons.append("never built")
        else:
            if prev.get("trees") != info["trees"]:
                reasons.append("upstream Dockerfile changed")
            if prev.get("base") != info["base"]:
                reasons.append("base image updated")
            if prev.get("config") != info["config"]:
                reasons.append("images.json changed")

        if reasons:
            plan[name] = info
            for platform in cfg["platforms"]:
                matrix.append({
                    "image": name,
                    "platform": platform,
                    "runner": CONFIG["runners"][platform],
                    "slug": f"{name}-{platform.replace('/', '-')}",
                })

        built = prev["version"] if prev else "–"
        action = "build: " + ", ".join(reasons) if reasons else "up to date"
        rows.append(f"| {name} | {info['version']} | {built} | {check_feed(cfg, info['version'])} | {action} |")

    summary("\n".join(rows))
    gh_output("plan", json.dumps(plan, separators=(",", ":")))
    gh_output("matrix", json.dumps(matrix, separators=(",", ":")))
    gh_output("images", json.dumps(sorted(plan), separators=(",", ":")))


def cmd_build(args: argparse.Namespace) -> None:
    info = load_plan()[args.image]
    cfg = CONFIG["images"][args.image]
    dest, _ = fetch_upstream(args.image, cfg, info["commit"])

    # one bake target per Dockerfile in the chain. the first one's FROM is pinned to the digest `check` saw; each later one's FROM (e.g. nginx:<v>-alpine-slim) is pointed at the previous target instead of being pulled from Docker Hub
    targets: dict[str, dict] = {}
    for i, path in enumerate(cfg["chain"]):
        context = dest / path
        source = f"docker-image://{info['base']}" if i == 0 else f"target:stage{i - 1}"
        targets[f"stage{i}"] = {
            "context": str(context),
            "dockerfile": "Dockerfile",
            "platforms": [args.platform],
            "contexts": {from_ref(context / "Dockerfile"): source},
        }

    final = f"stage{len(cfg['chain']) - 1}"
    repo = f"{registry()}/{cfg['name']}"
    targets[final]["labels"] = labels(cfg, info)
    targets[final]["output"] = [f"type=image,name={repo},push-by-digest=true,name-canonical=true,push=true"]

    WORK.mkdir(exist_ok=True)
    bake_file = WORK / f"bake-{args.image}.json"
    meta_file = WORK / f"meta-{args.image}.json"
    bake_file.write_text(json.dumps({"target": targets}, indent=2))
    stream("docker", "buildx", "bake", "--file", str(bake_file), "--metadata-file", str(meta_file), final)

    digest = json.loads(meta_file.read_text())[final]["containerimage.digest"]
    out = WORK / "digests"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{args.image}-{args.platform.replace('/', '-')}").write_text(digest)
    summary(f"built `{repo}@{digest}` for {args.platform}")


def cmd_merge(args: argparse.Namespace) -> None:
    info = load_plan()[args.image]
    cfg = CONFIG["images"][args.image]
    repo = f"{registry()}/{cfg['name']}"

    files = sorted(Path(args.digests).glob(f"{args.image}-*"))
    if len(files) != len(cfg["platforms"]):
        raise SystemExit(f"expected {len(cfg['platforms'])} platform digests, got {[f.name for f in files]}")

    cmd = ["docker", "buildx", "imagetools", "create"]
    for key, value in labels(cfg, info).items():
        cmd += ["--annotation", f"index:{key}={value}"]
    for tag in info["tags"]:
        cmd += ["--tag", f"{repo}:{tag}"]
    cmd += [f"{repo}@{f.read_text().strip()}" for f in files]
    stream(*cmd)

    digest = manifest_digest(f"{repo}:{info['tags'][0]}")
    out = WORK / "results"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{args.image}.json").write_text(json.dumps({
        "image": repo,
        "digest": digest,
        "built_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }))
    summary(f"pushed `{repo}:{info['tags'][0]}@{digest}`")


def cmd_record(args: argparse.Namespace) -> None:
    plan = load_plan()
    recorded = json.loads(VERSIONS.read_text())
    done = []

    for name, info in plan.items():
        result = Path(args.results) / f"{name}.json"
        if not result.exists():
            print(f"::warning::{name} was planned but has no result; leaving versions.json as it was")
            continue
        recorded[name] = {**info, **json.loads(result.read_text())}
        done.append(f"{name} {info['version']}")

    VERSIONS.write_text(json.dumps(recorded, indent=2, sort_keys=True) + "\n")
    WORK.mkdir(exist_ok=True)
    (WORK / "commit-msg.txt").write_text(f"build: {', '.join(done) or 'nothing'}\n")


def cmd_pins(_: argparse.Namespace) -> None:
    for name, entry in json.loads(VERSIONS.read_text()).items():
        print(f"{name:8} {entry['image']}:{entry['tags'][0]}@{entry['digest']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check")
    p.add_argument("--force", action="store_true", help="rebuild every image")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("build")
    p.add_argument("--image", required=True)
    p.add_argument("--platform", required=True)
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("merge")
    p.add_argument("--image", required=True)
    p.add_argument("--digests", default=str(WORK / "digests"))
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("record")
    p.add_argument("--results", default=str(WORK / "results"))
    p.set_defaults(func=cmd_record)

    sub.add_parser("pins").set_defaults(func=cmd_pins)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
