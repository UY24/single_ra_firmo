import json
import pandas as pd

# ---------- CONFIG ----------
INPUT_FILE = "input.json"   # your JSON file
OUTPUT_FILE = "output.xlsx"


# ---------- FLATTEN FUNCTION ----------
def flatten_json(y, parent_key='', sep='_'):
    items = []
    if isinstance(y, dict):
        for k, v in y.items():
            new_key = f"{parent_key}{sep}{k}" if parent_key else k
            items.extend(flatten_json(v, new_key, sep=sep).items())
    elif isinstance(y, list):
        # Convert lists to string (or expand if needed)
        items.append((parent_key, " | ".join(map(str, y))))
    else:
        items.append((parent_key, y))
    return dict(items)


# ---------- LOAD JSON ----------
with open(INPUT_FILE, "r", encoding="utf-8") as f:
    data = json.load(f)

# ---------- PROCESS ----------
rows = []

for item in data.get("results", []):
    flat_item = {}

    # Flatten top-level fields
    for key in ["row_index", "status", "error"]:
        flat_item[key] = item.get(key)

    # Flatten input
    if "input" in item:
        flat_item.update(flatten_json(item["input"], "input"))

    # Flatten output
    if "output" in item:
        flat_item.update(flatten_json(item["output"], "output"))

    rows.append(flat_item)

# ---------- CREATE DATAFRAME ----------
df = pd.DataFrame(rows)

# ---------- EXPORT ----------
df.to_excel(OUTPUT_FILE, index=False)

print(f"✅ Flattened Excel saved as: {OUTPUT_FILE}")
