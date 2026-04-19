---
description: Complete eBird Processing & Upload Workflow
---
# eBird Processing & Verification Workflow

This workflow executes the complete end-to-end pipeline from a directory of raw photos, out to aesthetically cropped media mapped efficiently for eBird species upload.

### Initial Setup Options
When the user invokes this workflow, immediately ask them to provide the following configuration choices upfront:
1. **Target Folder**: Absolute path to the folder containing the raw bird photos.
2. **Skill Level Preset**: What is your skill level preset for initial rating? (beginner, intermediate, or master)
3. **BirdID Processing**: Do you want to run bird species identification on the "Other_Birds" folders? (yes/no)
4. **Crop Tool**: Do you want bird photos cropped to center the subject?
   - `yolo` — YOLO AI model crop with 80% padding margin (aesthetic, automatic).
   - `none` — Skip cropping entirely.
5. **Sharpening Tool**: Which sharpening tool do you want to use? This applies consistently to ALL sharpened outputs: upload_ready, flying_birds, and picked folders.
   - `dxo` — DXO PureRAW (RAW files only) or DXO PhotoLab (RAW + JPEG). Opens DXO GUI; user applies DeepPRIME settings manually then confirms to continue. Used wherever a paired RAW file exists; falls back to `unsharp` for JPEG-only photos.
   - `unsharp` — Built-in Unsharp Mask (automatic, works on any JPEG, no GUI).
   - `none` — Skip sharpening entirely across all steps.
   Before running, auto-detect installed DXO products under `C:/Program Files/DxO/` and warn the user if they chose `dxo` but only PureRAW is installed and photos include JPEGs without paired RAW.
6. **Highlights & Shadows**: Do you want automatic per-image tone correction applied to upload_ready photos? (yes/no)
   - Lifts dark shadows and pulls back blown highlights using OpenCV LAB colorspace curve adjustment.
7. **eBird Date**: What is the recording date for the eBird checklists? (e.g., "25 Dec 2025"). If not known, reply "auto" to detect from EXIF.
8. **Separate Flying Birds**: Do you want flying bird photos separated into their own `flying_birds` subfolder? (yes/no)
   - If yes: the chosen Sharpening Tool (question 5) is applied to these photos.
9. **Separate Picked/Flagged Photos**: Do you want Pick-flagged (top-rated) photos separated into their own `picked` subfolder? (yes/no)
   - If yes: the chosen Sharpening Tool (question 5) is applied to these photos.
10. **eBird Upload**: Do you want Chrome opened with your eBird checklist tabs (one per date) for media upload? (yes/no)
    - If yes: Chrome will be launched with remote debugging, you will log in to eBird, checklists matching photo dates will be found automatically, and identified species rows will be highlighted green in each tab.
11. **Aesthetic Scoring**: Do you want to run SigLIP-based aesthetic scoring across all star folders to find the best-looking photos and export a `top_aesthetic/` folder?
    - Uses `aesthetic-predictor-v2-5` (Google SigLIP-SO400M backbone). Scores all JPGs 1–10. Finds hidden gems in lower-rated folders that SuperPicky rejected on technical grounds.
    - If yes: ask the user for a score threshold:
      - `6.5` — very best only (~top 3–5%)
      - `6.0` — strong cut (~top 20%)
      - `5.5` — broad selection (~top 50%)
    - Output: `<Target>/top_aesthetic/` containing top-scoring JPGs + paired ARW files, filenames prefixed with score for easy sorting. Also writes per-folder JSON files and a merged `aesthetic_scores_all.json`.

---

### Pre-flight Checks (Run Before All Steps)

#### Check 1: Subdirectory Structure Detection
Before running any step, check whether the target folder contains photos directly or in camera subfolders (e.g., 100NIKON, 101NIKON, DCIM, etc.).
- If photos are in **camera subfolders**: run each step on each subfolder independently (Option A). Collect all output star folders across subfolders for downstream steps.
- If photos are at the **top level**: run steps on the target folder directly.

#### Check 2: UTF-8 Encoding (Windows)
Always prefix all `uv run python` commands with `PYTHONIOENCODING=utf-8` on Windows to prevent UnicodeEncodeError crashes from emoji/Chinese characters in console output.
```
PYTHONIOENCODING=utf-8 uv run python ...
```

#### Check 3: eBird Date Auto-Detection
If the user answered "auto" for eBird Date, scan the first JPG in the target folder (or subfolders) using Pillow to read the `DateTimeOriginal` EXIF tag. If multiple unique dates are found, list them all and ask the user which date(s) to use. Convert any selected dates to the format "DD Mon YYYY" (e.g., "24 Dec 2025").

#### Check 4: Skill Level to Threshold Mapping
The `--skill-level` flag does not exist in the CLI. Map the preset to explicit threshold flags:
- **beginner**: `-s 300 -n 4.5`
- **intermediate**: `-s 380 -n 4.8`
- **master**: `-s 520 -n 5.5`

---

### Execution Steps
Once the user confirms configurations, execute the following steps in sequence. After launching each long-running command, check progress approximately every 60 seconds and report back as `[photo X/Y = Z%]` until complete.

#### Step 1: Initial Photo Evaluation
Run SuperPicky to rate and organize the photos. Run on each camera subfolder if Option A applies.
```
PYTHONIOENCODING=utf-8 uv run python superpicky_cli.py process "<Subfolder_or_Target>" -s <sharpness> -n <nima>
```

**CRITICAL — avoid duplicate launches:**
- Run with `run_in_background=true` in the Bash tool. Do NOT also add `&` inside the command string — this spawns two concurrent instances on the same folder, causing "Move failed" errors and burst-merge deadlocks requiring manual process termination.
- Do NOT pipe the command through `| tail -N` — this blocks the task output file from receiving any content until the process exits, preventing live monitoring.

**Progress monitoring:** Do not rely on the task output file. Instead read the log file directly:
```
tail -3 "<Target_Folder>/superpicky.log"
```
Poll every 60 seconds and report as `[NNN/TTT = Z%]`. The log contains the latest processed filename and index.

#### Step 2: Bird Species Identification (If BirdID = yes)
Find all `Other_Birds` folders across all processed subfolders (check both `3star_excellent` and `2star_good`). Run birdid on each found path.
```
PYTHONIOENCODING=utf-8 uv run python birdid_cli.py organize "<path_to_Other_Birds>" -y --write-exif
```
Repeat for every `Other_Birds` folder found across all subfolders.

#### Step 3: Clean Up Output
Remove non-English character prefixes (e.g., Chinese) from species folder names and sync the manifest. Pass all `Other_Birds` paths found in Step 2 as arguments in a single call.
```
PYTHONIOENCODING=utf-8 uv run python scripts/ebird_tools/remove_chinese_names.py "<path1>" "<path2>" ...
```

#### Step 4: Unique Species List
Scan all species subfolders under every `Other_Birds` directory found. Write a combined `unique_bird_species.txt` file to `<Target_Folder>` listing every unique species name (one per line, sorted alphabetically).

#### Step 5: Segregate Flying Birds (If Separate Flying Birds = yes)
SuperPicky writes `Flying: Yes` or `Flying: No` inside the `XMP:Description` field — **not** `XMP:Label` or a keyword. Scan all JPGs in 3star and 2star for `Flying: Yes` in their description, move them (with ARW + XMP companions) to a `flying_birds` subfolder at the **star folder root level** (e.g. `3star_excellent/flying_birds/`), then sharpen each moved JPG with Unsharp Mask.

**CRITICAL — placement:** Always place `flying_birds` at the star folder root (e.g. `3star_excellent/flying_birds/`), NOT inside `Other_Birds`. Placing it inside `Other_Birds` causes `prepare_ebird_upload.py` to treat it as a species folder in Step 7.

**CRITICAL — ExifTool batching:** ExifTool cannot accept more than ~200 file paths in a single command on Windows (command line length limit). Always batch in groups of 200:
```python
PYTHONIOENCODING=utf-8 uv run python -c "
import subprocess, glob, os, shutil
from PIL import Image, ImageFilter

et_path = 'C:/workspace/SuperPicky/exiftools_win/exiftool.exe'
BATCH = 200
star_dirs = ['<Target>/3star_excellent', '<Target>/2star_good']

for star_dir in star_dirs:
    jpgs = [f for f in glob.glob(os.path.join(star_dir, '**', '*.jpg'), recursive=True)
            if 'flying_birds' not in f.replace(os.sep, '/')]
    flying = []
    for i in range(0, len(jpgs), BATCH):
        batch = jpgs[i:i+BATCH]
        result = subprocess.run([et_path, '-XMP:Description', '-T', '-f'] + batch,
                                capture_output=True, text=True, encoding='utf-8')
        for jpg, desc in zip(batch, result.stdout.strip().split('\n')):
            if 'Flying: Yes' in desc:
                flying.append(jpg)

    out_dir = os.path.join(star_dir, 'flying_birds')
    os.makedirs(out_dir, exist_ok=True)
    for jpg in flying:
        base = os.path.splitext(jpg)[0]
        for ext in ['.jpg', '.ARW', '.xmp']:
            src = base + ext
            if os.path.exists(src):
                shutil.move(src, os.path.join(out_dir, os.path.basename(src)))
        # Sharpen using chosen sharpening tool (see below)
"
```

After moving, apply the user's chosen Sharpening Tool to these photos:
- **`unsharp`**: apply Unsharp Mask (radius=1.5, percent=150, threshold=3) to each moved JPG.
- **`dxo`**: collect paired ARW files in the `flying_birds` folder and open them in DXO. If no ARW exists for a photo (JPEG-only), fall back to `unsharp` for that file.
- **`none`**: skip sharpening.

Always prefer RAW: if a flying bird photo has a paired `.ARW`, use that for DXO processing rather than the JPEG.

#### Step 6: Segregate Picked/Flagged Photos (If Separate Picked = yes)
Within the `3star_excellent` folder(s), identify photos where EXIF `XMP:Rating` = 5 or `XMP:Label` = "Pick" / "Red". Move them (with ARW + XMP companions) to a `picked` subfolder at the **star folder root level** (e.g. `3star_excellent/picked/`), then apply the user's chosen Sharpening Tool (same logic as Step 5: prefer RAW for DXO, fall back to unsharp for JPEG-only).

**CRITICAL — placement:** Always place `picked` at the star folder root, NOT inside `Other_Birds`, for the same reason as flying_birds above.

Use ExifTool batching (BATCH=200) as shown in Step 5. Scan `XMP:Rating` and `XMP:Label` fields in batches, then move + sharpen matched files.

#### Step 6b: Aesthetic Scoring (If Aesthetic Scoring = yes)
Run after Steps 5 and 6 so that `flying_birds` and `picked` have already been moved to their final locations and are included in the scoring.

**Install check:** Ensure `aesthetic-predictor-v2-5` is installed before running:
```
uv pip install aesthetic-predictor-v2-5
```
The first run downloads the SigLIP-SO400M-patch14-384 backbone (~878 MB) to the HuggingFace cache. Subsequent runs load from cache.

**Score all star folders** (0star_reject, 1star_average, 2star_good, 3star_excellent) in a single pass. Deduplicate paths using `os.path.normcase` to avoid double-counting on Windows (case-insensitive glob matching can return both `*.jpg` and `*.JPG` for the same file):
```python
PYTHONIOENCODING=utf-8 uv run python -c "
from aesthetic_predictor_v2_5 import convert_v2_5_from_siglip
from PIL import Image
import torch, glob, os, json, warnings
warnings.filterwarnings('ignore')

model, processor = convert_v2_5_from_siglip(low_cpu_mem_usage=True, trust_remote_code=True)
model.eval()

target = '<Target_Folder>'
star_labels = ['3star_excellent', '2star_good', '1star_average', '0star_reject']

for label in star_labels:
    folder = os.path.join(target, label)
    if not os.path.isdir(folder):
        continue
    seen = set()
    jpgs = []
    for p in glob.glob(folder + '/**/*.jpg', recursive=True) + glob.glob(folder + '/**/*.JPG', recursive=True):
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            jpgs.append(p)
    jpgs.sort()
    print(f'{label}: scoring {len(jpgs)} photos...')
    results = []
    for i, path in enumerate(jpgs):
        try:
            img = Image.open(path).convert('RGB')
            inputs = processor(images=img, return_tensors='pt')
            with torch.no_grad():
                score = model(**inputs).logits.squeeze().float().item()
            subfolder = os.path.relpath(os.path.dirname(path), folder)
            results.append({'file': os.path.basename(path), 'subfolder': subfolder, 'score': round(score, 3), 'path': path, 'star_folder': label})
            if (i+1) % 30 == 0:
                print(f'  [{i+1}/{len(jpgs)}] {os.path.basename(path)} = {score:.3f}')
        except Exception as e:
            print(f'  ERROR {path}: {e}')
    results.sort(key=lambda x: x['score'], reverse=True)
    out = os.path.join(target, f'aesthetic_scores_{label}.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2)
    scores = [r['score'] for r in results]
    print(f'  Saved {out} | >=6.5: {sum(1 for s in scores if s>=6.5)} | >=6.0: {sum(1 for s in scores if s>=6.0)} | >=5.5: {sum(1 for s in scores if s>=5.5)}')
print('Scoring complete.')
"
```

**Merge and export top photos.** After all folders are scored, merge into a single ranked list and copy top photos + ARW pairs to `<Target>/top_aesthetic/`. Skip duplicate ARW files (a DxO-processed JPG and its original may share the same source ARW):
```python
PYTHONIOENCODING=utf-8 uv run python -c "
import json, os, shutil

target = '<Target_Folder>'
threshold = <score_threshold>  # e.g. 6.0
out_dir = os.path.join(target, 'top_aesthetic')
os.makedirs(out_dir, exist_ok=True)

all_entries = []
seen_paths = set()
for label in ['3star_excellent', '2star_good', '1star_average', '0star_reject']:
    p = os.path.join(target, f'aesthetic_scores_{label}.json')
    if not os.path.exists(p):
        continue
    with open(p, encoding='utf-8') as f:
        for e in json.load(f):
            key = os.path.normcase(os.path.abspath(e['path']))
            if key not in seen_paths:
                seen_paths.add(key)
                all_entries.append(e)

all_entries.sort(key=lambda x: x['score'], reverse=True)
with open(os.path.join(target, 'aesthetic_scores_all.json'), 'w', encoding='utf-8') as f:
    json.dump(all_entries, f, indent=2)

top = [e for e in all_entries if e['score'] >= threshold]
seen_arw = set()
copied_jpg = copied_arw = 0
for e in top:
    if not os.path.exists(e['path']):
        continue
    score_tag = f'{e[\"score\"]:.3f}'.replace('.', 'p')
    dest_name = f'{score_tag}_{e[\"star_folder\"]}_{e[\"file\"]}'
    shutil.copy2(e['path'], os.path.join(out_dir, dest_name))
    copied_jpg += 1
    base = os.path.splitext(e['path'])[0]
    if '_DxO_DeepPRIME' in base:
        base = base.replace('_DxO_DeepPRIME', '').replace('-ARW', '')
    arw = base + '.ARW'
    arw_key = os.path.normcase(os.path.abspath(arw))
    if os.path.exists(arw) and arw_key not in seen_arw:
        shutil.copy2(arw, os.path.join(out_dir, os.path.splitext(dest_name)[0] + '.ARW'))
        seen_arw.add(arw_key)
        copied_arw += 1

print(f'top_aesthetic/: {copied_jpg} JPGs + {copied_arw} ARW pairs (threshold >= {threshold})')
print('Top 10:')
for i, e in enumerate(top[:10], 1):
    print(f'  {i:2}. {e[\"score\"]:.3f}  [{e[\"star_folder\"]}] [{e[\"subfolder\"]}]  {e[\"file\"]}')
"
```

**Hidden gems report:** After copying, explicitly call out any entries in `top` where `star_folder` is `0star_reject` or `1star_average`. These are photos SuperPicky rejected on technical grounds (sharpness/NIMA) but which SigLIP rates highly on composition and aesthetic appeal — worth the user reviewing manually before discarding.

#### Step 7: Prepare eBird Uploads
Gather the single highest-rated photo for each unique species across all subfolders and stage them into `<Target_Folder>\upload_ready`.
```
PYTHONIOENCODING=utf-8 uv run python scripts/ebird_tools/prepare_ebird_upload.py "<Target_Folder>"
```
Note: `prepare_ebird_upload.py` expects the root target folder and will scan recursively for `Other_Birds` under 3star and 2star directories.

**CRITICAL — clean up false positives:** `prepare_ebird_upload.py` lists ALL subdirectories of `Other_Birds` as species, including non-species subdirs like `flying_birds`, `picked`, or `burst_XXX`. After running, delete any upload_ready files whose stem is not a real species name. Check for and remove files like `flying_birds.jpg`, `picked.jpg`, `burst_002.jpg` etc.:
```python
import os, glob
# Read known species
with open('<Target>/unique_bird_species.txt') as f:
    valid = {s.strip().replace(' ', '_') for s in f}
for jpg in glob.glob('<Target>/upload_ready/*.jpg'):
    stem = os.path.splitext(os.path.basename(jpg))[0]
    if stem not in valid:
        os.remove(jpg)
        print(f'Removed false positive: {jpg}')
```

#### Step 8: Sharpening (Based on Sharpening Tool choice)

**If `dxo`**: Auto-detect installed DXO products, warn if incompatible, then open source RAW files in DXO GUI.
```python
import subprocess, glob, os
# Priority: PhotoLab (JPEG+RAW) > PureRAW 4 > PureRAW 3 > PureRAW 2 (RAW only)
dxo_candidates = [
    ("C:/Program Files/DxO/DxO PhotoLab 9/PhotoLab.exe",  "PhotoLab 9",  True),
    ("C:/Program Files/DxO/DxO PhotoLab 8/PhotoLab.exe",  "PhotoLab 8",  True),
    ("C:/Program Files/DxO/DxO PhotoLab 7/PhotoLab.exe",  "PhotoLab 7",  True),
    ("C:/Program Files/DxO/DxO PureRAW 4/PureRawv4.exe",  "PureRAW 4",   False),
    ("C:/Program Files/DxO/DxO PureRAW 3/PureRawv3.exe",  "PureRAW 3",   False),
    ("C:/Program Files/DxO/DxO PureRAW 2/PureRawv2.exe",  "PureRAW 2",   False),
]
```

**CRITICAL — DXO PureRAW needs RAW files, not JPGs.** The `upload_ready` folder contains renamed JPGs (e.g. `Barn_Swallow.jpg`). For PureRAW (RAW-only), find the source ARW for each upload_ready species by replicating `prepare_ebird_upload.py`'s selection logic: for each species name in upload_ready, find the first `.jpg` in the corresponding `Other_Birds/<species>/` folder, then look for the paired `.ARW` with the same stem. Do this BEFORE Step 8b runs (Step 8b uses `cv2.imwrite` which strips EXIF, making EXIF-based matching impossible afterwards).

```python
# For each upload_ready species, find its source ARW
arw_files = []
for jpg in glob.glob('<Target>/upload_ready/*.jpg'):
    species = os.path.splitext(os.path.basename(jpg))[0].replace('_', ' ')
    for ob in ['3star_excellent/Other_Birds', '2star_good/Other_Birds']:
        folder = f'<Target>/{ob}/{species}'
        if os.path.exists(folder):
            for f in sorted(os.listdir(folder)):
                if f.lower().endswith('.jpg'):
                    arw = os.path.join(folder, os.path.splitext(f)[0] + '.ARW')
                    if os.path.exists(arw):
                        arw_files.append(arw)
                    break
            break

# Open DXO with the ARW files
for exe, name, supports_jpeg in dxo_candidates:
    if os.path.exists(exe):
        subprocess.Popen([exe] + arw_files)
        print(f"Opened {name} with {len(arw_files)} RAW files.")
        break
```
After launching, tell the user: **"DXO is open. Apply your DeepPRIME settings, export back to the same source folders (overwrite originals), then confirm here to continue."** Pause and wait for user confirmation before proceeding.

**If `unsharp`**: Sharpening is handled automatically inside `crop_and_sharpen.py` in Step 9 — no separate action needed here.

**If `none`**: Skip this step entirely.

#### Step 8b: Auto Highlights & Shadows Adjustment (If Highlights & Shadows = yes)
Skip entirely if the user answered "no" to question 6.

Run AFTER DXO confirmation (or immediately after Step 7 if not using DXO). Applies per-image aesthetic tone correction using OpenCV in LAB colorspace. Each image is analyzed individually: dark images get shadow lift, bright images get highlight compression. Midtones are preserved.

**Note:** `cv2.imwrite` strips all EXIF metadata from the saved JPGs. This is acceptable since upload_ready JPGs are final output files. However, find the source ARW files for DXO (Step 8) BEFORE running this step.
```python
PYTHONIOENCODING=utf-8 uv run python -c "
import cv2, numpy as np, glob, os

def adjust_highlights_shadows(img):
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32)
    L = lab[:,:,0]
    highlights_mean = float(np.mean(L[L > 200])) if np.any(L > 200) else 200.0
    shadows_mean    = float(np.mean(L[L < 60]))  if np.any(L < 60)  else 60.0
    shadow_lift    = max(0, min(25, int((60 - shadows_mean) * 0.4))) if shadows_mean < 55 else 8
    highlight_pull = max(0, min(20, int((highlights_mean - 220) * 0.3))) if highlights_mean > 220 else 5
    x = np.arange(256, dtype=np.float32)
    shadow_curve    = np.where(x < 80, x + shadow_lift * (1 - x/80)**1.5, x)
    highlight_curve = np.where(x > 180, x - highlight_pull * ((x - 180) / 75)**1.5, x)
    lut = np.clip(shadow_curve + (highlight_curve - x), 0, 255).astype(np.uint8)
    lab[:,:,0] = cv2.LUT(lab[:,:,0].astype(np.uint8), lut).astype(np.float32)
    return cv2.cvtColor(np.clip(lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)

import warnings; warnings.filterwarnings('ignore')
for path in sorted(glob.glob('<Target_Folder>/upload_ready/*.jpg')):
    img = cv2.imread(path)
    if img is not None:
        cv2.imwrite(path, adjust_highlights_shadows(img), [cv2.IMWRITE_JPEG_QUALITY, 95])
        print(f'  Adjusted: {os.path.basename(path)}')
"
```

#### Step 9: Crop (If Crop Tool = yolo)
Skip entirely if the user chose `none` for Crop Tool.

Aesthetically center-crop the birds using YOLO (80% padding margin). Operates on the upload_ready JPGs.
```
PYTHONIOENCODING=utf-8 uv run python scripts/ebird_tools/crop_and_sharpen.py "<Target_Folder>\upload_ready"
```
Note: `crop_and_sharpen.py` internally applies YOLO crop AND an Unsharp Mask pass. The sharpening inside this script is independent of the user's Sharpening Tool choice:
- If Sharpening Tool = `dxo` or `unsharp`: the internal Unsharp Mask pass is redundant but harmless.
- If Sharpening Tool = `none`: the internal sharpening still runs — warn the user if they want a fully unsharpened crop output.

**RAW note:** `crop_and_sharpen.py` operates on JPEGs only. If the user's Sharpening Tool is `dxo`, DXO should have already exported processed JPGs back to upload_ready before this step runs.

#### Step 10: eBird Chrome Upload Workflow (If eBird Upload = yes)

**CRITICAL — CDP WebSocket rules (learned from real session):**

1. **Integer message IDs required.** Chrome rejects UUID strings with `"Message must have integer 'id' property"`. Always use an incrementing integer counter:
```python
_cmd_id = 0
def send_ws(ws, method, params=None, timeout=12):
    global _cmd_id
    _cmd_id += 1
    mid = _cmd_id
    ws.send(json.dumps({'id': mid, 'method': method, 'params': params or {}}))
    deadline = time.time() + timeout
    ws.settimeout(2)  # short per-recv timeout to drain CDP event flood
    while time.time() < deadline:
        try:
            r = json.loads(ws.recv())
            if r.get('id') == mid:
                return r
        except websocket.WebSocketTimeoutException:
            pass
    return {}
```

2. **`ws.settimeout(2)` inside the loop, NOT on `create_connection`.** Setting `timeout=2` on `create_connection` causes the initial TCP connection to time out. Set it after connecting via `ws.settimeout(2)`.

3. **`webSocketDebuggerUrl` is the correct CDP key** for the tab's WebSocket URL (not `wsUrl`).

4. **After `Page.navigate`, Chrome floods the socket with CDP events.** The `ws.settimeout(2)` + deadline loop drains them while waiting for the matching response. Use `time.sleep(5–7)` after navigation before the next `Runtime.evaluate` call to let the page finish loading.

5. **Use `uv run --with websocket-client python`** for all CDP scripts.

---

**Sub-step 10a — Launch Chrome with CDP:**
Kill any existing Chrome instances and relaunch with remote debugging AND a separate user-data-dir (to avoid profile lock conflicts when the user's normal Chrome was already open):
```python
import subprocess, time, urllib.request, json
import websocket

subprocess.run(['taskkill', '/IM', 'chrome.exe', '/F'], capture_output=True)
time.sleep(2)

chrome_exe = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
subprocess.Popen([
    chrome_exe,
    '--remote-debugging-port=9222',
    '--remote-allow-origins=*',
    '--user-data-dir=C:/Users/<user>/AppData/Local/Google/Chrome/CDP_Profile',
    'https://ebird.org/login'
])

# Poll until CDP is ready (up to 30s)
for _ in range(15):
    time.sleep(2)
    try:
        data = json.loads(urllib.request.urlopen('http://localhost:9222/json').read())
        if any(t.get('type') == 'page' for t in data):
            break
    except:
        pass
```

Helper to get the active page's WebSocket URL:
```python
def get_ws_url():
    data = json.loads(urllib.request.urlopen('http://localhost:9222/json').read())
    pages = [t for t in data if t.get('type') == 'page']
    return pages[0]['webSocketDebuggerUrl'] if pages else None
```

**Sub-step 10b — Wait for eBird login:**
Ask the user to log in to eBird in Chrome, then wait for confirmation before proceeding. (Polling the URL via CDP is possible but asking the user is simpler and more reliable.)

**Sub-step 10c — Find matching checklists:**
Navigate to `https://ebird.org/mychecklists?currentRow=1&sortBy=date&o=desc` and paginate through rows (step 20) to find **all** checklists matching the photo dates. Collect ALL checklists per date (users often have multiple checklists per day from different locations).

```python
ws = websocket.create_connection(get_ws_url(), origin='http://localhost:9222')

found = {}  # date_str -> list of urls
for start_row in range(1, 300, 20):
    send_ws(ws, 'Page.navigate', {'url': f'https://ebird.org/mychecklists?currentRow={start_row}&sortBy=date&o=desc'})
    time.sleep(6)  # wait for page load before evaluating

    r = send_ws(ws, 'Runtime.evaluate', {'expression': '''(function(){
        var links = document.querySelectorAll("a[href*='/checklist/S']");
        var results = [];
        links.forEach(function(a) {
            var row = a.closest("tr, li") || a.parentElement;
            results.push({url: a.href, text: (row ? row.innerText : a.innerText).trim().substring(0,300)});
        });
        return JSON.stringify(results.slice(0,100));
    })()''', 'returnByValue': True})
    items = json.loads(r.get('result',{}).get('result',{}).get('value','[]'))

    if not items:
        break  # no more checklists

    for item in items:
        m = re.search(r'(\d{1,2} \w{3} \d{4})', item['text'])
        if m:
            date_str = m.group(1)
            if date_str in target_dates:
                found.setdefault(date_str, [])
                if item['url'] not in found[date_str]:
                    found[date_str].append(item['url'])

    if all(d in found for d in target_dates):
        break

ws.close()
```

If multiple checklists are found per date, report them and ask the user which ones to open (or open all).

**Sub-step 10d — Open checklist media tabs:**
Open each matched checklist **directly on its `/media` page** in a new Chrome tab using the CDP `/json/new` endpoint (PUT request). Save the tab data (including `webSocketDebuggerUrl`) for use in 10e/10g:
```python
tab_ids = {}
for date_str, urls in sorted(found.items()):
    for cl_url in urls:
        media_url = cl_url + '/media'
        resp = urllib.request.urlopen(
            urllib.request.Request('http://localhost:9222/json/new?' + media_url, method='PUT')
        )
        tab_data = json.loads(resp.read())
        tab_ids[date_str] = tab_data  # webSocketDebuggerUrl is in tab_data
        time.sleep(1)

time.sleep(6)  # wait for all tabs to load
```

**Sub-step 10e — Extract species and cross-match:**
For each tab, connect a fresh WebSocket using the tab's own `webSocketDebuggerUrl` and run JS to extract species names. The eBird media page uses `a[href*='/species/']` links whose `innerText` is the species common name:
```javascript
(function(){
    var names = new Set();
    document.querySelectorAll("a[href*='/species/']").forEach(e=>names.add(e.innerText.trim()));
    document.querySelectorAll(".Obs-species .Heading, .SpeciesName").forEach(e=>names.add(e.innerText.trim()));
    document.querySelectorAll(".Obs .Heading").forEach(e=>names.add(e.innerText.trim()));
    return JSON.stringify([...names].filter(s=>s.length>2));
})()
```
Cross-match extracted species against `unique_bird_species.txt`. Report matches per checklist.

**Note:** 0 matches is expected and correct if the checklist is from a different birding location than where the identified species were observed (e.g., a coastal checklist will have shorebirds but upload_ready may contain forest birds from a different session on the same day). In this case, inform the user and ask them to confirm the right checklists.

**Sub-step 10f — (Skipped):**
Tabs are already opened directly to the `/media` page in 10d. No separate navigation step needed.

**Sub-step 10g — Highlight matched species rows:**
For each tab that has matches, connect a fresh WebSocket using the tab's `webSocketDebuggerUrl` and inject CSS + JS to highlight rows for matched species in green:
```javascript
(function() {
    var targets = <species_json_array>;
    if (!document.getElementById('sp-highlight-style')) {
        var s = document.createElement('style');
        s.id = 'sp-highlight-style';
        s.textContent = '.sp-highlighted { background-color: #d4edda !important; border-left: 4px solid #28a745 !important; }';
        document.head.appendChild(s);
    }
    var highlighted = 0;
    targets.forEach(function(name) {
        document.querySelectorAll('a[href*="/species/"]').forEach(function(el) {
            if (el.innerText && el.innerText.trim() === name) {
                var row = el.closest('tr, li, .Obs, .ObsList-item, [class*=observation]') || el.parentElement.parentElement;
                if (row) { row.classList.add('sp-highlighted'); highlighted++; }
            }
        });
    });
    return 'Highlighted ' + highlighted + ' rows';
})()
```

**Sub-step 10h — Wait for user uploads:**
Tell the user: **"All checklist tabs are open on their media pages with matched species highlighted in green. Upload the corresponding files from `<Target>/upload_ready/` — each file is named by species (e.g. `Banded_Broadbill.jpg`). Confirm when done."** Pause and wait for confirmation.
