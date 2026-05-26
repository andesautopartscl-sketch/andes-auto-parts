================================================================================
📋 ULTIMO_INGRESO - COMPLETE CODE REFERENCE
================================================================================

This file shows all modified code sections for the ultimo_ingreso feature.


================================================================================
1. LOGIN ROUTE (app/seguridad/routes.py - Lines 38-65)
================================================================================

@seguridad_bp.route("/login2", methods=["GET", "POST"])
def login():
    print("\n" + "="*60)
    print("🔐 LOGIN ROUTE CALLED")
    print("="*60)
    
    error = None

    if request.method == "POST":

        username = request.form.get("username")
        password = request.form.get("password")
        print(f"📝 Login attempt: {username}")

        user = Usuario.query.filter_by(usuario=username).first()

        if user and user.activo:

            if check_password_hash(user.password_hash, password):

                session["usuario_id"] = user.id
                session["usuario_nombre"] = user.nombre
                session["usuario_rol"] = user.rol.nombre

                # ✅ UPDATE TIMESTAMPS ON SUCCESSFUL LOGIN
                print(f"✅ Password correct for user: {username}")
                print(f"📅 Setting ultimo_acceso and ultimo_ingreso to: {datetime.utcnow()}")
                
                user.ultimo_acceso = datetime.utcnow()
                user.ultimo_ingreso = datetime.utcnow()  # ✅ Record last login
                db.session.commit()
                
                print(f"✅ Database updated:")
                print(f"   - user.ultimo_acceso: {user.ultimo_acceso}")
                print(f"   - user.ultimo_ingreso: {user.ultimo_ingreso}")
                print("="*60 + "\n")
                
                return redirect("/buscar")
            else:
                print(f"❌ Password incorrect for user: {username}")
        else:
            print(f"❌ User not found or inactive: {username}")

        error = "Usuario o contraseña incorrectos"

    return render_template("seguridad/login.html", error=error)


KEY POINTS:
    • Line 59: user.ultimo_ingreso = datetime.utcnow()
    • Line 60: db.session.commit() saves to database
    • Lines 57-65: Console logging for debugging
    • Only executes on successful password check


================================================================================
2. API ENDPOINT (app/seguridad/routes.py - Lines 103-138)
================================================================================

@seguridad_bp.route("/api/usuarios")
def api_usuarios():
    print("\n" + "="*60)
    print("📋 API /usuarios CALLED")
    print("="*60)

    usuarios = Usuario.query.all()

    data = []

    for u in usuarios:
        # ✅ FORMAT DATES FOR DISPLAY
        ultimo_ingreso_fmt = u.ultimo_ingreso.strftime("%d-%m-%Y %H:%M") if u.ultimo_ingreso else "-"
        fecha_nacimiento_fmt = u.fecha_nacimiento.strftime("%d-%m-%Y") if u.fecha_nacimiento else "-"
        
        print(f"👤 {u.usuario}: ultimo_ingreso = {u.ultimo_ingreso} → Formatted: {ultimo_ingreso_fmt}")
        
        data.append({
            "id": u.id,
            "nombre": u.nombre,
            "usuario": u.usuario,
            "rol": u.rol.nombre if u.rol else "",
            "activo": u.activo,
            # ✅ ALL EXTENDED FIELDS
            "correo": u.correo or "-",
            "telefono": u.telefono or "-",
            "direccion": u.direccion or "-",
            "genero": u.genero or "-",
            "fecha_nacimiento": fecha_nacimiento_fmt,
            "rut": u.rut or "-",
            "ultimo_ingreso": ultimo_ingreso_fmt  # ✅ THIS IS THE KEY LINE
        })

    print(f"✅ Returning {len(data)} usuarios")
    print("="*60 + "\n")
    return jsonify(data)


KEY POINTS:
    • Line 116: Format check: if u.ultimo_ingreso else "-"
    • Line 118: strftime("%d-%m-%Y %H:%M") = DD-MM-YYYY HH:MM format
    • Line 120: Console log for debugging
    • Line 133: Include in JSON response
    • Shows "-" if user hasn't logged in yet


FORMAT EXPLANATION:
    %d = Day of month (01-31)
    %m = Month (01-12)
    %Y = Year (2026)
    %H = Hour (00-23)
    %M = Minute (00-59)
    
    Example: 2026-03-18 14:35:22.123456 → "18-03-2026 14:35"


================================================================================
3. FRONTEND TABLE HEADER (app/templates/buscar.html - Lines 398-408)
================================================================================

<table>
    <thead>
        <tr>
            <th>Nombre</th>
            <th>Usuario</th>
            <th>Correo</th>
            <th>Teléfono</th>
            <th>Género</th>
            <th>Último Ingreso</th>        <!-- ✅ NEW COLUMN -->
            <th>Rol</th>
            <th>Activo</th>
            <th>Acciones</th>
        </tr>
    </thead>

    <tbody id="usuarios-body">
    </tbody>
</table>


KEY POINTS:
    • Column header displays as "Último Ingreso"
    • Positioned after Género, before Rol
    • Total of 9 columns (was 5 before)
    • tbody id="usuarios-body" is where rows are inserted


================================================================================
4. FRONTEND JAVASCRIPT (app/templates/buscar.html - Lines 568-635)
================================================================================

function cargarUsuarios(){
    console.log("🔄 cargarUsuarios() CALLED");
    
    const tbody = document.getElementById("usuarios-body");
    console.log("🎯 Target tbody element:", tbody);
    
    if(!tbody) {
        console.error("❌ CRÍTICO: tbody con ID 'usuarios-body' NO ENCONTRADO en el DOM!");
        return;
    }
    
    // Limpiar tabla
    tbody.innerHTML = "";
    console.log("🧹 Tbody limpio");
    
    console.log("🌐 Fetching from /seguridad/api/usuarios...");
    fetch("/seguridad/api/usuarios")
    .then(res => {
        console.log("📡 Response status:", res.status);
        return res.json();
    })
    .then(data => {
        console.log("✅ Datos recibidos:", data);
        console.log("📊 Total usuarios:", data ? data.length : 0);
        
        if(!data || data.length === 0){
            console.warn("⚠️ No hay usuarios en la respuesta");
            tbody.innerHTML = '<tr><td colspan="9" style="text-align:center; padding:15px; color:#999;">No hay usuarios registrados</td></tr>';
            return;
        }
        
        // Construir HTML
        let html = '';
        data.forEach((u, index) => {
            console.log(`   👤 Usuario ${index}: ${u.usuario} (${u.nombre})`);
            console.log(`      📅 ultimo_ingreso: "${u.ultimo_ingreso}"`);
            
            // ✅ EXTRACT ALL FIELDS
            const correo = u.correo || '-';
            const telefono = u.telefono || '-';
            const genero = u.genero || '-';
            const ultimo_ingreso = u.ultimo_ingreso || '-';  // ✅ KEY LINE
            
            console.log(`      ✓ Renderizando con ultimo_ingreso: "${ultimo_ingreso}"`);
            
            html += `<tr>
                <td>${u.nombre}</td>
                <td>${u.usuario}</td>
                <td>${correo}</td>
                <td>${telefono}</td>
                <td>${genero}</td>
                <td><strong>${ultimo_ingreso}</strong></td>  <!-- ✅ DISPLAY HERE -->
                <td>${u.rol || 'Sin rol'}</td>
                <td>${u.activo ? '✔' : '✖'}</td>
                <td>
                    <span class="accion editar" onclick="editarUsuario(${u.id})" title="Editar">✏</span>
                    <span class="accion toggle" onclick="toggleUsuario(${u.id})" title="${u.activo ? 'Desactivar' : 'Activar'}">${u.activo ? '🔒' : '🔓'}</span>
                    <span class="accion eliminar" onclick="eliminarUsuario(${u.id})" title="Eliminar">🗑</span>
                </td>
            </tr>`;
        });
        
        console.log("🎨 Inserting", data.length, "rows into table");
        tbody.innerHTML = html;
        console.log("✨ Tabla renderizada con", data.length, "usuarios");
        console.log("🔍 HTML en tbody:", tbody.innerHTML.substring(0, 100) + "...");
    })
    .catch(err => {
        console.error("❌ Error cargando usuarios:", err);
        console.error("Stack trace:", err.stack);
        tbody.innerHTML = '<tr><td colspan="9" style="color:red; text-align:center; padding:15px;">❌ Error al cargar usuarios</td></tr>';
    });
}


KEY POINTS:
    • Line 600: Extract ultimo_ingreso from API response
    • Line 613: Display in <td><strong> for emphasis
    • Line 595-600: Console logs for debugging
    • Colspan="9" accounts for 9 columns
    • Falls back to "-" if no value


================================================================================
5. MODEL DEFINITION (app/seguridad/models.py - Already Defined)
================================================================================

class Usuario(db.Model):
    __tablename__ = "usuarios_sistema"
    
    # Original fields
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120))
    usuario = db.Column(db.String(80), unique=True)
    password_hash = db.Column(db.String(200))
    rol_id = db.Column(db.Integer, db.ForeignKey("roles.id"))
    activo = db.Column(db.Boolean, default=True)
    en_linea = db.Column(db.Boolean, default=False)
    
    # Extended fields
    correo = db.Column(db.String(120), unique=True, nullable=True)
    telefono = db.Column(db.String(20), nullable=True)
    direccion = db.Column(db.String(255), nullable=True)
    genero = db.Column(db.String(20), nullable=True)
    fecha_nacimiento = db.Column(db.Date, nullable=True)
    rut = db.Column(db.String(20), unique=True, nullable=True)
    
    # ✅ TIMESTAMPS (ALREADY IN MODEL)
    ultimo_acceso = db.Column(db.DateTime, nullable=True)
    ultimo_ingreso = db.Column(db.DateTime, nullable=True)  # ✅ This field
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    
    rol = db.relationship("Rol")


NO CHANGES NEEDED - Field already exists!


================================================================================
6. DATABASE MIGRATIONS (Not Required)
================================================================================

The ultimo_ingreso field was already created in the database during the
previous extended user system migration.

Field details:
    Column Name:  ultimo_ingreso
    Type:         DATETIME
    Nullable:     YES
    Default:      NULL
    Index:        No
    Unique:       No
    Foreign Key:  No


If field is missing, you can add it manually with:

    -- SQLite
    ALTER TABLE usuarios_sistema ADD COLUMN ultimo_ingreso DATETIME;
    
    -- PostgreSQL
    ALTER TABLE usuarios_sistema ADD COLUMN ultimo_ingreso TIMESTAMP;
    
    -- MySQL
    ALTER TABLE usuarios_sistema ADD COLUMN ultimo_ingreso DATETIME;


================================================================================
📊 DATA FLOW EXAMPLE
================================================================================

BEFORE LOGIN:
    usuario.ultimo_ingreso = NULL

DURING LOGIN:
    user.ultimo_ingreso = datetime.utcnow()  # Set to current UTC time
    db.session.commit()                       # Save to database

AFTER LOGIN:
    API response: {"ultimo_ingreso": "18-03-2026 14:35"}
    Table display: "18-03-2026 14:35" (bold)
    Console log: "📅 ultimo_ingreso: \"18-03-2026 14:35\""

NEXT LOGIN:
    user.ultimo_ingreso = datetime.utcnow()  # Updates to NEW time
    db.session.commit()                       # Overwrites old value


================================================================================
🧪 TESTING CHECKLIST
================================================================================

✓ Field exists in database:
  mysql> DESCRIBE usuarios_sistema;  (look for ultimo_ingreso)

✓ Model has field:
  python> from app.seguridad.models import Usuario
  python> print(hasattr(Usuario, 'ultimo_ingreso'))  # Should be True

✓ Login updates field:
  1. Watch terminal for "Setting ultimo_acceso and ultimo_ingreso"
  2. See "Database updated: - user.ultimo_ingreso: 2026-03-18..."

✓ API returns field:
  curl http://localhost:5000/seguridad/api/usuarios
  Look for "ultimo_ingreso": "18-03-2026 14:35"

✓ Frontend displays field:
  1. Open user dashboard (⚙️ Opciones)
  2. See "Último Ingreso" column header
  3. See formatted date in table (DD-MM-YYYY HH:MM)

✓ Console shows logs:
  F12 → Console tab in browser
  Should see: "📅 ultimo_ingreso: \"18-03-2026 14:35\""

✓ Multiple logins work:
  1. Log out and log in again
  2. Verify timestamp updates to NEW time
  3. Check table updates immediately


================================================================================
⚡ QUICK START VERIFICATION
================================================================================

1. Run verification script:
   $ python test_ultimo_ingreso.py

2. Start app:
   $ python run.py

3. Test login flow:
   a. Open http://localhost:5000/login
   b. Login with test user
   c. Check terminal output
   d. Go to /buscar, click ⚙️ Opciones
   e. Check table for "Último Ingreso" column

4. Check browser console (F12):
   a. Should see "🔄 cargarUsuarios() CALLED"
   b. Should see "📅 ultimo_ingreso:" entries
   c. No error messages


================================================================================
🔗 RELATED DOCUMENTATION
================================================================================

See: ULTIMO_INGRESO_DOCUMENTATION.md
     For complete implementation guide, troubleshooting, and flow diagrams.
