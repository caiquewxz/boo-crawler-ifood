import json, time
from pathlib import Path

s = json.loads(Path("configs/session.json").read_text())
keys = ("aAccessToken", "aRefreshToken", "aSessionId", "aAccountId", "aDeviceId")
found = {c["name"]: c for c in s.get("cookies", []) if c["name"] in keys}

for name in keys:
    if name not in found:
        print(name + ": AUSENTE")
        continue
    c = found[name]
    exp = c.get("expires", -1)
    if exp and exp > 0:
        rem = exp - time.time()
        status = "OK, expira em " + str(round(rem/3600, 1)) + "h" if rem > 0 else "EXPIRADO"
    else:
        status = "session cookie (sem expiry)"
    print(name + ": " + status)
    if name == "aAccessToken" and c.get("value"):
        print("  valor (50 chars): " + c["value"][:50])
