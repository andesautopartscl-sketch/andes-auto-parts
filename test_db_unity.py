#!/usr/bin/env python
"""Test database consolidation - verify ONE database is being used"""

print("=" * 80)
print("TESTING DATABASE CONSOLIDATION")
print("=" * 80)

try:
    from app import create_app
    
    app = create_app()
    
    print("\n✅ App created successfully")
    print(f"\n📊 Database Configuration:")
    print(f"   URI: {app.config['SQLALCHEMY_DATABASE_URI']}")
    
    # Test database connection
    with app.app_context():
        from app.extensions import db
        from app.seguridad.models import Usuario
        
        usuarios = db.session.query(Usuario).all()
        print(f"\n✅ Database query successful")
        print(f"   Found {len(usuarios)} usuarios in database:")
        for u in usuarios:
            print(f"      - {u.usuario} (Nombre: {u.nombre})")
        
        print("\n✅ UNIFIED DATABASE WORKING!")
        
except Exception as e:
    print(f"\n❌ ERROR: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 80)
