# Crate Digger

Crate Digger is a desktop producer toolkit for MPC / boombap / hip-hop / lofi workflows. Paste a URL or dig Discogs for sample-friendly gems, preview them in-app, queue ingestion, and build a searchable local vault with BPM, key, stems, chops, and MPC-ready export.

## What It Does

### Manual Rip
- Queue YouTube and YouTube Music URLs for background ingestion.
- Optional AI title extraction (DeepSeek) to recover artist/title from messy video names.
- Optional Demucs stem separation.
- Live queue drawer with per-job progress.

### Digital Crate (Sample Discovery)
- **Reel discovery** — one Dig surfaces a batch of Discogs masters (configurable reel size).
- **In-app waveform preview** with play/pause, scrubbing, and volume control (no leaving the app).
- **Sample-friendly weighting** — funk, soul, jazz, library/OST, Brazilian, Greek gems (Rebetiko, Laïko, Entekhno, etc.) are prioritized without hard-excluding anything (roulette-style).
- **Era presets** — 70s Soul/Funk, 60s–70s Jazz, Greek 60s–80s, Library/OST, Brazilian, Spiritual Jazz, and more.
- **YouTube Music matching** for every reel card; queue uses cached preview audio when available.
- **Recent digs** history browser.

### Vault
- Virtualized library table with search, genre/BPM/key/rating/tag/crate filters.
- **Track inspector** — local waveform preview, star rating, tags, notes.
- **Crates** — user-defined collections for projects/moods.
- **Duplicate detection** (checksum + artist/title clustering).
- **Export to MPC** — batch WAV export to SD card or folder.
- **Export chop kit** — numbered one-shots + bar-aligned loops + `pad_map.json` for pad assignment.

### Analysis & Chopping
- BPM + musical key + Camelot detection.
- Half/double-time correction into a producer-friendly tempo window.
- Downbeat / bar grid for MPC-ready loop points.
- Onset-based chop detection and 1/2/4-bar loop suggestions (`core/chopper.py`).

### Export
- PCM WAV at 44.1 or 48 kHz, 16- or 24-bit (configurable in Settings).
- Full-track export and chop-kit export with MPC pad mapping.

## Pipeline Overview

Each queued job runs through:

1. Download audio (+ optional AI metadata enrichment)
2. Analyze BPM, key, rhythm grid
3. Fetch and embed artwork
4. Write metadata tags
5. Relocate into vault folder structure
6. Index in SQLite
7. Optionally separate stems (Demucs)

Progress is emitted as typed queue events so the UI updates in real time without polling.

## Architecture

| Area | Modules |
|------|---------|
| Ingestion | `core/pipeline.py`, `core/queue_manager.py`, `core/downloader.py` |
| Discovery | `core/discovery.py`, `core/sampling_taxonomy.py`, `core/preview.py` |
| Analysis | `core/analyzer.py`, `core/chopper.py` |
| Library | `core/database.py`, `core/exporter.py` |
| UI | `ui/app.py`, `ui/tabs/*`, `ui/components/*` |
| Config | `utils/config.py`, `utils/paths.py` |

### Runtime Model

- **UI thread** — CustomTkinter shell, tabs, waveform playback (`sounddevice`).
- **Worker threads** — queue manager, discovery digs, preview fetch/decode, export.
- **Event bridge** — thread-safe marshaling of queue events to the UI.

## Requirements

- Windows, macOS, or Linux
- Python 3.11+ (3.11/3.12 recommended)
- FFmpeg is auto-provisioned via `imageio-ffmpeg`
- **Discogs personal access token** — required for Digital Crate discovery ([get one here](https://www.discogs.com/settings/developers))
- **DeepSeek API key** (optional) — for AI title extraction

## Installation

```bash
git clone <repo-url>
cd crate-digger
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

## Run

**Windows (recommended):** double-click `Run Crate Digger.bat` in the project folder.  
On first launch it creates a `.venv`, installs dependencies, then starts the app.

```bash
python main.py
```

Or from a terminal after manual setup:

```bash
pip install -r requirements.txt
python main.py
```

On first launch, Crate Digger creates application data automatically (config, logs, SQLite vault index).

## Configuration

Settings are saved automatically as you change them.

| Section | Options |
|---------|---------|
| **Library** | Vault root, staging directory, folder naming scheme |
| **Ingestion** | Concurrent workers, default stems, AI metadata toggle |
| **Stems** | Demucs model, compute device |
| **Sample discovery** | Min collectors, reel size, sample prioritization + intensity, compilations toggle, preview volume |
| **MPC export** | Sample rate (44.1/48 kHz), bit depth (16/24-bit) |
| **API keys** | Discogs token, DeepSeek key |

Sensitive values use the OS keyring when available, with a restricted plaintext fallback otherwise.

### Discovery defaults

- `prioritize_samples`: on — tilts the reel toward sample-friendly genres/eras (including Greek boost).
- `sample_weight_intensity`: 0.6 — balance between Discogs desirability and sample affinity.
- `reel_size`: 8 cards per Dig.
- `allow_compilations`: off by default (many breaks live on comps — enable in Settings or Digital Crate).

## Typical Workflows

### Dig → Preview → Queue
1. Open **Digital Crate**, add your Discogs token in Settings if prompted.
2. Pick an era preset or set filters, click **Dig the crate**.
3. Preview cards in-app; queue favorites (preview cache speeds up ingestion).
4. Find finished tracks in **Vault**.

### Chop kit for MPC
1. In **Vault**, double-click a track (or select → Inspect).
2. Click **Export chop kit** and choose a destination folder.
3. Copy `Chops/`, `Loops/`, and `pad_map.json` to your MPC.

## Development

- Entry point: `main.py`
- App shell: `ui/app.py`
- Manual test scripts: `scripts/`

```bash
python scripts/test_db_and_discovery.py   # DB + discovery smoke test
python scripts/check_stems_runtime.py     # Demucs runtime check
```

## Troubleshooting

### Digital Crate shows “token required” after saving in Settings
Return to the Digital Crate tab (or click it again) — the warning clears automatically once the discovery engine is running. If it persists, verify the token in Settings → API keys and check the log.

### Dig button is disabled
Add a valid Discogs personal access token in Settings. The Dig button and era presets stay disabled until discovery is online.

### Jobs stall or fail
- Verify network access for source URLs.
- Reduce concurrent workers if CPU/RAM is constrained.

### Stem separation fails (TorchCodec / diffq)
```bash
pip install -r requirements.txt
python scripts/check_stems_runtime.py
```
On Windows, install Microsoft Visual C++ Redistributable x64. For `mdx_extra_q`, install `diffq` or switch to `htdemucs` / `mdx_extra` in Settings.

### Preview / waveform issues
Ensure FFmpeg is available (bundled via `imageio-ffmpeg` on most installs). Preview downloads are cached under the app data `preview_cache/` directory.

## Contributing

1. Branch from `main`.
2. Keep changes focused and testable.
3. Run relevant scripts in `scripts/` before opening a PR.
4. Describe behavior changes and verification steps in the PR.

## License

No license file is currently defined in this repository.
