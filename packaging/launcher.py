"""Top-level launcher for PyInstaller bundles.

Wrapping cove_video_downloader.py lets PyInstaller treat the main script as
a library module rather than as the program's `__main__`, which avoids
subtle reimport issues when the process is relaunched.
"""
import runpy
import sys


def main():
    runpy.run_module("cove_video_downloader", run_name="__main__")


if __name__ == "__main__":
    raise SystemExit(main())
