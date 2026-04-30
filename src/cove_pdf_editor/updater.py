"""Auto-updater backed by the GitHub releases API.

Philosophy: never silently replace the user's binary. A background thread
polls the releases API on startup; when a newer version is published, the
user gets a dialog and chooses whether to install.

AppImage installs can do the download-and-swap end-to-end (the kernel keeps
the running mmap alive across an overwrite, so replacing the file on disk
and re-execing works). Windows Setup, Portable, and .deb just open the
GitHub release page — the user runs the installer themselves.

Usage from a MainWindow:

    from . import updater
    from . import __version__

    self._updater = updater.UpdateController(
        parent=self,
        current_version=__version__,
        repo="Sin213/cove-pdf-editor",
        app_display_name="Cove PDF Editor",
        cache_subdir="cove-pdf-editor",
    )
    QTimer.singleShot(4000, self._updater.check)
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog


@dataclass
class UpdateInfo:
    latest_version: str
    release_url: str
    asset_name: str | None = None
    asset_url: str | None = None
    asset_size: int = 0
    sha256_url: str | None = None


class ChecksumError(RuntimeError):
    """Raised when the downloaded asset's sha256 does not match its sidecar.

    Surfaced as a typed failure so callers can distinguish a tampered or
    truncated download from a generic IO error and refuse to swap the
    running binary.
    """


class CancelledError(RuntimeError):
    """Raised by the verification helpers when the supplied cancel poll
    returns True. Distinct from :class:`ChecksumError` so the worker can
    route it to the cancel path (failed("cancelled")) rather than the
    "checksum mismatch" UI."""


def _parse_version(v: str) -> tuple[int, int, int]:
    v = v.strip().lstrip("vV")
    out: list[int] = []
    for part in v.split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
        if len(out) == 3:
            break
    while len(out) < 3:
        out.append(0)
    return (out[0], out[1], out[2])


def version_newer(latest: str, current: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


def bundle_kind() -> str:
    """Detect how this instance was packaged so we can pick the matching
    release asset for in-place update."""
    if os.environ.get("APPIMAGE"):
        return "appimage"
    if sys.platform == "win32":
        if not getattr(sys, "frozen", False):
            return "source"
        exe_str = str(Path(sys.executable).resolve())
        if "Program Files" in exe_str or r"AppData\Local" in exe_str:
            return "win-setup"
        return "win-portable"
    if sys.platform.startswith("linux") and getattr(sys, "frozen", False):
        return "deb"
    return "source"


def preferred_asset(kind: str, assets: list[dict]) -> dict | None:
    def first_match(predicate) -> dict | None:
        return next((a for a in assets if predicate(a["name"].lower())), None)

    if kind == "appimage":
        return first_match(
            lambda n: n.endswith(".appimage") and not n.endswith(".sha256"),
        )
    if kind == "deb":
        return first_match(
            lambda n: n.endswith(".deb") and not n.endswith(".sha256"),
        )
    if kind == "win-setup":
        return first_match(
            lambda n: "setup" in n and n.endswith(".exe"),
        )
    if kind == "win-portable":
        return first_match(
            lambda n: "portable" in n and n.endswith(".exe"),
        )
    return None


def matching_sha256_asset(asset_name: str, assets: list[dict]) -> dict | None:
    """Find the ``<asset_name>.sha256`` sidecar in a release's asset list.

    The release pipeline (``.github/workflows/release.yml``) publishes one
    sidecar per shipped binary, so a missing sidecar at update time is a
    release-pipeline regression — surface it as a verification failure
    rather than silently skipping the check.
    """
    target = f"{asset_name}.sha256".lower()
    return next((a for a in assets if a["name"].lower() == target), None)


def _parse_sha256_sidecar(text: str) -> str:
    """Pull the hex hash out of a ``sha256sum`` / ``Get-FileHash`` sidecar.

    Both Linux ``sha256sum <file>`` and the Windows ``Out-File`` block in
    release.yml produce ``<hex>  <name>``. Take the first whitespace
    token of the first non-empty line, lower-cased.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        token = line.split()[0]
        if len(token) == 64 and all(c in "0123456789abcdefABCDEF" for c in token):
            return token.lower()
        raise ChecksumError(f"unrecognized sidecar contents: {line!r}")
    raise ChecksumError("empty sidecar")


def _sha256_of_file(
    path: Path,
    chunk_size: int = 1024 * 1024,
    is_cancelled=None,
) -> str:
    """Hash ``path`` chunk-wise so a multi-MB AppImage doesn't pin the
    thread for seconds. ``is_cancelled`` is an optional zero-arg callable
    polled at each chunk; when it returns True the loop raises
    :class:`CancelledError` so a cancel click during hashing aborts
    promptly."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            if is_cancelled is not None and is_cancelled():
                raise CancelledError("cancelled during hashing")
            block = f.read(chunk_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def fetch_sha256_sidecar(url: str, repo: str, timeout: float = 20.0) -> str:
    """GET the sidecar URL and return the parsed sha256 hex digest."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"{repo.split('/')[-1]}-updater"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # Sidecar files are tiny (a hex hash + name + newline); cap the
        # read so a hostile redirect can't dump unbounded bytes.
        raw = resp.read(4096).decode("ascii", errors="replace")
    return _parse_sha256_sidecar(raw)


def verify_sha256(
    path: Path,
    sidecar_url: str,
    repo: str,
    is_cancelled=None,
) -> None:
    """Verify ``path`` matches the hash advertised by ``sidecar_url``.

    On any failure (network error, malformed sidecar, unreadable file,
    hash mismatch) the partial download at ``path`` is unlinked and a
    :class:`ChecksumError` is raised. ``is_cancelled`` is an optional
    zero-arg callable; when it returns True at any of the three
    sampling points (before sidecar fetch, after sidecar fetch, during
    hashing) the partial is unlinked and :class:`CancelledError` is
    raised so the caller can route it to a cancel-failure path.
    """
    def _check_cancel() -> None:
        if is_cancelled is not None and is_cancelled():
            path.unlink(missing_ok=True)
            raise CancelledError("verification cancelled")

    _check_cancel()
    try:
        expected = fetch_sha256_sidecar(sidecar_url, repo)
    except ChecksumError:
        path.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001
        path.unlink(missing_ok=True)
        raise ChecksumError(f"could not fetch sidecar: {exc}") from exc
    _check_cancel()
    try:
        actual = _sha256_of_file(path, is_cancelled=is_cancelled)
    except CancelledError:
        path.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001
        # Wrap so an unreadable / vanished cache file is treated as a
        # verification failure (keeps the function contract tight: any
        # failure → ChecksumError, no leftover artifact).
        path.unlink(missing_ok=True)
        raise ChecksumError(f"could not hash downloaded file: {exc}") from exc
    if actual != expected:
        path.unlink(missing_ok=True)
        raise ChecksumError(
            f"sha256 mismatch: expected {expected}, got {actual}",
        )


def fetch_latest_release(repo: str, timeout: float = 8.0) -> dict | None:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{repo.split('/')[-1]}-updater",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except Exception:  # noqa: BLE001
        return None


class UpdateCheckWorker(QObject):
    updateAvailable = Signal(object)   # UpdateInfo
    noUpdate = Signal()
    failed = Signal(str)

    def __init__(self, current_version: str, repo: str) -> None:
        super().__init__()
        self._current = current_version
        self._repo = repo

    def run(self) -> None:
        data = fetch_latest_release(self._repo)
        if data is None:
            self.failed.emit("could not reach the releases API")
            return
        tag = data.get("tag_name") or ""
        if not tag:
            self.failed.emit("release had no tag_name")
            return
        latest = tag.lstrip("vV")
        if not version_newer(latest, self._current):
            self.noUpdate.emit()
            return
        assets = data.get("assets") or []
        asset = preferred_asset(bundle_kind(), assets)
        sidecar = (
            matching_sha256_asset(asset["name"], assets) if asset else None
        )
        info = UpdateInfo(
            latest_version=latest,
            release_url=(
                data.get("html_url")
                or f"https://github.com/{self._repo}/releases/tag/{tag}"
            ),
            asset_name=asset["name"] if asset else None,
            asset_url=asset["browser_download_url"] if asset else None,
            asset_size=int(asset["size"]) if asset else 0,
            sha256_url=sidecar["browser_download_url"] if sidecar else None,
        )
        self.updateAvailable.emit(info)


class DownloadWorker(QObject):
    """Stream a URL to a destination file, emit progress, then verify
    against the matching sha256 sidecar — all on the worker thread so
    the GUI never blocks on the network or on hashing a multi-MB file."""

    progress = Signal(int)           # 0–100
    finished = Signal(str)           # destination path (post-verify)
    failed = Signal(str)             # download / IO error
    verifyFailed = Signal(str)       # checksum failure (typed)

    def __init__(
        self, url: str, dest: Path, repo: str, sha256_url: str | None,
    ) -> None:
        super().__init__()
        self._url = url
        self._dest = dest
        self._repo = repo
        self._sha256_url = sha256_url
        # ``threading.Event`` is safe to set from any thread without
        # depending on the worker's Qt event loop (``run`` is busy on
        # the network / hashing and never pumps queued slot calls). The
        # GUI-thread cancel button connects via ``Qt.DirectConnection``
        # so ``cancel()`` runs on the GUI thread and writes the flag
        # synchronously; the worker observes it on its next read.
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            req = urllib.request.Request(
                self._url,
                headers={"User-Agent": f"{self._repo.split('/')[-1]}-updater"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                written = 0
                self._dest.parent.mkdir(parents=True, exist_ok=True)
                with open(self._dest, "wb") as f:
                    while True:
                        if self._cancel.is_set():
                            raise RuntimeError("cancelled")
                        chunk = resp.read(262144)
                        if not chunk:
                            break
                        f.write(chunk)
                        written += len(chunk)
                        if total > 0:
                            self.progress.emit(int(written * 100 / total))
        except Exception as exc:  # noqa: BLE001
            try:
                self._dest.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            self.failed.emit(str(exc))
            return
        # Honor cancel before kicking off verification.
        if self._cancel.is_set():
            self._discard_partial()
            self.failed.emit("cancelled")
            return
        # Verify on this worker thread so a 20-second sidecar timeout
        # plus full-file hashing never freezes the UI. ``verify_sha256``
        # unlinks the partial on any failure and polls ``is_cancelled``
        # at three points so a cancel click mid-hash aborts promptly.
        if self._sha256_url is None:
            self.failed.emit("missing checksum sidecar URL")
            return
        try:
            verify_sha256(
                self._dest, self._sha256_url, self._repo,
                is_cancelled=self._cancel.is_set,
            )
        except CancelledError:
            self.failed.emit("cancelled")
            return
        except ChecksumError as exc:
            self.verifyFailed.emit(str(exc))
            return
        # Belt-and-suspenders re-check: a cancel that landed in the
        # narrow window between the last poll and ``finished.emit``
        # must still abort the install.
        if self._cancel.is_set():
            self._discard_partial()
            self.failed.emit("cancelled")
            return
        self.finished.emit(str(self._dest))

    def _discard_partial(self) -> None:
        try:
            self._dest.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


def swap_in_appimage(new_path: Path) -> Path:
    """Replace the running AppImage with `new_path`, leave it executable, and
    return its final path."""
    current = os.environ.get("APPIMAGE")
    if not current:
        raise RuntimeError("APPIMAGE env var not set — not an AppImage install")
    target = Path(current).resolve()
    shutil.move(str(new_path), str(target))
    mode = os.stat(target).st_mode
    os.chmod(target, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def relaunch(path: Path) -> None:
    """Spawn `path` detached from the current process group so it survives
    our own exit — the running process keeps the old binary mmap'd while
    the new one takes over the path on disk."""
    subprocess.Popen(
        [str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


class UpdateController(QObject):
    """Attach to a QMainWindow. Call .check() to kick off a background poll;
    on a newer release it drives the prompt → download → swap → relaunch flow."""

    def __init__(
        self,
        parent,
        current_version: str,
        repo: str,
        app_display_name: str,
        cache_subdir: str,
    ) -> None:
        super().__init__(parent)
        self._parent = parent
        self._current = current_version
        self._repo = repo
        self._display_name = app_display_name
        self._cache_subdir = cache_subdir
        self._thread: QThread | None = None
        self._worker: UpdateCheckWorker | None = None
        self._download_thread: QThread | None = None
        self._download_worker: DownloadWorker | None = None
        self._progress: QProgressDialog | None = None
        self._prompt_shown = False
        self._pending_info: UpdateInfo | None = None

    def check(self) -> None:
        if self._thread is not None:
            return
        thread = QThread(self)
        worker = UpdateCheckWorker(self._current, self._repo)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.updateAvailable.connect(thread.quit)
        worker.noUpdate.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.updateAvailable.connect(self._on_update_available, Qt.QueuedConnection)
        thread.finished.connect(self._on_check_done, Qt.QueuedConnection)
        self._thread = thread
        self._worker = worker
        thread.start()

    def _on_check_done(self) -> None:
        self._thread = None
        self._worker = None

    def _on_update_available(self, info: UpdateInfo) -> None:
        if self._prompt_shown:
            return
        self._prompt_shown = True
        self._prompt(info)

    def _prompt(self, info: UpdateInfo) -> None:
        kind = bundle_kind()
        can_auto_install = kind == "appimage" and bool(info.asset_url)

        msg = QMessageBox(self._parent)
        msg.setIcon(QMessageBox.Information)
        msg.setWindowTitle(f"{self._display_name} — update available")
        msg.setText(
            f"{self._display_name} v{info.latest_version} is available.\n"
            f"You're running v{self._current}.",
        )
        if can_auto_install:
            msg.setInformativeText(
                f"{info.asset_name} ({info.asset_size // (1024 * 1024)} MB). "
                "The app will restart after the update.",
            )
            install_btn = msg.addButton("Update now", QMessageBox.AcceptRole)
            open_btn = msg.addButton("View release", QMessageBox.HelpRole)
            msg.addButton("Later", QMessageBox.RejectRole)
        else:
            msg.setInformativeText(
                "Open the release page to download the latest installer.",
            )
            install_btn = None
            open_btn = msg.addButton("View release", QMessageBox.AcceptRole)
            msg.addButton("Later", QMessageBox.RejectRole)
        msg.exec()
        clicked = msg.clickedButton()
        if install_btn is not None and clicked is install_btn:
            self._install(info)
        elif open_btn is not None and clicked is open_btn:
            QDesktopServices.openUrl(QUrl(info.release_url))

    def _install(self, info: UpdateInfo) -> None:
        if not info.asset_url or not info.asset_name:
            QDesktopServices.openUrl(QUrl(info.release_url))
            return
        if not info.sha256_url:
            # Refuse to swap the running binary without a checksum to
            # verify against. The release pipeline always publishes a
            # sidecar for shipped artifacts; a missing one means we
            # can't establish trust on the downloaded file.
            QMessageBox.warning(
                self._parent, "Update unavailable",
                "Couldn't find a sha256 sidecar for this release; "
                "skipping the in-place update for safety.",
            )
            QDesktopServices.openUrl(QUrl(info.release_url))
            return
        self._pending_info = info
        cache = Path(os.path.expanduser(f"~/.cache/{self._cache_subdir}"))
        cache.mkdir(parents=True, exist_ok=True)
        dest = cache / info.asset_name

        self._progress = QProgressDialog(
            f"Downloading {info.asset_name}…", "Cancel", 0, 100, self._parent,
        )
        self._progress.setWindowTitle(f"Updating {self._display_name}")
        self._progress.setAutoClose(False)
        self._progress.setAutoReset(False)
        self._progress.setMinimumDuration(0)
        self._progress.setValue(0)

        thread = QThread(self)
        worker = DownloadWorker(
            info.asset_url, dest, self._repo, info.sha256_url,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.verifyFailed.connect(thread.quit)
        # DirectConnection: cancel() runs on the GUI thread, so the
        # threading.Event is set synchronously even though the worker
        # thread is busy in network / hashing and isn't pumping its
        # event loop. Without this, a queued connection would park the
        # cancel slot until run() returns — too late.
        self._progress.canceled.connect(worker.cancel, Qt.DirectConnection)
        worker.progress.connect(self._progress.setValue, Qt.QueuedConnection)
        worker.finished.connect(self._on_downloaded, Qt.QueuedConnection)
        worker.failed.connect(self._on_download_failed, Qt.QueuedConnection)
        worker.verifyFailed.connect(self._on_verify_failed, Qt.QueuedConnection)
        thread.finished.connect(self._on_download_thread_done, Qt.QueuedConnection)
        self._download_thread = thread
        self._download_worker = worker
        thread.start()

    def _on_downloaded(self, path: str) -> None:
        # Reached only after DownloadWorker has already verified the
        # sha256 sidecar on its own thread, so the swap is safe to run
        # synchronously here.
        if self._progress is not None:
            self._progress.close()
        self._pending_info = None
        try:
            new_path = swap_in_appimage(Path(path))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(
                self._parent, "Update failed",
                f"Couldn't swap in the new AppImage:\n{exc}",
            )
            return
        relaunch(new_path)
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _on_download_failed(self, msg: str) -> None:
        if self._progress is not None:
            self._progress.close()
        self._pending_info = None
        QMessageBox.warning(
            self._parent, "Update failed",
            f"The download didn't complete:\n{msg}",
        )

    def _on_verify_failed(self, msg: str) -> None:
        if self._progress is not None:
            self._progress.close()
        self._pending_info = None
        QMessageBox.warning(
            self._parent, "Update failed",
            f"Downloaded update failed checksum verification:\n{msg}\n\n"
            "The partial download was discarded and your installed "
            "binary is unchanged.",
        )

    def _on_download_thread_done(self) -> None:
        self._download_thread = None
        self._download_worker = None
