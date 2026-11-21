import base64
import hashlib
import importlib.util
import re
import sys
from importlib.metadata import distribution
from pathlib import Path

BUILD = re.compile(r"\d{1,6}\.\d{1,6}\.\d{1,6} sha256\.[0-9a-f]{64}")
WHEEL_METADATA = {"METADATA", "WHEEL", "entry_points.txt"}


def walk_error(error: OSError) -> None:
    raise error


def wheel_identity() -> str:
    release = distribution("arete")
    installed = Path(release.locate_file("arete"))
    spec = importlib.util.find_spec("arete")
    if (not spec or not spec.origin or installed.is_symlink()
            or Path(spec.origin).resolve() != (installed / "__init__.py").resolve()):
        raise ValueError("expected the installed Arete package")

    inventory = {}
    metadata = set()
    for file in release.files or ():
        if "__pycache__" in file.parts and file.hash is None:
            continue
        if file.parts[:1] == ("arete",) and ".." not in file.parts:
            inventory[file.as_posix()] = file
        elif (len(file.parts) == 2 and file.parts[0].endswith(".dist-info")
              and file.name in WHEEL_METADATA):
            inventory[file.as_posix()] = file
            metadata.add(file.name)
    package_files = set()
    for folder, dirs, filenames in installed.walk(on_error=walk_error):
        if "__pycache__" in dirs:
            dirs.remove("__pycache__")
        package_files.update((folder / name).relative_to(installed.parent).as_posix() for name in filenames)
    if (metadata != WHEEL_METADATA or "arete/__init__.py" not in package_files
            or package_files != {name for name in inventory if name.startswith("arete/")}):
        raise ValueError("installed files differ from the wheel inventory")

    digest = hashlib.sha256()
    for name, file in sorted(inventory.items()):
        path = Path(file.locate())
        if path.is_symlink() or file.hash is None or file.hash.mode != "sha256":
            raise ValueError("expected a hashed wheel file")
        with path.open("rb") as stream:
            content_digest = hashlib.file_digest(stream, "sha256").digest()
        if base64.urlsafe_b64encode(content_digest).rstrip(b"=").decode() != file.hash.value:
            raise ValueError("installed file differs from the wheel")
        encoded = name.encode()
        digest.update(len(encoded).to_bytes(8, "big") + encoded + content_digest)

    identity = f"{release.version} sha256.{digest.hexdigest()}"
    if not BUILD.fullmatch(identity):
        raise ValueError("unsupported release version")
    return identity


def main() -> int:
    try:
        print(wheel_identity())
    except (OSError, ValueError, RuntimeError, TypeError):
        print("arete: installed release wheel identity unavailable", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
