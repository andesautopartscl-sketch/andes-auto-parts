#!/usr/bin/env python
"""Quick test to verify new routes are registered."""
from app import create_app

app = create_app()
routes = list(app.url_map.iter_rules())
target_routes = ['enviar_email', 'whatsapp']

print("\n" + "="*60)
print("CHECKING FOR NEW DOCUMENT SEND ROUTES")
print("="*60)

found = []
for r in routes:
    r_str = str(r)
    for target in target_routes:
        if target in r_str:
            found.append(r_str)
            print(f"[OK] Found: {r_str}")

if not found:
    print("[FAIL] NO ROUTES FOUND")
else:
    print(f"\n[OK] Total new routes found: {len(found)}")

print("="*60 + "\n")
