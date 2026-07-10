import os
import sqlite3
import secrets
import string
import uuid
from datetime import datetime, date, timedelta
from functools import wraps
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, redirect, url_for, session, flash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "healio-fallback-key-for-local-dev-only")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "healio-fallback-admin-for-local-dev-only")
ADMIN_CONTACT_EMAIL = os.environ.get("ADMIN_CONTACT_EMAIL", "admin@healio.local")
app.permanent_session_lifetime = timedelta(days=30)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

DB_PATH = "healio.db"
UPLOAD_FOLDER = os.path.join("static", "uploads")
ALLOWED_PHOTO_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
ALLOWED_DOCUMENT_EXTENSIONS = ALLOWED_PHOTO_EXTENSIONS | {"pdf"}


@app.after_request
def add_no_cache_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.errorhandler(413)
def handle_file_too_large(e):
    flash("That file is too large (max 5MB).")
    return redirect(request.referrer or url_for("index"))


# ---------- Database ----------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def create_database():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS doctors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doctor_code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            password TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS patients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_code TEXT UNIQUE NOT NULL,
            doctor_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            password TEXT NOT NULL,
            diagnosis TEXT,
            medication TEXT,
            treatment_duration TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (doctor_id) REFERENCES doctors(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_checkins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            check_date DATE NOT NULL,
            took_medication BOOLEAN NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (patient_id) REFERENCES patients(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS weekly_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            week_number INTEGER NOT NULL,
            symptoms TEXT,
            side_effects TEXT,
            noticeable_changes TEXT,
            symptom_trend TEXT,
            satisfaction_rating INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (patient_id) REFERENCES patients(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS doctor_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            doctor_id INTEGER NOT NULL,
            note_text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (patient_id) REFERENCES patients(id),
            FOREIGN KEY (doctor_id) REFERENCES doctors(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS flags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            flag_reason TEXT NOT NULL,
            resolved BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (patient_id) REFERENCES patients(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            doctor_id INTEGER NOT NULL,
            appointment_at TIMESTAMP NOT NULL,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (patient_id) REFERENCES patients(id),
            FOREIGN KEY (doctor_id) REFERENCES doctors(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient_type TEXT NOT NULL,
            recipient_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            link TEXT,
            read_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS medical_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            doctor_id INTEGER NOT NULL,
            record_type TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT,
            record_date DATE,
            file_filename TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (patient_id) REFERENCES patients(id),
            FOREIGN KEY (doctor_id) REFERENCES doctors(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS prescriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            doctor_id INTEGER NOT NULL,
            medication_name TEXT NOT NULL,
            dosage TEXT,
            instructions TEXT,
            prescribed_date DATE,
            file_filename TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (patient_id) REFERENCES patients(id),
            FOREIGN KEY (doctor_id) REFERENCES doctors(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS patient_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            original_name TEXT NOT NULL,
            file_size INTEGER,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (patient_id) REFERENCES patients(id)
        )
    """)

    conn.commit()
    conn.close()


def migrate_database():
    conn = get_db()
    cursor = conn.cursor()
    for table in ("doctors", "patients"):
        cursor.execute(f"PRAGMA table_info({table})")
        columns = {row["name"] for row in cursor.fetchall()}
        if "photo_filename" not in columns:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN photo_filename TEXT")

    cursor.execute("PRAGMA table_info(appointments)")
    appointment_columns = {row["name"] for row in cursor.fetchall()}
    if "status" not in appointment_columns:
        cursor.execute("ALTER TABLE appointments ADD COLUMN status TEXT DEFAULT 'scheduled'")
    if "appointment_type" not in appointment_columns:
        cursor.execute("ALTER TABLE appointments ADD COLUMN appointment_type TEXT")

    conn.commit()
    conn.close()


# ---------- Helpers ----------

def generate_code(prefix, length=6):
    chars = string.ascii_uppercase + string.digits
    return prefix + "".join(secrets.choice(chars) for _ in range(length))


def generate_password(length=10):
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def save_uploaded_file(file_storage, allowed_extensions, old_filename=None):
    """Validates and saves an uploaded file, returning its stored filename or None."""
    if not file_storage or not file_storage.filename:
        return None, "No file selected."

    ext = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in file_storage.filename else ""
    if ext not in allowed_extensions:
        return None, "Unsupported file type."

    filename = f"{uuid.uuid4().hex}.{ext}"
    safe_name = secure_filename(filename)
    file_storage.save(os.path.join(UPLOAD_FOLDER, safe_name))

    if old_filename:
        old_path = os.path.join(UPLOAD_FOLDER, old_filename)
        if os.path.exists(old_path):
            os.remove(old_path)

    return safe_name, None


def save_uploaded_photo(file_storage, old_filename=None):
    filename, error = save_uploaded_file(file_storage, ALLOWED_PHOTO_EXTENSIONS, old_filename)
    if error == "Unsupported file type.":
        error = "Unsupported file type. Use PNG, JPG, GIF, or WEBP."
    return filename, error


def save_uploaded_document(file_storage, old_filename=None):
    filename, error = save_uploaded_file(file_storage, ALLOWED_DOCUMENT_EXTENSIONS, old_filename)
    if error == "Unsupported file type.":
        error = "Unsupported file type. Use PNG, JPG, GIF, WEBP, or PDF."
    return filename, error


def format_file_size(num_bytes):
    if not num_bytes:
        return ""
    for unit in ("B", "KB", "MB"):
        if num_bytes < 1024:
            return f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


def login_required_doctor(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "doctor_id" not in session:
            return redirect(url_for("doctor_login"))
        return f(*args, **kwargs)
    return decorated


def login_required_patient(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "patient_id" not in session:
            return redirect(url_for("patient_login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated


def create_notification(recipient_type, recipient_id, message, link=None):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO notifications (recipient_type, recipient_id, message, link)
        VALUES (?, ?, ?, ?)
    """, (recipient_type, recipient_id, message, link))
    conn.commit()
    conn.close()


def check_and_create_flags(patient_id):
    """Run after check-ins / weekly reports. Flags missed doses and worsening symptom trends."""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT p.name, p.doctor_id FROM patients p WHERE p.id = ?
    """, (patient_id,))
    patient_row = cursor.fetchone()
    patient_name = patient_row["name"]
    doctor_id = patient_row["doctor_id"]

    def notify_doctor_of_flag(reason_label):
        cursor.execute("""
            INSERT INTO notifications (recipient_type, recipient_id, message, link)
            VALUES ('doctor', ?, ?, ?)
        """, (doctor_id, f"New alert for {patient_name}: {reason_label}", url_for("patient_detail", patient_id=patient_id)))

    # Missed dose flag: last 2 daily check-ins both "No"
    cursor.execute("""
        SELECT took_medication FROM daily_checkins
        WHERE patient_id = ?
        ORDER BY check_date DESC LIMIT 2
    """, (patient_id,))
    recent = cursor.fetchall()
    if len(recent) == 2 and not recent[0]["took_medication"] and not recent[1]["took_medication"]:
        cursor.execute("""
            SELECT id FROM flags WHERE patient_id = ? AND flag_reason = 'missed_doses' AND resolved = 0
        """, (patient_id,))
        if not cursor.fetchone():
            cursor.execute("""
                INSERT INTO flags (patient_id, flag_reason) VALUES (?, 'missed_doses')
            """, (patient_id,))
            notify_doctor_of_flag("missed doses")

    # Worsening symptoms flag: last 2 weekly reports both "worse"
    cursor.execute("""
        SELECT symptom_trend FROM weekly_reports
        WHERE patient_id = ?
        ORDER BY week_number DESC LIMIT 2
    """, (patient_id,))
    recent_weeks = cursor.fetchall()
    if len(recent_weeks) == 2 and recent_weeks[0]["symptom_trend"] == "worse" and recent_weeks[1]["symptom_trend"] == "worse":
        cursor.execute("""
            SELECT id FROM flags WHERE patient_id = ? AND flag_reason = 'worsening_symptoms' AND resolved = 0
        """, (patient_id,))
        if not cursor.fetchone():
            cursor.execute("""
                INSERT INTO flags (patient_id, flag_reason) VALUES (?, 'worsening_symptoms')
            """, (patient_id,))
            notify_doctor_of_flag("worsening symptoms")

    conn.commit()
    conn.close()


@app.context_processor
def inject_admin_contact_email():
    return {"admin_contact_email": ADMIN_CONTACT_EMAIL}


@app.context_processor
def inject_notifications():
    recipient_type = recipient_id = None
    if "doctor_id" in session:
        recipient_type, recipient_id = "doctor", session["doctor_id"]
    elif "patient_id" in session:
        recipient_type, recipient_id = "patient", session["patient_id"]

    if not recipient_type:
        return {"unread_notifications_count": 0}

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) as c FROM notifications
        WHERE recipient_type = ? AND recipient_id = ? AND read_at IS NULL
    """, (recipient_type, recipient_id))
    count = cursor.fetchone()["c"]
    conn.close()
    return {"unread_notifications_count": count}


# ---------- Public / Welcome ----------

@app.route("/")
def index():
    return render_template("welcome.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ---------- Admin ----------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin_doctors"))
        flash("Incorrect admin password.")
    return render_template("admin_login.html")


@app.route("/admin/doctors", methods=["GET", "POST"])
@admin_required
def admin_doctors():
    conn = get_db()
    cursor = conn.cursor()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if name:
            doctor_code = generate_code("DR-")
            password = generate_password()
            cursor.execute("""
                INSERT INTO doctors (doctor_code, name, password) VALUES (?, ?, ?)
            """, (doctor_code, name, password))
            conn.commit()
            flash(f"Doctor added — Code: {doctor_code} | Password: {password} (save this, it won't be shown again in full)")

    cursor.execute("SELECT id, doctor_code, name, created_at FROM doctors ORDER BY created_at DESC")
    doctors = cursor.fetchall()
    conn.close()
    return render_template("admin_doctors.html", doctors=doctors)


# ---------- Doctor ----------

@app.route("/doctor/login", methods=["GET", "POST"])
def doctor_login():
    if request.method == "POST":
        doctor_code = request.form.get("doctor_code", "").strip().upper()
        password = request.form.get("password", "")

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM doctors WHERE doctor_code = ?", (doctor_code,))
        doctor = cursor.fetchone()
        conn.close()

        if doctor and doctor["password"] == password:
            session["doctor_id"] = doctor["id"]
            session["doctor_name"] = doctor["name"]
            session["doctor_photo"] = doctor["photo_filename"]
            return redirect(url_for("doctor_dashboard"))
        flash("Invalid doctor code or password.")
    return render_template("doctor_login.html")


@app.route("/doctor/dashboard")
@login_required_doctor
def doctor_dashboard():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT p.id, p.patient_code, p.name, p.diagnosis, p.photo_filename,
        (SELECT COUNT(*) FROM flags f WHERE f.patient_id = p.id AND f.resolved = 0) as active_flags
        FROM patients p WHERE p.doctor_id = ?
        ORDER BY active_flags DESC, p.created_at DESC
    """, (session["doctor_id"],))
    patients = cursor.fetchall()

    today = date.today().isoformat()
    cursor.execute("""
        SELECT COUNT(*) as c FROM appointments
        WHERE doctor_id = ? AND appointment_at LIKE ?
    """, (session["doctor_id"], today + "%"))
    todays_appointment_count = cursor.fetchone()["c"]

    cursor.execute("""
        SELECT a.*, p.name as patient_name FROM appointments a
        JOIN patients p ON a.patient_id = p.id
        WHERE a.doctor_id = ? AND a.appointment_at >= ?
        ORDER BY a.appointment_at ASC LIMIT 1
    """, (session["doctor_id"], datetime.now().isoformat()))
    next_appointment = cursor.fetchone()

    cursor.execute("SELECT COUNT(*) as c FROM appointments WHERE doctor_id = ?", (session["doctor_id"],))
    total_appointments = cursor.fetchone()["c"]
    cursor.execute("SELECT COUNT(*) as c FROM appointments WHERE doctor_id = ? AND status = 'completed'", (session["doctor_id"],))
    completed_appointments = cursor.fetchone()["c"]

    cursor.execute("""
        SELECT f.*, p.name as patient_name, p.photo_filename as patient_photo FROM flags f
        JOIN patients p ON f.patient_id = p.id
        WHERE p.doctor_id = ? AND f.resolved = 0
        ORDER BY f.created_at DESC LIMIT 5
    """, (session["doctor_id"],))
    urgent_alerts = cursor.fetchall()

    cursor.execute("""
        SELECT a.*, p.name as patient_name, p.photo_filename as patient_photo FROM appointments a
        JOIN patients p ON a.patient_id = p.id
        WHERE a.doctor_id = ? AND a.appointment_at >= ? AND (a.status IS NULL OR a.status = 'scheduled')
        ORDER BY a.appointment_at ASC LIMIT 5
    """, (session["doctor_id"], datetime.now().isoformat()))
    upcoming_appointments = cursor.fetchall()

    conn.close()

    total_patients = len(patients)
    flagged_count = sum(1 for p in patients if p["active_flags"] > 0)

    hour = datetime.now().hour
    if hour < 12:
        greeting = "Good morning"
    elif hour < 17:
        greeting = "Good afternoon"
    else:
        greeting = "Good evening"

    return render_template(
        "doctor_dashboard.html",
        patients=patients,
        total_patients=total_patients,
        flagged_count=flagged_count,
        todays_appointment_count=todays_appointment_count,
        next_appointment=next_appointment,
        total_appointments=total_appointments,
        completed_appointments=completed_appointments,
        urgent_alerts=urgent_alerts,
        upcoming_appointments=upcoming_appointments,
        active="home",
        greeting=greeting,
    )


@app.route("/doctor/patients")
@login_required_doctor
def doctor_patients():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.id, p.patient_code, p.name, p.diagnosis, p.photo_filename,
        (SELECT COUNT(*) FROM flags f WHERE f.patient_id = p.id AND f.resolved = 0) as active_flags
        FROM patients p WHERE p.doctor_id = ?
        ORDER BY active_flags DESC, p.created_at DESC
    """, (session["doctor_id"],))
    patients = cursor.fetchall()
    conn.close()
    return render_template("doctor_patients.html", patients=patients, active="patients")


@app.route("/doctor/patients/new", methods=["GET", "POST"])
@login_required_doctor
def new_patient():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        diagnosis = request.form.get("diagnosis", "").strip()
        medication = request.form.get("medication", "").strip()
        treatment_duration = request.form.get("treatment_duration", "").strip()

        if name:
            patient_code = generate_code("PT-")
            password = generate_password()

            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO patients (patient_code, doctor_id, name, password, diagnosis, medication, treatment_duration)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (patient_code, session["doctor_id"], name, password, diagnosis, medication, treatment_duration))
            conn.commit()
            conn.close()

            flash(f"Patient added — Code: {patient_code} | Password: {password} (share these with the patient securely)")
            return redirect(url_for("doctor_dashboard"))

    return render_template("new_patient.html", active="add")


@app.route("/doctor/patients/<int:patient_id>", methods=["GET", "POST"])
@login_required_doctor
def patient_detail(patient_id):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM patients WHERE id = ? AND doctor_id = ?", (patient_id, session["doctor_id"]))
    patient = cursor.fetchone()
    if not patient:
        conn.close()
        return redirect(url_for("doctor_dashboard"))

    if request.method == "POST":
        note_text = request.form.get("note_text", "").strip()
        if note_text:
            cursor.execute("""
                INSERT INTO doctor_notes (patient_id, doctor_id, note_text) VALUES (?, ?, ?)
            """, (patient_id, session["doctor_id"], note_text))
            conn.commit()
            create_notification(
                "patient", patient_id,
                f"{session['doctor_name']} added a new note to your file",
                url_for("patient_notes"),
            )

    cursor.execute("""
        SELECT * FROM weekly_reports WHERE patient_id = ? ORDER BY week_number DESC
    """, (patient_id,))
    weekly_reports = cursor.fetchall()

    cursor.execute("""
        SELECT * FROM daily_checkins WHERE patient_id = ? ORDER BY check_date DESC LIMIT 14
    """, (patient_id,))
    daily_checkins = cursor.fetchall()

    cursor.execute("""
        SELECT dn.*, d.name as doctor_name FROM doctor_notes dn
        JOIN doctors d ON dn.doctor_id = d.id
        WHERE dn.patient_id = ? ORDER BY dn.created_at DESC
    """, (patient_id,))
    notes = cursor.fetchall()

    cursor.execute("""
        SELECT * FROM flags WHERE patient_id = ? AND resolved = 0 ORDER BY created_at DESC
    """, (patient_id,))
    active_flags = cursor.fetchall()

    cursor.execute("""
        SELECT * FROM appointments WHERE patient_id = ? ORDER BY appointment_at DESC
    """, (patient_id,))
    appointments = cursor.fetchall()

    cursor.execute("""
        SELECT * FROM medical_records WHERE patient_id = ? ORDER BY created_at DESC
    """, (patient_id,))
    medical_records = cursor.fetchall()

    cursor.execute("""
        SELECT * FROM prescriptions WHERE patient_id = ? ORDER BY created_at DESC
    """, (patient_id,))
    prescriptions = cursor.fetchall()

    cursor.execute("""
        SELECT * FROM patient_documents WHERE patient_id = ? ORDER BY uploaded_at DESC
    """, (patient_id,))
    patient_documents = cursor.fetchall()

    conn.close()

    adherence_pct = None
    if daily_checkins:
        taken = sum(1 for c in daily_checkins if c["took_medication"])
        adherence_pct = round(100 * taken / len(daily_checkins))

    trend_points = []
    reports_asc = list(reversed(weekly_reports))
    rated_reports = [r for r in reports_asc if r["satisfaction_rating"]]
    for i, r in enumerate(rated_reports):
        x = 0 if len(rated_reports) == 1 else round(i * 800 / (len(rated_reports) - 1))
        y = round(180 - (r["satisfaction_rating"] - 1) / 4 * 160)
        trend_points.append({"x": x, "y": y, "week": r["week_number"], "rating": r["satisfaction_rating"]})

    return render_template(
        "patient_detail.html",
        patient=patient,
        weekly_reports=weekly_reports,
        daily_checkins=daily_checkins,
        notes=notes,
        active_flags=active_flags,
        appointments=appointments,
        medical_records=medical_records,
        prescriptions=prescriptions,
        patient_documents=patient_documents,
        format_file_size=format_file_size,
        adherence_pct=adherence_pct,
        trend_points=trend_points,
        active="patients",
    )


@app.route("/doctor/patients/<int:patient_id>/appointments/new", methods=["POST"])
@login_required_doctor
def new_appointment(patient_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM patients WHERE id = ? AND doctor_id = ?", (patient_id, session["doctor_id"]))
    patient = cursor.fetchone()
    if not patient:
        conn.close()
        return redirect(url_for("doctor_dashboard"))

    appointment_at = request.form.get("appointment_at", "").strip()
    appointment_type = request.form.get("appointment_type", "").strip()
    notes = request.form.get("notes", "").strip()
    if appointment_at:
        cursor.execute("""
            INSERT INTO appointments (patient_id, doctor_id, appointment_at, appointment_type, notes)
            VALUES (?, ?, ?, ?, ?)
        """, (patient_id, session["doctor_id"], appointment_at, appointment_type, notes))
        conn.commit()
        flash("Appointment scheduled.")
        create_notification(
            "patient", patient_id,
            f"New appointment scheduled for {appointment_at}",
            url_for("patient_checkin"),
        )

    conn.close()
    return redirect(url_for("patient_detail", patient_id=patient_id))


@app.route("/doctor/patients/<int:patient_id>/records/new", methods=["POST"])
@login_required_doctor
def new_medical_record(patient_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM patients WHERE id = ? AND doctor_id = ?", (patient_id, session["doctor_id"]))
    patient = cursor.fetchone()
    if not patient:
        conn.close()
        return redirect(url_for("doctor_dashboard"))

    record_type = request.form.get("record_type", "lab")
    title = request.form.get("title", "").strip()
    summary = request.form.get("summary", "").strip()
    record_date = request.form.get("record_date", "").strip()

    if title:
        filename, error = save_uploaded_document(request.files.get("file"))
        if error and request.files.get("file") and request.files.get("file").filename:
            flash(error)
        cursor.execute("""
            INSERT INTO medical_records (patient_id, doctor_id, record_type, title, summary, record_date, file_filename)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (patient_id, session["doctor_id"], record_type, title, summary, record_date, filename))
        conn.commit()
        flash("Medical record added.")
        create_notification(
            "patient", patient_id,
            f"{session['doctor_name']} added a new {'scan' if record_type == 'scan' else 'lab result'}: {title}",
            url_for("patient_reports"),
        )

    conn.close()
    return redirect(url_for("patient_detail", patient_id=patient_id))


@app.route("/doctor/patients/<int:patient_id>/prescriptions/new", methods=["POST"])
@login_required_doctor
def new_prescription(patient_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM patients WHERE id = ? AND doctor_id = ?", (patient_id, session["doctor_id"]))
    patient = cursor.fetchone()
    if not patient:
        conn.close()
        return redirect(url_for("doctor_dashboard"))

    medication_name = request.form.get("medication_name", "").strip()
    dosage = request.form.get("dosage", "").strip()
    instructions = request.form.get("instructions", "").strip()
    prescribed_date = request.form.get("prescribed_date", "").strip()

    if medication_name:
        filename, error = save_uploaded_document(request.files.get("file"))
        if error and request.files.get("file") and request.files.get("file").filename:
            flash(error)
        cursor.execute("""
            INSERT INTO prescriptions (patient_id, doctor_id, medication_name, dosage, instructions, prescribed_date, file_filename)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (patient_id, session["doctor_id"], medication_name, dosage, instructions, prescribed_date, filename))
        conn.commit()
        flash("Prescription added.")
        create_notification(
            "patient", patient_id,
            f"{session['doctor_name']} added a new prescription: {medication_name}",
            url_for("patient_reports"),
        )

    conn.close()
    return redirect(url_for("patient_detail", patient_id=patient_id))


@app.route("/doctor/prescriptions/new", methods=["POST"])
@login_required_doctor
def new_prescription_any_patient():
    patient_id = request.form.get("patient_id", "")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM patients WHERE id = ? AND doctor_id = ?", (patient_id, session["doctor_id"]))
    patient = cursor.fetchone()
    if not patient:
        conn.close()
        flash("Select a valid patient.")
        return redirect(url_for("doctor_dashboard"))

    medication_name = request.form.get("medication_name", "").strip()
    dosage = request.form.get("dosage", "").strip()
    instructions = request.form.get("instructions", "").strip()
    prescribed_date = request.form.get("prescribed_date", "").strip()

    if medication_name:
        cursor.execute("""
            INSERT INTO prescriptions (patient_id, doctor_id, medication_name, dosage, instructions, prescribed_date)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (patient_id, session["doctor_id"], medication_name, dosage, instructions, prescribed_date))
        conn.commit()
        flash("Prescription added.")
        create_notification(
            "patient", patient_id,
            f"{session['doctor_name']} added a new prescription: {medication_name}",
            url_for("patient_reports"),
        )

    conn.close()
    return redirect(url_for("doctor_dashboard"))


@app.route("/doctor/flags/<int:flag_id>/resolve", methods=["POST"])
@login_required_doctor
def resolve_flag(flag_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE flags SET resolved = 1 WHERE id = ?
        AND patient_id IN (SELECT id FROM patients WHERE doctor_id = ?)
    """, (flag_id, session["doctor_id"]))
    conn.commit()
    patient_id = request.form.get("patient_id")
    conn.close()
    return redirect(url_for("patient_detail", patient_id=patient_id))


@app.route("/doctor/notifications")
@login_required_doctor
def doctor_notifications():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM notifications WHERE recipient_type = 'doctor' AND recipient_id = ?
        ORDER BY created_at DESC
    """, (session["doctor_id"],))
    notifications = cursor.fetchall()
    cursor.execute("""
        UPDATE notifications SET read_at = CURRENT_TIMESTAMP
        WHERE recipient_type = 'doctor' AND recipient_id = ? AND read_at IS NULL
    """, (session["doctor_id"],))
    conn.commit()
    conn.close()
    return render_template("doctor_notifications.html", notifications=notifications)


@app.route("/doctor/profile")
@login_required_doctor
def doctor_profile():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM doctors WHERE id = ?", (session["doctor_id"],))
    doctor = cursor.fetchone()
    cursor.execute("SELECT COUNT(*) as c FROM patients WHERE doctor_id = ?", (session["doctor_id"],))
    patient_count = cursor.fetchone()["c"]
    conn.close()
    return render_template("doctor_profile.html", doctor=doctor, patient_count=patient_count)


@app.route("/doctor/profile/photo", methods=["POST"])
@login_required_doctor
def doctor_profile_photo():
    filename, error = save_uploaded_photo(request.files.get("photo"), session.get("doctor_photo"))
    if error:
        flash(error)
    else:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE doctors SET photo_filename = ? WHERE id = ?", (filename, session["doctor_id"]))
        conn.commit()
        conn.close()
        session["doctor_photo"] = filename
        flash("Profile photo updated.")
    return redirect(url_for("doctor_profile"))


@app.route("/doctor/appointments/new", methods=["POST"])
@login_required_doctor
def new_appointment_any_patient():
    patient_id = request.form.get("patient_id", "")
    appointment_at = request.form.get("appointment_at", "").strip()
    appointment_type = request.form.get("appointment_type", "").strip()
    notes = request.form.get("notes", "").strip()

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM patients WHERE id = ? AND doctor_id = ?", (patient_id, session["doctor_id"]))
    patient = cursor.fetchone()
    if not patient:
        conn.close()
        flash("Select a valid patient.")
        return redirect(url_for("doctor_schedule"))

    if appointment_at:
        cursor.execute("""
            INSERT INTO appointments (patient_id, doctor_id, appointment_at, appointment_type, notes)
            VALUES (?, ?, ?, ?, ?)
        """, (patient_id, session["doctor_id"], appointment_at, appointment_type, notes))
        conn.commit()
        flash("Appointment scheduled.")
        create_notification(
            "patient", patient_id,
            f"New appointment scheduled for {appointment_at}",
            url_for("patient_checkin"),
        )

    conn.close()
    return redirect(url_for("doctor_schedule"))


@app.route("/doctor/appointments/<int:appointment_id>/status", methods=["POST"])
@login_required_doctor
def update_appointment_status(appointment_id):
    new_status = request.form.get("status", "")
    if new_status not in ("completed", "cancelled"):
        return redirect(url_for("doctor_schedule"))

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT a.patient_id FROM appointments a
        JOIN patients p ON a.patient_id = p.id
        WHERE a.id = ? AND p.doctor_id = ?
    """, (appointment_id, session["doctor_id"]))
    appt = cursor.fetchone()
    if appt:
        cursor.execute("UPDATE appointments SET status = ? WHERE id = ?", (new_status, appointment_id))
        conn.commit()
        flash(f"Appointment marked {new_status}.")
        if new_status == "cancelled":
            create_notification(
                "patient", appt["patient_id"],
                "Your doctor cancelled an upcoming appointment",
                url_for("patient_checkin"),
            )
    conn.close()

    view = request.form.get("view", "week")
    day = request.form.get("day", "")
    return redirect(url_for("doctor_schedule", view=view, day=day) if day else url_for("doctor_schedule", view=view))


@app.route("/doctor/schedule")
@login_required_doctor
def doctor_schedule():
    view = request.args.get("view", "week")
    if view not in ("today", "week", "month"):
        view = "week"
    day = request.args.get("day", "")

    today = date.today()
    monday = today - timedelta(days=today.weekday())
    week_days = [monday + timedelta(days=i) for i in range(7)]

    if view == "today":
        range_start, range_end = today, today + timedelta(days=1)
    elif view == "month":
        range_start = today.replace(day=1)
        range_end = date(today.year + 1, 1, 1) if today.month == 12 else date(today.year, today.month + 1, 1)
    else:
        selected_day = None
        if day:
            try:
                candidate = date.fromisoformat(day)
                if monday <= candidate <= monday + timedelta(days=6):
                    selected_day = candidate
            except ValueError:
                pass
        if selected_day:
            range_start, range_end = selected_day, selected_day + timedelta(days=1)
        else:
            range_start, range_end = monday, monday + timedelta(days=7)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT a.*, p.name as patient_name, p.photo_filename as patient_photo,
        (SELECT COUNT(*) FROM flags f WHERE f.patient_id = p.id AND f.resolved = 0) as patient_active_flags
        FROM appointments a
        JOIN patients p ON a.patient_id = p.id
        WHERE a.doctor_id = ? AND a.appointment_at >= ? AND a.appointment_at < ?
        ORDER BY a.appointment_at ASC
    """, (session["doctor_id"], range_start.isoformat(), range_end.isoformat()))
    appointments = cursor.fetchall()

    cursor.execute("SELECT id, name FROM patients WHERE doctor_id = ? ORDER BY name", (session["doctor_id"],))
    patients = cursor.fetchall()
    cursor.execute("SELECT COUNT(*) as c FROM patients WHERE doctor_id = ?", (session["doctor_id"],))
    total_patients = cursor.fetchone()["c"]
    conn.close()

    return render_template(
        "doctor_schedule.html",
        appointments=appointments,
        patients=patients,
        total_patients=total_patients,
        view=view,
        selected_day=day,
        week_days=week_days,
        today=today,
        active="schedule",
    )


@app.route("/doctor/reports")
@login_required_doctor
def doctor_reports():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT id, name, patient_code FROM patients WHERE doctor_id = ? ORDER BY name", (session["doctor_id"],))
    patient_rows = cursor.fetchall()

    total_active_flags = 0
    total_resolved_flags = 0
    for p in patient_rows:
        cursor.execute("SELECT COUNT(*) as c FROM flags WHERE patient_id = ? AND resolved = 0", (p["id"],))
        total_active_flags += cursor.fetchone()["c"]
        cursor.execute("SELECT COUNT(*) as c FROM flags WHERE patient_id = ? AND resolved = 1", (p["id"],))
        total_resolved_flags += cursor.fetchone()["c"]

    cursor.execute("""
        SELECT mr.*, p.name as patient_name FROM medical_records mr
        JOIN patients p ON mr.patient_id = p.id
        WHERE mr.doctor_id = ? ORDER BY mr.created_at DESC
    """, (session["doctor_id"],))
    medical_records = cursor.fetchall()

    cursor.execute("""
        SELECT rx.*, p.name as patient_name FROM prescriptions rx
        JOIN patients p ON rx.patient_id = p.id
        WHERE rx.doctor_id = ? ORDER BY rx.created_at DESC
    """, (session["doctor_id"],))
    prescriptions = cursor.fetchall()

    cursor.execute("""
        SELECT dn.*, p.name as patient_name FROM doctor_notes dn
        JOIN patients p ON dn.patient_id = p.id
        WHERE dn.doctor_id = ? ORDER BY dn.created_at DESC
    """, (session["doctor_id"],))
    notes = cursor.fetchall()

    conn.close()

    return render_template(
        "doctor_reports.html",
        total_patients=len(patient_rows),
        total_active_flags=total_active_flags,
        total_resolved_flags=total_resolved_flags,
        medical_records=medical_records,
        prescriptions=prescriptions,
        notes=notes,
        active="reports",
    )


# ---------- Patient ----------

@app.route("/patient/login", methods=["GET", "POST"])
def patient_login():
    if request.method == "POST":
        patient_code = request.form.get("patient_code", "").strip().upper()
        doctor_code = request.form.get("doctor_code", "").strip().upper()
        password = request.form.get("password", "")

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.*, d.doctor_code FROM patients p
            JOIN doctors d ON p.doctor_id = d.id
            WHERE p.patient_code = ? AND d.doctor_code = ?
        """, (patient_code, doctor_code))
        patient = cursor.fetchone()
        conn.close()

        if patient and patient["password"] == password:
            session.permanent = request.form.get("remember_device") == "yes"
            session["patient_id"] = patient["id"]
            session["patient_name"] = patient["name"]
            session["patient_photo"] = patient["photo_filename"]
            return redirect(url_for("patient_dashboard"))
        flash("Invalid patient code, doctor code, or password.")
    return render_template("patient_login.html")


@app.route("/patient/dashboard")
@login_required_patient
def patient_dashboard():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM patients WHERE id = ?", (session["patient_id"],))
    patient = cursor.fetchone()

    today = date.today().isoformat()
    cursor.execute("""
        SELECT * FROM daily_checkins WHERE patient_id = ? AND check_date = ?
    """, (session["patient_id"], today))
    todays_checkin = cursor.fetchone()

    cursor.execute("""
        SELECT dn.*, d.name as doctor_name FROM doctor_notes dn
        JOIN doctors d ON dn.doctor_id = d.id
        WHERE dn.patient_id = ? ORDER BY dn.created_at DESC LIMIT 3
    """, (session["patient_id"],))
    recent_notes = cursor.fetchall()

    cursor.execute("""
        SELECT * FROM weekly_reports WHERE patient_id = ? ORDER BY week_number DESC LIMIT 1
    """, (session["patient_id"],))
    latest_report = cursor.fetchone()

    cursor.execute("""
        SELECT took_medication FROM daily_checkins WHERE patient_id = ? ORDER BY check_date DESC LIMIT 7
    """, (session["patient_id"],))
    recent_checkins = cursor.fetchall()
    weekly_adherence_pct = None
    if recent_checkins:
        taken = sum(1 for c in recent_checkins if c["took_medication"])
        weekly_adherence_pct = round(100 * taken / len(recent_checkins))

    cursor.execute("""
        SELECT a.*, d.name as doctor_name, d.photo_filename as doctor_photo FROM appointments a
        JOIN doctors d ON a.doctor_id = d.id
        WHERE a.patient_id = ? AND a.appointment_at >= ? AND (a.status IS NULL OR a.status = 'scheduled')
        ORDER BY a.appointment_at ASC LIMIT 1
    """, (session["patient_id"], datetime.now().isoformat()))
    next_appointment = cursor.fetchone()

    conn.close()

    return render_template(
        "patient_dashboard.html",
        patient=patient,
        todays_checkin=todays_checkin,
        recent_notes=recent_notes,
        latest_report=latest_report,
        weekly_adherence_pct=weekly_adherence_pct,
        next_appointment=next_appointment,
        active="home",
    )


@app.route("/patient/checkin", methods=["GET", "POST"])
@login_required_patient
def patient_checkin():
    today = date.today().isoformat()

    if request.method == "POST":
        took_medication = request.form.get("took_medication") == "yes"

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id FROM daily_checkins WHERE patient_id = ? AND check_date = ?
        """, (session["patient_id"], today))
        existing = cursor.fetchone()

        if existing:
            cursor.execute("""
                UPDATE daily_checkins SET took_medication = ? WHERE id = ?
            """, (took_medication, existing["id"]))
        else:
            cursor.execute("""
                INSERT INTO daily_checkins (patient_id, check_date, took_medication)
                VALUES (?, ?, ?)
            """, (session["patient_id"], today, took_medication))
        conn.commit()
        conn.close()

        check_and_create_flags(session["patient_id"])

        flash("Check-in saved. Thank you!")
        return redirect(url_for("patient_dashboard"))

    # Build current week's day strip (Mon-Sun) with check-in status
    today_date = date.today()
    monday = today_date - timedelta(days=today_date.weekday())
    conn = get_db()
    cursor = conn.cursor()
    week_days = []
    for i in range(7):
        d = monday + timedelta(days=i)
        cursor.execute("""
            SELECT took_medication FROM daily_checkins WHERE patient_id = ? AND check_date = ?
        """, (session["patient_id"], d.isoformat()))
        row = cursor.fetchone()
        week_days.append({
            "letter": d.strftime("%a")[0],
            "num": d.day,
            "is_today": d == today_date,
            "status": (None if not row else ("yes" if row["took_medication"] else "no")),
        })
    taken_count = sum(1 for d in week_days if d["status"] == "yes")

    cursor.execute("""
        SELECT a.*, d.name as doctor_name FROM appointments a
        JOIN doctors d ON a.doctor_id = d.id
        WHERE a.patient_id = ? AND a.appointment_at >= ?
        ORDER BY a.appointment_at ASC LIMIT 1
    """, (session["patient_id"], datetime.now().isoformat()))
    next_appointment = cursor.fetchone()

    cursor.execute("SELECT name FROM patients WHERE id = ?", (session["patient_id"],))
    patient_name = cursor.fetchone()["name"]

    conn.close()

    hour = datetime.now().hour
    if hour < 12:
        greeting = "Good morning"
    elif hour < 17:
        greeting = "Good afternoon"
    else:
        greeting = "Good evening"

    return render_template(
        "patient_checkin.html",
        active="checkin",
        week_days=week_days,
        taken_count=taken_count,
        next_appointment=next_appointment,
        greeting=greeting,
        first_name=patient_name.split(" ")[0],
    )


@app.route("/patient/weekly-tracker", methods=["GET", "POST"])
@login_required_patient
def weekly_tracker():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT COALESCE(MAX(week_number), 0) + 1 as next_week FROM weekly_reports WHERE patient_id = ?
    """, (session["patient_id"],))
    next_week = cursor.fetchone()["next_week"]

    if request.method == "POST":
        symptoms = request.form.get("symptoms", "").strip()
        side_effects = request.form.get("side_effects", "").strip()
        noticeable_changes = request.form.get("noticeable_changes", "").strip()
        symptom_trend = request.form.get("symptom_trend", "same")
        satisfaction_rating = int(request.form.get("satisfaction_rating", 3))

        cursor.execute("""
            INSERT INTO weekly_reports
            (patient_id, week_number, symptoms, side_effects, noticeable_changes, symptom_trend, satisfaction_rating)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (session["patient_id"], next_week, symptoms, side_effects, noticeable_changes, symptom_trend, satisfaction_rating))
        conn.commit()
        conn.close()

        check_and_create_flags(session["patient_id"])

        flash("Weekly report submitted. Thank you!")
        return redirect(url_for("patient_dashboard"))

    conn.close()
    return render_template("weekly_tracker.html", next_week=next_week, active="checkin")


@app.route("/patient/history")
@login_required_patient
def patient_history():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM weekly_reports WHERE patient_id = ? ORDER BY week_number DESC
    """, (session["patient_id"],))
    reports = cursor.fetchall()

    cursor.execute("""
        SELECT COUNT(*) as c FROM flags WHERE patient_id = ? AND resolved = 0
    """, (session["patient_id"],))
    flagged_count = cursor.fetchone()["c"]

    conn.close()
    return render_template("patient_history.html", reports=reports, flagged_count=flagged_count, active="history")


@app.route("/patient/notes")
@login_required_patient
def patient_notes():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT dn.*, d.name as doctor_name FROM doctor_notes dn
        JOIN doctors d ON dn.doctor_id = d.id
        WHERE dn.patient_id = ? ORDER BY dn.created_at DESC
    """, (session["patient_id"],))
    notes = cursor.fetchall()
    conn.close()
    return render_template("patient_notes.html", notes=notes, active="notes")


@app.route("/patient/reports")
@login_required_patient
def patient_reports():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT mr.*, d.name as doctor_name FROM medical_records mr
        JOIN doctors d ON mr.doctor_id = d.id
        WHERE mr.patient_id = ? ORDER BY mr.created_at DESC
    """, (session["patient_id"],))
    medical_records = cursor.fetchall()

    cursor.execute("""
        SELECT p.*, d.name as doctor_name FROM prescriptions p
        JOIN doctors d ON p.doctor_id = d.id
        WHERE p.patient_id = ? ORDER BY p.created_at DESC
    """, (session["patient_id"],))
    prescriptions = cursor.fetchall()

    cursor.execute("""
        SELECT dn.*, d.name as doctor_name FROM doctor_notes dn
        JOIN doctors d ON dn.doctor_id = d.id
        WHERE dn.patient_id = ? ORDER BY dn.created_at DESC
    """, (session["patient_id"],))
    notes = cursor.fetchall()

    conn.close()
    return render_template(
        "patient_reports.html",
        medical_records=medical_records,
        prescriptions=prescriptions,
        notes=notes,
        active="reports",
    )


@app.route("/patient/reports/request", methods=["POST"])
@login_required_patient
def request_new_report():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT doctor_id, name FROM patients WHERE id = ?", (session["patient_id"],))
    patient = cursor.fetchone()
    conn.close()

    create_notification(
        "doctor", patient["doctor_id"],
        f"{patient['name']} requested a new report",
        url_for("patient_detail", patient_id=session["patient_id"]),
    )
    flash("Your doctor has been notified.")
    return redirect(url_for("patient_reports"))


@app.route("/patient/notifications")
@login_required_patient
def patient_notifications():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM notifications WHERE recipient_type = 'patient' AND recipient_id = ?
        ORDER BY created_at DESC
    """, (session["patient_id"],))
    notifications = cursor.fetchall()
    cursor.execute("""
        UPDATE notifications SET read_at = CURRENT_TIMESTAMP
        WHERE recipient_type = 'patient' AND recipient_id = ? AND read_at IS NULL
    """, (session["patient_id"],))
    conn.commit()
    conn.close()
    return render_template("patient_notifications.html", notifications=notifications)


@app.route("/patient/profile")
@login_required_patient
def patient_profile():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.*, d.name as doctor_name, d.doctor_code FROM patients p
        JOIN doctors d ON p.doctor_id = d.id
        WHERE p.id = ?
    """, (session["patient_id"],))
    patient = cursor.fetchone()

    cursor.execute("""
        SELECT * FROM patient_documents WHERE patient_id = ? ORDER BY uploaded_at DESC
    """, (session["patient_id"],))
    documents = cursor.fetchall()

    conn.close()
    return render_template("patient_profile.html", patient=patient, documents=documents, format_file_size=format_file_size)


@app.route("/patient/profile/photo", methods=["POST"])
@login_required_patient
def patient_profile_photo():
    filename, error = save_uploaded_photo(request.files.get("photo"), session.get("patient_photo"))
    if error:
        flash(error)
    else:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE patients SET photo_filename = ? WHERE id = ?", (filename, session["patient_id"]))
        conn.commit()
        conn.close()
        session["patient_photo"] = filename
        flash("Profile photo updated.")
    return redirect(url_for("patient_profile"))


@app.route("/patient/documents/new", methods=["POST"])
@login_required_patient
def new_patient_document():
    file_storage = request.files.get("document")
    if not file_storage or not file_storage.filename:
        flash("No file selected.")
        return redirect(url_for("patient_profile"))

    original_name = secure_filename(file_storage.filename)
    filename, error = save_uploaded_document(file_storage)
    if error:
        flash(error)
        return redirect(url_for("patient_profile"))

    file_size = os.path.getsize(os.path.join(UPLOAD_FOLDER, filename))

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO patient_documents (patient_id, filename, original_name, file_size)
        VALUES (?, ?, ?, ?)
    """, (session["patient_id"], filename, original_name, file_size))
    conn.commit()
    conn.close()
    flash("Document uploaded.")
    return redirect(url_for("patient_profile"))


@app.route("/patient/documents/<int:document_id>/delete", methods=["POST"])
@login_required_patient
def delete_patient_document(document_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM patient_documents WHERE id = ? AND patient_id = ?", (document_id, session["patient_id"]))
    document = cursor.fetchone()
    if document:
        file_path = os.path.join(UPLOAD_FOLDER, document["filename"])
        if os.path.exists(file_path):
            os.remove(file_path)
        cursor.execute("DELETE FROM patient_documents WHERE id = ?", (document_id,))
        conn.commit()
        flash("Document removed.")
    conn.close()
    return redirect(url_for("patient_profile"))


# ---------- Static info pages ----------

@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/help")
def help_center():
    return render_template("help.html")


@app.route("/contact")
def contact():
    return render_template("contact.html")


# ---------- App startup ----------

create_database()
migrate_database()
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

if __name__ == "__main__":
    app.run(debug=True)
