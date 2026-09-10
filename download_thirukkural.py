import json
import urllib.request

urls = [
    "https://raw.githubusercontent.com/tk120404/thirukkural/master/thirukkural.json",
    "https://raw.githubusercontent.com/fizerkhan/thirukural/master/data/thirukural.json",
    "https://raw.githubusercontent.com/vijayanandrp/Thirukkural-Tamil-Dataset/master/thirukkural.json",
    "https://raw.githubusercontent.com/Karthikraja1/Thirukkural-JSON/master/thirukkural.json"
]
output_path = "c:/Users/acer/OneDrive/Documents/KURAL-TEST/thirukkural.json"

for url in urls:
    print(f"Trying {url}")
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
        
        kurals = []
        if isinstance(data, dict) and "kural" in data:
            kurals = data["kural"]
        elif isinstance(data, list):
            kurals = data
        elif isinstance(data, dict) and "kurals" in data:
            kurals = data["kurals"]
            
        print(f"Total kurals found: {len(kurals)}")
        if len(kurals) == 1330:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(kurals, f, ensure_ascii=False, indent=2)
            print("Successfully saved 1330 Kurals.")
            break
        else:
            print("Found but count is not 1330.")
    except Exception as e:
        print(f"Failed: {e}")
