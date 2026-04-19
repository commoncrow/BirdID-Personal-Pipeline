import re, os

fpath = r'C:\Users\sandy\AppData\Local\Temp\claude\c--workspace-SuperPicky\fa1e1978-71b1-4aca-8213-6178268ac18f\tasks\bui9dto5o.output'
with open(fpath, 'r', encoding='utf-8') as f:
    content = f.read()

entries = re.split(r'\[(\d+)/138\]', content)

group_shots = []
current_idx = None

for i, part in enumerate(entries):
    if i % 2 == 1:
        current_idx = int(part)
    elif current_idx is not None and i > 0:
        fname_match = re.search(r'Processing: (.+?)[\r\n]', part)
        birds_match = re.search(r'(\d+) birds,', part)
        species_match = re.search(r'Top species: (.+?) \((\d+\.\d+)%\)', part)

        if fname_match and birds_match:
            n_birds = int(birds_match.group(1))
            if n_birds >= 2:
                fname = os.path.basename(fname_match.group(1).strip())
                species = species_match.group(1) if species_match else 'Unknown'
                pct = species_match.group(2) if species_match else '?'
                group_shots.append((n_birds, current_idx, fname, species, pct))
        current_idx = None

group_shots.sort(reverse=True)
print(f'Total group shots (2+ birds): {len(group_shots)}')
print()
for n, idx, fname, species, pct in group_shots:
    print(f'{n:3d} birds | [{idx}/138] {fname} | {species} ({pct}%)')
