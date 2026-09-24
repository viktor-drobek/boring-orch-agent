"""Explicit bounded file tools. This is a tool policy, not an OS sandbox."""
import errno
import os
from pathlib import Path
import tempfile

from .model import Invalid, fields, representable_path


class Workspace:
    def __init__(self, root, tools, store_home):
        self.root = Path(root).resolve()
        self.allowed = set(tools)
        self.store_home = Path(store_home).resolve()

    @staticmethod
    def os_error(exc, relative):
        """A model-safe description of a failed file operation.

        ``str(OSError)`` embeds absolute host paths; the model only gets the errno
        name, the OS reason and the workspace-relative path it asked for.
        """
        name = errno.errorcode.get(exc.errno, type(exc).__name__) if exc.errno else type(exc).__name__
        reason = f" ({exc.strerror})" if exc.strerror else ""
        target = relative if isinstance(relative, str) and representable_path(relative) else "."
        return f"File operation failed: {name}{reason} for workspace path {target!r}"

    def path(self, value):
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise Invalid("A relative workspace path is required")
        if not representable_path(value):
            raise Invalid("Paths must not contain NUL bytes or characters the filesystem cannot represent")
        part = Path(value)
        if part.is_absolute() or any(p.startswith(".") and p != "." for p in part.parts):
            raise Invalid("Absolute paths, parent paths and hidden files are unavailable")
        path = self.root / part
        cursor = self.root
        for component in part.parts:
            cursor = cursor / component
            if cursor.is_symlink():
                raise Invalid("Symbolic links are unavailable")
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root) or resolved.is_relative_to(self.store_home):
            raise Invalid("Path is outside the accessible workspace")
        return resolved

    def call(self, action):
        name = action.get("action")
        if not isinstance(name, str) or name not in self.allowed:
            raise Invalid("Tool is not permitted by this task")
        fields(action, {"action", "path", "content"} if name == "write_file" else {"action", "path"}, "tool call")
        path = self.path(action.get("path", "."))
        if name == "list_files":
            if not path.is_dir():
                raise Invalid("Not a directory")
            items = []
            # Stop at 201 entries rather than loading an unbounded directory.
            with os.scandir(path) as entries:
                for entry in entries:
                    if entry.name.startswith(".") or entry.is_symlink() or Path(entry.path).resolve().is_relative_to(self.store_home):
                        continue
                    items.append({"name": entry.name, "directory": entry.is_dir(follow_symlinks=False)})
                    if len(items) == 201:
                        break
            return {"entries": sorted(items[:200], key=lambda x: x["name"]), "truncated": len(items) > 200}
        if name == "read_file":
            if not path.is_file():
                raise Invalid("Not a regular file")
            with path.open("rb") as file:
                data = file.read(65537)
            if len(data) > 65536:
                raise Invalid("File exceeds the 64 KiB tool limit")
            try:
                return {"content": data.decode("utf-8")}
            except UnicodeError as exc:
                raise Invalid("Only UTF-8 text files are supported") from exc
        if name == "write_file":
            content = action.get("content")
            try:
                size = len(content.encode()) if isinstance(content, str) else None
            except UnicodeError:
                size = None
            if size is None or size > 65536:
                raise Invalid("write_file requires UTF-8 text of at most 64 KiB")
            if path.exists() and not path.is_file():
                raise Invalid("Target must be a regular file")
            # path() has already rejected escapes, hidden names and symlinked components, so
            # every directory created here lies inside the workspace.
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".boa-write-", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    file.write(content)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return {"written": str(path.relative_to(self.root)), "bytes": len(content.encode())}
        raise Invalid("Unknown tool")

    def missing(self, relative_paths):
        """Expected files that are not regular files inside the workspace right now."""
        absent = []
        for relative in relative_paths:
            try:
                if not self.path(relative).is_file():
                    absent.append(relative)
            except (Invalid, OSError):
                # A path the OS cannot stat (for example a too-long name) is not a written file.
                absent.append(relative)
        return absent
