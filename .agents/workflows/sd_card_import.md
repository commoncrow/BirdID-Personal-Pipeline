---
description: SD Card Import, Archive & eBird Processing Workflow
---
# SD Card Import & eBird Processing Workflow

> **Windows / PowerShell note:** Do NOT use `PYTHONIOENCODING=utf-8 uv run ...` syntax — that is bash-only.
> On Windows PowerShell always prefix commands as:
> ```powershell
> $env:PYTHONIOENCODING='utf-8'; uv run python ...
> ```

This workflow monitors for an SD card insertion from a supported camera (Nikon P900/P1000 or Sony A6600), imports photos and videos to the correct Dropbox destination, archives the card, and kicks off the eBird processing pipeline.

---

## Supported Cameras & File Types

| Camera | File Types | RAW? |
|--------|-----------|------|
| Nikon P900 | `.JPG` | No (JPEG only) |
| Nikon P1000 | `.JPG` | No (JPEG only) |
| Sony A6600 | `.ARW` + `.JPG` | Yes (RAW + JPEG) |

**Video files:** `.MP4`, `.MOV`, `.AVI` — all cameras.

---

## Destination Structure

The workflow tries the **primary destination** first. If there is not enough free space, it automatically offers the **alternate destination** and asks the user to confirm before proceeding.

| Priority | Path | Notes |
|----------|------|-------|
| Primary  | `D:\Dropbox\Snaps & Videos\Sony\2026` | Synced Dropbox folder |
| Alternate | `E:\2026` | Local high-capacity drive |

```
<DEST_BASE>\                        ← resolved in Step 4 (primary or alternate)
└── <PlaceName>\                   ← top-level folder named by location
    ├── <photos — ARW/JPG copied here>
    └── videos\
        └── <YYYY-MM-DD>\          ← one subfolder per shooting date
            └── <video files>
```

The `<PlaceName>` folder is created fresh for this import session. Photos are copied flat into `<PlaceName>\`. Videos are nested by shooting date inside `<PlaceName>\videos\<YYYY-MM-DD>\`.

---

## Step 1: Monitor for SD Card Insertion

Poll the available drive letters every 5 seconds using PowerShell/Python. A valid SD card must contain a `DCIM` folder.

```python
PYTHONIOENCODING=utf-8 uv run python -c "
import os, time, string

def get_drives():
    return [f'{d}:\\' for d in string.ascii_uppercase if os.path.exists(f'{d}:\\')]

known = set(get_drives())
print('Waiting for SD card insertion... (Ctrl+C to cancel)')
while True:
    current = set(get_drives())
    new = current - known
    for drive in new:
        if os.path.isdir(os.path.join(drive, 'DCIM')):
            print(f'SD card detected: {drive}')
            exit(0)
    known = current
    time.sleep(5)
"
```

Once a drive with `DCIM` is detected, proceed. Record the SD card root as `<SD_ROOT>` (e.g., `E:\`).

---

## Step 2: Identify Camera Type

Inspect the DCIM folder structure and file extensions to determine the camera:

```python
PYTHONIOENCODING=utf-8 uv run python -c "
import os, glob

sd_root = r'<SD_ROOT>'
dcim = os.path.join(sd_root, 'DCIM')

arw_files = glob.glob(os.path.join(dcim, '**', '*.ARW'), recursive=True)
jpg_files = glob.glob(os.path.join(dcim, '**', '*.JPG'), recursive=True) + \
            glob.glob(os.path.join(dcim, '**', '*.jpg'), recursive=True)
mp4_files = glob.glob(os.path.join(dcim, '**', '*.MP4'), recursive=True) + \
            glob.glob(os.path.join(dcim, '**', '*.MOV'), recursive=True) + \
            glob.glob(os.path.join(dcim, '**', '*.AVI'), recursive=True)

# Detect Sony folder structure (SONY/ARW subfolders) or Nikon (100NIKON, NIKON etc.)
subdirs = [d.upper() for d in os.listdir(dcim) if os.path.isdir(os.path.join(dcim, d))]
is_sony = any('SONY' in d or 'ARW' in d for d in subdirs) or len(arw_files) > 0
is_nikon = any('NIKON' in d for d in subdirs) or (not is_sony and len(jpg_files) > 0)

camera = 'Sony A6600' if is_sony else 'Nikon P900/P1000'
print(f'Detected camera: {camera}')
print(f'Photos: {len(jpg_files)} JPG, {len(arw_files)} ARW')
print(f'Videos: {len(mp4_files)} video files')
"
```

Report the detected camera and file counts to the user before continuing. If both ARW and Nikon patterns are absent and the card is ambiguous, ask the user to confirm the camera model.

---

## Step 3: Detect Place Name from Geo Tags

Try to extract a place name from GPS EXIF data in the first few photos. Prefer `.ARW` for Sony, `.JPG` for Nikon.

```python
PYTHONIOENCODING=utf-8 uv run python -c "
import subprocess, glob, os, json

et_path = 'C:/workspace/SuperPicky/exiftools_win/exiftool.exe'
sd_root = r'<SD_ROOT>'
dcim = os.path.join(sd_root, 'DCIM')

# Sample up to 5 photos
photos = (
    glob.glob(os.path.join(dcim, '**', '*.ARW'), recursive=True) +
    glob.glob(os.path.join(dcim, '**', '*.JPG'), recursive=True)
)[:5]

coords = None
for photo in photos:
    result = subprocess.run(
        [et_path, '-GPSLatitude', '-GPSLongitude', '-n', '-j', photo],
        capture_output=True, text=True, encoding='utf-8'
    )
    try:
        data = json.loads(result.stdout)
        if data and 'GPSLatitude' in data[0] and 'GPSLongitude' in data[0]:
            coords = (data[0]['GPSLatitude'], data[0]['GPSLongitude'])
            break
    except:
        pass

if coords:
    lat, lon = coords
    print(f'GPS found: {lat:.4f}, {lon:.4f}')
    # Reverse geocode using Nominatim (no API key required)
    import urllib.request, urllib.parse
    url = f'https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json&zoom=10'
    req = urllib.request.Request(url, headers={'User-Agent': 'SuperPicky-Workflow/1.0'})
    geo = json.loads(urllib.request.urlopen(req).read())
    addr = geo.get('address', {})
    place = addr.get('suburb') or addr.get('town') or addr.get('city') or addr.get('county') or addr.get('state', 'Unknown')
    print(f'Suggested place name: {place}')
else:
    print('No GPS data found in sampled photos.')
    place = None
"
```

Present the suggestion to the user:
- If a geo-tag place name was found (e.g., `Bharatpur`), say: **"Detected location from GPS: `<place>`. Use this as the folder name, or enter a different name?"**
- If no GPS: **"No GPS data found. What would you like to name this location folder? (e.g., `Bharatpur`, `Kaziranga_Trip`)"**

Wait for user input. Sanitize the response: replace spaces with `_`, strip special characters. Record as `<PlaceName>`.

**Full destination path:** `D:\Dropbox\Snaps & Videos\Sony\2026\<PlaceName>`

---

## Step 4: Check Available Disk Space & Resolve Destination

Check the **primary destination** first. If it has insufficient space, automatically check the **alternate destination** and ask the user to confirm before switching. The resolved path is recorded as `<DEST_BASE>` for all subsequent steps.

```python
PYTHONIOENCODING=utf-8 uv run python -c "
import os, shutil

sd_root   = r'<SD_ROOT>'
DESTINATIONS = [
    (r'D:\Dropbox\Snaps & Videos\Sony\2026', 'Primary (Dropbox, D:)'),
    (r'E:\2026',                              'Alternate (local drive, E:)'),
]

# Total size of DCIM
total_bytes = 0
for dirpath, dirnames, filenames in os.walk(os.path.join(sd_root, 'DCIM')):
    for f in filenames:
        fp = os.path.join(dirpath, f)
        try:
            total_bytes += os.path.getsize(fp)
        except:
            pass
total_gb = total_bytes / 1024**3
print('SD card size: ' + str(round(total_gb, 2)) + ' GB')

for path, label in DESTINATIONS:
    try:
        free_bytes = shutil.disk_usage(path).free
        free_gb    = free_bytes / 1024**3
        ok = free_bytes >= total_bytes * 1.05
        status = 'SPACE_OK' if ok else 'INSUFFICIENT'
        print(label + ' | ' + path + ' | free=' + str(round(free_gb,2)) + ' GB | ' + status)
    except Exception as e:
        print(label + ' | ' + path + ' | ERROR: ' + str(e))
"
```

**Resolution logic:**
- If **primary has enough space**: use primary. Set `<DEST_BASE>` = `D:\Dropbox\Snaps & Videos\Sony\2026`.
- If **primary is insufficient but alternate has space**: report both sizes to the user, e.g.:
  > ⚠️ Primary (D:) has only X GB free — not enough for Y GB of photos.
  > Alternate (E:\2026) has Z GB free ✅. Switch to E:\2026?
  Wait for user confirmation before proceeding. Set `<DEST_BASE>` = `E:\2026`.
- If **both are insufficient**: stop entirely and report sizes for both drives. Ask the user to free space or provide a different path.
- If the user provides a **custom path** at any point, use that as `<DEST_BASE>` after verifying it has sufficient space.

---

## Step 5: Copy Photos to Destination

Create the destination folder and copy all photo files (JPG + ARW) from the SD card's DCIM tree into `<PlaceName>\` flat (no subdirectory nesting for photos).

Use the `<DEST_BASE>` resolved in Step 4.

```python
PYTHONIOENCODING=utf-8 uv run python -c "
import os, glob, shutil

sd_root   = r'<SD_ROOT>'
dest_dir  = r'<DEST_BASE>\<PlaceName>'
dcim      = os.path.join(sd_root, 'DCIM')

os.makedirs(dest_dir, exist_ok=True)

photo_exts = {'.jpg', '.jpeg', '.arw', '.nef', '.dng'}
all_files  = glob.glob(os.path.join(dcim, '**', '*'), recursive=True)
photos     = [f for f in all_files if os.path.isfile(f) and os.path.splitext(f)[1].lower() in photo_exts]
photos.sort()

total = len(photos)
copied = 0
skipped = 0
for i, src in enumerate(photos):
    dest = os.path.join(dest_dir, os.path.basename(src))
    # Handle filename collisions (different subfolders, same filename)
    if os.path.exists(dest):
        base, ext = os.path.splitext(os.path.basename(src))
        counter = 1
        while os.path.exists(dest):
            dest = os.path.join(dest_dir, f'{base}_{counter:03d}{ext}')
            counter += 1
    try:
        shutil.copy2(src, dest)
        copied += 1
    except Exception as e:
        print(f'  ERROR copying {src}: {e}')
        skipped += 1
    if (i + 1) % 50 == 0 or (i + 1) == total:
        print(f'  Photos: [{i+1}/{total}]')

print(f'Done. {copied} photos copied, {skipped} skipped.')
"
```

Report progress every 50 files as `[N/Total]`.

---

## Step 6: Copy Videos to Destination (Ordered by Date)

Copy all video files into `<PlaceName>\videos\<YYYY-MM-DD>\` subfolders, grouped by their shooting date from EXIF or file modification date.

```python
PYTHONIOENCODING=utf-8 uv run python -c "
import os, glob, shutil, subprocess, json
from datetime import datetime

sd_root   = r'<SD_ROOT>'
dest_dir  = r'<DEST_BASE>\<PlaceName>'
dcim      = os.path.join(sd_root, 'DCIM')
et_path   = 'C:/workspace/SuperPicky/exiftools_win/exiftool.exe'

video_exts = {'.mp4', '.mov', '.avi', '.mts', '.m2ts'}
all_files  = glob.glob(os.path.join(dcim, '**', '*'), recursive=True)
videos     = [f for f in all_files if os.path.isfile(f) and os.path.splitext(f)[1].lower() in video_exts]
videos.sort()

if not videos:
    print('No video files found. Skipping video copy.')
    exit(0)

print(f'Found {len(videos)} video files. Reading dates...')

total = len(videos)
copied = 0
for i, src in enumerate(videos):
    # Try EXIF date first
    date_str = None
    try:
        result = subprocess.run(
            [et_path, '-DateTimeOriginal', '-CreateDate', '-j', src],
            capture_output=True, text=True, encoding='utf-8'
        )
        data = json.loads(result.stdout)
        if data:
            raw_date = data[0].get('DateTimeOriginal') or data[0].get('CreateDate')
            if raw_date:
                dt = datetime.strptime(raw_date[:10], '%Y:%m:%d')
                date_str = dt.strftime('%Y-%m-%d')
    except:
        pass

    # Fall back to file modification date
    if not date_str:
        mtime = os.path.getmtime(src)
        date_str = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d')

    date_folder = os.path.join(dest_dir, 'videos', date_str)
    os.makedirs(date_folder, exist_ok=True)

    dest = os.path.join(date_folder, os.path.basename(src))
    if os.path.exists(dest):
        base, ext = os.path.splitext(os.path.basename(src))
        counter = 1
        while os.path.exists(dest):
            dest = os.path.join(date_folder, f'{base}_{counter:03d}{ext}')
            counter += 1

    try:
        shutil.copy2(src, dest)
        copied += 1
    except Exception as e:
        print(f'  ERROR copying {src}: {e}')

    if (i + 1) % 10 == 0 or (i + 1) == total:
        print(f'  Videos: [{i+1}/{total}] → {date_str}')

print(f'Done. {copied} video files copied.')
"
```

After completing, report how many date subfolders were created and list them (e.g., `videos/2026-05-22/` — 3 files, `videos/2026-05-23/` — 7 files).

---

## Step 7: Verify Copy Integrity

Do a basic file-count verification — confirm the number of files copied to destination matches what was on the card:

```python
PYTHONIOENCODING=utf-8 uv run python -c "
import os, glob

sd_root  = r'<SD_ROOT>'
dest_dir = r'<DEST_BASE>\<PlaceName>'
dcim     = os.path.join(sd_root, 'DCIM')

photo_exts = {'.jpg', '.jpeg', '.arw', '.nef', '.dng'}
video_exts = {'.mp4', '.mov', '.avi', '.mts', '.m2ts'}

all_sd = glob.glob(os.path.join(dcim, '**', '*'), recursive=True)
sd_photos = [f for f in all_sd if os.path.isfile(f) and os.path.splitext(f)[1].lower() in photo_exts]
sd_videos = [f for f in all_sd if os.path.isfile(f) and os.path.splitext(f)[1].lower() in video_exts]

dest_photos = [f for f in glob.glob(os.path.join(dest_dir, '*')) if os.path.isfile(f) and os.path.splitext(f)[1].lower() in photo_exts]
dest_videos = [f for f in glob.glob(os.path.join(dest_dir, 'videos', '**', '*'), recursive=True) if os.path.isfile(f) and os.path.splitext(f)[1].lower() in video_exts]

print(f'Photos  — SD: {len(sd_photos)}, Dest: {len(dest_photos)}  {\"OK\" if len(sd_photos) == len(dest_photos) else \"MISMATCH\"}')
print(f'Videos  — SD: {len(sd_videos)}, Dest: {len(dest_videos)}  {\"OK\" if len(sd_videos) == len(dest_videos) else \"MISMATCH\"}')
"
```

If counts match, proceed. If there is a MISMATCH, **stop and warn the user** — do not archive the SD card until counts are confirmed. Ask the user whether to retry or proceed anyway.

---

## Step 8: Archive SD Card Contents

Move all DCIM content into an `archived_<YYYYMMDD>` folder on the SD card itself. This preserves the originals on-card while clearing the active DCIM structure.

Get today's date:
```python
from datetime import date; print(date.today().strftime('%Y%m%d'))
```
Archive folder: `<SD_ROOT>\archived_<YYYYMMDD>` (e.g., `E:\archived_20260523`)

```python
PYTHONIOENCODING=utf-8 uv run python -c "
import os, shutil
from datetime import date

sd_root    = r'<SD_ROOT>'
today      = date.today().strftime('%Y%m%d')
archive    = os.path.join(sd_root, f'archived_{today}')

os.makedirs(archive, exist_ok=True)

# Move DCIM into archive
dcim_src = os.path.join(sd_root, 'DCIM')
dcim_dst = os.path.join(archive, 'DCIM')

if os.path.exists(dcim_src):
    shutil.move(dcim_src, dcim_dst)
    print(f'Moved DCIM → {dcim_dst}')

# Also move MISC / PRIVATE / other camera folders if present
for folder in os.listdir(sd_root):
    full = os.path.join(sd_root, folder)
    if os.path.isdir(full) and folder.upper() not in ['SYSTEM VOLUME INFORMATION', f'ARCHIVED_{today}'.upper()]:
        dst = os.path.join(archive, folder)
        shutil.move(full, dst)
        print(f'Moved {folder} → {dst}')

print(f'Archive complete: {archive}')
"
```

Confirm to the user: **"SD card archived to `<SD_ROOT>\archived_<YYYYMMDD>`. It is now safe to remove the card."**

---

## Step 9: Kick Off eBird Processing

Now invoke the `/ebird_processing` workflow with the newly imported photo folder as the target.

**Important pre-filled context for eBird processing:**

| Setting | Value |
|---------|-------|
| **Target Folder** | `<DEST_BASE>\<PlaceName>` |
| **Photo type** | **RAW (ARW)** if Sony A6600 · **JPEG only** if Nikon P900/P1000 |
| **Subdirectory structure** | Photos are at top level — no camera subfolders (flat copy from Step 5) |

Tell the user:
> **"Import complete! Ready to start eBird processing on `<PlaceName>`. The target folder is:**
> `<DEST_BASE>\<PlaceName>`
>
> I'll now ask the eBird processing questions. You can skip questions that don't apply."

Then begin the `/ebird_processing` workflow Setup Questions (Step 1 of that workflow), with `Target Folder` already filled in. Ask the remaining 10 configuration questions as normal.

**Camera-specific notes for eBird processing:**
- **Sony A6600**: Sharpening with `dxo` is preferred since paired `.ARW` files exist alongside JPEGs.
- **Nikon P900/P1000**: JPEG-only — `dxo` with PureRAW is not applicable (no RAW files). Recommend `unsharp` or `none`. Warn the user if they choose `dxo` with Nikon.

---

## Error Handling & Edge Cases

| Situation | Action |
|-----------|--------|
| SD card removed before copy completes | Stop immediately, report last file copied. Do NOT archive. Ask user to re-insert and retry. |
| Destination folder `<PlaceName>` already exists | Warn user: "Folder already exists. Append to it, rename with suffix `_2`, or cancel?" |
| No DCIM folder on detected drive | Skip — not a valid camera card. Keep monitoring. |
| Geo-reverse-geocode fails (no internet) | Fall back to asking the user directly. |
| File with same name exists in destination | Append `_001`, `_002`, etc. (handled in copy loops above). |
| Multiple cards inserted simultaneously | Process the first valid DCIM card detected; ignore others until this import completes. |
| Space check passes but fills up mid-copy | Catch `OSError`/`shutil.Error` on each file copy; stop and report how far the copy got. |
| Primary destination full, alternate also full | Stop entirely. Report both sizes. Ask user for a custom destination path. |
| User provides custom destination path | Verify the path exists and has enough space before accepting it as `<DEST_BASE>`. |
