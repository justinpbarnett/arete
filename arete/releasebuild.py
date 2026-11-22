import base64
import hashlib
import importlib.util
import json
import re
import sys
from email.parser import BytesParser
from importlib.metadata import distribution
from pathlib import Path
from zipfile import BadZipFile, ZipFile

BUILD = re.compile(r"[0-9][0-9A-Za-z.!+_-]{0,127} sha256\.[0-9a-f]{64}")
WHEEL_METADATA = {"METADATA", "WHEEL", "entry_points.txt"}


def walk_error(error: OSError) -> None:
    raise error


def wheel_identity(wheel: Path) -> str:
    release = distribution("arete")
    installed = Path(release.locate_file("arete"))
    spec = importlib.util.find_spec("arete")
    if (not spec or not spec.origin or installed.is_symlink()
            or Path(spec.origin).resolve() != (installed / "__init__.py").resolve()):
        raise ValueError("expected the installed Arete package")

    inventory = {}
    metadata = {}
    for file in release.files or ():
        if "__pycache__" in file.parts and file.hash is None:
            continue
        if file.parts[:1] == ("arete",) and ".." not in file.parts:
            inventory[file.as_posix()] = file
        elif (len(file.parts) == 2 and file.parts[0].endswith(".dist-info")
              and file.name in WHEEL_METADATA):
            inventory[file.as_posix()] = file
            if file.name in metadata:
                raise ValueError("duplicate installed wheel metadata")
            metadata[file.name] = file.as_posix()
    package_files = set()
    for folder, dirs, filenames in installed.walk(on_error=walk_error):
        if "__pycache__" in dirs:
            dirs.remove("__pycache__")
        package_files.update((folder / name).relative_to(installed.parent).as_posix() for name in filenames)
    if (metadata.keys() != WHEEL_METADATA or "arete/__init__.py" not in package_files
            or package_files != {name for name in inventory if name.startswith("arete/")}):
        raise ValueError("installed files differ from the wheel inventory")

    with wheel.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        provenance = json.loads(release.read_text("direct_url.json") or "{}")
        archive_info = provenance.get("archive_info", {})
        recorded = archive_info.get("hashes", {}).get("sha256")
        if recorded is None:
            recorded = archive_info.get("hash", "").removeprefix("sha256=")
        if recorded != digest:
            raise ValueError("archive digest differs from installation provenance")
        with ZipFile(stream) as archive:
            names = [member.filename for member in archive.infolist()
                     if not member.is_dir() and (member.filename.startswith("arete/")
                                                 or member.filename in metadata.values())]
            if len(names) != len(inventory) or set(names) != inventory.keys():
                raise ValueError("archive files differ from the installed inventory")
            for name, file in inventory.items():
                verify_installed_file(archive, name, file)
            version = BytesParser().parsebytes(archive.read(metadata["METADATA"]), headersonly=True).get("Version")

    identity = f"{version} sha256.{digest}"
    if version != release.version or not BUILD.fullmatch(identity):
        raise ValueError("unsupported release version")
    return identity


def verify_installed_file(archive, name, file) -> None:
    path = Path(file.locate())
    if path.is_symlink() or file.hash is None or file.hash.mode != "sha256":
        raise ValueError("expected a hashed wheel file")
    with path.open("rb") as installed, archive.open(name) as member:
        digest = hashlib.file_digest(installed, "sha256").digest()
        if (base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != file.hash.value
                or hashlib.file_digest(member, "sha256").digest() != digest):
            raise ValueError("installed file differs from the wheel")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m arete.releasebuild WHEEL", file=sys.stderr)
        return 2
    try:
        print(wheel_identity(Path(sys.argv[1])))
    except (OSError, ValueError, RuntimeError, TypeError, AttributeError, BadZipFile):
        print("arete: installed release wheel identity unavailable", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
