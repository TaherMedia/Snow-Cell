#!/usr/bin/env python3
"""Launcher used by scripts/setup.py for every bot.

It runs <bot>/bot.py exactly like ``python bot.py`` (same working directory, same
sys.path[0], same sys.argv, ``__name__ == "__main__"``) but turns the Windows
CTRL_BREAK_EVENT (and SIGTERM on POSIX) into a KeyboardInterrupt, i.e. the same thing
that pressing Ctrl+C does. A bot that cleans up on Ctrl+C therefore also shuts down
gracefully when setup.py stops it. No bot code is modified or inspected.

Fallback for the rare case that the console signal cannot be delivered: with
``--stop-file PATH`` a small watcher thread raises the same KeyboardInterrupt as soon as
PATH exists (setup.py creates that file only when delivering the signal failed).

usage: python bot_runner.py [--stop-file PATH] <path/to/bot.py> [bot arguments...]
"""
import _thread
import os
import runpy
import signal
import sys
import threading
import time

STOP_FILE_POLL_SECONDS = 0.5


def _watch_stop_file(path: str) -> None:
    def watcher() -> None:
        while True:
            if os.path.exists(path):
                print("[bot_runner] stop file found; requesting a clean stop.", flush=True)
                _thread.interrupt_main()  # same effect as Ctrl+C in the main thread
                return
            time.sleep(STOP_FILE_POLL_SECONDS)

    threading.Thread(target=watcher, name="stop-file-watcher", daemon=True).start()


def main() -> int:
    args = sys.argv[1:]
    stop_file = ""
    if args and args[0] == "--stop-file":
        if len(args) < 2:
            print("usage: bot_runner.py [--stop-file PATH] <path/to/bot.py> [args...]", file=sys.stderr)
            return 2
        stop_file, args = args[1], args[2:]
    if not args:
        print("usage: bot_runner.py [--stop-file PATH] <path/to/bot.py> [args...]", file=sys.stderr)
        return 2
    target = os.path.abspath(args[0])
    if not os.path.isfile(target):
        print(f"bot_runner: script not found: {target}", file=sys.stderr)
        return 2
    folder = os.path.dirname(target)
    os.chdir(folder)
    sys.path.insert(0, folder)  # what `python bot.py` does
    sys.argv = [target] + args[1:]
    # SIGINT is included on purpose: a parent that started us with SIGINT ignored (nohup, a
    # background job) would otherwise make Python skip its own KeyboardInterrupt handler.
    for name in ("SIGINT", "SIGBREAK", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, signal.default_int_handler)
            except (ValueError, OSError):
                pass
    if stop_file:
        _watch_stop_file(stop_file)
    try:
        runpy.run_path(target, run_name="__main__")
    except KeyboardInterrupt:
        print("[bot_runner] stop requested (KeyboardInterrupt); exiting cleanly.", flush=True)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
