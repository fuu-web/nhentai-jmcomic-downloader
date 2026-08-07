# Project Layout

The legacy scripts remain at the project root for compatibility:

- `unified_gui.py`: current GUI and orchestration layer.
- `nhentai_crawler.py`: standalone NHentai CLI.
- `utils.py`: shared filename and download-index helpers.
- `jmcomic/`: bundled JM client and downloader implementation.
- `entrypoints/start_gui.py`: stable GUI entry point.
- `start.bat`: Windows one-click launcher with dependency checks.
- `tests/`: offline smoke tests for persistence, cancellation, and image validation.

The next structural refactor should move network clients, download services,
and Tk widgets into separate packages. The root scripts should remain thin
compatibility wrappers until that refactor has tests.
