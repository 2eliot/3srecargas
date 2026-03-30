from datetime import datetime
import uuid
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class Category(db.Model):
    __tablename__ = 'categories'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    slug = db.Column(db.String(50), unique=True, nullable=False)
    icon = db.Column(db.String(10), default='🎮')
    games = db.relationship('Game', backref='category', lazy='dynamic')

    def to_dict(self):
        return {'id': self.id, 'name': self.name, 'slug': self.slug, 'icon': self.icon}


class Game(db.Model):
    __tablename__ = 'games'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    slug = db.Column(db.String(100), unique=True, nullable=False)
    image = db.Column(db.String(255))
    description = db.Column(db.Text)
    category_id = db.Column(db.Integer, db.ForeignKey('categories.id'), nullable=False)
    position = db.Column(db.Integer, default=100)
    requires_zone_id = db.Column(db.Boolean, default=False)
    player_id_label = db.Column(db.String(50), default='Player ID')
    zone_id_label = db.Column(db.String(50), default='Zone ID')
    is_automated = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    packages = db.relationship(
        'Package', backref='game', lazy='dynamic',
        order_by='Package.sort_order'
    )
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'slug': self.slug,
            'image': self.image,
            'description': self.description,
            'category_id': self.category_id,
            'category_slug': self.category.slug if self.category else None,
            'requires_zone_id': self.requires_zone_id,
            'player_id_label': self.player_id_label,
            'zone_id_label': self.zone_id_label,
            'is_automated': self.is_automated,
            'is_active': self.is_active,
            'position': self.position,
        }


class Package(db.Model):
    __tablename__ = 'packages'
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    price = db.Column(db.Numeric(10, 2), nullable=False)
    image = db.Column(db.String(255))
    is_automated = db.Column(db.Boolean, default=False)
    sort_order = db.Column(db.Integer, default=100)
    is_active = db.Column(db.Boolean, default=True)
    pins = db.relationship('Pin', backref='package', lazy='dynamic')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def pin_count(self):
        return self.pins.filter_by(is_used=False).count()

    def to_dict(self):
        return {
            'id': self.id,
            'game_id': self.game_id,
            'name': self.name,
            'description': self.description,
            'price': str(self.price),
            'image': self.image,
            'is_automated': self.is_automated,
            'pin_count': self.pin_count if self.is_automated else None,
        }


class Pin(db.Model):
    __tablename__ = 'pins'
    id = db.Column(db.Integer, primary_key=True)
    package_id = db.Column(db.Integer, db.ForeignKey('packages.id'), nullable=False)
    code = db.Column(db.String(255), nullable=False)
    is_used = db.Column(db.Boolean, default=False)
    used_at = db.Column(db.DateTime)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Order(db.Model):
    __tablename__ = 'orders'
    id = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(
        db.String(12), unique=True,
        default=lambda: str(uuid.uuid4())[:8].upper()
    )
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=False)
    package_id = db.Column(db.Integer, db.ForeignKey('packages.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    discount_id = db.Column(db.Integer, db.ForeignKey('discounts.id'), nullable=True)
    player_id = db.Column(db.String(100))
    player_nickname = db.Column(db.String(200))
    zone_id = db.Column(db.String(100))
    email = db.Column(db.String(255))
    phone = db.Column(db.String(50))
    payment_method = db.Column(db.String(50), nullable=False)
    payment_reference = db.Column(db.String(255), nullable=False)
    payment_reference_last5 = db.Column(db.String(6))
    payment_capture = db.Column(db.String(255))
    payment_amount = db.Column(db.Numeric(10, 2))
    payment_currency = db.Column(db.String(3))
    payer_dni_type = db.Column(db.String(2))
    payer_dni_number = db.Column(db.String(20))
    payer_bank_origin = db.Column(db.String(20))
    payer_phone = db.Column(db.String(20))
    payer_payment_date = db.Column(db.Date)
    payer_movement_type = db.Column(db.String(20))
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    original_amount = db.Column(db.Numeric(10, 2))  # precio antes del descuento
    discount_amount = db.Column(db.Numeric(10, 2), default=0)  # monto del descuento aplicado
    status = db.Column(db.String(20), default='pending')
    affiliate_code = db.Column(db.String(50))
    affiliate_id = db.Column(db.Integer, db.ForeignKey('affiliates.id'), nullable=True)
    pin_id = db.Column(db.Integer, db.ForeignKey('pins.id'), nullable=True)
    pin_delivered = db.Column(db.String(255))
    automation_response = db.Column(db.Text)
    payment_verification_id = db.Column(db.String(100))
    payment_verified_at = db.Column(db.DateTime)
    payment_verification_attempts = db.Column(db.Integer, default=0)
    payment_last_verification_at = db.Column(db.DateTime)
    idempotency_key = db.Column(db.String(64), unique=True, index=True)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    game = db.relationship('Game', backref='orders')
    package = db.relationship('Package', backref='orders')
    affiliate = db.relationship('Affiliate', backref='orders')
    pin = db.relationship('Pin', foreign_keys=[pin_id])
    user = db.relationship('User', backref='orders', foreign_keys=[user_id])

    STATUS_LABELS = {
        'pending': ('Pendiente', 'status-pending'),
        'approved': ('Aprobada', 'status-approved'),
        'completed': ('Completada', 'status-completed'),
        'rejected': ('Rechazada', 'status-rejected'),
    }

    @property
    def status_label(self):
        return self.STATUS_LABELS.get(self.status, (self.status, ''))[0]

    @property
    def status_class(self):
        return self.STATUS_LABELS.get(self.status, (self.status, ''))[1]


class Discount(db.Model):
    __tablename__ = 'discounts'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    description = db.Column(db.String(255))
    discount_type = db.Column(db.String(10), nullable=False)  # 'percentage' or 'fixed'
    discount_value = db.Column(db.Numeric(10, 2), nullable=False)  # % or fixed amount
    min_amount = db.Column(db.Numeric(10, 2))  # minimum order amount to apply
    max_discount = db.Column(db.Numeric(10, 2))  # maximum discount amount
    usage_limit = db.Column(db.Integer)  # max times it can be used
    used_count = db.Column(db.Integer, default=0)  # times used
    is_active = db.Column(db.Boolean, default=True)
    expires_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    orders = db.relationship('Order', backref='discount', lazy='dynamic')
    
    def calculate_discount(self, amount):
        """Calcula el monto de descuento para un monto dado"""
        if not self.is_active:
            return 0
        
        # Verificar si ha expirado
        if self.expires_at and self.expires_at < datetime.utcnow():
            return 0
        
        # Verificar límite de uso
        if self.usage_limit and self.used_count >= self.usage_limit:
            return 0
        
        # Verificar monto mínimo
        if self.min_amount and amount < self.min_amount:
            return 0
        
        if self.discount_type == 'percentage':
            discount = float(amount) * float(self.discount_value) / 100
            # Aplicar descuento máximo si existe
            if self.max_discount and discount > float(self.max_discount):
                discount = float(self.max_discount)
        else:  # fixed
            discount = float(self.discount_value)
            # No puede ser mayor al monto total
            if discount > float(amount):
                discount = float(amount)
        
        return round(discount, 2)
    
    def is_valid_for_amount(self, amount):
        """Verifica si el descuento es válido para un monto dado"""
        return self.calculate_discount(amount) > 0


class Affiliate(db.Model):
    __tablename__ = 'affiliates'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(255))
    commission_rate = db.Column(db.Numeric(5, 2), default=5.0)
    client_discount_rate = db.Column(db.Numeric(5, 2), default=0.0)  # % descuento al cliente
    balance = db.Column(db.Numeric(10, 2), default=0.0)
    total_earned = db.Column(db.Numeric(10, 2), default=0.0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    commissions = db.relationship('AffiliateCommission', backref='affiliate')


class AffiliateCommission(db.Model):
    __tablename__ = 'affiliate_commissions'
    id = db.Column(db.Integer, primary_key=True)
    affiliate_id = db.Column(db.Integer, db.ForeignKey('affiliates.id'), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    is_paid = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    order = db.relationship('Order')


class PaymentMethod(db.Model):
    __tablename__ = 'payment_methods'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    logo = db.Column(db.String(255))
    contact_email = db.Column(db.String(255))
    pay_id = db.Column(db.String(255))
    contact_phone = db.Column(db.String(50))
    bank_name = db.Column(db.String(100))
    id_number = db.Column(db.String(30))
    account_currency = db.Column(db.String(3), default='bs')
    show_contact_email = db.Column(db.Boolean, default=False)
    show_pay_id = db.Column(db.Boolean, default=False)
    show_contact_phone = db.Column(db.Boolean, default=False)
    sort_order = db.Column(db.Integer, default=100)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    uses_rate = db.Column(db.Boolean, default=True)
    pabilo_user_bank_id = db.Column(db.String(100))

    def to_dict(self):
        return {
            'id': self.id,
            'code': self.code,
            'name': self.name,
            'logo': self.logo,
            'contact_email': self.contact_email,
            'pay_id': self.pay_id,
            'contact_phone': self.contact_phone,
            'bank_name': self.bank_name,
            'id_number': self.id_number,
            'account_currency': self.account_currency,
            'show_contact_email': self.show_contact_email,
            'show_pay_id': self.show_pay_id,
            'show_contact_phone': self.show_contact_phone,
            'pabilo_user_bank_id': self.pabilo_user_bank_id,
        }


class Setting(db.Model):
    __tablename__ = 'settings'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.String(255), nullable=False)
    description = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class User(db.Model, UserMixin):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(20))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class RevendedoresCatalogItem(db.Model):
    __tablename__ = 'revendedores_catalog'
    id = db.Column(db.Integer, primary_key=True)
    remote_product_id = db.Column(db.Integer, nullable=True)
    remote_product_name = db.Column(db.String(200), default='')
    remote_package_id = db.Column(db.Integer, nullable=True)
    remote_package_name = db.Column(db.String(200), default='')
    active = db.Column(db.Boolean, default=True)
    raw_json = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('remote_product_id', 'remote_package_id', name='uq_rev_product_package'),
    )


class RevendedoresItemMapping(db.Model):
    __tablename__ = 'revendedores_item_mappings'
    id = db.Column(db.Integer, primary_key=True)
    store_package_id = db.Column(db.Integer, db.ForeignKey('packages.id'), nullable=False)
    catalog_item_id = db.Column(db.Integer, db.ForeignKey('revendedores_catalog.id'), nullable=False)
    active = db.Column(db.Boolean, default=True)
    auto_enabled = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    catalog_item = db.relationship('RevendedoresCatalogItem')
    package = db.relationship('Package')

    __table_args__ = (
        db.UniqueConstraint('store_package_id', name='uq_rev_mapping_package'),
    )


class AdminUser(db.Model, UserMixin):
    __tablename__ = 'admin_users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    email = db.Column(db.String(255), unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
