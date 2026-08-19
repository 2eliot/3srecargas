from datetime import datetime
import random
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


def generate_order_number():
    """Número de orden solo con dígitos (antes era hexadecimal, con letras
    A-F mezcladas con números, lo cual confundía al leerlo o dictarlo por
    teléfono). Se arma con la cola del timestamp en milisegundos (siempre
    creciente, prácticamente nunca se repite) más 3 dígitos aleatorios
    extra como margen adicional contra colisiones."""
    millis_tail = str(int(datetime.utcnow().timestamp() * 1000))[-9:]
    random_suffix = ''.join(random.choices('0123456789', k=3))
    return millis_tail + random_suffix


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
    player_id_input_type = db.Column(db.String(20), default='numeric')
    zone_id_label = db.Column(db.String(50), default='Zone ID')
    bs_rate_override = db.Column(db.Numeric(10, 4))
    is_automated = db.Column(db.Boolean, default=False)
    show_selection_popup = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    packages = db.relationship(
        'Package', backref='game', lazy='dynamic',
        order_by='Package.sort_order'
    )
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def get_bs_rate(self, fallback_rate=0.0):
        if self.bs_rate_override is not None:
            return float(self.bs_rate_override)
        return float(fallback_rate or 0.0)

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
            'player_id_input_type': self.player_id_input_type,
            'zone_id_label': self.zone_id_label,
            'bs_rate_override': str(self.bs_rate_override) if self.bs_rate_override is not None else None,
            'is_automated': self.is_automated,
            'show_selection_popup': self.show_selection_popup,
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
    usd_price = db.Column(db.Numeric(10, 2))
    image = db.Column(db.String(255))
    is_automated = db.Column(db.Boolean, default=False)
    sort_order = db.Column(db.Integer, default=100)
    is_active = db.Column(db.Boolean, default=True)
    # Aviso opcional que se muestra al elegir este paquete. Valores:
    # None/'' = sin aviso, 'one_time_purchase' = "solo se compra una vez en
    # tu cuenta", 'redeem_code' = "es un código que debes canjear tú mismo".
    announcement_type = db.Column(db.String(30))
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
            'usd_price': str(self.usd_price) if self.usd_price is not None else None,
            'image': self.image,
            'is_automated': self.is_automated,
            'pin_count': self.pin_count if self.is_automated else None,
            'announcement_type': self.announcement_type or '',
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
        default=generate_order_number
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
    ai_extracted_reference = db.Column(db.String(255))
    payment_capture = db.Column(db.String(255))
    delivery_proof = db.Column(db.String(255))
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
    # ID de la transacción de Binance Pay que saldó esta orden. Es único: una
    # misma transferencia no puede acreditarse a dos órdenes distintas (algo
    # posible antes, cuando el emparejamiento por monto no dejaba rastro).
    binance_tx_id = db.Column(db.String(120), unique=True, index=True)
    notes = db.Column(db.Text)
    # Evita otorgar puntos dos veces a la misma orden si el flujo de
    # aprobación se re-ejecuta (reintentos del scheduler, doble clic, etc.).
    points_awarded = db.Column(db.Boolean, default=False)
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


class MiniGameCounter(db.Model):
    __tablename__ = 'minigame_counters'

    id = db.Column(db.Integer, primary_key=True)
    game_key = db.Column(db.String(50), unique=True, nullable=False)
    play_count = db.Column(db.Integer, default=0)
    last_position = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class OrderMiniGameOpportunity(db.Model):
    __tablename__ = 'order_minigame_opportunities'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False, unique=True)
    status = db.Column(db.String(20), default='available')
    selected_game_key = db.Column(db.String(50))
    selected_choice_index = db.Column(db.Integer)
    result_kind = db.Column(db.String(30))
    reward_tier = db.Column(db.Integer)
    reward_label = db.Column(db.String(120))
    reward_amount = db.Column(db.Numeric(10, 2))
    reward_discount_id = db.Column(db.Integer, db.ForeignKey('discounts.id'))
    reward_discount_code = db.Column(db.String(50))
    # Orden interna de $0 creada para entregar el premio real (diamantes/
    # oro) al ID que ganó, cuando el resultado es 'game_prize'.
    prize_order_id = db.Column(db.Integer, db.ForeignKey('orders.id'))
    counter_position = db.Column(db.Integer)
    counter_cycle = db.Column(db.Integer)
    result_payload = db.Column(db.Text)
    unlocked_at = db.Column(db.DateTime, default=datetime.utcnow)
    played_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    order = db.relationship('Order', backref=db.backref('minigame_opportunity', uselist=False), foreign_keys=[order_id])
    reward_discount = db.relationship('Discount')
    prize_order = db.relationship('Order', foreign_keys=[prize_order_id])


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
    one_per_player = db.Column(db.Boolean, default=False)  # 1 uso por ID de jugador
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
    one_per_player = db.Column(db.Boolean, default=False)  # 1 uso por ID de jugador
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
    pabilo_requires_phone_dni = db.Column(db.Boolean, default=False)
    tutorial_video = db.Column(db.String(255))

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
            'pabilo_requires_phone_dni': self.pabilo_requires_phone_dni,
            'tutorial_video': self.tutorial_video,
        }


class Setting(db.Model):
    __tablename__ = 'settings'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.String(255), nullable=False)
    description = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class RankingArchive(db.Model):
    __tablename__ = 'ranking_archives'
    id = db.Column(db.Integer, primary_key=True)
    ranking_key = db.Column(db.String(50), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    position = db.Column(db.Integer, nullable=False)
    game_name = db.Column(db.String(100), nullable=False)
    player_id = db.Column(db.String(100))
    nickname = db.Column(db.String(200))
    masked_player_id = db.Column(db.String(100), nullable=False)
    masked_nickname = db.Column(db.String(200), nullable=False)
    total_units = db.Column(db.Integer, default=0)
    prize_label = db.Column(db.String(100), default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('ranking_key', 'year', 'month', 'position', name='uq_ranking_archive_month_pos'),
    )


class User(db.Model, UserMixin):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    account_scope = db.Column(db.String(120), index=True)
    account_scope_label = db.Column(db.String(120))
    account_identifier = db.Column(db.String(255))
    account_identifier_normalized = db.Column(db.String(255), index=True)
    account_kind = db.Column(db.String(50), default='player_id')
    contact_email = db.Column(db.String(255))
    phone = db.Column(db.String(20))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def get_id(self):
        return f'user:{self.id}'
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def display_name(self):
        return (self.account_identifier or self.username or '').strip()

    @property
    def scope_name(self):
        return (self.account_scope_label or 'Cuenta').strip()

    @property
    def preferred_contact_email(self):
        if self.contact_email:
            return self.contact_email
        if self.account_kind in {'wallet_email', 'delivery_email', 'binance_account', 'email'}:
            return self.account_identifier or ''
        return ''

    @property
    def avatar_letter(self):
        value = self.display_name or self.scope_name or 'U'
        return value[:1].upper()


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
    catalog_item_id_2 = db.Column(db.Integer, db.ForeignKey('revendedores_catalog.id'), nullable=True)
    active = db.Column(db.Boolean, default=True)
    auto_enabled = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    catalog_item = db.relationship('RevendedoresCatalogItem', foreign_keys=[catalog_item_id])
    catalog_item_2 = db.relationship('RevendedoresCatalogItem', foreign_keys=[catalog_item_id_2])
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

    def get_id(self):
        return f'admin:{self.id}'

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class ProcessLock(db.Model):
    """Bloqueo distribuido respaldado por la BD.

    Sustituye a los threading.Lock() en memoria, que solo sirven dentro de
    un mismo proceso: con Gunicorn corriendo varios workers cada uno tiene
    su propio Lock() y no se coordinan entre sí. Una fila aquí, con
    expiración, funciona igual sin importar cuántos procesos haya.
    """
    __tablename__ = 'process_locks'
    key = db.Column(db.String(100), primary_key=True)
    holder = db.Column(db.String(100))
    expires_at = db.Column(db.DateTime, nullable=False)


class PlayerPoints(db.Model):
    """Saldo de puntos de un ID de jugador en un juego específico. Se gana
    recargando por ID (nunca con códigos ni wallet) y solo sirve para
    canjearse por premios reales del mismo juego."""
    __tablename__ = 'player_points'
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=False)
    player_id = db.Column(db.String(100), nullable=False)
    points_balance = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    game = db.relationship('Game')

    __table_args__ = (
        db.UniqueConstraint('game_id', 'player_id', name='uq_player_points_game_player'),
    )


class PointsPrizeMapping(db.Model):
    """Qué paquete se entrega como premio al canjear puntos, por juego."""
    __tablename__ = 'points_prize_mappings'
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=False, unique=True)
    package_id = db.Column(db.Integer, db.ForeignKey('packages.id'), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    game = db.relationship('Game')
    package = db.relationship('Package')


class PointsSpinLog(db.Model):
    """Historial de giros de la ruleta de puntos, para auditoría en el admin."""
    __tablename__ = 'points_spin_logs'
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=False)
    player_id = db.Column(db.String(100), nullable=False)
    points_spent = db.Column(db.Integer, default=0)
    won = db.Column(db.Boolean, default=False)
    reward_label = db.Column(db.String(150))
    prize_order_id = db.Column(db.Integer, db.ForeignKey('orders.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    game = db.relationship('Game')
    prize_order = db.relationship('Order')


class PushSubscription(db.Model):
    """Suscripción Web Push de un navegador. `endpoint` identifica al
    navegador/dispositivo de forma única (lo asigna el proveedor push).
    Si `order_id` está presente, además del broadcast general recibe el
    aviso de 'tu recarga está lista' para esa orden puntual."""
    __tablename__ = 'push_subscriptions'
    id = db.Column(db.Integer, primary_key=True)
    endpoint = db.Column(db.Text, nullable=False, unique=True)
    p256dh_key = db.Column(db.String(255), nullable=False)
    auth_key = db.Column(db.String(255), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_sent_at = db.Column(db.DateTime)

    order = db.relationship('Order')


class GiftCode(db.Model):
    """Código de regalo que se canjea por una recarga real.

    Se reparten en el canal y en TikTok; para usarlos hay que pasar por la
    web (/redimir). Cada código está atado a un paquete concreto, así que
    el monto queda decidido desde que se genera el lote. Un código = un
    solo uso, garantizado por un UPDATE condicional al momento de canjear.
    """
    __tablename__ = 'gift_codes'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, nullable=False, index=True)
    package_id = db.Column(db.Integer, db.ForeignKey('packages.id'), nullable=False)
    # Etiqueta del lote con el que se generó, para saber de qué campaña
    # salió cada código sin tener que cruzar fechas a mano.
    batch = db.Column(db.String(60))
    source = db.Column(db.String(30))  # 'canal', 'tiktok', 'otro'
    is_active = db.Column(db.Boolean, default=True)
    is_used = db.Column(db.Boolean, default=False, index=True)
    used_at = db.Column(db.DateTime)
    used_player_id = db.Column(db.String(100), index=True)
    used_zone_id = db.Column(db.String(50))
    used_nickname = db.Column(db.String(120))
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=True)
    expires_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    package = db.relationship('Package')
    order = db.relationship('Order')

    @property
    def is_expired(self):
        return bool(self.expires_at and self.expires_at < datetime.utcnow())

    @property
    def is_redeemable(self):
        return bool(self.is_active and not self.is_used and not self.is_expired)
