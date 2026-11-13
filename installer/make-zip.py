"""Builds dist/osu-review-<version>.zip - the download for manual installs and for the built-in updater.

Layout: osu-review/ + the program files (the updater only accepts this layout)."""
import json
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOP = ["Install.bat", "README.txt", "Start.bat", "Uninstall.bat", "app.py", "window.py", "version.json"]
DIRS = ["core", "web"]


def main():
    ver = json.load(open(os.path.join(ROOT, "version.json"), encoding="utf-8"))["version"]
    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    out = os.path.join(ROOT, "dist", f"osu-review-{ver}.zip")
    names = list(TOP)
    for d in DIRS:
        for base, subdirs, files in os.walk(os.path.join(ROOT, d)):
            subdirs[:] = sorted(s for s in subdirs if s != "__pycache__")
            for f in sorted(files):
                if f.endswith(".pyc") or f == "gh_bundle.json":
                    continue
                names.append(os.path.relpath(os.path.join(base, f), ROOT).replace(os.sep, "/"))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for n in names:
            z.write(os.path.join(ROOT, n), "osu-review/" + n)
    print(f"{out}: {len(names)} files, {os.path.getsize(out) / 1024:.0f} KB")
    gh = os.environ.get("GITHUB_ENV")
    if gh:
        with open(gh, "a", encoding="utf-8") as f:
            f.write(f"ZIP={out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
