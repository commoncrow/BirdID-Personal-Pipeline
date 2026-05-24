import os
import re
import sys
import glob
import json
import time
import shutil
import subprocess
import urllib.request
import urllib.parse
from datetime import datetime

# Ensure sibling scripts in the same directory are importable
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

# Step 7 auto-fill — imported from companion script
try:
    from ebird_add_missing_species import run_add_missing_species, is_species_plausible_for_location, extract_species_and_location, get_location_config, find_exiftool_path
    ADD_MISSING_AVAILABLE = True
except ImportError:
    ADD_MISSING_AVAILABLE = False
    is_species_plausible_for_location = None
    extract_species_and_location = None
    get_location_config = None
    find_exiftool_path = None

# Optional dependencies will be verified at runtime
try:
    import torch
    from PIL import Image
    from aesthetic_predictor_v2_5 import convert_v2_5_from_siglip
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False

try:
    import websocket
    WEBSOCKET_AVAILABLE = True
except ImportError:
    websocket = None
    WEBSOCKET_AVAILABLE = False

if find_exiftool_path:
    ET_PATH = find_exiftool_path()
else:
    ET_PATH = 'C:/workspace/SuperPicky/exiftools_win/exiftool.exe'

def log(msg):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] {msg}")

def run_cmd(args, desc=""):
    if desc:
        log(f"Running: {desc}...")
    # Add PYTHONIOENCODING env var
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    
    result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', env=env)
    if result.returncode != 0:
        log(f"❌ Error running {' '.join(args)}")
        print(result.stderr)
        sys.exit(result.returncode)
    return result.stdout

def run_exiftool(args, desc=""):
    """Like run_cmd but tolerates non-zero exit codes from ExifTool.
    ExifTool returns exit code 1 when some files are unreadable but still
    emits valid JSON for the files it could read."""
    if desc:
        log(f"Running: {desc}...")
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', env=env)
    # ExifTool writes warnings to stderr but still produces valid JSON stdout
    if result.returncode not in (0, 1):
        log(f"❌ ExifTool unexpected exit code {result.returncode}")
        print(result.stderr)
    return result.stdout

def scan_shooting_dates(target_dir):
    log("Scanning shooting dates from directory...")
    # Batch run exiftool on all files in target_dir recursively
    args = [ET_PATH, '-r', '-DateTimeOriginal', '-j', '-ext', 'jpg', '-ext', 'JPG', target_dir]
    stdout = run_exiftool(args, "ExifTool shooting dates query")
    
    dates = set()
    try:
        data = json.loads(stdout)
        log(f"Scanned {len(data)} image entries via ExifTool.")
        for item in data:
            raw_date = item.get('DateTimeOriginal')
            if raw_date:
                try:
                    # '2026:02:20 10:15:30' -> '20 Feb 2026'
                    dt = datetime.strptime(raw_date[:10], '%Y:%m:%d')
                    dates.add(dt.strftime('%d %b %Y'))
                except Exception as e:
                    pass
    except Exception as e:
        log(f"⚠️ Error parsing ExifTool JSON: {e}")
        
    sorted_dates = sorted(list(dates))
    log(f"📅 Detected Shooting Dates: {', '.join(sorted_dates)}")
    return sorted_dates

def run_superpicky(target_dir):
    log("🚀 Step 1: Running SuperPicky Rating...")
    args = ['uv', 'run', 'python', 'superpicky_cli.py', 'process', target_dir, '-s', '520', '-n', '5.5']
    # Execute and print output directly to show live progress
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    subprocess.run(args, env=env, check=True)
    log("✅ SuperPicky Rating complete.")

def run_birdid(target_dir):
    log("🚀 Step 2: Running BirdID Classification...")
    star3_ob = os.path.join(target_dir, "3star_excellent", "Other_Birds")
    star2_ob = os.path.join(target_dir, "2star_good", "Other_Birds")
    
    ob_dirs = []
    for d in [star3_ob, star2_ob]:
        if os.path.isdir(d) and any(os.path.isfile(os.path.join(d, f)) for f in os.listdir(d)):
            ob_dirs.append(d)
            
    if not ob_dirs:
        log("ℹ️ No 'Other_Birds' directories with photos found. Skipping BirdID.")
        return
        
    for ob_path in ob_dirs:
        log(f"Running BirdID on: {ob_path}")
        args = ['uv', 'run', 'python', 'birdid_cli.py', 'organize', ob_path, '-y', '--write-exif']
        subprocess.run(args, check=True)
    log("✅ BirdID Classification complete.")

def run_clean_names(target_dir):
    log("🚀 Step 3: Cleaning Species Folder Names...")
    star3_ob = os.path.join(target_dir, "3star_excellent", "Other_Birds")
    star2_ob = os.path.join(target_dir, "2star_good", "Other_Birds")
    
    ob_dirs = [d for d in [star3_ob, star2_ob] if os.path.isdir(d)]
    if not ob_dirs:
        log("ℹ️ No species directories to clean.")
        return
        
    args = ['uv', 'run', 'python', 'scripts/ebird_tools/remove_chinese_names.py'] + ob_dirs
    run_cmd(args, "English species name folder cleanup")
    log("✅ Folder names cleaned.")

def run_staging(target_dir):
    log("🚀 Step 4: Staging Top Species Photos for Upload...")
    args = ['uv', 'run', 'python', 'scripts/ebird_tools/prepare_ebird_upload.py', target_dir]
    run_cmd(args, "Copying top photos to upload_ready")
    log("✅ Photos staged in 'upload_ready' folder.")

def run_batch_aesthetic_scoring(target_dir, batch_size=None, score_threshold=6.0):
    log("🚀 Step 5: Running Optimized PyTorch Batch Aesthetic Scoring...")
    if not TORCH_AVAILABLE:
        log("❌ torch or aesthetic-predictor-v2-5 is not installed. Run 'uv pip install aesthetic-predictor-v2-5 torch' first.")
        sys.exit(1)
        
    if batch_size is None:
        batch_size = 16 if torch.cuda.is_available() else 1
        log(f"Auto-detected optimal batch size: {batch_size} ({'GPU' if torch.cuda.is_available() else 'CPU'} mode)")
        
    # Optimize CPU threads to prevent hyperthreading thrashing
    torch.set_num_threads(min(4, os.cpu_count() or 1))
    
    log("Loading SigLIP aesthetic model...")
    model, processor = convert_v2_5_from_siglip(low_cpu_mem_usage=True, trust_remote_code=True)
    model.eval()
    log("Model loaded successfully.")

    # We score 3star, 2star, and 1star. We SKIP 0star_reject by default to save 50% CPU time!
    star_labels = ['3star_excellent', '2star_good', '1star_average']
    all_results = []

    for label in star_labels:
        folder = os.path.join(target_dir, label)
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
        if not jpgs:
            continue
            
        log(f"{label}: scoring {len(jpgs)} photos in batches of {batch_size}...")
        results = []
        
        # Batch inference loop
        for idx in range(0, len(jpgs), batch_size):
            batch_paths = jpgs[idx:idx+batch_size]
            batch_images = []
            valid_paths = []
            
            for path in batch_paths:
                try:
                    img = Image.open(path).convert('RGB')
                    batch_images.append(img)
                    valid_paths.append(path)
                except Exception as e:
                    log(f"  ⚠️ Error opening image {path}: {e}")
                    
            if not batch_images:
                continue
                
            try:
                inputs = processor(images=batch_images, return_tensors='pt', padding=True)
                with torch.no_grad():
                    outputs = model(**inputs).logits
                    
                if outputs.dim() == 0 or outputs.shape[0] == 1:
                    batch_scores = [outputs.squeeze().float().item()]
                else:
                    batch_scores = outputs.squeeze().float().tolist()
                    
                for path, score in zip(valid_paths, batch_scores):
                    subfolder = os.path.relpath(os.path.dirname(path), folder)
                    results.append({
                        'file': os.path.basename(path),
                        'subfolder': subfolder,
                        'score': round(score, 3),
                        'path': path,
                        'star_folder': label
                    })
            except Exception as e:
                log(f"  ❌ Batch scoring error at index {idx}: {e}")
                
            log(f"  Progress: [{min(idx+batch_size, len(jpgs))}/{len(jpgs)}]")

        results.sort(key=lambda x: x['score'], reverse=True)
        out = os.path.join(target_dir, f'aesthetic_scores_{label}.json')
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2)
            
        scores = [r['score'] for r in results]
        log(f"  Saved {out} | >=6.5: {sum(1 for s in scores if s>=6.5)} | >=6.0: {sum(1 for s in scores if s>=6.0)} | >=5.5: {sum(1 for s in scores if s>=5.5)}")
        all_results.extend(results)

    # Save the merged manifest
    all_results.sort(key=lambda x: x['score'], reverse=True)
    with open(os.path.join(target_dir, 'aesthetic_scores_all.json'), 'w', encoding='utf-8') as f:
        json.dump(all_results, f, indent=2)

    # Export top photos
    out_dir = os.path.join(target_dir, 'top_aesthetic')
    os.makedirs(out_dir, exist_ok=True)
    
    top = [e for e in all_results if e['score'] >= score_threshold]
    copied_jpg = 0
    for e in top:
        if not os.path.exists(e['path']):
            continue
        score_tag = f"{e['score']:.3f}".replace('.', 'p')
        dest_name = f"{score_tag}_{e['star_folder']}_{e['file']}"
        shutil.copy2(e['path'], os.path.join(out_dir, dest_name))
        copied_jpg += 1

    log(f"top_aesthetic/: {copied_jpg} JPGs exported (threshold >= {score_threshold})")
    
    print("\n👑 Top 10 Photos:")
    for i, e in enumerate(all_results[:10], 1):
        print(f"  {i:2}. {e['score']:.3f}  [{e['star_folder']}] [{e['subfolder']}]  {e['file']}")
        
    print("\n💎 Hidden Gems Report (Top photos from average/1star folders):")
    hidden_gems = [e for e in all_results if e['star_folder'] == '1star_average'][:10]
    if hidden_gems:
        for i, e in enumerate(hidden_gems, 1):
            print(f"  {i:2}. {e['score']:.3f}  [{e['star_folder']}] [{e['subfolder']}]  {e['file']}")
    else:
        print("  None found.")
    log("✅ Batch Aesthetic Scoring complete.")

def run_crop_and_sharpen(target_dir):
    log("🚀 Step 6: Running YOLO Crop & Sharpen...")
    args = ['uv', 'run', 'python', 'scripts/ebird_tools/crop_and_sharpen.py', os.path.join(target_dir, 'upload_ready')]
    subprocess.run(args, check=True)
    log("✅ YOLO Crop & Sharpen complete.")

# ─── Chrome CDP helpers ───────────────────────────────────────────────────────
_ws_cmd_id = 0
def send_ws(ws, method, params=None, timeout=12):
    global _ws_cmd_id
    _ws_cmd_id += 1
    mid = _ws_cmd_id
    ws.send(json.dumps({'id': mid, 'method': method, 'params': params or {}}))
    deadline = time.time() + timeout
    ws.settimeout(2)
    while time.time() < deadline:
        try:
            r = json.loads(ws.recv())
            if r.get('id') == mid:
                return r
        except websocket.WebSocketTimeoutException:
            pass
        except Exception as e:
            print("WS error:", e)
            break
    return {}

def get_chrome_pages():
    try:
        req = urllib.request.urlopen('http://localhost:9222/json')
        return json.loads(req.read().decode('utf-8'))
    except Exception as e:
        print("Error getting pages from CDP:", e)
        return []

def get_ws_url():
    pages = [t for t in get_chrome_pages() if t.get('type') == 'page' and 'chrome-extension' not in t.get('url', '')]
    return pages[0]['webSocketDebuggerUrl'] if pages else None

def open_tab(url):
    """Open a new Chrome tab via CDP and return the tab data dict."""
    encoded = urllib.parse.quote(url, safe='')
    resp = urllib.request.urlopen(
        urllib.request.Request(f'http://localhost:9222/json/new?{encoded}', method='PUT')
    )
    return json.loads(resp.read().decode('utf-8'))

def close_tab(tab_id):
    """Close a Chrome tab by its CDP id."""
    try:
        urllib.request.urlopen(f'http://localhost:9222/json/close/{tab_id}')
    except Exception:
        pass

def find_checklists_for_dates(target_dates):
    """Navigate eBird mychecklists and return {date_str: [url, ...]} for target_dates.
    Assumes Chrome is already open with a page tab available.
    Returns an empty dict if Chrome is unavailable.
    """
    ws_url = get_ws_url()
    if not ws_url:
        return {}

    ws = websocket.create_connection(ws_url, origin='http://localhost:9222')
    send_ws(ws, 'Page.navigate', {'url': 'https://ebird.org/mychecklists?currentRow=1&sortBy=date&o=desc'})
    time.sleep(6)
    # Reconnect after navigation to get fresh ws url
    ws_url = get_ws_url()
    ws.close()
    ws = websocket.create_connection(ws_url, origin='http://localhost:9222')

    found = {}  # date_str -> [url, ...]
    oldest_target = min(datetime.strptime(d, '%d %b %Y') for d in target_dates)

    for start_row in range(1, 300, 20):
        log(f"  Scanning mychecklists row {start_row}–{start_row+19}...")
        send_ws(ws, 'Page.navigate', {'url': f'https://ebird.org/mychecklists?currentRow={start_row}&sortBy=date&o=desc'})
        time.sleep(5)

        r = send_ws(ws, 'Runtime.evaluate', {'expression': '''(function(){
            var links = document.querySelectorAll("a[href*='/checklist/S']");
            var results = [];
            links.forEach(function(a) {
                var row = a.closest("tr, li") || a.parentElement;
                results.push({url: a.href, text: (row ? row.innerText : a.innerText).trim().substring(0,300)});
            });
            return JSON.stringify(results.slice(0,100));
        })()''', 'returnByValue': True})

        val_str = r.get('result', {}).get('result', {}).get('value', '[]')
        items = json.loads(val_str)
        if not items:
            log("  No more checklists on page. Stopping search.")
            break

        oldest_on_page = None
        for item in items:
            m = re.search(r'(\d{1,2} \w{3} \d{4})', item['text'])
            if m:
                date_str = m.group(1)
                if date_str in target_dates:
                    found.setdefault(date_str, [])
                    if item['url'] not in found[date_str]:
                        found[date_str].append(item['url'])
                try:
                    dt = datetime.strptime(m.group(1), '%d %b %Y')
                    if oldest_on_page is None or dt < oldest_on_page:
                        oldest_on_page = dt
                except Exception:
                    pass

        if oldest_on_page and oldest_on_page < oldest_target:
            log("  Reached checklists older than target range. Stopping.")
            break

    ws.close()
    return found


def collect_photographed_species(target_dir):
    """Return a set of bird species names found in 3star/2star folder structure."""
    species = set()
    for star_label in ['3star_excellent', '2star_good']:
        star_dir = os.path.join(target_dir, star_label)
        if not os.path.isdir(star_dir):
            continue
        for item in os.listdir(star_dir):
            full = os.path.join(star_dir, item)
            if not os.path.isdir(full):
                continue
            if item == 'Other_Birds':
                # BirdID places classified species as subdirs inside Other_Birds
                for sub in os.listdir(full):
                    if os.path.isdir(os.path.join(full, sub)) and sub != 'Other_Birds':
                        species.add(sub)
            else:
                species.add(item)
    return species


def run_check_missing_species(target_dir, target_dates):
    """Step 6.5: Cross-check photographed species against eBird checklists.

    - Writes unique_bird_species.txt (required by Step 7 highlight).
    - Connects to Chrome to fetch species already in checklists.
    - Reports photographed species NOT present in any checklist so
      you can add them to eBird before uploading media.
    """
    log("🚀 Step 6.5: Checking photographed species against eBird checklists...")

    # ── 1. Collect photographed species ────────────────────────────────────────
    photographed = collect_photographed_species(target_dir)
    if not photographed:
        log("ℹ️ No species folders found — skipping missing-species check.")
        return

    log(f"📸 Photographed species ({len(photographed)}): {', '.join(sorted(photographed))}")

    # ── 2. Always write unique_bird_species.txt (Step 7 depends on this) ───────
    species_file = os.path.join(target_dir, 'unique_bird_species.txt')
    with open(species_file, 'w', encoding='utf-8') as f:
        for sp in sorted(photographed):
            f.write(sp + '\n')
    log(f"✅ Written {len(photographed)} species → unique_bird_species.txt")

    # ── 3. Find eBird checklists for target dates via CDP ──────────────────────
    if not WEBSOCKET_AVAILABLE:
        log("⚠️ websocket-client not installed — cannot cross-check eBird. Skipping.")
        return
    if not target_dates:
        log("ℹ️ No shooting dates available — skipping eBird cross-check.")
        return

    ws_url = get_ws_url()
    if not ws_url:
        log("⚠️ Chrome not reachable on port 9222 — skipping eBird cross-check.")
        log("   Start Chrome with: --remote-debugging-port=9222")
        return

    log("Searching mychecklists for target dates...")
    found_checklists = find_checklists_for_dates(target_dates)

    dates_no_checklist = sorted([d for d in target_dates if d not in found_checklists])
    log(f"Found checklists for {len(found_checklists)}/{len(target_dates)} shooting dates.")

    # ── 4. Fetch species in each checklist ─────────────────────────────────────
    checklist_species = {}  # url -> set[str]
    all_cl_urls = [url for urls in found_checklists.values() for url in urls]

    detected_country = None
    detected_state = None

    if all_cl_urls:
        log(f"Scanning {len(all_cl_urls)} checklists for species lists...")
        for cl_url in all_cl_urls:
            log(f"  → {cl_url}")
            try:
                if extract_species_and_location:
                    species_set, loc = extract_species_and_location(cl_url)
                    checklist_species[cl_url] = species_set
                    if not detected_country and loc.get('country') and loc.get('state'):
                        detected_country = loc['country']
                        detected_state = loc['state']
                        log(f"    🌍 Detected eBird location: {detected_state}, {detected_country}")
                else:
                    tab = open_tab(cl_url)
                    time.sleep(4)
                    tab_ws = websocket.create_connection(tab['webSocketDebuggerUrl'], origin='http://localhost:9222')
                    r = send_ws(tab_ws, 'Runtime.evaluate', {'expression': '''(function(){
                        var names = new Set();
                        document.querySelectorAll("a[href*='/species/']").forEach(e => names.add(e.innerText.trim()));
                        document.querySelectorAll(".Obs-species .Heading, .SpeciesName").forEach(e => names.add(e.innerText.trim()));
                        document.querySelectorAll(".Obs .Heading").forEach(e => names.add(e.innerText.trim()));
                        return JSON.stringify([...names].filter(s => s.length > 2));
                    })()''', 'returnByValue': True})
                    val_str = r.get('result', {}).get('result', {}).get('value', '[]')
                    checklist_species[cl_url] = set(json.loads(val_str))
                    tab_ws.close()
                    close_tab(tab['id'])
            except Exception as e:
                log(f"  ⚠️ Error scanning {cl_url}: {e}")

    # ── 5. Compute missing species ──────────────────────────────────────────────
    all_ebird_species = set()
    for sp_set in checklist_species.values():
        all_ebird_species.update(sp_set)

    # Load from folder location config if eBird location scraping failed
    folder_country = detected_country
    folder_state = detected_state
    if get_location_config and (not folder_country or not folder_state):
        folder_country, folder_state = get_location_config(target_dir)

    unique_months = []
    if target_dates:
        for d_str in target_dates:
            try:
                m_num = datetime.strptime(d_str, '%d %b %Y').month
                if m_num not in unique_months:
                    unique_months.append(m_num)
            except Exception:
                pass

    missing_raw = photographed - all_ebird_species
    missing = set()
    flagged = set()
    for sp in missing_raw:
        if is_species_plausible_for_location and is_species_plausible_for_location(sp, target_dir, country=folder_country, state=folder_state, month=unique_months):
            missing.add(sp)
        else:
            flagged.add(sp)

    # ── 6. Print actionable report ─────────────────────────────────────────────
    sep = "═" * 62
    print(f"\n{sep}")
    print("  📋  EBIRD PRE-UPLOAD SPECIES CHECK")
    print(sep)

    if dates_no_checklist:
        print(f"\n  ⚠️  Shooting dates with NO eBird checklist found:")
        for d in dates_no_checklist:
            print(f"       • {d}")
        print(f"  → Create checklists at: https://ebird.org/submit")

    if missing:
        print(f"\n  🐦  Photographed but NOT in any checklist ({len(missing)} species):")
        for sp in sorted(missing):
            print(f"       • {sp}")
        print(f"\n  💡  Add these species to your checklists before uploading media.")
        print(f"  → Your checklists: https://ebird.org/mychecklists")
    elif not dates_no_checklist:
        print(f"\n  ✅  All photographed species are present in your eBird checklists!")
        print(f"  Ready to upload media.")

    if flagged:
        print(f"\n  ⚠️  Flagged & Ignored ({len(flagged)} species):")
        for sp in sorted(flagged):
            print(f"       • {sp}")

    if found_checklists:
        print(f"\n  📂  Checklists checked ({sum(len(v) for v in found_checklists.values())} total):")
        for date_str in sorted(found_checklists, key=lambda d: datetime.strptime(d, '%d %b %Y')):
            for url in found_checklists[date_str]:
                sp_count = len(checklist_species.get(url, []))
                print(f"       {date_str}  {url}  ({sp_count} species)")

    print(f"{sep}\n")
    log("✅ Missing species check complete.")

def run_ebird_upload_highlight(target_dir, target_dates):
    log("🚀 Step 8: Connecting to Chrome for eBird Checklist Highlights...")
    if not WEBSOCKET_AVAILABLE:
        log("❌ websocket-client is not installed. Run 'uv pip install websocket-client' first.")
        return

    species_file = os.path.join(target_dir, "unique_bird_species.txt")
    if not os.path.exists(species_file):
        # Step 6.5 should have generated this — try generating it now as fallback
        log("⚠️ unique_bird_species.txt missing — generating from folder structure...")
        photographed = collect_photographed_species(target_dir)
        if not photographed:
            log("❌ No species folders found and no species file. Cannot highlight.")
            return
        with open(species_file, 'w', encoding='utf-8') as f:
            for sp in sorted(photographed):
                f.write(sp + '\n')
        log(f"  Generated {len(photographed)} species → unique_bird_species.txt")
        
    with open(species_file, "r", encoding="utf-8") as f:
        unique_species = [line.strip() for line in f if line.strip()]

    log(f"Loaded {len(unique_species)} species for highlighting.")

    ws_url = get_ws_url()
    if not ws_url:
        log("⚠️ Chrome tab with remote debugging not found on port 9222.")
        log("Make sure Chrome is running with remote debugging flags active!")
        return

    log(f"Searching checklists for dates: {', '.join(target_dates)}...")
    found = find_checklists_for_dates(target_dates)


    if not found:
        log("❌ No matching checklists found on eBird.")
        return

    log(f"\nSummary of checklists found ({sum(len(v) for v in found.values())} total):")
    for date_str, urls in sorted(found.items()):
        log(f"  {date_str}: {len(urls)} checklists")
        for u in urls:
            log(f"    - {u}")

    log("\nOpening checklist media tabs in Chrome...")
    tab_connections = []
    for date_str, urls in sorted(found.items()):
        for cl_url in urls:
            media_url = cl_url + '/media'
            log(f"  Opening: {media_url}")
            try:
                tab_data = open_tab(media_url)
                tab_connections.append({
                    'date': date_str,
                    'url': media_url,
                    'ws_url': tab_data['webSocketDebuggerUrl']
                })
            except Exception as e:
                log(f"  Failed to open tab for {media_url}: {e}")
            time.sleep(1)


    log("\nWaiting 7 seconds for tabs to load...")
    time.sleep(7)

    log("\nConnecting to checklist tabs to highlight species rows...")
    for conn in tab_connections:
        log(f"  Tab: {conn['url']}")
        try:
            tab_ws = websocket.create_connection(conn['ws_url'], origin='http://localhost:9222')
            
            r = send_ws(tab_ws, 'Runtime.evaluate', {'expression': '''(function(){
                var names = new Set();
                document.querySelectorAll("a[href*='/species/']").forEach(e=>names.add(e.innerText.trim()));
                document.querySelectorAll(".Obs-species .Heading, .SpeciesName").forEach(e=>names.add(e.innerText.trim()));
                document.querySelectorAll(".Obs .Heading").forEach(e=>names.add(e.innerText.trim()));
                return JSON.stringify([...names].filter(s=>s.length>2));
            })()''', 'returnByValue': True})
            
            val_str = r.get('result', {}).get('result', {}).get('value', '[]')
            extracted_names = json.loads(val_str)
            
            matches = [name for name in extracted_names if name in unique_species]
            log(f"    Extracted {len(extracted_names)} species, {len(matches)} matches: {', '.join(matches)}")
            
            if matches:
                js_highlight = f"""(function() {{
                    var targets = {json.dumps(matches)};
                    if (!document.getElementById('sp-highlight-style')) {{
                        var s = document.createElement('style');
                        s.id = 'sp-highlight-style';
                        s.textContent = '.sp-highlighted {{ background-color: #d4edda !important; border-left: 4px solid #28a745 !important; }}';
                        document.head.appendChild(s);
                    }}
                    var highlighted = 0;
                    targets.forEach(function(name) {{
                        document.querySelectorAll('a[href*="/species/"]').forEach(function(el) {{
                            if (el.innerText && el.innerText.trim() === name) {{
                                var row = el.closest('tr, li, .Obs, .ObsList-item, [class*=observation]') || el.parentElement.parentElement;
                                if (row) {{ row.classList.add('sp-highlighted'); highlighted++; }}
                            }}
                        }});
                    }});
                    return 'Highlighted ' + highlighted + ' rows';
                }})()"""
                
                hr = send_ws(tab_ws, 'Runtime.evaluate', {'expression': js_highlight, 'returnByValue': True})
                h_res = hr.get('result', {}).get('result', {}).get('value', 'No result')
                log(f"    -> {h_res}")
            else:
                log("    -> No matches to highlight.")
                
            tab_ws.close()
        except Exception as e:
            log(f"    Error processing tab: {e}")

    log("🎉 eBird Chrome checklist Highlights complete!")

def main():
    if len(sys.argv) < 2:
        print("Usage: python ebird_pipeline.py <target_root_folder> [--skip-rating] [--country <CODE>] [--state <NAME>]")
        sys.exit(1)
        
    target_dir = os.path.abspath(sys.argv[1])
    skip_rating = "--skip-rating" in sys.argv
    
    if not os.path.isdir(target_dir):
        print(f"Error: Target directory does not exist: {target_dir}")
        sys.exit(1)

    country_arg = None
    state_arg = None
    if "--country" in sys.argv:
        idx = sys.argv.index("--country")
        if idx + 1 < len(sys.argv):
            country_arg = sys.argv[idx + 1]
    if "--state" in sys.argv:
        idx = sys.argv.index("--state")
        if idx + 1 < len(sys.argv):
            state_arg = sys.argv[idx + 1]

    # Pre-cache/resolve location early, prompting if interactive and not resolved
    if get_location_config:
        get_location_config(target_dir, country_arg=country_arg, state_arg=state_arg)
        
    log("==========================================================")
    log("🐦 Optimized eBird Unified Import & Processing Pipeline")
    log("==========================================================")
    log(f"Target Directory: {target_dir}")
    
    # Pre-flight: scan shooting dates first (batch, takes <1s)
    target_dates = scan_shooting_dates(target_dir)
    if not target_dates:
        log("⚠️ No shooting dates found via ExifOriginal. Defaulting to eBird 'auto' mode during checklist query.")
        
    # Execute sequence
    if not skip_rating:
        run_superpicky(target_dir)
    else:
        log("ℹ️ Skipping SuperPicky Initial Rating step.")
        
    run_birdid(target_dir)
    run_clean_names(target_dir)
    run_staging(target_dir)
    
    # Run batch aesthetic scoring (PyTorch batch inference optimized)
    run_batch_aesthetic_scoring(target_dir)
    
    run_crop_and_sharpen(target_dir)

    # Step 6.5: Cross-check species against eBird before media upload
    run_check_missing_species(target_dir, target_dates)

    # Step 7: Auto-fill missing species into eBird checklists
    if target_dates:
        if ADD_MISSING_AVAILABLE:
            run_add_missing_species(target_dir, target_dates)
        else:
            log("⚠️ ebird_add_missing_species.py not found — skipping Step 7.")

    # Step 8: Highlight checklist rows that have matching photos
    if target_dates:
        run_ebird_upload_highlight(target_dir, target_dates)
    else:
        log("ℹ️ No shooting dates detected; skipping eBird checklist highlighting step.")
        
    log("==========================================================")
    log("🎉 Optimized End-to-End Pipeline Execution Complete!")
    log(f"Staged photos ready for upload at: {os.path.join(target_dir, 'upload_ready')}")
    log("==========================================================")

if __name__ == "__main__":
    main()
