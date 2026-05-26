"""
FLASK ROUTE DEBUGGER
=====================
Run this to see all registered routes and identify conflicts
"""

from app import create_app

app = create_app()

print("\n" + "="*80)
print("🔍 FLASK REGISTERED ROUTES (Execution Order)")
print("="*80 + "\n")

routes_by_path = {}

for rule in app.url_map.iter_rules():
    if rule.endpoint.startswith('static'):
        continue
    
    path = rule.rule
    method = ','.join(rule.methods - {'HEAD', 'OPTIONS'}) or 'GET'
    endpoint = rule.endpoint
    
    # Group by path to find conflicts
    if path not in routes_by_path:
        routes_by_path[path] = []
    routes_by_path[path].append({
        'endpoint': endpoint,
        'methods': method,
        'rule': rule
    })

# Print all routes
for path in sorted(routes_by_path.keys()):
    routes = routes_by_path[path]
    
    if len(routes) > 1:
        print(f"⚠️  CONFLICT DETECTED on {path}")
        for i, r in enumerate(routes):
            print(f"   [{i}] {r['endpoint']:30s} [{r['methods']}]")
    else:
        r = routes[0]
        # Extract blueprint name
        blueprint = r['endpoint'].split('.')[0] if '.' in r['endpoint'] else 'root'
        print(f"✓  {path:40s} → {r['endpoint']:30s} [{r['methods']}]")

print("\n" + "="*80)
print("🔑 BLUEPRINT REGISTRATION ORDER (from app/__init__.py)")
print("="*80 + "\n")

blueprints_encountered = {}
for rule in app.url_map.iter_rules():
    if rule.endpoint.startswith('static'):
        continue
    
    endpoint_parts = rule.endpoint.split('.')
    if len(endpoint_parts) >= 1:
        bp_name = endpoint_parts[0]
        if bp_name not in blueprints_encountered:
            blueprints_encountered[bp_name] = True
            print(f"  {len(blueprints_encountered):2d}. Blueprint: {bp_name}")

print("\n" + "="*80)
print("🚨 CRITICAL CONFLICTS TO RESOLVE")
print("="*80 + "\n")

conflicts_found = False
for path, routes in routes_by_path.items():
    if len(routes) > 1:
        conflicts_found = True
        print(f"\n🔴 Path: {path}")
        for i, r in enumerate(routes):
            print(f"   → Endpoint: {r['endpoint']}")
            print(f"     Methods: {r['methods']}")

if not conflicts_found:
    print("✅ No duplicate routes found\n")

print("\n" + "="*80)
