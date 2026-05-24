import sys, json
sys.path.append('scripts/ebird_tools')
from ebird_add_missing_species import js_eval, open_tab, wait_for_page_load
import websocket

tab = open_tab('https://ebird.org/edit/checklist?subID=S330727457')
ws = websocket.create_connection(tab['webSocketDebuggerUrl'], origin='http://localhost:9222')
wait_for_page_load(ws)

expr = (
    "(function() {"
    "var inputs = Array.from(document.querySelectorAll('input'));"
    "return JSON.stringify(inputs.map(function(el) {"
    "return {id: el.id, name: el.name, cls: el.className, type: el.type, ph: el.placeholder, val: el.value};"
    "}).filter(function(i) { return i.type !== 'hidden'; }));"
    "})()"
)
result = js_eval(ws, expr)
data = json.loads(result) if result else []
for item in data:
    print(json.dumps(item))
ws.close()
