"""Stage pinned upstream archives for the Windows source offer, without network access."""

import argparse
from hashlib import file_digest
import json
from pathlib import Path
import re
from shutil import copyfile
from subprocess import check_output


MANIFEST = Path(__file__).with_name("source-archives.json")


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def stage_sources(manifest: Path, caches: list[Path], output: Path) -> None:
    manifest_bytes = manifest.read_bytes()
    archives = json.loads(manifest_bytes)["archives"]
    if not archives:
        raise ValueError("Source archive inventory is empty")
    verified = {}
    for entry in archives:
        name = entry["filename"]
        digest = entry["sha256"]
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.tar\.(?:xz|gz)", name)
                or name in verified or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError(f"Invalid source archive entry: {name}")
        source = next((cache / name for cache in caches if (cache / name).is_file()), None)
        if source is None:
            raise FileNotFoundError(name)
        if sha256(source) != digest:
            raise ValueError(f"SHA-256 mismatch: {name}")
        verified[name] = (source, digest)

    # An existing output could contain artifacts from a different build.
    output.mkdir(exist_ok=False)
    for name, (source, digest) in verified.items():
        staged = output / name
        copyfile(source, staged)
        if sha256(staged) != digest:
            raise ValueError(f"SHA-256 mismatch after staging: {name}")
    (output / MANIFEST.name).write_bytes(manifest_bytes)
    sums = "".join(f"{digest}  {name}\n" for name, (_, digest) in verified.items())
    (output / "SHA256SUMS").write_text(sums, encoding="ascii", newline="\n")


def export_release_source(repository: Path, commit: str, output: Path) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Provide a full Git commit SHA")

    git = ["git", "-C", str(repository)]
    resolved = check_output([*git, "rev-parse", "--verify", commit + "^{commit}"],
                            text=True).strip()
    if resolved != commit:
        raise ValueError("The selected object is not the exact release commit")
    tree = check_output([*git, "rev-parse", commit + "^{tree}"], text=True).strip()
    archive = output / f"fuPDF-source-{commit}.zip"
    receipt = output / "RELEASE-SOURCE.json"
    if archive.exists() or receipt.exists():
        raise FileExistsError("Release source archive or receipt already exists")
    data = check_output([*git, "-c", "core.autocrlf=false", "archive",
                         "--format=zip", "--prefix=app/", commit])
    with archive.open("xb") as stream:
        stream.write(data)
    with receipt.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump({
            "commit": commit,
            "tree": tree,
            "source_archive": archive.name,
            "source_archive_sha256": sha256(archive),
        }, stream, indent=2)
        stream.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release-commit", help="also export exactly this full Git commit")
    args = parser.parse_args()
    if args.release_commit is not None and not re.fullmatch(r"[0-9a-f]{40}", args.release_commit):
        parser.error("--release-commit must be a full Git commit SHA")
    stage_sources(MANIFEST, args.cache, args.output)
    if args.release_commit is not None:
        export_release_source(MANIFEST.resolve().parents[2], args.release_commit, args.output)


if __name__ == "__main__":
    main()
