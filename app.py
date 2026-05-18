# app.py
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from datetime import datetime, timedelta, timezone
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

# For M‑Pesa Daraja
import base64
import json

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
app.config['MAIL_DEFAULT_SENDER'] = 'Jackiemburu03@gmail.com'

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

# -------------------- Helper: Send WhatsApp in Background (RELIABLE & FAST ENOUGH) --------------------
def send_whatsapp_async(phone_num, message):
    """Send WhatsApp – reliable, fast enough (runs in background)"""
    try:
        clean_phone = phone_num.replace('+', '').replace(' ', '')
        # Primary method: pywhatkit with adequate wait_time
        kit.sendwhatmsg_instantly(
            phone_no=clean_phone,
            message=message,
            wait_time=18,       # 18 seconds – safe for slow connections
            tab_close=True
        )
        print(f"WhatsApp sent (primary) to {clean_phone}")
    except Exception as e:
        print(f"Primary error: {e} – trying fallback")
        # Fallback: webbrowser + pyautogui (always works if WhatsApp Web is logged in)
        try:
            import webbrowser
            import pyautogui as pg
            encoded_msg = message.replace(' ', '%20').replace('\n', '%0A')
            url = f"https://web.whatsapp.com/send?phone={clean_phone}&text={encoded_msg}"
            webbrowser.open(url)
            time.sleep(14)      # Enough for page to load
            pg.press('enter')
            time.sleep(2)
            pg.hotkey('ctrl', 'w')
            print(f"WhatsApp sent (fallback) to {clean_phone}")
        except Exception as e2:
            print(f"Fallback also failed: {e2}")

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

# -------------------- Client: Book a service --------------------
@app.route('/book_service/<int:service_id>', methods=['POST'])
@login_required
def book_service(service_id):
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
    flash(f'Booking request sent to {service.provider.full_name} for "{service.title}". Please complete payment.', 'success')
    return redirect(url_for('mpesa_payment_page', booking_id=booking.id))

# -------------------- M‑Pesa Daraja STK Push --------------------
@app.route('/mpesa-payment/<int:booking_id>')
@login_required
def mpesa_payment_page(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if current_user.id != booking.client_id:
        flash('Unauthorized', 'danger')
        return redirect(url_for('dashboard'))
    if booking.payment and booking.payment.status in ['authorized', 'captured']:
        flash('Payment already processed.', 'warning')
        return redirect(url_for('dashboard'))
    return render_template('mpesa_payment.html', booking=booking)

# -------------------- M-Pesa Direct Query (for real callback) --------------------
def mpesa_query_status(checkout_request_id):
    """Directly query M-Pesa for transaction status (fast)"""
    try:
        consumer_key = os.environ.get('DARAJA_API_CONSUMER_KEY')
        consumer_secret = os.environ.get('DARAJA_API_CONSUMER_SECRET')
        if not consumer_key or not consumer_secret:
            return 'pending'

        auth_url = "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
        creds = base64.b64encode(f"{consumer_key}:{consumer_secret}".encode()).decode()
        auth_resp = requests.get(auth_url, headers={"Authorization": f"Basic {creds}"}, timeout=5)
        if auth_resp.status_code != 200:
            return 'pending'
        token = auth_resp.json()['access_token']

        shortcode = os.environ.get('DARAJA_API_SHORT_CODE', '174379')
        passkey = os.environ.get('DARAJA_API_PASS_KEY', 'bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919')
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
        password = base64.b64encode(f"{shortcode}{passkey}{timestamp}".encode()).decode()

        payload = {
            "BusinessShortCode": shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "CheckoutRequestID": checkout_request_id
        }
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        resp = requests.post("https://sandbox.safaricom.co.ke/mpesa/stkpushquery/v1/query", json=payload, headers=headers, timeout=10)

        if resp.status_code == 200:
            result = resp.json()
            rc = result.get('ResultCode')
            if rc == '0':
                return 'completed'
            elif rc == '1037':
                return 'pending'
            else:
                return 'failed'
        return 'pending'
    except Exception as e:
        print(f"Query error: {e}")
        return 'pending'

@app.route('/initiate-mpesa-payment/<int:booking_id>', methods=['POST'])
@login_required
def initiate_mpesa_payment(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if current_user.id != booking.client_id:
        flash('Unauthorized', 'danger')
        return redirect(url_for('dashboard'))

    phone = request.form.get('phone')
    if not phone:
        flash('Phone number is required', 'danger')
        return redirect(url_for('mpesa_payment_page', booking_id=booking.id))

    phone = re.sub(r'\D', '', phone)
    if phone.startswith('0'):
        phone = '254' + phone[1:]
    elif not phone.startswith('254'):
        phone = '254' + phone

    amount = int(booking.service.price)
    shortcode = os.environ.get('DARAJA_API_SHORT_CODE', '174379')
    passkey = os.environ.get('DARAJA_API_PASS_KEY', 'bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919')
    callback_url = os.environ.get('MPESA_CALLBACK_URL', 'https://your-ngrok-url.ngrok-free.app/mpesa-callback')

    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    password_str = shortcode + passkey + timestamp
    password = base64.b64encode(password_str.encode()).decode()

    payload = {
        "BusinessShortCode": shortcode,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": amount,
        "PartyA": phone,
        "PartyB": shortcode,
        "PhoneNumber": phone,
        "CallBackURL": callback_url,
        "AccountReference": f"Booking{booking.id}",
        "TransactionDesc": f"Payment for {booking.service.title}"
    }

    # Get access token
    auth_url = "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
    consumer_key = os.environ.get('DARAJA_API_CONSUMER_KEY')
    consumer_secret = os.environ.get('DARAJA_API_CONSUMER_SECRET')
    credentials = base64.b64encode(f"{consumer_key}:{consumer_secret}".encode()).decode()
    headers = {"Authorization": f"Basic {credentials}", "Content-Type": "application/json"}
    try:
        auth_response = requests.get(auth_url, headers=headers)
        auth_response.raise_for_status()
        access_token = auth_response.json()['access_token']
    except Exception:
        flash('Payment service unavailable. Please try again later.', 'danger')
        return redirect(url_for('dashboard'))

    stk_url = "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest"
    stk_headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    try:
        stk_response = requests.post(stk_url, json=payload, headers=stk_headers)
        stk_response.raise_for_status()
        result = stk_response.json()
    except Exception:
        flash('Failed to initiate payment. Please try again.', 'danger')
        return redirect(url_for('mpesa_payment_page', booking_id=booking.id))

    if result.get('ResponseCode') == '0':
        if booking.payment:
            payment = booking.payment
        else:
            payment = Payment(booking_id=booking.id, amount=booking.service.price)
            db.session.add(payment)
        payment.status = 'pending'
        payment.checkout_request_id = result.get('CheckoutRequestID')
        payment.phone_number = phone
        db.session.commit()

        flash('M‑Pesa STK push sent! Check your phone and enter your PIN.', 'info')
        return redirect(url_for('mpesa_payment_status', booking_id=booking.id))
    else:
        flash(f'Payment initiation failed: {result.get("errorMessage", "Unknown error")}', 'danger')
        return redirect(url_for('mpesa_payment_page', booking_id=booking.id))

@app.route('/mpesa-callback', methods=['POST'])
def mpesa_callback():
    data = request.get_json()
    if not data:
        return 'Invalid request', 400

    stk_callback = data.get('Body', {}).get('stkCallback', {})
    result_code = stk_callback.get('ResultCode')
    result_desc = stk_callback.get('ResultDesc')
    checkout_request_id = stk_callback.get('CheckoutRequestID')

    payment = Payment.query.filter_by(checkout_request_id=checkout_request_id).first()
    if not payment:
        return 'Payment record not found', 404

    payment.result_code = result_code
    payment.result_desc = result_desc

    if result_code == 0:
        metadata = stk_callback.get('CallbackMetadata', {}).get('Item', [])
        metadata_dict = {item['Name']: item.get('Value') for item in metadata}
        payment.status = 'authorized'
        payment.captured_at = datetime.utcnow()
        payment.mpesa_receipt_number = metadata_dict.get('MpesaReceiptNumber')
        db.session.commit()

        booking = payment.booking
        if booking:
            booking.status = 'confirmed'
            db.session.commit()
    else:
        payment.status = 'failed'
        db.session.commit()

    return 'OK', 200

@app.route('/mpesa-payment-status/<int:booking_id>')
@login_required
def mpesa_payment_status(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if current_user.id != booking.client_id:
        flash('Unauthorized', 'danger')
        return redirect(url_for('dashboard'))
    payment = booking.payment
    return render_template('mpesa_payment_status.html', booking=booking, payment=payment)

# -------------------- FAST API: Payment Status with Direct Query --------------------
@app.route('/api/check-payment-status/<int:booking_id>')
@login_required
def check_payment_status(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if current_user.id != booking.client_id:
        return jsonify({'error': 'Unauthorized'}), 403

    payment = booking.payment
    if not payment:
        return jsonify({'status': 'pending'})

    db.session.refresh(payment)
    db.session.refresh(booking)

    if payment.status in ('authorized', 'captured'):
        return jsonify({'status': 'completed', 'receipt': payment.mpesa_receipt_number})

    if payment.status == 'pending' and payment.checkout_request_id:
        m_status = mpesa_query_status(payment.checkout_request_id)
        if m_status == 'completed':
            payment.status = 'authorized'
            payment.captured_at = datetime.utcnow()
            booking.status = 'confirmed'
            db.session.commit()
            return jsonify({'status': 'completed', 'receipt': payment.mpesa_receipt_number})
        elif m_status == 'failed':
            payment.status = 'failed'
            db.session.commit()
            return jsonify({'status': 'failed'})

    return jsonify({'status': 'pending'})

@app.route('/force-check-payment/<int:booking_id>')
@login_required
def force_check_payment(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if current_user.id != booking.client_id:
        return jsonify({'error': 'Unauthorized'}), 403

    payment = booking.payment
    if not payment or not payment.checkout_request_id:
        return jsonify({'status': 'no_payment'})

    m_status = mpesa_query_status(payment.checkout_request_id)
    if m_status == 'completed':
        payment.status = 'authorized'
        payment.captured_at = datetime.utcnow()
        booking.status = 'confirmed'
        db.session.commit()
        return jsonify({'status': 'updated', 'payment_status': 'authorized'})
    elif m_status == 'failed':
        payment.status = 'failed'
        db.session.commit()
        return jsonify({'status': 'updated', 'payment_status': 'failed'})
    else:
        return jsonify({'status': 'pending', 'payment_status': 'pending'})

# -------------------- FALLBACK: Force update payment status (frontend auto-confirm) --------------------
@app.route('/api/confirm-payment/<int:booking_id>', methods=['POST'])
@login_required
def confirm_payment(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if current_user.id != booking.client_id:
        return jsonify({'error': 'Unauthorized'}), 403

    payment = booking.payment
    if not payment:
        payment = Payment(booking_id=booking.id, amount=booking.service.price)
        db.session.add(payment)

    if payment.status == 'pending':
        payment.status = 'authorized'
        payment.captured_at = datetime.now(timezone.utc)
        payment.mpesa_receipt_number = f"AUTO{int(datetime.now().timestamp())}"
        booking.status = 'confirmed'
        db.session.commit()
        print(f"✅ Payment {payment.id} auto-confirmed (fallback)")
        return jsonify({'status': 'updated', 'message': 'Payment confirmed'})
    
    return jsonify({'status': 'already_updated', 'current': payment.status})

# -------------------- API: Legacy Payment Status --------------------
@app.route('/api/payment-status/<int:booking_id>')
@login_required
def api_payment_status(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if current_user.id != booking.client_id:
        return jsonify({'error': 'Unauthorized'}), 403

    payment = booking.payment
    if not payment:
        return jsonify({'status': 'pending', 'amount': booking.service.price})

    return jsonify({
        'status': payment.status,
        'amount': payment.amount,
        'mpesa_receipt_number': payment.mpesa_receipt_number,
        'result_desc': payment.result_desc,
        'captured_at': payment.captured_at.isoformat() if payment.captured_at else None
    })

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

# -------------------- Booking Status Management --------------------
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

    if new_status == 'active' and booking.status not in ['pending', 'confirmed']:
        flash('Cannot start session – booking not in a valid state.', 'danger')
        return redirect(url_for('dashboard'))

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
            body = f"""Provider {provider.full_name} has started the session at:
Client: {booking.client.full_name}
Location: {location_str}
Live map: {maps_link}
Service: {booking.service.title}
Scheduled: {booking.scheduled_time}
This is an automatic safety notification – real coordinates are included."""
            send_emergency_alerts(emergency_email, emergency_phone, subject, body)

    return redirect(url_for('dashboard'))

# -------------------- Emergency Confirmation Page --------------------
@app.route('/emergency-sent')
@login_required
def emergency_sent():
    return render_template('emergency_sent.html')

# -------------------- Panic & Safety (with fast WhatsApp) --------------------
@app.route('/panic/<int:booking_id>', methods=['POST'])
@login_required
def panic_trigger(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if current_user.id != booking.provider_id:
        flash('Unauthorized', 'danger')
        return redirect(url_for('dashboard'))

    provider = booking.provider
    emergency_email = provider.emergency_contact_email
    emergency_phone = provider.emergency_contact_phone
    if not emergency_email and not emergency_phone:
        flash('⚠️ No emergency contact email or phone set. Please update your profile first.', 'warning')
        return redirect(url_for('profile'))

    last_loc = SafetyLog.query.filter_by(booking_id=booking.id, is_panic=False).order_by(SafetyLog.timestamp.desc()).first()
    location_msg = f"Lat: {last_loc.latitude}, Lng: {last_loc.longitude}" if last_loc else "Location unknown (no GPS data)"

    subject = "🚨 SERVICE PLUS - EMERGENCY PANIC ALERT"
    body = f"""EMERGENCY PANIC BUTTON TRIGGERED

Provider: {provider.full_name}
Client: {booking.client.full_name}
Service: {booking.service.title}
Scheduled: {booking.scheduled_time.strftime('%Y-%m-%d %H:%M')}
Client Address: {booking.client.address or 'Not provided'}
Last known location: {location_msg}

Please contact the provider immediately."""
    alerts_sent = send_emergency_alerts(emergency_email, emergency_phone, subject, body)

    log = SafetyLog(booking_id=booking.id, user_id=current_user.id, is_panic=True, message='Panic button triggered by provider')
    db.session.add(log)
    audit = AuditLog(user_id=current_user.id, booking_id=booking.id, event_type='PANIC', description="Panic button triggered – emergency contact notified", location_info=location_msg)
    db.session.add(audit)
    db.session.commit()

    if alerts_sent:
        return redirect(url_for('emergency_sent'))
    else:
        flash('⚠️ Could not send alerts. Support has been notified.', 'warning')
        return redirect(url_for('dashboard'))

# -------------------- Active Sessions, Upcoming Tasks, Audit Logs, Location Update --------------------
@app.route('/active-sessions')
@login_required
def active_sessions():
    if current_user.role != 'provider':
        flash('Access denied', 'danger')
        return redirect(url_for('dashboard'))
    active_bookings = Booking.query.filter_by(provider_id=current_user.id, status='active').all()
    return render_template('active_sessions.html', bookings=active_bookings)

@app.route('/upcoming-tasks')
@login_required
def upcoming_tasks():
    if current_user.role != 'provider':
        flash('Access denied', 'danger')
        return redirect(url_for('dashboard'))
    pending_bookings = Booking.query.filter_by(provider_id=current_user.id, status='pending').all()
    return render_template('upcoming_tasks.html', bookings=pending_bookings)

@app.route('/audit-logs')
@login_required
def audit_logs():
    if current_user.role != 'provider':
        flash('Access denied', 'danger')
        return redirect(url_for('dashboard'))
    logs = AuditLog.query.filter_by(user_id=current_user.id).order_by(AuditLog.created_at.desc()).all()
    return render_template('audit_logs.html', logs=logs)

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
        log = SafetyLog(booking_id=booking.id, user_id=current_user.id, latitude=lat, longitude=lng, is_panic=False)
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
                body = f"""Provider {provider.full_name} is on site at:
Client: {booking.client.full_name}
Location: {loc_str}
Live map: {maps_link}
Service: {booking.service.title}
Scheduled: {booking.scheduled_time}
This is an automatic safety update."""
                send_emergency_alerts(emergency_email, emergency_phone, subject, body)
                entry.last_notification_time = now
                entry.notification_count += 1
                entry.last_latitude = client.latitude
                entry.last_longitude = client.longitude
                entry.last_emergency_email = emergency_email
                entry.last_emergency_phone = emergency_phone
                db.session.commit()
                audit = AuditLog(user_id=provider.id, booking_id=booking.id, event_type='LOCATION_ALERT', description="Periodic location update sent to emergency contact", location_info=loc_str)
                db.session.add(audit)
                db.session.commit()
        return jsonify({'status': 'ok'}), 200
    return jsonify({'error': 'Invalid coordinates'}), 400

# -------------------- User Profile --------------------
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

@app.route('/client-profile')
@login_required
def client_profile():
    if current_user.role != 'client':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    return render_template('client_profile.html', user=current_user)

# -------------------- Identity Verification --------------------
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

@app.route('/admin/incidents')
@login_required
@admin_required
def admin_incidents():
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
    app.run(debug=True, host='0.0.0.0', port=5000)