# Crate Digger

Crate Digger is a desktop music-ingest and vault-management application for producers and DJs.
It turns a source URL into a fully tagged, analyzed track in a structured local library, with optional stem separation and queue-based background processing.

## What It Does

- Queues YouTube and YouTube Music URLs for ingestion.
- Downloads audio and artwork, then writes embedded metadata.
- Analyzes BPM, musical key, and Camelot key.
- Organizes tracks into a deterministic vault folder structure.
- Indexes tracks in SQLite for fast browsing and filtering.
- Supports optional Demucs-based stem separation.
- Provides a responsive CustomTkinter UI with event-driven queue updates.

## Pipeline Overview

Each queued job runs through the following stages:

1. Download audio
2. Analyze BPM and key
3. Fetch and process artwork
4. Write metadata tags
5. Relocate into vault structure
6. Index in SQLite
7. Optionally separate stems

Progress is emitted as typed queue events so the UI updates in real time without polling.

## Architecture

### Main Modules

- core/: ingestion pipeline, queue manager, downloader, analyzer, metadata, artwork, database, stems, discovery
- ui/: application shell, tabs, reusable UI components, theme tokens, event bridge
- utils/: config management, ffmpeg provisioning, path helpers
- scripts/: manual test scripts and shell smoke checks

### Runtime Model

- UI thread: CustomTkinter app shell and tab rendering
- Worker threads: queue manager executes ingestion jobs concurrently
- Event bus: worker events marshalled to UI safely via a bridge

## Requirements

- Windows, macOS, or Linux
- Python 3.11+ (3.11/3.12 recommended)
- FFmpeg is auto-provisioned via imageio-ffmpeg

## Installation

1. Clone the repository.
2. Create and activate a virtual environment.
3. Install dependencies.

```bash
pip install -r requirements.txt
```

## Run The App

```bash
python main.py
```

On first launch, Crate Digger creates application data files automatically (config, logs, database).

## Configuration

Configuration is persisted via the in-app settings and stored as JSON.

- General: vault root, staging root, worker count, default stems toggle
- Downloader: retry and fragment controls
- Stems: model and device preferences
- Discovery: Discogs token state and discovery defaults
- UI: window size and last active tab

Sensitive values are stored through OS keyring when available, with a fallback strategy when keyring is not present.

## Development Notes

- Entry point: main.py
- Primary app shell: ui/app.py
- Pipeline orchestrator: core/pipeline.py
- Queue engine: core/queue_manager.py

The codebase is structured to keep UI concerns separate from ingestion logic and to preserve responsiveness under concurrent workloads.

## Troubleshooting

### App Fails To Start

- Confirm the virtual environment is active.
- Reinstall dependencies from requirements.txt.
- Check the log file in the app data directory for traceback details.

### Jobs Stall Or Fail

- Verify network access for source URLs.
- Confirm source URL format is supported.
- Reduce worker count in settings if the system is resource constrained.

### Stem Separation Is Slow

- This is expected on CPU-only systems.
- Use a smaller model or disable stems by default if throughput is more important.

### Stem Separation Fails (TorchCodec Or diffq Errors)

- If you see errors mentioning TorchCodec, your torchaudio runtime cannot encode/decode audio for Demucs.
- Recreate the exact pinned stack used by this project:

```bash
pip install -r requirements.txt
```

- Run the runtime verifier before testing Manual Rip:

```bash
python scripts/check_stems_runtime.py
```

- On Windows, ensure Microsoft Visual C++ Redistributable x64 is installed and up to date.

- If you selected the mdx_extra_q model, install diffq or switch to htdemucs/mdx_extra:

```bash
pip install diffq
```

## Contributing

1. Create a branch from main.
2. Keep changes focused and testable.
3. Run relevant script-based checks from scripts/ before opening a PR.
4. Submit a clear PR description with behavior changes and verification steps.

## License

No license file is currently defined in this repository.
