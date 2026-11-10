# YTScrape

Modern desktop app for downloading YouTube videos, Shorts, playlists and channels as
stable, widely compatible local files. It can also **split** a video into one file per timestamp, or **chain** several videos into one file
with **auto-generated chapters and timestamps**.
Powered by [yt-dlp](https://github.com/yt-dlp/yt-dlp) + ffmpeg, UI built with CustomTkinter.

## Run

Double-click **`YTScrape.bat`**. The first run creates `.venv` and installs dependencies.

Manual:
```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python ytscrape.py
```

## Using it

1. **Paste links** (several at once, separated by spaces) and press **Enter** / *Add & Preview*.
   Each video appears as a preview card with thumbnail, title, channel, views, date and length.
   Playlists and channels expand into one card per video (limit set in *Options*).
   Click a thumbnail to open the video in your browser.
2. **Tick** the items you want, and reorder them with ▲ ▼.
3. Choose **Video** (MP4/MKV/WebM, 360p to 4K) or **Audio** (MP3/M4A/Opus/FLAC/WAV) in the *Format* tab.
4. Press **Download**.

### Chaining + timestamps

In the **Chain** tab, turn on *Chain selected items*:

- Each card gets an editable **Chapter** field, pre-filled from the video title.
  *Clean titles* strips noise such as "(Official Music Video)", "[4K Remastered]", "| Lyrics" and hashtags.
- The **Timestamps** box updates live as you tick, reorder or rename, using the videos' lengths.
- *Download & Chain* downloads every ticked item, then joins them **in list order** into one file:
  - `My Mix.mp4` / `My Mix.mp3` (etc.) with one embedded **chapter per item**
  - `My Mix.timestamps.txt` with the exact timestamps, ready to paste into a YouTube description:
    ```
    00:00 First Song
    03:41 Second Song
    07:12 Third Song
    ```
- Sources are normalised during the join, so videos with different resolutions or codecs combine cleanly.
  Video becomes H.264/AAC (16:9, letterboxed, 30 fps); audio uses the format and bitrate you chose.
- Individual part files are deleted afterwards unless *Keep individual files* is on.

### Splitting by timestamps

In the **Split** tab, turn on *Split into parts*. This is useful for full albums, mixes and long sets:

- YTScrape reads each video's **chapters**. If there are none, it looks for **timestamps in the description**.
  Each card shows how many parts it found (e.g. `✂ 12 parts`).
- **✂ Edit timestamps** opens an editor where you can fix titles, add or remove parts,
  or paste your own list (from a pinned comment, for example). It understands most layouts:
  `00:00 Intro`, `1:02:03 - Song`, `[03:45] Title`, `Song | 10:01`, `1. 03:45 Title`, `00:00 - 03:45 Song`.
- *Download & Split* saves one file per part in a **folder named after the video**:
  ```
  Downloads/YTScrape/Album Name/
      01 - Intro.opus
      02 - First Song.opus
      03 - Second Song.opus
  ```
- Part names can be `01 - Title`, `Title`, or `01 - 00.03.45 - Title`.
- Each part is tagged with its title, track number (e.g. 2/12), album (the video title) and artist (the channel).
- Audio is cut without re-encoding, so there is no quality loss. Video parts are re-encoded to H.264 so cuts are exact.
- *Keep the full file too* moves the original into the folder next to its parts. Otherwise the original is deleted.
- Videos with no timestamps are kept as a single file.

Chain and Split can't be used together; turning one on switches the other off.

### Other features

- "Max compatibility" (default) downloads H.264 + AAC MP4 files, which play everywhere.
- Embeds cover art, metadata, chapters and optional subtitles.
- Can save each video's full info `.json` and description.
- Automatic retries, resumable downloads, and an archive option to skip videos already downloaded.
- Can use your browser's cookies for age-restricted or members-only videos you have access to.
- Light, dark or system theme. Settings are remembered in `settings.json`.
- *Update yt-dlp* button in *Options*. If downloads start failing, update first, because YouTube changes often.

## Files

- `ytscrape.py`: the GUI
- `core.py`: yt-dlp options, link inspection, downloading, timestamp parsing, and ffmpeg chaining/splitting (no GUI code)

## Requirements

- Python 3.10+
- ffmpeg: uses the one on PATH, otherwise the bundled `imageio-ffmpeg` binary (copied into `bin/`)
- Node.js or Deno (recommended): yt-dlp uses it to solve YouTube's JS challenges

Only download content you have the right to download.
