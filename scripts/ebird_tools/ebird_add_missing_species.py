"""ebird_add_missing_species.py

Scans processed photo folders, cross-references photographed species against
eBird checklists, prints a missing-species report, and optionally auto-fills
the missing species into the eBird checklist edit page via Chrome CDP.

For each shooting date the LARGEST checklist (most species already logged)
is chosen as the target. Edit tabs are left open for user review — nothing
is auto-saved.

Usage:
    # Full report + auto-fill for everything under E:\\2026
    uv run python scripts/ebird_tools/ebird_add_missing_species.py E:\\2026

    # Single folder
    uv run python scripts/ebird_tools/ebird_add_missing_species.py E:\\2026\\Solan

    # Report only (no browser automation)
    uv run python scripts/ebird_tools/ebird_add_missing_species.py E:\\2026 --report-only
"""

import os
import re
import sys
import json
import time
import subprocess
import urllib.request
import urllib.parse
from datetime import datetime

# Force UTF-8 output on Windows (default console is CP1252)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')
os.environ['PYTHONIOENCODING'] = 'utf-8'

try:
    import websocket
    WEBSOCKET_AVAILABLE = True
except ImportError:
    websocket = None
    WEBSOCKET_AVAILABLE = False

def find_exiftool_path():
    """Dynamically discover ExifTool executable across Windows and macOS/Linux."""
    script_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    
    if sys.platform == 'win32':
        candidates = [
            os.path.join(script_dir, 'exiftools_win', 'exiftool.exe'),
            'C:/workspace/SuperPicky/exiftools_win/exiftool.exe',
            'exiftool.exe',
            'exiftool'
        ]
    else:
        # macOS or Linux
        candidates = [
            os.path.join(script_dir, 'exiftools_mac', 'exiftool'),
            '/opt/homebrew/bin/exiftool',
            '/usr/local/bin/exiftool',
            'exiftool'
        ]
        
    for c in candidates:
        if os.path.isfile(c):
            return c
            
    # Try calling 'exiftool' from PATH
    try:
        import subprocess
        subprocess.run(['exiftool', '-ver'], capture_output=True, check=True, timeout=3)
        return 'exiftool'
    except Exception:
        pass
        
    return 'C:/workspace/SuperPicky/exiftools_win/exiftool.exe' if sys.platform == 'win32' else 'exiftool'


ET_PATH = find_exiftool_path()
STAR_LABELS = ['3star_excellent', '2star_good']

_GBIF_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gbif_cache.json')

def _load_gbif_cache():
    if os.path.exists(_GBIF_CACHE_FILE):
        try:
            with open(_GBIF_CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_gbif_cache(cache):
    try:
        with open(_GBIF_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, indent=2)
    except Exception:
        pass

def geocode_location(name):
    """Query Nominatim dynamically to resolve a location name to country and state/province."""
    if not name or name.isdigit() or len(name) < 3:
        return None, None
    try:
        url = f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote(name)}&format=json&addressdetails=1&limit=1"
        req = urllib.request.Request(url, headers={'User-Agent': 'SuperPickyLocationResolver/1.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
            if data:
                address = data[0].get('address', {})
                country_code = address.get('country_code', '').upper()
                state = address.get('state') or address.get('province') or address.get('region')
                return country_code, state
    except Exception as e:
        log(f"      ⚠️ Geocoding failed for '{name}': {e}")
    return None, None


def get_location_config(target_dir, country_arg=None, state_arg=None):
    """
    Retrieve location configuration (country code and state/province) for a target directory.
    Pauses and prompts the user if not resolved automatically.
    """
    if country_arg and state_arg:
        return country_arg.upper(), state_arg

    loc_file = os.path.join(target_dir, 'location.json')
    if os.path.exists(loc_file):
        try:
            with open(loc_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if data.get('country') and data.get('state'):
                    return data['country'].upper(), data['state']
        except Exception:
            pass

    # Try path auto-detection
    path_lower = target_dir.lower()
    country, state = None, None
    if "seattle" in path_lower:
        country, state = "US", "Washington"
    elif "solan" in path_lower:
        country, state = "IN", "Himachal Pradesh"

    # Try geocoding the folder name
    if not country or not state:
        folder_name = os.path.basename(os.path.normpath(target_dir))
        # Handle date/year/upload folder names by looking at parent folder
        if folder_name.isdigit() or re.search(r'^\d{1,2}\s+\w{3}\s+\d{4}$', folder_name) or folder_name.lower() in ['upload_ready', 'top_aesthetic']:
            folder_name = os.path.basename(os.path.dirname(os.path.normpath(target_dir)))
        
        country, state = geocode_location(folder_name)

    # 🛑 Pause and demand location if still unresolved in interactive mode
    if (not country or not state) and sys.stdin.isatty():
        print(f"\n🌍 Location could not be auto-detected for: '{os.path.basename(target_dir)}'")
        print("   Please enter the location details to enable GBIF occurrence validation.")
        while not country or not state:
            try:
                if not country:
                    country = input("   👉 Enter Country Code (2 letters, e.g. US, IN): ").strip().upper()
                if not state:
                    state = input("   👉 Enter State/Province (e.g. Washington, Himachal Pradesh): ").strip()
                
                if not country or not state:
                    print("   ❌ Both Country and State are required to proceed. Please enter them.")
            except (KeyboardInterrupt, EOFError):
                print("\n   ⚠️ Prompt cancelled. Validation will run without location.")
                break

    # Cache the resolved location so you only enter it once
    if country and state:
        try:
            with open(loc_file, 'w', encoding='utf-8') as f:
                json.dump({'country': country, 'state': state}, f, indent=2)
            log(f"💾 Saved location configuration to: {loc_file}")
        except Exception:
            pass

    return country, state


_warned_no_location = False


def is_species_plausible_for_location(species_name, folder_path, country=None, state=None, month=None):
    """
    Query GBIF dynamically to check if the species has substantial records
    in the target country/state during the specified month(s),
    flagging rare/accidental/misclassified species.
    """
    global _warned_no_location

    # Try to load from target location config if not passed directly
    if not country or not state:
        country, state = get_location_config(folder_path)

    # Safe fallback if still unresolved
    if not country or not state:
        if not _warned_no_location:
            log(f"⚠️ Warning: Location details could not be resolved for: {os.path.basename(folder_path)}")
            log("   GBIF occurrence validation will be skipped. All species will default to 'plausible'.")
            _warned_no_location = True
        return True

    # Build the list of target months (include adjacent 2 months back and forth to tolerate migration shifts)
    months = []
    if month is not None:
        if isinstance(month, int):
            expanded = set()
            for offset in [-2, -1, 0, 1, 2]:
                m = (month + offset - 1) % 12 + 1
                expanded.add(m)
            months = sorted(list(expanded))
        elif isinstance(month, list):
            expanded = set()
            for m in month:
                for offset in [-2, -1, 0, 1, 2]:
                    val = (m + offset - 1) % 12 + 1
                    expanded.add(val)
            months = sorted(list(expanded))

    months_str = ",".join(str(m) for m in months) if months else "all"
    cache = _load_gbif_cache()
    cache_key = f"{species_name}||{country}||{state}||{months_str}"
    if cache_key in cache:
        return cache[cache_key]

    try:
        # Match vernacular species name to taxonomy
        url = f"https://api.gbif.org/v1/species/search?q={urllib.parse.quote(species_name)}"
        req = urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'SuperPicky/1.0'}))
        res = json.loads(req.read().decode('utf-8'))
        results = res.get('results', [])
        if not results:
            scientific_name = species_name
        else:
            scientific_name = results[0].get('canonicalName', species_name)

        # Search occurrences in the detected country and state/province
        occ_url = f"https://api.gbif.org/v1/occurrence/search?scientificName={urllib.parse.quote(scientific_name)}&country={country}&stateProvince={urllib.parse.quote(state)}&limit=1"
        for m in months:
            occ_url += f"&month={m}"

        req_occ = urllib.request.urlopen(urllib.request.Request(occ_url, headers={'User-Agent': 'SuperPicky/1.0'}))
        res_occ = json.loads(req_occ.read().decode('utf-8'))
        count = res_occ.get('count', 0)

        # Plausible if we have 200 or more records
        plausible = count >= 200

        cache[cache_key] = plausible
        _save_gbif_cache(cache)
        return plausible
    except Exception as e:
        log(f"      ⚠️ GBIF Query failed for '{species_name}': {e}. Defaulting to plausible.")
        return True


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


# ─── ExifTool helpers ─────────────────────────────────────────────────────────

def collect_species_by_date(target_dir):
    """
    Scan all JPG/JPEG files in target_dir using ExifTool.
    Return a dict mapping date string (e.g. '02 May 2026') -> set of species photographed on that date,
    and a set of all photographed species.
    """
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    args = [ET_PATH, '-r', '-DateTimeOriginal', '-j',
            '-ext', 'jpg', '-ext', 'JPG', '-ext', 'jpeg', '-ext', 'JPEG', target_dir]
    result = subprocess.run(args, capture_output=True, text=True,
                            encoding='utf-8', env=env)
    
    date_to_species = {}
    all_species = set()
    
    try:
        data = json.loads(result.stdout)
    except Exception as e:
        log(f"  ⚠️ Failed to parse ExifTool json: {e}")
        data = []
        
    for item in data:
        raw_path = item.get('SourceFile', '')
        raw_date = item.get('DateTimeOriginal', '')
        if not raw_path or not raw_date:
            continue
            
        # Parse date
        try:
            ds = datetime.strptime(raw_date[:10], '%Y:%m:%d').strftime('%d %b %Y')
        except Exception:
            continue
            
        # Parse species name from path
        norm_path = raw_path.replace('\\', '/')
        parts = norm_path.split('/')
        
        star_idx = -1
        for idx, part in enumerate(parts):
            if part in STAR_LABELS:
                star_idx = idx
                break
                
        if star_idx == -1 or star_idx + 1 >= len(parts):
            continue
            
        sp_part = parts[star_idx + 1]
        if sp_part == 'Other_Birds' and star_idx + 2 < len(parts):
            species_name = parts[star_idx + 2]
        else:
            species_name = sp_part
            
        # Exclude files directly in the species dir or other files
        if '.' in species_name or species_name.lower().endswith(('.jpg', '.jpeg')):
            continue
            
        date_to_species.setdefault(ds, set()).add(species_name)
        all_species.add(species_name)
        
    return date_to_species, all_species


def scan_shooting_dates(target_dir):
    """Return sorted list of shooting date strings like '14 Jan 2026'."""
    date_to_species, _ = collect_species_by_date(target_dir)
    return sorted(list(date_to_species.keys()))


def collect_photographed_species(target_dir):
    """Return set of bird species names from 3star/2star folder structure."""
    _, all_species = collect_species_by_date(target_dir)
    return all_species


def find_processed_folders(root_dir):
    """Return list of immediate subdirs of root_dir that contain star folders."""
    result = []
    for item in sorted(os.listdir(root_dir)):
        full = os.path.join(root_dir, item)
        if not os.path.isdir(full):
            continue
        if any(os.path.isdir(os.path.join(full, l)) for l in STAR_LABELS):
            result.append(full)
    return result


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
            log(f"WS error: {e}")
            break
    return {}


def get_chrome_pages():
    try:
        req = urllib.request.urlopen('http://localhost:9222/json')
        return json.loads(req.read().decode('utf-8'))
    except Exception:
        return []


def get_ws_url():
    pages = [t for t in get_chrome_pages()
             if t.get('type') == 'page'
             and 'chrome-extension' not in t.get('url', '')]
    return pages[0]['webSocketDebuggerUrl'] if pages else None


def open_tab(url):
    """Open a new Chrome tab via CDP and return the tab data dict."""
    encoded = urllib.parse.quote(url, safe='')
    resp = urllib.request.urlopen(
        urllib.request.Request(
            f'http://localhost:9222/json/new?{encoded}', method='PUT'))
    return json.loads(resp.read().decode('utf-8'))


def close_tab(tab_id):
    """Close a Chrome tab by its CDP id."""
    try:
        urllib.request.urlopen(f'http://localhost:9222/json/close/{tab_id}')
    except Exception:
        pass


def js_eval(ws, expr):
    """Evaluate JS expression and return the primitive value."""
    r = send_ws(ws, 'Runtime.evaluate',
                {'expression': expr, 'returnByValue': True})
    return r.get('result', {}).get('result', {}).get('value')


def wait_for_js_condition(ws, expr, timeout=10.0, poll_interval=0.1):
    """Poll a JS expression until it returns a truthy value or timeout is reached."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            val = js_eval(ws, expr)
            if val:
                return val
        except Exception:
            pass
        time.sleep(poll_interval)
    return None


def wait_for_page_load(ws, timeout=12.0):
    """Wait for document.readyState to be 'complete'."""
    return wait_for_js_condition(ws, 'document.readyState === "complete"', timeout=timeout)


def wait_for_autocomplete(ws, timeout=3.0, poll_interval=0.1):
    """Wait until autocomplete suggestions appear or empty state button is visible."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            raw = js_eval(ws, _AUTOCOMPLETE_JS)
            suggestions = json.loads(raw) if raw else []
            if suggestions:
                return suggestions
            empty_present = js_eval(ws, """(function() {
                var btn = Array.from(document.querySelectorAll('.Suggest-empty button, button')).find(b => /Add Species/i.test(b.innerText));
                return !!btn;
            })()""")
            if empty_present:
                break
        except Exception:
            pass
        time.sleep(poll_interval)
    try:
        raw = js_eval(ws, _AUTOCOMPLETE_JS)
        return json.loads(raw) if raw else []
    except Exception:
        return []


def press_key(ws, key, code=None):
    """Simulate a key press (down + up)."""
    for ktype in ('keyDown', 'keyUp'):
        p = {'type': ktype, 'key': key}
        if code:
            p['code'] = code
        send_ws(ws, 'Input.dispatchKeyEvent', p)
    time.sleep(0.08)


def type_via_js(ws, selector, text):
    """Set value of input using React-compatible native setter and dispatch events."""
    js = f"""(function() {{
        var el = document.querySelector({json.dumps(selector)});
        if (!el) return false;
        el.focus();
        var nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
        nativeSetter.call(el, {json.dumps(text)});
        el.dispatchEvent(new Event('input', {{bubbles: true}}));
        el.dispatchEvent(new Event('change', {{bubbles: true}}));
        return true;
    }})()"""
    res = js_eval(ws, js)
    time.sleep(0.3)
    return res


# ─── eBird checklist discovery ────────────────────────────────────────────────

def find_checklists_for_dates(target_dates):
    """
    Navigate mychecklists and return {date_str: [url, ...]} for target_dates.
    Requires Chrome to already have a page tab.
    """
    tab = None
    try:
        tab = open_tab('about:blank')
        ws_url = tab['webSocketDebuggerUrl']
    except Exception:
        ws_url = get_ws_url()

    if not ws_url:
        return {}

    ws = websocket.create_connection(ws_url, origin='http://localhost:9222')
    send_ws(ws, 'Page.navigate', {
        'url': 'https://ebird.org/mychecklists?currentRow=1&sortBy=date&o=desc'})
    wait_for_page_load(ws)

    found = {}
    oldest_target = min(datetime.strptime(d, '%d %b %Y') for d in target_dates)

    for start_row in range(1, 300, 20):
        log(f"  Scanning mychecklists row {start_row}–{start_row + 19}...")
        send_ws(ws, 'Page.navigate', {
            'url': f'https://ebird.org/mychecklists?currentRow={start_row}&sortBy=date&o=desc'})
        wait_for_page_load(ws)

        r = send_ws(ws, 'Runtime.evaluate', {'expression': """(function(){
            var links = document.querySelectorAll("a[href*='/checklist/S']");
            var out = [];
            links.forEach(function(a){
                var row = a.closest('tr,li') || a.parentElement;
                out.push({url:a.href, text:(row?row.innerText:a.innerText).trim().substring(0,300)});
            });
            return JSON.stringify(out.slice(0,100));
        })()""", 'returnByValue': True})

        items = json.loads(
            r.get('result', {}).get('result', {}).get('value', '[]'))
        if not items:
            log("  No more checklists on page.")
            break

        oldest_on_page = None
        for item in items:
            m = re.search(r'(\d{1,2} \w{3} \d{4})', item['text'])
            if m:
                ds = m.group(1)
                if ds in target_dates:
                    found.setdefault(ds, [])
                    if item['url'] not in found[ds]:
                        found[ds].append(item['url'])
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
    if tab:
        try:
            close_tab(tab['id'])
        except Exception:
            pass
    return found


def extract_species_and_location(cl_url):
    """Open checklist, extract species and eBird region breadcrumbs, close tab."""
    try:
        tab = open_tab(cl_url)
        tw = websocket.create_connection(
            tab['webSocketDebuggerUrl'], origin='http://localhost:9222')
        wait_for_page_load(tw)
            
        # 1. Extract species
        r = send_ws(tw, 'Runtime.evaluate', {'expression': """(function(){
            var names = new Set();
            document.querySelectorAll("a[href*='/species/']").forEach(e=>names.add(e.innerText.trim()));
            document.querySelectorAll(".Obs-species .Heading,.SpeciesName,.Obs .Heading")
                    .forEach(e=>names.add(e.innerText.trim()));
            return JSON.stringify([...names].filter(s=>s.length>2));
        })()""", 'returnByValue': True})
        val = r.get('result', {}).get('result', {}).get('value', '[]')
        species = set(json.loads(val))
        
        # 2. Extract location breadcrumbs
        r_loc = send_ws(tw, 'Runtime.evaluate', {'expression': """(function(){
            var breadcrumbs = [];
            document.querySelectorAll('a[href*="/region/"]').forEach(function(a) {
                var m = a.href.match(/\\/region\\/([A-Z]{2}(?:-[A-Z0-9]+){0,2})$/i);
                if (m) {
                    breadcrumbs.push({
                        code: m[1],
                        text: a.innerText.trim()
                    });
                }
            });
            return JSON.stringify(breadcrumbs);
        })()""", 'returnByValue': True})
        loc_val = r_loc.get('result', {}).get('result', {}).get('value', '[]')
        crumbs = json.loads(loc_val)
        
        location = {'country': None, 'state': None}
        for c in crumbs:
            code = c['code']
            parts = code.split('-')
            if len(parts) == 1 and len(parts[0]) == 2:
                location['country'] = parts[0].upper()
            elif len(parts) == 2:
                location['state'] = c['text']
                
        tw.close()
        close_tab(tab['id'])
        return species, location
    except Exception as e:
        log(f"  ⚠️ Error reading {cl_url}: {e}")
        return set(), {'country': None, 'state': None}


# ─── Report builder ───────────────────────────────────────────────────────────

def build_missing_report(root_dir):
    """
    Scan processed folders, cross-reference species with eBird checklists.

    Returns list of report dicts, one per processed folder:
      {
        'folder': str,
        'dates': [str, ...],
        'photographed': set,
        'per_date': {
            date_str: {
                'best_cl': url,
                'all_cls': [url, ...],
                'cl_species_count': int,
                'missing': set,
                'present': set,
            }
        },
        'dates_no_checklist': [str, ...],
      }
    """
    # Determine if root_dir is itself a processed folder or a parent
    if any(os.path.isdir(os.path.join(root_dir, l)) for l in STAR_LABELS):
        folders = [root_dir]
    else:
        folders = find_processed_folders(root_dir)

    if not folders:
        log(f"No processed folders (with star subfolders) found under {root_dir}")
        return []

    log(f"Scanning {len(folders)} folder(s): "
        f"{[os.path.basename(f) for f in folders]}")

    all_target_dates = set()
    folder_meta = []
    for folder in folders:
        date_to_species, photographed = collect_species_by_date(folder)
        dates = sorted(list(date_to_species.keys()))
        all_target_dates.update(dates)
        folder_meta.append({
            'folder': folder,
            'photographed': photographed,
            'date_to_species': date_to_species,
            'dates': dates
        })
        log(f"  {os.path.basename(folder)}: "
            f"{len(photographed)} species, {len(dates)} shooting dates")

    if not all_target_dates:
        log("No shooting dates found.")
        return []

    if not WEBSOCKET_AVAILABLE:
        log("⚠️ websocket-client not installed — cannot query eBird.")
        return []

    if not get_ws_url():
        log("⚠️ Chrome not reachable on port 9222.")
        return []

    # Single mychecklists pass covers all folders
    log(f"Querying eBird for {len(all_target_dates)} unique date(s)...")
    all_checklists = find_checklists_for_dates(sorted(all_target_dates))

    # Fetch species for every discovered checklist (open+close tabs)
    all_urls = [u for urls in all_checklists.values() for u in urls]
    log(f"Fetching species from {len(all_urls)} checklist(s)...")
    checklist_species = {}
    detected_country = None
    detected_state = None
    for url in all_urls:
        log(f"  → {url}")
        species, loc = extract_species_and_location(url)
        checklist_species[url] = species
        log(f"    {len(species)} species")
        if not detected_country and loc.get('country') and loc.get('state'):
            detected_country = loc['country']
            detected_state = loc['state']
            log(f"    🌍 Detected eBird location: {detected_state}, {detected_country}")

    # Build per-folder report
    reports = []
    for fm in folder_meta:
        folder = fm['folder']
        photographed = fm['photographed']
        date_to_species = fm['date_to_species']
        dates = fm['dates']

        folder_country = detected_country
        folder_state = detected_state
        if not folder_country or not folder_state:
            folder_country, folder_state = get_location_config(folder)

        per_date = {}
        for ds in dates:
            urls = all_checklists.get(ds, [])
            if not urls:
                continue
            # Largest = most species already logged
            best = max(urls, key=lambda u: len(checklist_species.get(u, set())))
            present_in_best = checklist_species.get(best, set())
            
            # Use only species photographed on this specific date
            todays_photographed = date_to_species.get(ds, set())
            missing_raw = todays_photographed - present_in_best
            
            missing_valid = set()
            flagged = set()
            for sp in missing_raw:
                try:
                    m_val = datetime.strptime(ds, '%d %b %Y').month
                except Exception:
                    m_val = None
                if is_species_plausible_for_location(sp, folder, country=folder_country, state=folder_state, month=m_val):
                    missing_valid.add(sp)
                else:
                    flagged.add(sp)
            
            per_date[ds] = {
                'best_cl': best,
                'all_cls': urls,
                'cl_species_count': len(present_in_best),
                'missing': missing_valid,
                'flagged': flagged,
                'present': todays_photographed & present_in_best,
            }

        reports.append({
            'folder': folder,
            'dates': dates,
            'photographed': photographed,
            'per_date': per_date,
            'dates_no_checklist': [d for d in dates if d not in all_checklists],
        })

    return reports


def print_report(reports):
    """Print a human-readable missing species report."""
    sep = '═' * 66
    print(f"\n{sep}")
    print("  📋  EBIRD MISSING SPECIES REPORT")
    print(sep)

    grand_total_missing = 0
    for rpt in reports:
        folder_name = os.path.basename(rpt['folder'])
        n_missing_total = sum(len(pd['missing']) for pd in rpt['per_date'].values())
        grand_total_missing += n_missing_total
        print(f"\n  📁  {folder_name}"
              f"  ({len(rpt['photographed'])} photographed | "
              f"{len(rpt['dates'])} dates | "
              f"{n_missing_total} missing entries)")
        print(f"  {'─' * 62}")

        for ds in sorted(rpt['per_date'], key=lambda d: datetime.strptime(d, '%d %b %Y')):
            pd = rpt['per_date'][ds]
            n_cls = len(pd['all_cls'])
            print(f"\n  📅  {ds}")
            print(f"      Target checklist ({n_cls} found, using largest "
                  f"with {pd['cl_species_count']} species):")
            print(f"      {pd['best_cl']}")
            if pd['missing']:
                print(f"      🐦 Missing ({len(pd['missing'])}): "
                      f"{', '.join(sorted(pd['missing']))}")
            else:
                print(f"      ✅ All photographed species present!")
            if pd.get('flagged'):
                print(f"      ⚠️  Flagged & Ignored ({len(pd['flagged'])}): "
                      f"{', '.join(sorted(pd['flagged']))}")
            if pd['present']:
                print(f"      ✓  Present ({len(pd['present'])}): "
                      f"{', '.join(sorted(pd['present']))}")

        for ds in sorted(rpt['dates_no_checklist']):
            print(f"\n  📅  {ds}")
            print(f"      ⚠️  No eBird checklist found for this date.")
            print(f"      → Create one at: https://ebird.org/submit")

    print(f"\n  📊  Grand total missing entries: {grand_total_missing}")
    print(f"{sep}\n")


# ─── Edit-page automation ─────────────────────────────────────────────────────

# JavaScript to find and focus the Jump-to-species input.
# Real eBird edit page uses id='jumpToSpp', class='Suggest-input'
_FOCUS_SPECIES_INPUT_JS = """(function() {
    var candidates = [
        document.querySelector('#jumpToSpp'),
        document.querySelector('input.Suggest-input'),
        document.querySelector('input[id*="jumpTo" i]'),
        document.querySelector('input[placeholder*="species" i]'),
        document.querySelector('input[placeholder*="Enter species" i]'),
    ].filter(Boolean);
    if (!candidates.length) return null;
    var el = candidates[0];
    el.focus();
    el.select();
    el.value = '';
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    return el.id || el.placeholder || 'found';
})()"""

# JavaScript to collect visible autocomplete suggestions
_AUTOCOMPLETE_JS = """(function() {
    var selectors = [
        '[role="listbox"] [role="option"]',
        '[role="option"]',
        'ul[class*="suggest"] li',
        'ul[class*="autocomplete"] li',
        '.tt-suggestion',
        '[class*="suggestion"]',
        '[class*="autocomplete"] li',
    ];
    for (var s of selectors) {
        var els = Array.from(document.querySelectorAll(s))
                       .filter(e => e.offsetParent !== null);
        if (els.length > 0) {
            var items = els.slice(0, 5).map(e => e.innerText.trim());
            items = items.filter(s => !/no matches|no species/i.test(s));
            if (items.length > 0) {
                return JSON.stringify(items);
            }
        }
    }
    return '[]';
})()"""


def _get_edit_url(cl_url):
    """
    Build the correct eBird edit-species URL from a checklist URL.
    e.g. https://ebird.org/checklist/S330727457
      -> https://ebird.org/edit/checklist?subID=S330727457
    Falls back to scraping the 'Edit Species' link from the checklist page.
    """
    import re as _re
    m = _re.search(r'/(S\d+)', cl_url)
    if m:
        return f'https://ebird.org/edit/checklist?subID={m.group(1)}'
    return None


def navigate_to_edit(tab_ws, cl_url):
    """
    Navigate a tab to the checklist edit-species page.
    Returns True if the Jump-to-species input is found.
    """
    edit_url = _get_edit_url(cl_url)

    if edit_url:
        log(f"    Navigating to edit URL: {edit_url}")
        send_ws(tab_ws, 'Page.navigate', {'url': edit_url})
        wait_for_page_load(tab_ws)
        found = wait_for_js_condition(tab_ws, _FOCUS_SPECIES_INPUT_JS, timeout=5.0)
        if found:
            log(f"    Edit mode confirmed (input: '{found}')")
            return True

    # Fallback: scrape the 'Edit Species' link from the checklist page
    log(f"    Falling back: looking for 'Edit Species' link on {cl_url}")
    send_ws(tab_ws, 'Page.navigate', {'url': cl_url})
    wait_for_page_load(tab_ws)
    scraped_url = js_eval(tab_ws, """(function(){
        var a = Array.from(document.querySelectorAll('a'))
                     .find(a => /edit species/i.test(a.innerText));
        return a ? a.href : null;
    })()""")
    if scraped_url:
        log(f"    Found 'Edit Species' link: {scraped_url}")
        send_ws(tab_ws, 'Page.navigate', {'url': scraped_url})
        wait_for_page_load(tab_ws)
        found = wait_for_js_condition(tab_ws, _FOCUS_SPECIES_INPUT_JS, timeout=5.0)
        if found:
            log(f"    Edit mode confirmed via link (input: '{found}')")
            return True

    log(f"    Could not reach edit mode for {cl_url} — tab left open for manual entry.")
    return False


def _fuzzy_match(query, candidate):
    """True if all words of query appear in candidate (case-insensitive)."""
    q = query.lower()
    c = candidate.lower()
    return q in c or all(w in c for w in q.split())


def _normalize_species_for_search(name):
    """
    Generically normalize spelling and spacing differences between world/British taxonomy
    and eBird's canonical American taxonomy (e.g. Grey -> Gray, compound suffix words).
    """
    n = name.strip()
    
    # 1. Global case-insensitive replacement of "Grey" -> "Gray"
    n = re.sub(r'\bGrey\b', 'Gray', n, flags=re.IGNORECASE)
    n = re.sub(r'\bGrey-', 'Gray-', n, flags=re.IGNORECASE)
    
    # 2. Generic compound word transformations (case-insensitive suffixes/mid-words)
    replacements = [
        # Two-word chats -> single word
        (r'\bBush\s+Chat\b', 'Bushchat'),
        (r'\bStone\s+Chat\b', 'Stonechat'),
        (r'\bRock\s+Chat\b', 'Rockchat'),
        
        # Space-separated compounds -> hyphenated compounds
        (r'\bRock\s+Thrush\b', 'Rock-Thrush'),
        (r'\bTurtle\s+Dove\b', 'Turtle-Dove'),
        (r'\bCollared\s+Dove\b', 'Collared-Dove'),
        (r'\bBlue\s+Magpie\b', 'Blue-Magpie'),
        (r'\bWood\s+Pigeon\b', 'Wood-Pigeon'),
        (r'\bGreen\s+Pigeon\b', 'Green-Pigeon'),
        (r'\bImperial\s+Pigeon\b', 'Imperial-Pigeon'),
        (r'\bFruit\s+Dove\b', 'Fruit-Dove'),
        (r'\bTree\s+Creeper\b', 'Treecreeper'),
    ]
    
    for pattern, replacement in replacements:
        n = re.sub(pattern, replacement, n, flags=re.IGNORECASE)
        
    return n


def load_species_counts(target_dir):
    """Load custom species counts map from location.json in the target directory."""
    counts_map = {}
    loc_file = os.path.join(target_dir, 'location.json')
    if os.path.exists(loc_file):
        try:
            with open(loc_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                raw_counts = data.get('counts', {})
                counts_map = {k.strip(): str(v).upper() for k, v in raw_counts.items()}
        except Exception:
            pass
    return counts_map


def get_scientific_name(species_name):
    """Query GBIF dynamically to get the canonical scientific name for a species."""
    try:
        url = f"https://api.gbif.org/v1/species/search?q={urllib.parse.quote(species_name)}"
        req = urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'SuperPicky/1.0'}))
        res = json.loads(req.read().decode('utf-8'))
        results = res.get('results', [])
        if results:
            return results[0].get('canonicalName', species_name)
    except Exception:
        pass
    return species_name


def load_country_spelling_cache(country_code):
    """Load spelling cache for a specific country."""
    if not country_code:
        return {}
    cache_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"spelling_cache_{country_code.upper()}.json")
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_country_spelling_cache(country_code, cache):
    """Save spelling cache for a specific country."""
    if not country_code:
        return
    cache_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"spelling_cache_{country_code.upper()}.json")
    try:
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def add_one_species(tab_ws, species_name, count="X", country_code=None):
    """
    Type species_name into the Jump-to-species box, select the first
    autocomplete match, enter count. If name has no autocomplete match,
    infers canonical eBird name using scientific name and updates the country spelling cache.
    """
    # 1. Load country spelling cache
    cache = load_country_spelling_cache(country_code)

    # 2. Re-focus and clear the input before each species
    found = js_eval(tab_ws, _FOCUS_SPECIES_INPUT_JS)
    if not found:
        return 'no_input'

    # 3. Check if cached
    search_query = cache.get(species_name)
    if not search_query:
        # Fall back to baseline normalizations
        search_query = _normalize_species_for_search(species_name)

    # 4. Type the search query
    type_via_js(tab_ws, '#jumpToSpp', search_query)
    suggestions = wait_for_autocomplete(tab_ws)

    # 5. Check if "Add Species" button is present and click it (global checklist taxonomy search)
    clicked_add = js_eval(tab_ws, """(function() {
        var btn = Array.from(document.querySelectorAll('.Suggest-empty button, button')).find(b => /Add Species/i.test(b.innerText));
        if (btn && btn.offsetParent !== null) {
            btn.click();
            return true;
        }
        return false;
    })()""")
    if clicked_add:
        log("      Found 'Add Species' button — clicking to search entire taxonomy...")
        suggestions = wait_for_autocomplete(tab_ws, timeout=6.0)

    # 6. Collect suggestions
    if not suggestions:
        raw = js_eval(tab_ws, _AUTOCOMPLETE_JS)
        suggestions = json.loads(raw) if raw else []

    # 7. Check if suggestion matches
    matched_common_name = None
    if suggestions:
        first = suggestions[0].strip()
        first_clean = first.split(" - ")[0].strip()
        if _fuzzy_match(search_query, first_clean):
            matched_common_name = first_clean

    # 8. 🚨 INFERENCE FALLBACK: If no match, try scientific name!
    if not matched_common_name:
        log(f"      ⚠️ No match for '{search_query}'. Resolving scientific name from GBIF...")
        sci_name = get_scientific_name(species_name)
        if sci_name and sci_name != species_name:
            log(f"      👉 Retrying autocomplete with scientific name: '{sci_name}'")
            # Clear input and type scientific name
            type_via_js(tab_ws, '#jumpToSpp', sci_name)
            suggestions = wait_for_autocomplete(tab_ws)

            # Click Add Species if needed
            clicked_add = js_eval(tab_ws, """(function() {
                var btn = Array.from(document.querySelectorAll('.Suggest-empty button, button')).find(b => /Add Species/i.test(b.innerText));
                if (btn && btn.offsetParent !== null) {
                    btn.click();
                    return true;
                }
                return false;
            })()""")
            if clicked_add:
                suggestions = wait_for_autocomplete(tab_ws, timeout=6.0)

            # Collect suggestions again
            if not suggestions:
                raw = js_eval(tab_ws, _AUTOCOMPLETE_JS)
                suggestions = json.loads(raw) if raw else []
            if suggestions:
                first = suggestions[0].strip()
                first_clean = first.split(" - ")[0].strip()
                if "no matches" not in first_clean.lower() and "no species" not in first_clean.lower():
                    matched_common_name = first_clean
                    log(f"      💡 Successfully inferred eBird name: '{matched_common_name}' from scientific name '{sci_name}'")

    if not matched_common_name:
        log(f"      ❌ Could not resolve eBird name for '{species_name}' — skipping")
        press_key(tab_ws, 'Escape')
        return 'no_match'

    log(f"      ✓ Autocomplete matched: '{matched_common_name}'")

    # Save to country spelling cache if we learned a new mapping!
    if country_code and species_name not in cache:
        cache[species_name] = matched_common_name
        save_country_spelling_cache(country_code, cache)
        log(f"      💾 Learned & Cached spelling mapping: '{species_name}' ➔ '{matched_common_name}' ({country_code.upper()})")

    # Click the first autocomplete suggestion directly (more reliable than ArrowDown+Enter)
    click_result = js_eval(tab_ws, """(function() {
        var selectors = [
            '[role="listbox"] [role="option"]',
            '[role="option"]',
            'ul[class*="suggest"] li',
            'ul[class*="autocomplete"] li',
            '.tt-suggestion',
            '[class*="suggestion"]',
            '[class*="autocomplete"] li',
        ];
        for (var s of selectors) {
            var els = Array.from(document.querySelectorAll(s))
                           .filter(e => e.offsetParent !== null && !/no matches|no species/i.test(e.innerText));
            if (els.length > 0) {
                els[0].click();
                return 'clicked';
            }
        }
        return null;
    })()""")

    if not click_result:
        # Fallback: ArrowDown + Enter
        press_key(tab_ws, 'ArrowDown', 'ArrowDown')
        time.sleep(0.3)
        press_key(tab_ws, 'Enter', 'Enter')

    # Give eBird React time to render the newly added species row
    time.sleep(1.5)

    # Snapshot input.sc count before — lets us detect newly added row as fallback
    sc_count_before = js_eval(tab_ws, "document.querySelectorAll('input.sc').length") or 0

    # Extract clean common name (strip scientific name suffix for DOM search)
    clean_name = matched_common_name.split('(')[0].strip()
    # Strip any trailing scientific name (capitalised binomial after the common name)
    import re as _re
    clean_name = _re.sub(r'\s+[A-Z][a-z]+ [a-z]+.*$', '', clean_name).strip()
    log(f"      🔍 Searching for count input in row: '{clean_name}' (sc_before={sc_count_before})")

    # Wait for the species row with the matched name to appear (real eBird count inputs have class 'sc')
    row_appeared = wait_for_js_condition(tab_ws, f"""(function() {{
        var name = {json.dumps(clean_name)}.toLowerCase();
        var allText = Array.from(document.querySelectorAll('td, th, label, span, div'));
        for (var el of allText) {{
            var txt = (el.innerText || el.textContent || '').trim();
            if (txt.toLowerCase().indexOf(name) !== -1 && txt.length < name.length + 35) {{
                var row = el.closest('.SubmitChecklist-species, tr, li') || el.parentElement;
                if (row) {{
                    var inp = row.querySelector('input.sc, input[name*="count"]');
                    if (inp) return 'ready';
                }}
            }}
        }}
        return null;
    }})()""", timeout=5.0)

    # Find the count input (class 'sc') in the row that contains the species name text.
    # Skip if a value is already set — don't override what the user has entered.
    count_set = js_eval(tab_ws, f"""(function() {{
        var name = {json.dumps(clean_name)}.toLowerCase();
        var allText = Array.from(document.querySelectorAll('td, th, label, span, div'));
        for (var el of allText) {{
            var txt = (el.innerText || el.textContent || '').trim();
            if (txt.toLowerCase().indexOf(name) !== -1 && txt.length < name.length + 35) {{
                var row = el.closest('.SubmitChecklist-species, tr, li') || el.parentElement;
                if (!row) continue;
                var inp = row.querySelector('input.sc, input[name*="count"]');
                if (!inp) continue;
                // Always highlight the row green for easy review
                row.style.backgroundColor = '#e8f5e9';
                row.style.borderLeft = '5px solid #2e7d32';
                row.style.transition = 'background-color 0.3s';
                // Don't override an existing non-empty value
                var existing = (inp.value || '').trim();
                if (existing !== '' && existing !== '0') {{
                    return 'already_set';
                }}
                inp.focus();
                var nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                nativeSetter.call(inp, {json.dumps(str(count))});
                inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                inp.blur();
                return 'set';
            }}
        }}
        return false;
    }})()""")

    if count_set == 'already_set':
        log(f"      ℹ️  Count already set for '{species_name}' — keeping existing value")
    elif not count_set:
        # Fallback: use the newly added input.sc (detected by snapshot count)
        fallback_set = js_eval(tab_ws, f"""(function() {{
            var inputs = Array.from(document.querySelectorAll('input.sc'));
            if (inputs.length <= {sc_count_before}) return false;
            var inp = inputs[{sc_count_before}];  // the first newly added one
            inp.style.outline = '3px solid #2e7d32';  // highlight it visually
            var row = inp.closest('tr') || inp.parentElement;
            if (row) {{ row.style.backgroundColor = '#e8f5e9'; row.style.borderLeft = '5px solid #2e7d32'; }}
            var existing = (inp.value || '').trim();
            if (existing !== '' && existing !== '0') return 'already_set';
            inp.focus();
            var ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            ns.call(inp, {json.dumps(str(count))});
            inp.dispatchEvent(new Event('input', {{bubbles: true}}));
            inp.dispatchEvent(new Event('change', {{bubbles: true}}));
            inp.blur();
            return 'set';
        }})()""")
        if fallback_set in ('set', 'already_set'):
            log(f"      ✅ Count set via fallback (sc index {sc_count_before}) for '{species_name}'")
        else:
            log(f"      ⚠️ Could not find count input in species row for '{species_name}' — count not entered")

    time.sleep(0.3)
    press_key(tab_ws, 'Escape', 'Escape')  # dismiss any open dropdown
    time.sleep(0.2)

    return 'ok'



def auto_fill_checklist(cl_url, missing_species, counts_map=None, country_code=None):
    """
    Open the checklist edit page in a new tab and auto-fill all missing species.
    Tab is left open for user review — nothing is saved automatically.

    Returns dict with lists: ok, no_match, error.
    """
    log(f"\n    Opening edit tab: {cl_url}")
    tab = open_tab(cl_url)
    tab_ws = websocket.create_connection(
        tab['webSocketDebuggerUrl'], origin='http://localhost:9222')
    wait_for_page_load(tab_ws)

    results = {'ok': [], 'no_match': [], 'error': []}

    in_edit_mode = navigate_to_edit(tab_ws, cl_url)
    if not in_edit_mode:
        log(f"    Tab left open — please switch to edit mode manually.")
        tab_ws.close()
        return results

    if counts_map is None:
        counts_map = {}

    for sp in sorted(missing_species):
        count = counts_map.get(sp, "X")
        log(f"    ➕ Adding: {sp} (Present status/count: '{count}')")
        try:
            outcome = add_one_species(tab_ws, sp, count=count, country_code=country_code)
            results[outcome].append(sp)
        except Exception as e:
            log(f"    ❌ Error adding '{sp}': {e}")
            results['error'].append(sp)

    tab_ws.close()
    log(f"    💾 Tab left open for review — click Save when ready.")
    return results


# ─── Pipeline-callable wrapper ────────────────────────────────────────────────

def run_add_missing_species(target_dir, target_dates):
    """
    Step 7: Find missing species in eBird checklists and auto-fill them.
    Designed to be called from ebird_pipeline.py.

    Args:
        target_dir   : the processed folder (e.g. E:\\2026\\Solan)
        target_dates : list of date strings like ['24 Apr 2026', ...]
    """
    log("🚀 Step 7: Auto-filling missing species into eBird checklists...")

    if not WEBSOCKET_AVAILABLE:
        log("⚠️ websocket-client not installed — skipping.")
        return
    if not target_dates:
        log("ℹ️ No target dates — skipping.")
        return

    ws_url = get_ws_url()
    if not ws_url:
        log("⚠️ Chrome not reachable on port 9222 — skipping.")
        log("   Start Chrome with: --remote-debugging-port=9222")
        return

    date_to_species, photographed = collect_species_by_date(target_dir)
    if not photographed:
        log("ℹ️ No species folders found — skipping.")
        return

    log(f"Photographed species ({len(photographed)}): "
        f"{', '.join(sorted(photographed))}")

    # Find checklists for target dates
    log(f"Finding checklists for {len(target_dates)} date(s)...")
    found_checklists = find_checklists_for_dates(target_dates)
    if not found_checklists:
        log("No checklists found for target dates.")
        return

    # Fetch existing species per checklist
    all_urls = [u for urls in found_checklists.values() for u in urls]
    log(f"Scanning {len(all_urls)} checklist(s) for existing species...")
    checklist_species = {}
    detected_country = None
    detected_state = None
    for url in all_urls:
        species, loc = extract_species_and_location(url)
        checklist_species[url] = species
        if not detected_country and loc.get('country') and loc.get('state'):
            detected_country = loc['country']
            detected_state = loc['state']
            log(f"    🌍 Detected eBird location: {detected_state}, {detected_country}")

    # Load from folder location config if eBird location scraping failed
    folder_country = detected_country
    folder_state = detected_state
    if not folder_country or not folder_state:
        folder_country, folder_state = get_location_config(target_dir)

    # Per-date: pick the largest checklist, compute missing, auto-fill
    all_results = {}
    for ds in sorted(found_checklists, key=lambda d: datetime.strptime(d, '%d %b %Y')):
        urls = found_checklists[ds]
        best = max(urls, key=lambda u: len(checklist_species.get(u, set())))
        
        # Use only species photographed on this specific date
        todays_photographed = date_to_species.get(ds, set())
        missing_raw = todays_photographed - checklist_species.get(best, set())
        
        try:
            m_val = datetime.strptime(ds, '%d %b %Y').month
        except Exception:
            m_val = None
        missing = {sp for sp in missing_raw if is_species_plausible_for_location(sp, target_dir, country=folder_country, state=folder_state, month=m_val)}

        if not missing:
            log(f"  {ds}: ✅ All species present in checklist — skipping.")
            continue

        log(f"  {ds}: {len(missing)} missing → targeting {best}")
        counts_map = load_species_counts(target_dir)
        result = auto_fill_checklist(best, missing, counts_map=counts_map, country_code=folder_country)
        all_results[ds] = {'url': best, 'result': result, 'missing': missing}

    # Final summary
    sep = '═' * 62
    print(f"\n{sep}")
    print("  📊  STEP 7 SUMMARY — Missing Species Auto-Fill")
    print(sep)
    if not all_results:
        print("\n  ✅ No missing species found across all dates!")
    for ds, data in sorted(all_results.items()):
        res = data['result']
        print(f"\n  📅 {ds}  {data['url']}")
        if res['ok']:
            print(f"    ✅ Added   ({len(res['ok'])}): {', '.join(res['ok'])}")
        if res['no_match']:
            print(f"    ⚠️ Skipped ({len(res['no_match'])}): "
                  f"{', '.join(res['no_match'])}  (no autocomplete match)")
        if res['error']:
            print(f"    ❌ Error   ({len(res['error'])}): {', '.join(res['error'])}")
    print(f"\n  💡 Review open Chrome tabs and click Save when ready.")
    print(f"{sep}\n")
    log("✅ Step 7 complete.")


# ─── CLI entry point ──────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python ebird_add_missing_species.py <dir> [--report-only] [--country <CODE>] [--state <NAME>]")
        sys.exit(1)

    root_dir = os.path.abspath(sys.argv[1])
    report_only = '--report-only' in sys.argv

    if not os.path.isdir(root_dir):
        print(f"Error: {root_dir} is not a directory")
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
    get_location_config(root_dir, country_arg=country_arg, state_arg=state_arg)

    if not WEBSOCKET_AVAILABLE:
        print("Error: websocket-client not installed.")
        print("Run: uv pip install websocket-client")
        sys.exit(1)

    if not get_ws_url():
        print("Error: Chrome not reachable on port 9222.")
        print("Start Chrome with: --remote-debugging-port=9222")
        sys.exit(1)

    log(f"Root directory: {root_dir}")
    reports = build_missing_report(root_dir)
    if not reports:
        return

    print_report(reports)

    if report_only:
        log("--report-only: exiting without auto-fill.")
        return

    # Auto-fill each folder
    for rpt in reports:
        folder_name = os.path.basename(rpt['folder'])
        dates_with_missing = [d for d, pd in rpt['per_date'].items() if pd['missing']]
        if not dates_with_missing:
            log(f"{folder_name}: ✅ Nothing to add.")
            continue

        log(f"\n{'=' * 60}")
        log(f"Auto-filling: {folder_name} ({len(dates_with_missing)} date(s) with missing species)")
        log(f"{'=' * 60}")

        for ds in sorted(dates_with_missing, key=lambda d: datetime.strptime(d, '%d %b %Y')):
            pd = rpt['per_date'][ds]
            counts_map = load_species_counts(rpt['folder'])
            log(f"  {ds}: filling {len(pd['missing'])} species into {pd['best_cl']}")
            folder_country, folder_state = get_location_config(rpt['folder'])
            result = auto_fill_checklist(pd['best_cl'], pd['missing'], counts_map=counts_map, country_code=folder_country)
            if result['ok']:
                log(f"    ✅ Added: {', '.join(result['ok'])}")
            if result['no_match']:
                log(f"    ⚠️ No match: {', '.join(result['no_match'])}")
            if result['error']:
                log(f"    ❌ Error: {', '.join(result['error'])}")

    log("\n💡 Review all open Chrome tabs and click Save when ready.")


if __name__ == '__main__':
    main()
