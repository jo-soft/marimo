# Copyright 2024 Marimo. All rights reserved.
from __future__ import annotations

import contextlib
import io
import os
import sys
import threading
from collections import deque
from typing import (
    TYPE_CHECKING,
    Any,
    Optional,
    Protocol,
)

from marimo import _loggers
from marimo._messaging.cell_output import CellChannel
from marimo._messaging.console_output_worker import ConsoleMsg, buffered_writer
from marimo._messaging.mimetypes import ConsoleMimeType
from marimo._messaging.types import (
    KernelMessage,
    Stderr,
    Stdin,
    Stdout,
    Stream,
)
from marimo._server.types import QueueType
from marimo._types.ids import CellId_t

if TYPE_CHECKING:
    import queue
    from collections.abc import Iterable, Iterator

LOGGER = _loggers.marimo_logger()


# Byte limits on outputs exist for two reasons
#
# 1. We use a multiprocessing.Connection object to send outputs from
#    the kernel to the server (the server then sends the output to
#    the frontend via a websocket). The Connection object has a limit
#    of ~32MiB that it can send before it chokes
#    (https://docs.python.org/3/library/multiprocessing.html#multiprocessing.connection.Connection.send).
#
#    TODO(akshayka): Get around this by breaking up the message sent
#    over the Connection or plumbing the websocket into the kernel.
#
# 2. The frontend chokes when we send outputs that are too big, i.e.
#    it freezes and sometimes even crashes. That can lead to lost work.
#    It appears this is the bottleneck right now, compared to 1.
#
# Usually users only output gigantic things accidentally, so refusing
# to show large outputs should in most cases not bother the user too much.
# In any case, it's better than breaking the frontend/kernel.


def output_max_bytes() -> int:
    from marimo._runtime.context import ContextNotInitializedError, get_context

    try:
        return get_context().marimo_config["runtime"]["output_max_bytes"]
    except ContextNotInitializedError:
        return 5_000_000


def std_stream_max_bytes() -> int:
    from marimo._runtime.context import ContextNotInitializedError, get_context

    try:
        return get_context().marimo_config["runtime"]["std_stream_max_bytes"]
    except ContextNotInitializedError:
        return 1_000_000


class PipeProtocol(Protocol):
    def send(self, obj: Any) -> None:
        pass


class QueuePipe:
    def __init__(self, queue: queue.Queue[KernelMessage]):
        self._queue = queue

    def send(self, obj: Any) -> None:
        self._queue.put_nowait(obj)


class ThreadSafeStream(Stream):
    """A thread-safe wrapper around a pipe.

    Does not own the pipe or queue.
    """

    def __init__(
        self,
        pipe: PipeProtocol,
        input_queue: QueueType[str],
        redirect_console: bool,
        cell_id: Optional[CellId_t] = None,
    ):
        self.pipe = pipe
        self.cell_id = cell_id
        self.redirect_console = redirect_console
        # A single stream is shared by the kernel and the code completion
        # worker. The lock should almost always be uncontended.
        self.stream_lock = threading.Lock()

        if self.redirect_console:
            # Console outputs are buffered
            self.console_msg_cv = threading.Condition(threading.Lock())
            self.console_msg_queue: deque[ConsoleMsg | None] = deque()
            self.buffered_console_thread = threading.Thread(
                target=buffered_writer,
                args=(self.console_msg_queue, self, self.console_msg_cv),
            )
            self.buffered_console_thread.start()

        # stdin messages are pulled from this queue
        self.input_queue = input_queue

    def write(self, op: str, data: dict[Any, Any]) -> None:
        with self.stream_lock:
            try:
                self.pipe.send((op, data))
            except OSError as e:
                # Most likely a BrokenPipeError, caused by the
                # server process shutting down
                LOGGER.debug("Error when writing (op: %s) to pipe: %s", op, e)

    def stop(self) -> None:
        """Teardown resources created by the stream."""
        # Sending `None` through the queue signals the console thread to exit.
        # We don't join the thread in case its processing outputs still; don't
        # want to block the entire program.
        if self.redirect_console:
            self.console_msg_queue.append(None)
            with self.console_msg_cv:
                self.console_msg_cv.notify()


def _forward_os_stream(
    standard_stream: Stdout | Stderr, fd: int, should_exit: threading.Event
) -> None:
    """Watch a file descriptor and forward it to a stream object."""

    # This coarse try/except block silences exceptions; a raised exception
    # at this point could cause bad errors, such as an infinite stream of data
    # to be written to the fd/routed through the stream.
    #
    # TODO(akshayka): Make this loop bomb-proof, so that exceptions raised are
    # exceptions we actually want to pay attention to; then store the exception
    # and print it to the terminal later (outside an execution context).
    try:
        while not should_exit.is_set():
            data = os.read(fd, 1024)
            if not data:
                break
            standard_stream.write(data.decode())
    except Exception:
        ...


class Watcher:
    """Watches and redirects a standard stream."""

    def __init__(
        self, standard_stream: ThreadSafeStdout | ThreadSafeStderr
    ) -> None:
        self.standard_stream = standard_stream
        self.fd = self.standard_stream._original_fd
        self.read_fd, self.write_fd = os.pipe()
        self._should_exit = threading.Event()
        self.thread = threading.Thread(
            target=_forward_os_stream,
            args=(self.standard_stream, self.read_fd, self._should_exit),
            daemon=True,
        )
        self.thread.start()

    def start(self) -> None:
        # Save the file for the standard stream by opening a new file
        # descriptor for it
        self.fd_dup = os.dup(self.fd)
        self.standard_stream._set_fileno(self.fd_dup)
        # Change the original file descriptor for the standard stream
        # to refer to the write end of the pipe
        os.dup2(self.write_fd, self.fd)

    def pause(self) -> None:
        # Restore the original file descriptor to point to the standard
        # stream file
        os.dup2(self.fd_dup, self.fd)
        os.close(self.fd_dup)
        self.standard_stream._set_fileno(None)

    def stop(self) -> None:
        os.close(self.write_fd)
        os.close(self.read_fd)
        self._should_exit.set()


# NB: Python doesn't provide a standard out class to inherit from, so
# we inherit from TextIOBase.
class ThreadSafeStdout(Stdout):
    encoding = sys.stdout.encoding
    errors = sys.stdout.errors
    _fileno: int | None = None

    def __init__(self, stream: ThreadSafeStream):
        self._stream = stream
        self._original_fd = sys.stdout.fileno()
        self._watcher = Watcher(self)

    def stop(self) -> None:
        self._watcher.stop()

    def fileno(self) -> int:
        if self._fileno is not None:
            return self._fileno
        raise io.UnsupportedOperation("Stream not redirected, no fileno.")

    def _set_fileno(self, fileno: int | None) -> None:
        self._fileno = fileno

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def flush(self) -> None:
        # TODO(akshayka): maybe force the buffered writer to write
        return

    def _write_with_mimetype(
        self, data: str, mimetype: ConsoleMimeType
    ) -> int:
        assert self._stream.cell_id is not None
        if not isinstance(data, str):
            raise TypeError(
                f"write() argument must be a str, not {type(data).__name__}"
            )
        max_bytes = std_stream_max_bytes()
        if sys.getsizeof(data) > max_bytes:
            sys.stderr.write(
                "Warning: marimo truncated a very large console output.\n"
            )
            data = data[: int(max_bytes)] + " ... "
        self._stream.console_msg_queue.append(
            ConsoleMsg(
                stream=CellChannel.STDOUT,
                cell_id=self._stream.cell_id,
                data=data,
                mimetype=mimetype,
            )
        )
        with self._stream.console_msg_cv:
            self._stream.console_msg_cv.notify()
        return len(data)

    # Buffer type not available python < 3.12, hence type ignore
    def writelines(self, sequence: Iterable[str]) -> None:  # type: ignore[override] # noqa: E501
        for line in sequence:
            self.write(line)


class ThreadSafeStderr(Stderr):
    encoding = sys.stderr.encoding
    errors = sys.stderr.errors
    _fileno: int | None = None

    def __init__(self, stream: ThreadSafeStream):
        self._stream = stream
        self._original_fd = sys.stderr.fileno()
        self._watcher = Watcher(self)

    def stop(self) -> None:
        self._watcher.stop()

    def fileno(self) -> int:
        if self._fileno is not None:
            return self._fileno
        raise io.UnsupportedOperation("Stream not redirected, no fileno.")

    def _set_fileno(self, fileno: int | None) -> None:
        self._fileno = fileno

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def flush(self) -> None:
        # TODO(akshayka): maybe force the buffered writer to write
        return

    def _write_with_mimetype(
        self, data: str, mimetype: ConsoleMimeType
    ) -> int:
        assert self._stream.cell_id is not None
        if not isinstance(data, str):
            raise TypeError(
                f"write() argument must be a str, not {type(data).__name__}"
            )
        max_bytes = std_stream_max_bytes()
        if sys.getsizeof(data) > max_bytes:
            data = (
                "Warning: marimo truncated a very large console output.\n"
                + data[: int(max_bytes)]
                + " ... "
            )

        with self._stream.console_msg_cv:
            self._stream.console_msg_queue.append(
                ConsoleMsg(
                    stream=CellChannel.STDERR,
                    cell_id=self._stream.cell_id,
                    data=data,
                    mimetype=mimetype,
                )
            )
            self._stream.console_msg_cv.notify()
        return len(data)

    def writelines(self, sequence: Iterable[str]) -> None:  # type: ignore[override] # noqa: E501
        for line in sequence:
            self.write(line)


class ThreadSafeStdin(Stdin):
    """Implements a subset of stdin."""

    encoding = sys.stdin.encoding
    errors = sys.stdin.errors

    def __init__(self, stream: ThreadSafeStream):
        self._stream = stream

    def fileno(self) -> int:
        raise io.UnsupportedOperation(
            "marimo's stdin is a pseudofile, which has no fileno."
        )

    def writable(self) -> bool:
        return False

    def readable(self) -> bool:
        return True

    def _readline_with_prompt(self, prompt: str = "") -> str:
        """Read input from the standard in stream, with an optional prompt."""
        assert self._stream.cell_id is not None
        if not isinstance(prompt, str):
            raise TypeError(
                f"prompt must be a str, not {type(prompt).__name__}"
            )

        max_bytes = std_stream_max_bytes()
        if sys.getsizeof(prompt) > max_bytes:
            prompt = (
                "Warning: marimo truncated a very large console output.\n"
                + prompt[: int(max_bytes)]
                + " ... "
            )

        with self._stream.console_msg_cv:
            # This sends a prompt request to the frontend.
            self._stream.console_msg_queue.append(
                ConsoleMsg(
                    stream=CellChannel.STDIN,
                    cell_id=self._stream.cell_id,
                    data=prompt,
                    mimetype="text/plain",
                )
            )
            self._stream.console_msg_cv.notify()

        return self._stream.input_queue.get()

    def readline(self, size: int | None = -1) -> str:  # type: ignore[override]  # noqa: E501
        # size only included for compatibility with sys.stdin.readline API;
        # we don't support it.
        del size
        return self._readline_with_prompt(prompt="")

    def readlines(self, hint: int | None = -1) -> list[str]:  # type: ignore[override]  # noqa: E501
        # Just an alias for readline.
        #
        # hint only included for compatibility with sys.stdin.readlines API;
        # we don't support it.
        del hint
        return self._readline_with_prompt(prompt="").split("\n")


@contextlib.contextmanager
def redirect(standard_stream: Stdout | Stderr) -> Iterator[None]:
    """Redirect a standard stream to the frontend."""
    try:
        if isinstance(standard_stream, ThreadSafeStdout) or isinstance(
            standard_stream, ThreadSafeStderr
        ):
            standard_stream._watcher.start()
        yield
    finally:
        if isinstance(standard_stream, ThreadSafeStdout) or isinstance(
            standard_stream, ThreadSafeStderr
        ):
            standard_stream._watcher.pause()
