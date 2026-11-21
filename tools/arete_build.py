import base64
import hashlib
import shutil
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = {
    "__init__.py": ROOT / "tools/launcher/arete_launcher/__init__.py",
    "local_paths.py": ROOT / "arete/local_paths.py",
    "serve_guard.py": ROOT / "arete/serve_guard.py",
}


def get_requires_for_build_wheel(config_settings=None):
    return [] if shutil.which("uv") else ["uv_build>=0.10.2,<0.11"]


get_requires_for_build_editable = get_requires_for_build_sdist = get_requires_for_build_wheel


def native_build(hook, directory):
    if executable := shutil.which("uv"):
        cmd = [executable, "build-backend", hook, "--quiet", directory]
        return subprocess.check_output(cmd, cwd=ROOT, text=True).strip()

    import uv_build

    return getattr(uv_build, hook.replace("-", "_"))(directory)


def build_sdist(sdist_directory, config_settings=None):
    return native_build("build-sdist", sdist_directory)


def wheel_with_launcher(hook, wheel_directory):
    name = native_build(hook, wheel_directory)
    path = Path(wheel_directory) / name
    with NamedTemporaryFile(dir=wheel_directory, delete=False) as temporary:
        draft = Path(temporary.name)
    try:
        with ZipFile(path) as source, ZipFile(draft, "w", compression=ZIP_DEFLATED) as wheel:
            record, = (n for n in source.namelist() if n.endswith(".dist-info/RECORD"))
            rows = source.read(record).decode().splitlines()
            rows.remove(f"{record},,")
            for entry in source.infolist():
                if entry.filename != record:
                    wheel.writestr(entry, source.read(entry))
            for module, file in LAUNCHER.items():
                content = file.read_bytes()
                installed = f"arete_launcher/{module}"
                digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
                wheel.writestr(installed, content)
                rows.append(f"{installed},sha256={digest},{len(content)}")
            wheel.writestr(record, "\n".join([*rows, f"{record},,", ""]))
        draft.replace(path)
    finally:
        draft.unlink(missing_ok=True)
    return name


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    return wheel_with_launcher("build-wheel", wheel_directory)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    return wheel_with_launcher("build-editable", wheel_directory)
