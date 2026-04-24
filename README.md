# Crate Digger

Desktop sample discovery and vault management app for music producers.

## Quick Start

1. Create and activate a Python 3.11/3.12 virtual environment.
2. Install dependencies:
   - `pip install -r requirements.txt`
3. Run the app:
   - `python main.py`

## Project Layout

- `core/`: ingest, analysis, metadata, queueing, DB, export
- `ui/`: CustomTkinter application shell, components, tabs
- `utils/`: config, ffmpeg setup, path helpers
- `build/`: PyInstaller spec and hooks