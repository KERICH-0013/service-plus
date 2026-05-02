# models.py
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

# Initialize db (will be configured in app.py)
db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False)   # 'client' or 'provider'
    is_verified = db.Column(db.Boolean, default=False)
    full_name = db.Column(db.String(100))
    address = db.Column(db.String(200), nullable=True)
    latitude = db.Column(db.Float, nullable=True)      # geocoded latitude
    longitude = db.Column(db.Float, nullable=True)     # geocoded longitude
    is_admin = db.Column(db.Boolean, default=False)

    # Emergency contact fields (for panic button)
    emergency_contact_name = db.Column(db.String(100), nullable=True)
    emergency_contact_email = db.Column(db.String(100), nullable=True)
    emergency_contact_phone = db.Column(db.String(20), nullable=True)
    emergency_contact_carrier = db.Column(db.String(20), nullable=True)

    # Relationships
    bookings_as_client = db.relationship(
        'Booking', foreign_keys='Booking.client_id',
        back_populates='client', lazy=True
    )
    bookings_as_provider = db.relationship(
        'Booking', foreign_keys='Booking.provider_id',
        back_populates='provider', lazy=True
    )
    services = db.relationship('Service', back_populates='provider', lazy=True)
    safety_logs = db.relationship('SafetyLog', back_populates='user', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f'<User {self.email} ({self.role})>'


class Service(db.Model):
    __tablename__ = 'services'

    id = db.Column(db.Integer, primary_key=True)
    provider_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    title = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    price = db.Column(db.Float, nullable=False)
    duration_minutes = db.Column(db.Integer, default=60)
    category = db.Column(db.String(50))
    is_active = db.Column(db.Boolean, default=True)

    provider = db.relationship('User', back_populates='services')
    bookings = db.relationship('Booking', back_populates='service', lazy=True)

    def __repr__(self):
        return f'<Service {self.title} - ${self.price}>'


class Booking(db.Model):
    __tablename__ = 'bookings'

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    provider_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    service_id = db.Column(db.Integer, db.ForeignKey('services.id'), nullable=False)
    scheduled_time = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default='pending')
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())

    client = db.relationship('User', foreign_keys=[client_id], back_populates='bookings_as_client')
    provider = db.relationship('User', foreign_keys=[provider_id], back_populates='bookings_as_provider')
    service = db.relationship('Service', back_populates='bookings')
    payment = db.relationship('Payment', back_populates='booking', uselist=False, lazy=True)
    safety_logs = db.relationship('SafetyLog', back_populates='booking', lazy=True)

    def __repr__(self):
        return f'<Booking #{self.id} - {self.status}>'


class Payment(db.Model):
    __tablename__ = 'payments'

    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey('bookings.id'), nullable=False, unique=True)
    amount = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default='pending')   # pending, authorized, captured, failed
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    captured_at = db.Column(db.DateTime, nullable=True)

    # M‑Pesa specific fields
    checkout_request_id = db.Column(db.String(100), nullable=True)   # from STK push response
    mpesa_receipt_number = db.Column(db.String(50), nullable=True)
    phone_number = db.Column(db.String(20), nullable=True)
    result_code = db.Column(db.Integer, nullable=True)
    result_desc = db.Column(db.String(200), nullable=True)

    booking = db.relationship('Booking', back_populates='payment')

    def __repr__(self):
        return f'<Payment for booking {self.booking_id} - {self.status}>'


class SafetyLog(db.Model):
    __tablename__ = 'safety_logs'

    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey('bookings.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    timestamp = db.Column(db.DateTime, default=db.func.current_timestamp())
    is_panic = db.Column(db.Boolean, default=False)
    check_in_response = db.Column(db.String(20), nullable=True)
    message = db.Column(db.Text, nullable=True)

    user = db.relationship('User', back_populates='safety_logs')
    booking = db.relationship('Booking', back_populates='safety_logs')

    def __repr__(self):
        return f'<SafetyLog booking={self.booking_id} panic={self.is_panic}>'


class VerificationRecord(db.Model):
    __tablename__ = 'verification_records'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, unique=True)
    id_document_path = db.Column(db.String(200))
    selfie_path = db.Column(db.String(200))
    status = db.Column(db.String(20), default='pending')
    submitted_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    reviewed_at = db.Column(db.DateTime, nullable=True)
    reviewed_by_admin_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    rejection_reason = db.Column(db.Text, nullable=True)

    user = db.relationship('User', foreign_keys=[user_id], backref='verification_record', lazy=True)

    def __repr__(self):
        return f'<VerificationRecord user={self.user_id} status={self.status}>'


class LocationNotificationLog(db.Model):
    __tablename__ = 'location_notification_logs'

    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey('bookings.id'), nullable=False, unique=True)
    last_notification_time = db.Column(db.DateTime, nullable=False, default=db.func.current_timestamp())
    notification_count = db.Column(db.Integer, default=0)
    last_latitude = db.Column(db.Float, nullable=True)
    last_longitude = db.Column(db.Float, nullable=True)
    last_emergency_email = db.Column(db.String(100), nullable=True)
    last_emergency_phone = db.Column(db.String(20), nullable=True)

    booking = db.relationship('Booking', backref='location_notification_log', uselist=False)


class AuditLog(db.Model):
    __tablename__ = 'audit_logs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    booking_id = db.Column(db.Integer, db.ForeignKey('bookings.id'), nullable=True)
    event_type = db.Column(db.String(50), nullable=False)
    description = db.Column(db.Text, nullable=True)
    location_info = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())

    user = db.relationship('User', backref='audit_logs')
    booking = db.relationship('Booking', backref='audit_logs')