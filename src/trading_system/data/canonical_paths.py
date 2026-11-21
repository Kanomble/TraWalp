"""Windows mandatory sharing locks for copy-only migration, fail closed elsewhere.

Unlike stat/check/open, these pins prohibit rename/delete for every path component
through SQLite close. Protected files also deny write sharing. No SQLite handle
is ever opened on Source or Backup. This is not a general purpose file copier.
"""

from __future__ import annotations

import os
from contextlib import ExitStack, contextmanager
from pathlib import Path


@contextmanager
def pinned_files(paths, *, directories=()):
    if os.name != "nt":
        raise RuntimeError("MANDATORY_PATH_PINNING_UNSUPPORTED_PLATFORM")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    invalid = ctypes.c_void_p(-1).value

    def handle(path, access, sharing, flags):
        h = create(str(path), access, sharing, None, 3, flags, None)
        if h == invalid:
            raise ctypes.WinError(ctypes.get_last_error())
        return h

    absolute = {k: Path(v).absolute() for k, v in paths.items()}
    initial = {k: (p.stat().st_dev, p.stat().st_ino) for k, p in absolute.items()}
    with ExitStack() as stack:
        parents = {p for f in absolute.values() for p in f.parents}
        for directory in map(Path, directories):
            parents.add(directory)
            parents.update(directory.parents)
        for parent in sorted(parents, key=lambda p: len(p.parts)):
            # FILE_FLAG_BACKUP_SEMANTICS; share read/write, never delete.
            h = handle(parent, 0, 3, 0x02000000)
            stack.callback(close, h)
        files = {}
        for role, path in absolute.items():
            h = handle(path, 0x80000000, 3 if role == "work" else 1, 0)
            try:
                fd = msvcrt.open_osfhandle(h, os.O_RDONLY | os.O_BINARY)
            except BaseException:
                close(h)
                raise
            stream = stack.enter_context(os.fdopen(fd, "rb"))
            stat = os.fstat(stream.fileno())
            if (stat.st_dev, stat.st_ino) != initial[role] or stat.st_nlink != 1:
                raise ValueError("PINNED_FILE_IDENTITY_CHANGED")
            files[role] = stream
        yield files
