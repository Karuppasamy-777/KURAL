import json
with open("c:/Users/acer/OneDrive/Documents/KURAL-TEST/thirukkural.json", "r", encoding="utf-8") as f:
    data = json.load(f)
print("Keys in first item:", data[0].keys())
print("First item:", data[0])
