# app.py
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from datetime import datetime, timedelta
import re
import os
import threading
import time
from functools import wraps
from werkzeug.utils import secure_filename
from flask_mail import Mail, Message
from dotenv import load_dotenv

# Import for WhatsApp alerts and geocoding
import pywhatkit as kit
import pyautogui
import requests

load_dotenv()

from config import DevelopmentConfig
from models import db, User, Service, Booking, Payment, SafetyLog, VerificationRecord, LocationNotificationLog, AuditLog
import pytesseract
from PIL import Image

app = Flask(__name__)
app.config.from_object(DevelopmentConfig)

# -------------------- Email Configuration (Gmail SMTP) --------------------
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USE_SSL'] = False
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = 'labankipkoechkerich@gmail.com'

mail = Mail(app)

# -------------------- Geocoding Helper --------------------
def geocode_address(address):
    if not address:
        return None, None
    url = "https://nominatim.openstreetmap.org/search"
    params = {'q': address, 'format': 'json', 'limit': 1}
    try:
        response = requests.get(url, params=params, headers={'User-Agent': 'ServicePlus/1.0'})
        response.raise_for_status()
        data = response.json()
        if data:
            return float(data[0]['lat']), float(data[0]['lon'])
    except Exception as e:
        print(f"Geocoding error: {e}")
    return None, None

# -------------------- Helper: Send WhatsApp in Background --------------------
def send_whatsapp_async(phone_num, message):
    try:
        kit.sendwhatmsg_instantly(phone_no=phone_num, message=message,
                                  wait_time=15, tab_close=True)
        time.sleep(2)
        print(f"WhatsApp message sent to {phone_num}")
    except Exception as e:
        print(f"WhatsApp async error: {e}")

# -------------------- Dual‑Channel Emergency Alerts --------------------
def send_emergency_alerts(recipient_email, recipient_phone, subject, body):
    email_sent = False
    whatsapp_triggered = False

    if recipient_email:
        try:
            msg = Message(subject, recipients=[recipient_email])
            msg.body = body
            mail.send(msg)
            email_sent = True
            print(f"Email sent to {recipient_email}")
        except Exception as e:
            print(f"Email error: {e}")

    if recipient_phone:
        clean_phone = re.sub(r'\s', '', recipient_phone)
        if not clean_phone.startswith('+'):
            digits = re.sub(r'\D', '', clean_phone)
            if digits.startswith('0'):
                phone_for_whatsapp = '254' + digits[1:]
            elif len(digits) >= 10 and not digits.startswith('254'):
                phone_for_whatsapp = '254' + digits
            else:
                phone_for_whatsapp = digits
            phone_for_whatsapp = '+' + phone_for_whatsapp if not phone_for_whatsapp.startswith('+') else phone_for_whatsapp
        else:
            phone_for_whatsapp = clean_phone

        threading.Thread(target=send_whatsapp_async, args=(phone_for_whatsapp, body), daemon=True).start()
        whatsapp_triggered = True
        print(f"WhatsApp sending triggered for {phone_for_whatsapp}")

    return email_sent or whatsapp_triggered

# -------------------- Upload Configuration --------------------
UPLOAD_FOLDER = 'static/uploads/id_documents'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'pdf'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# Initialize database
db.init_app(app)

# Initialize login manager
login_manager = LoginManager(app)
login_manager.login_view = 'login'

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# -------------------- Admin Required Decorator --------------------
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash('Access denied. Admin privileges required.', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

# -------------------- Helper Functions --------------------
def extract_errors(form_data):
    errors = []
    email = form_data.get('email', '').strip().lower()
    password = form_data.get('password', '')
    confirm = form_data.get('confirm_password', '')
    role = form_data.get('role', '')
    full_name = form_data.get('full_name', '').strip()

    if not role or role not in ['client', 'provider']:
        errors.append('Please select a valid role (Client or Provider).')
    if not full_name:
        errors.append('Full name is required.')
    if not email or not re.match(r'[^@]+@[^@]+\.[^@]+', email):
        errors.append('A valid email address is required.')
    if len(password) < 6:
        errors.append('Password must be at least 6 characters.')
    if password != confirm:
        errors.append('Passwords do not match.')
    if User.query.filter_by(email=email).first():
        errors.append('Email already registered. Please log in.')
    return errors, email, password, role, full_name

# -------------------- Frontend Routes --------------------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        errors, email, password, role, full_name = extract_errors(request.form)
        if errors:
            for error in errors:
                flash(error, 'danger')
            return render_template('register.html')

        if 'id_document' not in request.files:
            flash('Please upload your ID document.', 'danger')
            return render_template('register.html')
        file = request.files['id_document']
        if file.filename == '':
            flash('No file selected.', 'danger')
            return render_template('register.html')
        if not allowed_file(file.filename):
            flash('Invalid file type. Only PNG, JPG, or PDF allowed.', 'danger')
            return render_template('register.html')

        user = User(email=email, role=role, full_name=full_name)
        user.set_password(password)
        user.address = request.form.get('address', '')
        db.session.add(user)
        db.session.commit()

        if user.address:
            lat, lng = geocode_address(user.address)
            user.latitude = lat
            user.longitude = lng
            db.session.commit()

        filename = secure_filename(f"user_{user.id}_{file.filename}")
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)

        verification = VerificationRecord(
            user_id=user.id,
            id_document_path=filepath,
            status='pending'
        )
        db.session.add(verification)
        db.session.commit()

        flash('Registration successful! Your ID is pending verification. Please log in.', 'success')
        return redirect(url_for('login'))

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid credentials', 'danger')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

# -------------------- Dashboard --------------------
@app.route('/dashboard')
@login_required
def dashboard():
    if current_user.role == 'client':
        services = Service.query.filter_by(is_active=True).all()
        print(f"DEBUG: Client {current_user.email} sees {len(services)} services")
        return render_template('dashboard_client.html', services=services)
    else:
        bookings = Booking.query.filter_by(provider_id=current_user.id).all()
        services_count = Service.query.filter_by(provider_id=current_user.id).count()
        return render_template('dashboard_provider.html', bookings=bookings, services_count=services_count)

# -------------------- Client: Book a service (UPDATED: no redirect to payment) --------------------
@app.route('/book_service/<int:service_id>', methods=['POST'])
@login_required
def book_service(service_id):
    # Mandatory identity verification check
    if current_user.role == 'client' and not current_user.is_verified:
        flash('You must verify your identity before booking a service.', 'warning')
        return redirect(url_for('verify_identity'))

    service = Service.query.get_or_404(service_id)
    scheduled_time_str = request.form.get('scheduled_time')
    if not scheduled_time_str:
        flash('Please select a date and time.', 'danger')
        return redirect(url_for('dashboard'))
    try:
        scheduled_time = datetime.fromisoformat(scheduled_time_str)
    except ValueError:
        flash('Invalid date/time format.', 'danger')
        return redirect(url_for('dashboard'))

    existing = Booking.query.filter_by(provider_id=service.provider_id, scheduled_time=scheduled_time).first()
    if existing:
        flash('The provider is already booked at that time. Please choose another slot.', 'danger')
        return redirect(url_for('dashboard'))

    booking = Booking(
        client_id=current_user.id,
        provider_id=service.provider_id,
        service_id=service.id,
        scheduled_time=scheduled_time,
        status='pending'
    )
    db.session.add(booking)
    db.session.commit()
    flash(f'Booking request sent to {service.provider.full_name} for "{service.title}". Please authorize payment from your dashboard.', 'success')
    return redirect(url_for('dashboard'))

# -------------------- Payment Escrow System (Stripe‑like) --------------------
@app.route('/payment/<int:booking_id>')
@login_required
def payment_page(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if current_user.id != booking.client_id:
        flash('Unauthorized', 'danger')
        return redirect(url_for('dashboard'))
    if booking.payment and booking.payment.status != 'pending':
        flash('Payment already processed.', 'warning')
        return redirect(url_for('dashboard'))
    return render_template('payment.html', booking=booking)

@app.route('/authorize-payment/<int:booking_id>', methods=['POST'])
@login_required
def authorize_payment(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if current_user.id != booking.client_id:
        flash('Unauthorized', 'danger')
        return redirect(url_for('dashboard'))

    if booking.payment:
        payment = booking.payment
    else:
        payment = Payment(booking_id=booking.id, amount=booking.service.price)
        db.session.add(payment)

    payment.status = 'authorized'
    db.session.commit()

    flash('Payment authorized! Funds are now held in escrow. The provider will capture after service completion.', 'success')
    return redirect(url_for('dashboard'))

# -------------------- Provider: Manage Services --------------------
@app.route('/provider/services')
@login_required
def provider_services():
    if current_user.role != 'provider':
        flash('Access denied', 'danger')
        return redirect(url_for('dashboard'))
    services = Service.query.filter_by(provider_id=current_user.id).all()
    return render_template('provider_services.html', services=services)

@app.route('/provider/services/add', methods=['GET', 'POST'])
@login_required
def add_service():
    if current_user.role != 'provider':
        flash('Access denied', 'danger')
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        title = request.form.get('title')
        description = request.form.get('description')
        price = float(request.form.get('price'))
        duration = int(request.form.get('duration_minutes', 60))
        category = request.form.get('category')
        service = Service(
            provider_id=current_user.id,
            title=title,
            description=description,
            price=price,
            duration_minutes=duration,
            category=category
        )
        db.session.add(service)
        db.session.commit()
        flash('Service added successfully', 'success')
        return redirect(url_for('provider_services'))
    return render_template('add_service.html')

@app.route('/provider/services/edit/<int:service_id>', methods=['GET', 'POST'])
@login_required
def edit_service(service_id):
    service = Service.query.get_or_404(service_id)
    if service.provider_id != current_user.id:
        flash('Unauthorized', 'danger')
        return redirect(url_for('provider_services'))
    if request.method == 'POST':
        service.title = request.form.get('title')
        service.description = request.form.get('description')
        service.price = float(request.form.get('price'))
        service.duration_minutes = int(request.form.get('duration_minutes', 60))
        service.category = request.form.get('category')
        service.is_active = 'is_active' in request.form
        db.session.commit()
        flash('Service updated', 'success')
        return redirect(url_for('provider_services'))
    return render_template('edit_service.html', service=service)

@app.route('/provider/services/delete/<int:service_id>')
@login_required
def delete_service(service_id):
    service = Service.query.get_or_404(service_id)
    if service.provider_id != current_user.id:
        flash('Unauthorized', 'danger')
        return redirect(url_for('provider_services'))
    db.session.delete(service)
    db.session.commit()
    flash('Service deleted', 'success')
    return redirect(url_for('provider_services'))

# -------------------- Booking Status Management (with payment capture) --------------------
@app.route('/booking/status/<int:booking_id>/<string:new_status>')
@login_required
def update_booking_status(booking_id, new_status):
    booking = Booking.query.get_or_404(booking_id)
    if current_user.role != 'provider' or booking.provider_id != current_user.id:
        flash('Unauthorized', 'danger')
        return redirect(url_for('dashboard'))
    if new_status not in ['confirmed', 'active', 'completed', 'cancelled']:
        flash('Invalid status', 'danger')
        return redirect(url_for('dashboard'))

    old_status = booking.status
    booking.status = new_status
    db.session.commit()
    flash(f'Booking marked as {new_status}', 'success')

    if new_status == 'active':
        log = AuditLog(user_id=current_user.id, booking_id=booking.id, event_type='SESSION_START',
                       description=f"Session started for {booking.client.full_name}")
        db.session.add(log)
        db.session.commit()
    elif new_status == 'completed':
        log = AuditLog(user_id=current_user.id, booking_id=booking.id, event_type='SESSION_COMPLETE',
                       description=f"Session marked as completed")
        db.session.add(log)
        db.session.commit()

        if booking.payment and booking.payment.status == 'authorized':
            booking.payment.status = 'captured'
            booking.payment.captured_at = datetime.utcnow()
            db.session.commit()
            flash('Payment captured successfully!', 'success')

    if new_status == 'active':
        provider = booking.provider
        emergency_email = provider.emergency_contact_email
        emergency_phone = provider.emergency_contact_phone

        if emergency_email or emergency_phone:
            client = booking.client
            lat = client.latitude
            lng = client.longitude
            if lat and lng:
                location_str = f"Latitude: {lat}, Longitude: {lng}"
                maps_link = f"https://www.google.com/maps?q={lat},{lng}"
            else:
                client_address = client.address or "Address not provided"
                location_str = client_address
                maps_link = f"https://www.google.com/maps/search/?api=1&query={client_address.replace(' ', '+')}"

            subject = "📍 SERVICE PLUS - Session Started (Live Location)"
            body = f"""
Provider {provider.full_name} has started the session at:
Client: {booking.client.full_name}
Location: {location_str}
Live map: {maps_link}
Service: {booking.service.title}
Scheduled: {booking.scheduled_time}
This is an automatic safety notification – real coordinates are included.
"""
            send_emergency_alerts(emergency_email, emergency_phone, subject, body)
            print(f"Session start alert sent to {emergency_email or emergency_phone} with location {location_str}")

    return redirect(url_for('dashboard'))

# -------------------- Emergency Confirmation Page --------------------
@app.route('/emergency-sent')
@login_required
def emergency_sent():
    return render_template('emergency_sent.html')

# -------------------- Panic & Safety (with audit log) --------------------
@app.route('/panic/<int:booking_id>', methods=['POST'])
@login_required
def panic_trigger(booking_id):
    print("=== PANIC BUTTON CLICKED ===")
    print(f"Booking ID: {booking_id}")

    booking = Booking.query.get_or_404(booking_id)
    print(f"Booking found: {booking.id}, status {booking.status}")

    if current_user.id != booking.provider_id:
        flash('Unauthorized', 'danger')
        print("Unauthorized: current_user.id", current_user.id, "provider_id", booking.provider_id)
        return redirect(url_for('dashboard'))

    provider = booking.provider
    print(f"Provider: {provider.full_name}, email: {provider.email}")
    emergency_email = provider.emergency_contact_email
    emergency_phone = provider.emergency_contact_phone
    emergency_name = provider.emergency_contact_name or "Emergency Contact"

    print(f"Emergency email: {emergency_email}")
    print(f"Emergency phone: {emergency_phone}")

    if not emergency_email and not emergency_phone:
        flash('⚠️ No emergency contact email or phone set. Please update your profile first.', 'warning')
        print("No contact details, redirecting to profile")
        return redirect(url_for('profile'))

    last_loc = SafetyLog.query.filter_by(booking_id=booking.id, is_panic=False).order_by(SafetyLog.timestamp.desc()).first()
    location_msg = f"Lat: {last_loc.latitude}, Lng: {last_loc.longitude}" if last_loc else "Location unknown (no GPS data)"
    print(f"Location: {location_msg}")

    subject = "🚨 SERVICE PLUS - EMERGENCY PANIC ALERT"
    body = f"""
EMERGENCY PANIC BUTTON TRIGGERED

Provider: {provider.full_name}
Client: {booking.client.full_name}
Service: {booking.service.title}
Scheduled: {booking.scheduled_time.strftime('%Y-%m-%d %H:%M')}
Client Address: {booking.client.address or 'Not provided'}
Last known location: {location_msg}

Please contact the provider immediately. This alert was sent automatically.
    """

    alerts_sent = send_emergency_alerts(emergency_email, emergency_phone, subject, body)

    log = SafetyLog(
        booking_id=booking.id,
        user_id=current_user.id,
        is_panic=True,
        message='Panic button triggered by provider'
    )
    db.session.add(log)
    db.session.commit()
    print("Panic event logged")

    audit = AuditLog(user_id=current_user.id, booking_id=booking.id, event_type='PANIC',
                     description=f"Panic button triggered – emergency contact notified",
                     location_info=location_msg)
    db.session.add(audit)
    db.session.commit()

    if alerts_sent:
        return redirect(url_for('emergency_sent'))
    else:
        flash('⚠️ Could not send email. Support has been notified.', 'warning')
        return redirect(url_for('dashboard'))

# -------------------- Active Sessions Page --------------------
@app.route('/active-sessions')
@login_required
def active_sessions():
    if current_user.role != 'provider':
        flash('Access denied', 'danger')
        return redirect(url_for('dashboard'))
    active_bookings = Booking.query.filter_by(provider_id=current_user.id, status='active').all()
    return render_template('active_sessions.html', bookings=active_bookings)

# -------------------- Upcoming Tasks Page --------------------
@app.route('/upcoming-tasks')
@login_required
def upcoming_tasks():
    if current_user.role != 'provider':
        flash('Access denied', 'danger')
        return redirect(url_for('dashboard'))
    pending_bookings = Booking.query.filter_by(provider_id=current_user.id, status='pending').all()
    return render_template('upcoming_tasks.html', bookings=pending_bookings)

# -------------------- Audit Logs Page --------------------
@app.route('/audit-logs')
@login_required
def audit_logs():
    if current_user.role != 'provider':
        flash('Access denied', 'danger')
        return redirect(url_for('dashboard'))
    logs = AuditLog.query.filter_by(user_id=current_user.id).order_by(AuditLog.created_at.desc()).all()
    return render_template('audit_logs.html', logs=logs)

# -------------------- Location Update (with audit log for periodic alerts) --------------------
@app.route('/api/location/<int:booking_id>', methods=['POST'])
@login_required
def update_location(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if current_user.id != booking.provider_id:
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.get_json()
    lat = data.get('latitude')
    lng = data.get('longitude')
    if lat and lng:
        log = SafetyLog(
            booking_id=booking.id,
            user_id=current_user.id,
            latitude=lat,
            longitude=lng,
            is_panic=False
        )
        db.session.add(log)
        db.session.commit()

        if booking.status == 'active':
            provider = booking.provider
            emergency_email = provider.emergency_contact_email
            emergency_phone = provider.emergency_contact_phone

            entry = LocationNotificationLog.query.filter_by(booking_id=booking.id).first()
            if not entry:
                entry = LocationNotificationLog(booking_id=booking.id)
                db.session.add(entry)
                db.session.commit()

            now = datetime.utcnow()
            time_since_last = (now - entry.last_notification_time).total_seconds() if entry.last_notification_time else 301
            send_now = (entry.notification_count == 0) or (time_since_last >= 300)

            if send_now and (emergency_email or emergency_phone):
                client = booking.client
                if client.latitude and client.longitude:
                    loc_str = f"Latitude: {client.latitude}, Longitude: {client.longitude}"
                    maps_link = f"https://www.google.com/maps?q={client.latitude},{client.longitude}"
                else:
                    client_address = client.address or "Address not provided"
                    loc_str = client_address
                    maps_link = f"https://www.google.com/maps/search/?api=1&query={client_address.replace(' ', '+')}"

                subject = "📍 SERVICE PLUS - Provider Location Update"
                body = f"""
Provider {provider.full_name} is on site at:
Client: {booking.client.full_name}
Location: {loc_str}
Live map: {maps_link}
Service: {booking.service.title}
Scheduled: {booking.scheduled_time}
This is an automatic safety update – coordinates are based on the client's registered address.
"""
                send_emergency_alerts(emergency_email, emergency_phone, subject, body)

                entry.last_notification_time = now
                entry.notification_count += 1
                entry.last_latitude = client.latitude
                entry.last_longitude = client.longitude
                entry.last_emergency_email = emergency_email
                entry.last_emergency_phone = emergency_phone
                db.session.commit()

                audit = AuditLog(user_id=provider.id, booking_id=booking.id, event_type='LOCATION_ALERT',
                                 description=f"Periodic location update sent to emergency contact",
                                 location_info=loc_str)
                db.session.add(audit)
                db.session.commit()

        return jsonify({'status': 'ok'}), 200

    return jsonify({'error': 'Invalid coordinates'}), 400

# -------------------- User Profile (Emergency Contact + Address geocoding) --------------------
@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        current_user.emergency_contact_name = request.form.get('emergency_contact_name')
        current_user.emergency_contact_email = request.form.get('emergency_contact_email')
        current_user.emergency_contact_phone = request.form.get('emergency_contact_phone')
        current_user.emergency_contact_carrier = request.form.get('emergency_contact_carrier')

        new_address = request.form.get('address')
        if new_address != current_user.address:
            current_user.address = new_address
            if new_address:
                lat, lng = geocode_address(new_address)
                current_user.latitude = lat
                current_user.longitude = lng
            else:
                current_user.latitude = None
                current_user.longitude = None

        db.session.commit()
        flash('Profile updated successfully.', 'success')
        return redirect(url_for('profile'))

    return render_template('profile.html')

# -------------------- Client Profile (Read‑only) --------------------
@app.route('/client-profile')
@login_required
def client_profile():
    if current_user.role != 'client':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    return render_template('client_profile.html', user=current_user)

# -------------------- Instant Identity Verification (OCR) --------------------
def is_likely_an_id(image_path):
    try:
        with Image.open(image_path) as img:
            text = pytesseract.image_to_string(img).upper()
        keywords = ['PASSPORT', 'IDENTITY', 'ID', 'NATIONAL', 'LICENSE', 'DRIVING', 'RESIDENT', 'CARD', 'GOVERNMENT']
        return any(kw in text for kw in keywords)
    except Exception as e:
        print(f"OCR error: {e}. Accepting image anyway (demo mode).")
        return True

@app.route('/verify-identity', methods=['GET', 'POST'])
@login_required
def verify_identity():
    if current_user.role != 'client':
        flash('Only clients need to verify identity.', 'danger')
        return redirect(url_for('dashboard'))

    if current_user.is_verified:
        flash('You are already fully verified.', 'info')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        if 'id_document' not in request.files:
            flash('Please select a file.', 'danger')
            return redirect(url_for('verify_identity'))

        file = request.files['id_document']
        if file.filename == '':
            flash('No file selected.', 'danger')
            return redirect(url_for('verify_identity'))

        if not allowed_file(file.filename):
            flash('Invalid file type. Use PNG, JPG, or PDF.', 'danger')
            return redirect(url_for('verify_identity'))

        ext = file.filename.rsplit('.', 1)[1].lower()
        temp_filename = secure_filename(f"temp_{current_user.id}_{file.filename}")
        temp_path = os.path.join(app.config['UPLOAD_FOLDER'], temp_filename)
        file.save(temp_path)

        if ext in ['png', 'jpg', 'jpeg']:
            if not is_likely_an_id(temp_path):
                os.remove(temp_path)
                flash('The uploaded file does not appear to be a valid government‑issued ID. Please upload a clear photo of your ID (passport, driver’s license, national ID).', 'danger')
                return redirect(url_for('verify_identity'))

        perm_filename = secure_filename(f"user_{current_user.id}_id_{file.filename}")
        perm_path = os.path.join(app.config['UPLOAD_FOLDER'], perm_filename)
        os.rename(temp_path, perm_path)

        record = VerificationRecord.query.filter_by(user_id=current_user.id).first()
        if not record:
            record = VerificationRecord(user_id=current_user.id)
            db.session.add(record)

        record.id_document_path = perm_path
        record.status = 'approved'
        record.submitted_at = datetime.utcnow()
        record.reviewed_at = datetime.utcnow()
        record.reviewed_by_admin_id = None
        db.session.commit()

        current_user.is_verified = True
        db.session.commit()

        flash('🎉 Your identity has been fully verified instantly! You can now book services.', 'success')
        return redirect(url_for('dashboard'))

    return render_template('verify.html')

# -------------------- Admin Routes --------------------
@app.route('/admin/dashboard')
@login_required
@admin_required
def admin_dashboard():
    pending_records = VerificationRecord.query.filter_by(status='pending').all()
    return render_template('admin_dashboard.html', records=pending_records)

@app.route('/admin/verify/<int:record_id>/<string:action>')
@login_required
@admin_required
def admin_verify(record_id, action):
    record = VerificationRecord.query.get_or_404(record_id)
    user = record.user
    if action == 'approve':
        record.status = 'approved'
        user.is_verified = True
        flash(f'User {user.full_name} has been verified.', 'success')
    elif action == 'reject':
        record.status = 'rejected'
        user.is_verified = False
        flash(f'User {user.full_name} has been rejected.', 'danger')
    else:
        flash('Invalid action.', 'danger')
        return redirect(url_for('admin_dashboard'))
    db.session.commit()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/users')
@login_required
@admin_required
def admin_users():
    all_users = User.query.all()
    return render_template('admin_users.html', users=all_users)

# -------------------- Admin Incident Management (NEW) --------------------
@app.route('/admin/incidents')
@login_required
@admin_required
def admin_incidents():
    # Join AuditLog with User to get user details
    incidents = db.session.query(AuditLog, User).join(User, AuditLog.user_id == User.id).order_by(AuditLog.created_at.desc()).all()
    return render_template('admin_incidents.html', incidents=incidents)

# -------------------- Support Page --------------------
@app.route('/support')
@login_required
def support():
    return render_template('support.html')

# -------------------- Database Initialization --------------------
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True)
    