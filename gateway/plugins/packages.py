from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
from pathlib import Path, PurePosixPath
import stat
import tempfile
import zipfile

from gateway.plugins.schema import MAX_PACKAGE, PluginError, read_manifest


@contextmanager
def unpack(root: Path, content: bytes, gateway_version: str):
    if len(content) > MAX_PACKAGE:
        raise PluginError("PLUGIN_INVALID_PACKAGE", "插件包超过 16 MiB")
    root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(dir=root, prefix="staging-") as temporary:
            stage = Path(temporary)
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                entries = archive.infolist()
                if len(entries) > 1000 or sum(item.file_size for item in entries) > 64 * 1024 * 1024:
                    raise PluginError("PLUGIN_INVALID_PACKAGE", "插件展开大小或文件数超限")
                seen = set()
                for item in entries:
                    path = PurePosixPath(item.filename)
                    key = str(path).casefold()
                    mode = item.external_attr >> 16
                    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                                *(f"lpt{i}" for i in range(1, 10))}
                    if (path.is_absolute() or ".." in path.parts or "\\" in item.filename
                            or ":" in item.filename or any(part.startswith(".") for part in path.parts)
                            or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}
                            or item.file_size > max(1, item.compress_size) * 200
                            or key in seen or any(part.rstrip(" .") != part or part.split(".")[0].casefold() in reserved
                                                  for part in path.parts)):
                        raise PluginError("PLUGIN_INVALID_PACKAGE", "插件包包含非法路径、链接、重复文件或异常压缩比例")
                    seen.add(key)
                    destination = stage / path
                    if item.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                    else:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(archive.read(item))
            manifest = read_manifest(stage, gateway_version)
            yield stage, manifest, hashlib.sha256(content).hexdigest()
    except (zipfile.BadZipFile, RuntimeError, EOFError, OSError) as exc:
        raise PluginError("PLUGIN_INVALID_PACKAGE", "无法读取或解包插件，请检查 ZIP 内容") from exc


def pack(source: Path, destination: Path, version: str) -> str:
    read_manifest(source, version)
    files = list(source.rglob("*"))
    if any(path.is_symlink() for path in files):
        raise PluginError("PLUGIN_INVALID_PACKAGE", "插件目录包含符号链接")
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            if path.is_file() and "__pycache__" not in path.parts and not path.name.startswith("."):
                archive.write(path, path.relative_to(source).as_posix())
    content = destination.read_bytes()
    with tempfile.TemporaryDirectory() as temporary:
        with unpack(Path(temporary), content, version):
            pass
    return hashlib.sha256(content).hexdigest()
