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

ET_PATH = 'C:/workspace/SuperPicky/exiftools_win/exiftool.exe'
STAR_LABELS = ['3star_excellent', '2star_good']


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


# ─── ExifTool helpers ─────────────────────────────────────────────────────────

def scan_shooting_dates(target_dir):
    """Return sorted list of shooting date strings like '14 Jan 2026'."""
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    args = [ET_PATH, '-r', '-DateTimeOriginal', '-j',
            '-ext', 'jpg', '-ext', 'JPG', target_dir]
    result = subprocess.run(args, capture_output=True, text=True,
                            encoding='utf-8', env=env)
    dates = set()
    try:
        for item in json.loads(result.stdout):
            raw = item.get('DateTimeOriginal', '')
            if raw:
                try:
                    dates.add(datetime.strptime(raw[:10], '%Y:%m:%d')
                              .strftime('%d %b %Y'))
                except Exception:
                    pass
    except Exception:
        pass
    return sorted(dates)


# ─── Folder helpers ───────────────────────────────────────────────────────────

def collect_photographed_species(target_dir):
    """Return set of bird species names from 3star/2star folder structure."""
    species = set()
    for label in STAR_LABELS:
        d = os.path.join(target_dir, label)
        if not os.path.isdir(d):
            continue
        for item in os.listdir(d):
            full = os.path.join(d, item)
            if not os.path.isdir(full):
                continue
            if item == 'Other_Birds':
                # BirdID-classified subdirs live here
                for sub in os.listdir(full):
                    if os.path.isdir(os.path.join(full, sub)) and sub != 'Other_Birds':
                        species.add(sub)
            else:
                species.add(item)
    return species


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


def press_key(ws, key, code=None):
    """Simulate a key press (down + up)."""
    for ktype in ('keyDown', 'keyUp'):
        p = {'type': ktype, 'key': key}
        if code:
            p['code'] = code
        send_ws(ws, 'Input.dispatchKeyEvent', p)
    time.sleep(0.08)


# ─── eBird checklist discovery ────────────────────────────────────────────────

def find_checklists_for_dates(target_dates):
    """
    Navigate mychecklists and return {date_str: [url, ...]} for target_dates.
    Requires Chrome to already have a page tab.
    """
    ws_url = get_ws_url()
    if not ws_url:
        return {}

    ws = websocket.create_connection(ws_url, origin='http://localhost:9222')
    send_ws(ws, 'Page.navigate', {
        'url': 'https://ebird.org/mychecklists?currentRow=1&sortBy=date&o=desc'})
    time.sleep(6)
    ws_url = get_ws_url()
    ws.close()
    ws = websocket.create_connection(ws_url, origin='http://localhost:9222')

    found = {}
    oldest_target = min(datetime.strptime(d, '%d %b %Y') for d in target_dates)

    for start_row in range(1, 300, 20):
        log(f"  Scanning mychecklists row {start_row}–{start_row + 19}...")
        send_ws(ws, 'Page.navigate', {
            'url': f'https://ebird.org/mychecklists?currentRow={start_row}&sortBy=date&o=desc'})
        time.sleep(5)

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
    return found


def extract_species_from_checklist(cl_url):
    """Open checklist in a new tab, extract species names, close tab."""
    try:
        tab = open_tab(cl_url)
        time.sleep(4)
        tw = websocket.create_connection(
            tab['webSocketDebuggerUrl'], origin='http://localhost:9222')
        r = send_ws(tw, 'Runtime.evaluate', {'expression': """(function(){
            var names = new Set();
            document.querySelectorAll("a[href*='/species/']").forEach(e=>names.add(e.innerText.trim()));
            document.querySelectorAll(".Obs-species .Heading,.SpeciesName,.Obs .Heading")
                    .forEach(e=>names.add(e.innerText.trim()));
            return JSON.stringify([...names].filter(s=>s.length>2));
        })()""", 'returnByValue': True})
        val = r.get('result', {}).get('result', {}).get('value', '[]')
        species = set(json.loads(val))
        tw.close()
        close_tab(tab['id'])
        return species
    except Exception as e:
        log(f"  ⚠️ Error reading {cl_url}: {e}")
        return set()


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
        photographed = collect_photographed_species(folder)
        dates = scan_shooting_dates(folder)
        all_target_dates.update(dates)
        folder_meta.append({'folder': folder,
                            'photographed': photographed,
                            'dates': dates})
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
    for url in all_urls:
        log(f"  → {url}")
        checklist_species[url] = extract_species_from_checklist(url)
        log(f"    {len(checklist_species[url])} species")

    # Build per-folder report
    reports = []
    for fm in folder_meta:
        folder = fm['folder']
        photographed = fm['photographed']
        dates = fm['dates']

        per_date = {}
        for ds in dates:
            urls = all_checklists.get(ds, [])
            if not urls:
                continue
            # Largest = most species already logged
            best = max(urls, key=lambda u: len(checklist_species.get(u, set())))
            present_in_best = checklist_species.get(best, set())
            per_date[ds] = {
                'best_cl': best,
                'all_cls': urls,
                'cl_species_count': len(present_in_best),
                'missing': photographed - present_in_best,
                'present': photographed & present_in_best,
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
                print(f"      ✅ All {len(pd['present'])} photographed species present!")
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
            return JSON.stringify(els.slice(0, 5).map(e => e.innerText.trim()));
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
        time.sleep(5)
        found = js_eval(tab_ws, _FOCUS_SPECIES_INPUT_JS)
        if found:
            log(f"    Edit mode confirmed (input: '{found}')")
            return True

    # Fallback: scrape the 'Edit Species' link from the checklist page
    log(f"    Falling back: looking for 'Edit Species' link on {cl_url}")
    send_ws(tab_ws, 'Page.navigate', {'url': cl_url})
    time.sleep(4)
    scraped_url = js_eval(tab_ws, """(function(){
        var a = Array.from(document.querySelectorAll('a'))
                     .find(a => /edit species/i.test(a.innerText));
        return a ? a.href : null;
    })()""")
    if scraped_url:
        log(f"    Found 'Edit Species' link: {scraped_url}")
        send_ws(tab_ws, 'Page.navigate', {'url': scraped_url})
        time.sleep(5)
        found = js_eval(tab_ws, _FOCUS_SPECIES_INPUT_JS)
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


def add_one_species(tab_ws, species_name):
    """
    Type species_name into the Jump-to-species box, select the first
    autocomplete match, enter count = 1.

    Returns: 'ok' | 'no_input' | 'no_match'
    """
    # Re-focus and clear the input before each species
    found = js_eval(tab_ws, _FOCUS_SPECIES_INPUT_JS)
    if not found:
        return 'no_input'

    # Normalize species name to match eBird's canonical taxonomy (American English + specific compounds)
    search_query = _normalize_species_for_search(species_name)

    # Type the species name — Input.insertText triggers autocomplete events
    send_ws(tab_ws, 'Input.insertText', {'text': search_query})
    time.sleep(2.5)  # wait for autocomplete dropdown

    # Check for "Add Species" button and click if present (for species not on the default checklist)
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
        time.sleep(2.5)  # wait for global search results

    # Collect suggestions
    raw = js_eval(tab_ws, _AUTOCOMPLETE_JS)
    suggestions = json.loads(raw) if raw else []

    if not suggestions:
        log(f"      ⚠️ No autocomplete results for '{species_name}' — skipping")
        press_key(tab_ws, 'Escape')
        return 'no_match'

    first = suggestions[0].strip()
    if not _fuzzy_match(search_query, first):
        log(f"      ⚠️ First suggestion '{first}' doesn't match '{search_query}' — skipping")
        press_key(tab_ws, 'Escape')
        time.sleep(0.3)
        return 'no_match'

    log(f"      ✓ Autocomplete matched: '{first}'")

    # Select first suggestion: ArrowDown → Enter
    press_key(tab_ws, 'ArrowDown', 'ArrowDown')
    time.sleep(0.3)
    press_key(tab_ws, 'Enter', 'Enter')
    time.sleep(1.2)  # wait for species row to appear + count field to focus

    # Enter count = 1  (eBird focuses the count field after species selection)
    send_ws(tab_ws, 'Input.insertText', {'text': '1'})
    time.sleep(0.3)
    press_key(tab_ws, 'Tab', 'Tab')
    time.sleep(0.5)

    return 'ok'


def auto_fill_checklist(cl_url, missing_species):
    """
    Open the checklist edit page in a new tab and auto-fill all missing species.
    Tab is left open for user review — nothing is saved automatically.

    Returns dict with lists: ok, no_match, error.
    """
    log(f"\n    Opening edit tab: {cl_url}")
    tab = open_tab(cl_url)
    time.sleep(3)
    tab_ws = websocket.create_connection(
        tab['webSocketDebuggerUrl'], origin='http://localhost:9222')

    results = {'ok': [], 'no_match': [], 'error': []}

    in_edit_mode = navigate_to_edit(tab_ws, cl_url)
    if not in_edit_mode:
        log(f"    Tab left open — please switch to edit mode manually.")
        tab_ws.close()
        return results

    for sp in sorted(missing_species):
        log(f"    ➕ Adding: {sp}")
        try:
            outcome = add_one_species(tab_ws, sp)
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

    photographed = collect_photographed_species(target_dir)
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
    for url in all_urls:
        checklist_species[url] = extract_species_from_checklist(url)

    # Per-date: pick the largest checklist, compute missing, auto-fill
    all_results = {}
    for ds in sorted(found_checklists, key=lambda d: datetime.strptime(d, '%d %b %Y')):
        urls = found_checklists[ds]
        best = max(urls, key=lambda u: len(checklist_species.get(u, set())))
        missing = photographed - checklist_species.get(best, set())

        if not missing:
            log(f"  {ds}: ✅ All species present in checklist — skipping.")
            continue

        log(f"  {ds}: {len(missing)} missing → targeting {best}")
        result = auto_fill_checklist(best, missing)
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
        print("Usage: python ebird_add_missing_species.py <dir> [--report-only]")
        sys.exit(1)

    root_dir = os.path.abspath(sys.argv[1])
    report_only = '--report-only' in sys.argv

    if not os.path.isdir(root_dir):
        print(f"Error: {root_dir} is not a directory")
        sys.exit(1)

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
            log(f"  {ds}: filling {len(pd['missing'])} species into {pd['best_cl']}")
            result = auto_fill_checklist(pd['best_cl'], pd['missing'])
            if result['ok']:
                log(f"    ✅ Added: {', '.join(result['ok'])}")
            if result['no_match']:
                log(f"    ⚠️ No match: {', '.join(result['no_match'])}")
            if result['error']:
                log(f"    ❌ Error: {', '.join(result['error'])}")

    log("\n💡 Review all open Chrome tabs and click Save when ready.")


if __name__ == '__main__':
    main()
