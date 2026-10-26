"""Vendor the KNOWLEDGE-INTAKE-0001 contract from a bezalel-rfc checkout.

Copies the committed bytes of the reference oracle, both schemas, the golden
batches, and the oracle's own conformance tests and golden builder at one git
ref into integrations/knowledge/vendor/, mirroring their bezalel-rfc paths, and
writes PIN with the source commit and the SHA-256 of every vendored file
(contract section 6). Reading git objects rather than the working tree
guarantees PIN names the commit the bytes came from. Run from the repository
root:

    .venv/bin/python scripts/vendor_knowledge_contract.py --rfc ../bezalel-rfc --ref origin/main
"""

import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys

VENDOR = Path(__file__).resolve().parents[1] / "integrations/knowledge/vendor"
SOURCES = (
    "reference/knowledge_contract.py",
    "schemas/knowledge-batch-manifest-v1.schema.json",
    "schemas/knowledge-record-v1.schema.json",
    "tests/test_knowledge_contract.py",
    "tests/fixtures/knowledge/build_golden.py",
    "tests/fixtures/knowledge/golden",
)
HEADER = (
    "# KNOWLEDGE-INTAKE-0001 vendored files (contract section 6). Do not edit them.\n"
    "# Regenerate with: .venv/bin/python scripts/vendor_knowledge_contract.py --rfc <bezalel-rfc> --ref origin/main\n"
)


def _git(rfc, *args):
    try:
        return subprocess.run(["git", "-C", str(rfc), *args], check=True, capture_output=True).stdout
    except subprocess.CalledProcessError as error:
        sys.stderr.write(error.stderr.decode(errors="replace"))
        raise SystemExit(f"git {args[0]} failed in {rfc}")


def vendor(rfc, ref, target=VENDOR):
    commit = _git(rfc, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
    paths = [path for path in _git(rfc, "ls-tree", "-r", "-z", "--name-only", commit, "--", *SOURCES)
             .decode().split("\0") if path]
    if not any(path.startswith("tests/fixtures/knowledge/golden/") for path in paths):
        raise SystemExit("no golden batches found; is --rfc a bezalel-rfc checkout?")
    missing = [source for source in SOURCES if source.endswith((".py", ".json")) and source not in paths]
    if missing:
        raise SystemExit(f"missing at {commit}: {', '.join(missing)}")
    shutil.rmtree(target, ignore_errors=True)
    lines = [HEADER, f"source_repository wdphillipsRT/bezalel-rfc\nsource_commit {commit}\n"]
    for path in sorted(paths):
        data = _git(rfc, "show", f"{commit}:{path}")
        destination = target / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {path}\n")
    (target / "PIN").write_text("".join(lines), encoding="utf-8")
    return commit


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rfc", type=Path, required=True, help="bezalel-rfc checkout")
    parser.add_argument("--ref", default="origin/main", help="git ref to vendor (default: origin/main)")
    arguments = parser.parse_args()
    commit = vendor(arguments.rfc, arguments.ref)
    print(f"vendored bezalel-rfc {commit} into {VENDOR}")


if __name__ == "__main__":
    main()
