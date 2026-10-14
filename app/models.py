from datetime import datetime
import secrets
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


def generate_order_number():
    """12 dígitos totalmente aleatorios (generador criptográfico). El número
    de orden funciona como la llave de la página de la orden, que muestra
    correo y PIN: basado en la hora se podía adivinar probando alrededor de
    cuándo se hizo una compra."""
    return str(secrets.randbelow(9 * 10**11) + 10**11)


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
    # Precio exclusivo en Bs: si está puesto, se cobra este monto fijo tal
    # cual (sin multiplicar por la tasa USD/Bs) cuando el método de pago
    # cobra en Bs. order.payment_amount/payment_currency guardan este mismo
    # valor en la orden, que es lo que _get_bs_amount() (payment_verification.py)
    # usa con prioridad para comparar contra lo que reporta Pabilo — por eso
    # no hace falta tocar nada ahí para que la verificación siga funcionando.
    bs_price = db.Column(db.Numeric(10, 2))
    image = db.Column(db.String(255))
    is_automated = db.Column(db.Boolean, default=False)
    sort_order = db.Column(db.Integer, default=100)
    is_active = db.Column(db.Boolean, default=True)
    # Aviso opcional que se muestra al elegir este paquete. Valores:
    # None/'' = sin aviso, 'one_time_purchase' = "solo se compra una vez en
    # tu cuenta", 'redeem_code' = "es un código que debes canjear tú mismo".
    announcement_type = db.Column(db.String(30))
    # Puntos que otorga comprar este paquete. None = no está fijado a mano y
    # se calcula con el rate global (monto pagado × points_per_dollar); un
    # número lo deja clavado sin importar el monto, el método de pago ni el
    # descuento, que es justo lo que la tarjeta le prometió al cliente.
    points_reward = db.Column(db.Integer)
    # Tarjeta "Finaliza tu pedido manual con soporte" que se muestra en
    # /order/<numero> cuando la orden queda pendiente de revisión manual.
    # Activada por defecto; algunos paquetes no quieren mostrarla.
    show_support_fallback = db.Column(db.Boolean, default=True)
    # Lo que le cuesta a la tienda conseguir este paquete (en USD), para
    # calcular ganancia en Estadísticas. No tiene nada que ver con lo que se
    # le cobra al cliente (price/usd_price/bs_price) y nunca se expone en la
    # tienda ni en las APIs públicas — solo se usa en el admin.
    cost_usd = db.Column(db.Numeric(10, 2))
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
            'bs_price': str(self.bs_price) if self.bs_price is not None else None,
            'image': self.image,
            'is_automated': self.is_automated,
            'pin_count': self.pin_count if self.is_automated else None,
            'announcement_type': self.announcement_type or '',
            'points_reward': self.points_reward,
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
    # CUENTA que hizo la compra (con la sesión abierta), sea cual sea el ID al que recargó. `user_id` es el cliente DE ESE ID y no cambia: el
    # historial del ID sigue siendo suyo sin importar qué cuenta le recargó. Solo se guarda desde que existe esta columna (las cuentas
    # empiezan en cero): lo usan "Mi cuenta", el nivel y las insignias (utils/auth_accounts.py y utils/profile_social.py).
    account_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
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
    # Código entregado al cliente (uno por línea si la orden trae varios).
    pin_delivered = db.Column(db.Text)
    automation_response = db.Column(db.Text)
    payment_verification_id = db.Column(db.String(100))
    payment_verified_at = db.Column(db.DateTime)
    payment_verification_attempts = db.Column(db.Integer, default=0)
    payment_last_verification_at = db.Column(db.DateTime)
    idempotency_key = db.Column(db.String(64), unique=True, index=True)
    # Pago incompleto detectado por Pabilo (el banco reportó menos de lo
    # esperado): mientras esto sea True la orden espera a que el cliente
    # suba el pago restante en /order/<numero>. paid_amount_bs es el total
    # en Bs ya confirmado hasta ahora (puede sumar más de un pago).
    awaiting_payment_completion = db.Column(db.Boolean, default=False)
    paid_amount_bs = db.Column(db.Numeric(10, 2))
    remainder_reference = db.Column(db.String(255))
    # Todas las referencias de pagos restantes YA sumadas a paid_amount_bs
    # (separadas por coma): ninguna se puede sumar dos veces, ni la del
    # primer pago se puede mandar como "restante".
    remainder_references = db.Column(db.Text)
    # Ids que Pabilo asigna a cada pago restante YA sumado (uno por pago del
    # banco, sin importar cómo se escribió la referencia): detecta que el
    # "pago restante" sea en realidad el mismo pago del primero.
    remainder_verification_ids = db.Column(db.Text)
    remainder_capture = db.Column(db.String(255))
    remainder_ai_extracted_reference = db.Column(db.String(255))
    # Última respuesta cruda (JSON) que mandó Pabilo al verificar el pago de
    # esta orden. Se pisa cada vez (no es un historial), solo sirve para
    # diagnosticar un monto o estado raro sin adivinar — antes no se
    # guardaba nada de esto y no había forma de revisar hacia atrás.
    payment_verification_raw_response = db.Column(db.Text)
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


class Affiliate(db.Model, UserMixin):
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

    # --- Autoservicio de mini influencers (login propio en /minis) ---
    # Un afiliado creado a mano desde /admin/affiliates (is_mini=False) no
    # tiene password_hash ni pasa por 'pending': nace 'approved' para no
    # romper el flujo admin-managed que ya existe. Un mini se auto-registra
    # en /minis, nace 'pending' e is_mini=True, y solo entra a su panel una
    # vez que el admin lo aprueba.
    password_hash = db.Column(db.String(255))
    status = db.Column(db.String(20), default='approved')  # pending | approved | rejected
    is_mini = db.Column(db.Boolean, default=False)
    channel_url = db.Column(db.String(255))
    whatsapp_phone = db.Column(db.String(50))
    application_note = db.Column(db.Text)
    rejection_reason = db.Column(db.Text)
    reviewed_at = db.Column(db.DateTime)
    # Lista separada por comas de nombres de MiniRank ya pagados, para no
    # pagar el mismo bono de rango dos veces (ver award_rank_bonus).
    ranks_paid = db.Column(db.Text)

    def get_id(self):
        return f'mini:{self.id}'

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return bool(self.password_hash) and check_password_hash(self.password_hash, password)


class AffiliateCommission(db.Model):
    __tablename__ = 'affiliate_commissions'
    id = db.Column(db.Integer, primary_key=True)
    affiliate_id = db.Column(db.Integer, db.ForeignKey('affiliates.id'), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    is_paid = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    order = db.relationship('Order')


class MiniVideo(db.Model):
    """Link de video que un mini influencer sube como prueba de alcance.

    views_declared es autoreportado por el mini y nunca se usa para pagar
    solo: reward_amount lo fija el admin a mano al aprobar (tierRewardCents
    de la tabla de tramos es solo una sugerencia en el panel, ver
    app/utils/mini_influencers.py)."""
    __tablename__ = 'mini_videos'
    id = db.Column(db.Integer, primary_key=True)
    affiliate_id = db.Column(db.Integer, db.ForeignKey('affiliates.id'), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    platform = db.Column(db.String(20), default='otro')
    views_declared = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default='pending')  # pending | approved | rejected
    reward_amount = db.Column(db.Numeric(10, 2), default=0)
    note = db.Column(db.Text)
    reviewed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    affiliate = db.relationship('Affiliate', backref='mini_videos')


class AffiliateWithdrawal(db.Model):
    """Solicitud de retiro del balance de un afiliado/mini. El débito real
    solo ocurre al aprobar (ver approve_withdrawal), nunca al crear."""
    __tablename__ = 'affiliate_withdrawals'
    id = db.Column(db.Integer, primary_key=True)
    affiliate_id = db.Column(db.Integer, db.ForeignKey('affiliates.id'), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    method = db.Column(db.String(50))  # MiniPayoutMethod.name de un método activo
    payout_details = db.Column(db.Text)  # datos de cobro en texto libre (cuenta/cédula/teléfono)
    status = db.Column(db.String(20), default='pending')  # pending | approved | rejected
    rejection_reason = db.Column(db.Text)
    # Captura que sube el admin al aprobar, para que el mini vea con qué se
    # le pagó sin tener que preguntar por soporte (ruta relativa a uploads/).
    payment_proof = db.Column(db.String(255))
    reviewed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    affiliate = db.relationship('Affiliate', backref='withdrawals')


class MiniNotification(db.Model):
    """Aviso para el panel del mini: se crea cada vez que el admin aprueba o
    rechaza algo suyo (video, retiro, solicitud) o le paga un bono de rango,
    para que lo vea sin tener que estar revisando el panel a cada rato."""
    __tablename__ = 'mini_notifications'
    id = db.Column(db.Integer, primary_key=True)
    affiliate_id = db.Column(db.Integer, db.ForeignKey('affiliates.id'), nullable=False)
    message = db.Column(db.String(300), nullable=False)
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    affiliate = db.relationship('Affiliate', backref='notifications')


class MiniCourseVideo(db.Model):
    """Video del "Curso de edición" que el mini ve en un popup de su panel
    (varios links de YouTube, cada uno con su propia descripción de qué
    enseña), configurable desde Admin > Minis > Config."""
    __tablename__ = 'mini_course_videos'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text)
    youtube_url = db.Column(db.String(500), nullable=False)
    sort_order = db.Column(db.Integer, default=100)


class MiniViewTier(db.Model):
    """Tramo "vistas de un video -> recompensa sugerida" que se muestra en
    el panel del mini. Es solo referencia: el admin igual fija el monto real
    a mano en cada video (ver MiniVideo.reward_amount)."""
    __tablename__ = 'mini_view_tiers'
    id = db.Column(db.Integer, primary_key=True)
    min_views = db.Column(db.Integer, nullable=False)
    max_views = db.Column(db.Integer)  # NULL = sin techo, "o más"
    reward_amount = db.Column(db.Numeric(10, 2), nullable=False)
    sort_order = db.Column(db.Integer, default=100)


class MiniRank(db.Model):
    """Escalera de rangos (Bronce/Plata/Oro...) por cantidad de órdenes ya
    aprobadas/completadas que usaron el código del mini. bonus_amount se
    paga una sola vez por rango (ver Affiliate.ranks_paid)."""
    __tablename__ = 'mini_ranks'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)
    uses_required = db.Column(db.Integer, nullable=False)
    bonus_amount = db.Column(db.Numeric(10, 2), nullable=False)
    sort_order = db.Column(db.Integer, default=100)


class MiniPayoutMethod(db.Model):
    """Método de pago para retirar el balance de un mini (comisiones y
    bonos). Va aparte de PaymentMethod (con el que el CLIENTE paga a la
    tienda) porque son cosas distintas: acá es la tienda pagándole al
    mini, así que el admin arma su propia lista desde /admin/minis."""
    __tablename__ = 'mini_payout_methods'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=100)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


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
    value = db.Column(db.Text, nullable=False)
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
    prize_order_id = db.Column(db.Integer, db.ForeignKey('orders.id'))
    # Rankings de CUENTAS o de NIVELES: la cuenta que ganó el puesto (en los rankings de juego queda vacío) y cómo se entregó el premio
    # ("Puntos acreditados a su perfil", "Entregar a mano"...), para que el dueño sepa a quién le falta algo.
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    prize_detail = db.Column(db.String(200), default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('ranking_key', 'year', 'month', 'position', name='uq_ranking_archive_month_pos'),
    )


class RankingBoard(db.Model):
    """Un ranking mensual que el dueño crea desde Admin → Dinámicas → Ranking. Tres tipos:
      * 'game'     — IDs de un juego que más recargaron en el mes (el de siempre: Free Fire, Blood Strike, Mobile Legends...).
      * 'accounts' — CUENTAS que más recargaron en el mes (con su sesión abierta, a cualquier ID).
      * 'levels'   — CUENTAS con más nivel (puntos de nivel del perfil).
    Los premios de cada puesto están en RankingBoardPrize. `key` es el nombre estable con el que se archiva cada mes (RankingArchive.ranking_key)."""
    __tablename__ = 'ranking_boards'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(40), unique=True, nullable=False)
    name = db.Column(db.String(60), nullable=False)
    kind = db.Column(db.String(12), nullable=False, default='game')
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=True)
    units_label = db.Column(db.String(40), default='')
    enabled = db.Column(db.Boolean, default=False)
    prize_places = db.Column(db.Integer, default=5)
    # Primer mes ('AAAA-MM') que este ranking puede pagar: se pone al crearlo o encenderlo. Vacío = sin límite (los rankings que ya existían).
    pays_from = db.Column(db.String(7), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    game = db.relationship('Game')
    prizes = db.relationship('RankingBoardPrize', backref='board', cascade='all, delete-orphan', order_by='RankingBoardPrize.position')


class RankingBoardPrize(db.Model):
    """Premio de un puesto de un ranking: nada, un paquete (se entrega al ID ganador), puntos de la tienda o un texto libre (lo entrega el dueño)."""
    __tablename__ = 'ranking_board_prizes'
    id = db.Column(db.Integer, primary_key=True)
    board_id = db.Column(db.Integer, db.ForeignKey('ranking_boards.id'), nullable=False, index=True)
    position = db.Column(db.Integer, nullable=False)
    prize_type = db.Column(db.String(10), nullable=False, default='none')
    package_id = db.Column(db.Integer, db.ForeignKey('packages.id'), nullable=True)
    points = db.Column(db.Integer, default=0)
    text = db.Column(db.String(120), default='')
    auto = db.Column(db.Boolean, default=False)

    package = db.relationship('Package')

    __table_args__ = (
        db.UniqueConstraint('board_id', 'position', name='uq_ranking_board_prize_position'),
    )


class AccountPoints(db.Model):
    """Puntos de la tienda del PERFIL (la cuenta). Solo se consiguen como premio de un ranking o porque el administrador se los pone: no se ganan
    recargando. Para usarlos el dueño del perfil los pasa a un ID (PlayerPoints), que es donde funciona el canje y la ruleta."""
    __tablename__ = 'account_points'
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), primary_key=True)
    balance = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AccountPointsLog(db.Model):
    """Historial de la bolsa de puntos de un perfil: de dónde llegó cada punto y a qué ID se pasó."""
    __tablename__ = 'account_points_log'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    delta = db.Column(db.Integer, nullable=False)
    balance_after = db.Column(db.Integer, nullable=False, default=0)
    kind = db.Column(db.String(12), nullable=False, default='admin')    # 'ranking' | 'admin' | 'id'
    note = db.Column(db.String(200), default='')
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=True)
    player_id = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


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
    def is_registered_account(self):
        """Cuenta con usuario y contraseña (la que se crea en "Registrarse"), a diferencia del cliente "por ID" o de Google."""
        return (self.account_kind or '') == 'registered'

    @property
    def has_chosen_username(self):
        """False mientras el usuario sea el interno `u_<hash>` que se genera solo para los clientes por ID y por Google."""
        name = (self.username or '').strip().lower()
        return not (len(name) == 26 and name.startswith('u_') and all(ch in '0123456789abcdef' for ch in name[2:]))

    @property
    def display_name(self):
        if (self.account_kind or '') in {'registered', 'google_email'} and self.has_chosen_username:
            return (self.username or '').strip()
        return (self.account_identifier or self.username or '').strip()

    @property
    def scope_name(self):
        return (self.account_scope_label or 'Cuenta').strip()

    @property
    def preferred_contact_email(self):
        if self.contact_email:
            return self.contact_email
        if self.account_kind in {'wallet_email', 'delivery_email', 'binance_account', 'email', 'google_email'}:
            return self.account_identifier or ''
        return ''

    @property
    def avatar_letter(self):
        value = self.display_name or self.scope_name or 'U'
        return value[:1].upper()


class UserLinkedId(db.Model):
    """ID de un juego o servicio que una CUENTA registrada enlazó a su perfil. Con eso el perfil muestra las órdenes
    hechas con ese ID (la cuenta no se fusiona con el cliente "por ID": las órdenes siguen siendo de ese cliente)."""
    __tablename__ = 'user_linked_ids'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=False)
    identifier = db.Column(db.String(255), nullable=False)
    identifier_normalized = db.Column(db.String(255), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # YA NO SE USA (2026-10-14): las cuentas son independientes de los IDs. La tabla se queda con lo que hubiera en bases de prueba antiguas.
    verified_at = db.Column(db.DateTime, nullable=True)

    game = db.relationship('Game')
    user = db.relationship('User', backref=db.backref('linked_ids', lazy='dynamic', cascade='all, delete-orphan'))

    __table_args__ = (
        db.UniqueConstraint('user_id', 'game_id', 'identifier_normalized', name='uq_user_linked_id'),
    )


class AccountNotice(db.Model):
    """Aviso corto para una cuenta (p. ej. 'ese ID se pasó a otra cuenta'). Se muestra una sola vez en Mi cuenta."""
    __tablename__ = 'account_notices'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    message = db.Column(db.String(300), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    seen_at = db.Column(db.DateTime, nullable=True)


class UserProfile(db.Model):
    """Perfil PÚBLICO de una cuenta (estilo Discord): nombre, "acerca de mí", foto, banner, fondo, marcos y colores que
    el dueño personaliza. Es aparte de la cuenta (que guarda la seguridad: correo, contraseña, IDs). Las imágenes viven
    en uploads/profiles/<id de la cuenta>/ y el resto de la apariencia va validado en `config_json`."""
    __tablename__ = 'user_profiles'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, unique=True, index=True)
    display_name = db.Column(db.String(40), default='')
    bio = db.Column(db.String(400), default='')
    is_public = db.Column(db.Boolean, default=True)
    # El administrador puede ocultar un perfil (moderación) aunque su dueño lo tenga público.
    moderation_hidden = db.Column(db.Boolean, default=False)
    avatar_path = db.Column(db.String(255), default='')
    banner_path = db.Column(db.String(255), default='')
    background_path = db.Column(db.String(255), default='')
    avatar_frame_path = db.Column(db.String(255), default='')
    banner_frame_path = db.Column(db.String(255), default='')
    config_json = db.Column(db.Text, default='')
    # Frase corta de estado (máx. 60) bajo el nombre, y dos permisos del dueño sobre sus números.
    status_text = db.Column(db.String(60), default='')
    show_stats = db.Column(db.Boolean, default=True)
    in_rankings = db.Column(db.Boolean, default=True)
    # Marco elegido de la tienda de marcos (ver ProfileFrame); si no hay, se usa el marco propio que la cuenta subió (avatar_frame_path / banner_frame_path).
    avatar_frame_id = db.Column(db.Integer, nullable=True)
    banner_frame_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('profile', uselist=False, cascade='all, delete-orphan'))


class ProfileFrame(db.Model):
    """Marco de la tienda de marcos del perfil: una imagen (PNG/WEBP/GIF, también con movimiento) con el centro transparente que se pone encima
    de la foto de perfil o del banner. Lo sube el administrador y le pone precio en PUNTOS (0 = gratis para todos)."""
    __tablename__ = 'profile_frames'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), nullable=False)
    kind = db.Column(db.String(10), nullable=False, default='avatar')      # 'avatar' (foto de perfil) | 'banner'
    image_path = db.Column(db.String(255), nullable=False)                 # profile_frames/frame_<12 hex>.<ext>
    price_points = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class UserFrame(db.Model):
    """Un marco de la tienda que una cuenta ya compró con puntos. YA NO SE USA (2026-10-14): todos los marcos son gratis."""
    __tablename__ = 'user_frames'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    frame_id = db.Column(db.Integer, db.ForeignKey('profile_frames.id'), nullable=False, index=True)
    points_spent = db.Column(db.Integer, default=0)
    paid_game_id = db.Column(db.Integer, nullable=True)
    paid_player_id = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    frame = db.relationship('ProfileFrame')

    __table_args__ = (db.UniqueConstraint('user_id', 'frame_id', name='uq_user_frame'),)


class ProfileStat(db.Model):
    """Números del perfil (diamantes, nivel, racha, ranking...) calculados desde las órdenes que compró la
    cuenta. Se guardan aquí y se recalculan cada tanto (no en cada visita)."""
    __tablename__ = 'profile_stats'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, unique=True, index=True)
    total_diamonds = db.Column(db.Integer, default=0)
    month_diamonds = db.Column(db.Integer, default=0)
    month_key = db.Column(db.String(7), default='')
    orders_count = db.Column(db.Integer, default=0)
    streak_months = db.Column(db.Integer, default=0)
    best_month_diamonds = db.Column(db.Integer, default=0)
    # Producto (paquete) que más compra, solo el nombre. Nunca el juego ni cuántas veces.
    favorite_product = db.Column(db.String(100), default='')
    first_order_at = db.Column(db.DateTime, nullable=True)
    month_level = db.Column(db.String(20), default='')
    month_rank = db.Column(db.Integer, nullable=True)
    all_time_rank = db.Column(db.Integer, nullable=True)
    reputation = db.Column(db.Integer, default=0, index=True)
    reputation_rank = db.Column(db.Integer, nullable=True)
    # Nivel POR NÚMEROS (como Steam): puntos de experiencia (XP) que solo suben con la actividad (recargas, me gusta, seguidores, juegos, misiones...).
    # Se calculan en utils/profile_xp.py; el administrador pone cuántos puntos da cada cosa y cuánto cuesta cada nivel.
    xp_total = db.Column(db.Integer, default=0)
    xp_level = db.Column(db.Integer, default=1)
    xp_rank = db.Column(db.Integer, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('profile_stat', uselist=False, cascade='all, delete-orphan'))


class ProfileLike(db.Model):
    """Me gusta de una cuenta a otra. Uno por perfil, se puede quitar; nadie se da like a sí mismo."""
    __tablename__ = 'profile_likes'
    id = db.Column(db.Integer, primary_key=True)
    from_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    to_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('from_user_id', 'to_user_id', name='uq_profile_like'),)


class ProfileFollow(db.Model):
    __tablename__ = 'profile_follows'
    id = db.Column(db.Integer, primary_key=True)
    follower_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    following_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('follower_id', 'following_id', name='uq_profile_follow'),)


class UserBadge(db.Model):
    """Insignia ganada por una cuenta (el catálogo vive en utils/profile_social.py)."""
    __tablename__ = 'user_badges'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    badge_key = db.Column(db.String(40), nullable=False)
    earned_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('user_id', 'badge_key', name='uq_user_badge'),)


class ProfileAward(db.Model):
    """Premio de la vitrina del perfil (lo carga el administrador a mano, o sale solo al cerrar un mes: Campeón)."""
    __tablename__ = 'profile_awards'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    title = db.Column(db.String(80), nullable=False)
    description = db.Column(db.String(200), default='')
    image_path = db.Column(db.String(255), default='')
    is_title = db.Column(db.Boolean, default=False)   # el más reciente con esto en True sale junto al nombre
    # 'manual' = lo cargó el administrador; 'top1' / 'top2' / 'top3' = salió solo al cerrar un mes (month_key = 'AAAA-MM')
    kind = db.Column(db.String(10), default='manual')
    month_key = db.Column(db.String(7), default='')
    awarded_at = db.Column(db.DateTime, default=datetime.utcnow)


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
    items = db.relationship(
        'RevendedoresMappingItem',
        order_by='RevendedoresMappingItem.sort_order',
        cascade='all, delete-orphan',
    )

    __table_args__ = (
        db.UniqueConstraint('store_package_id', name='uq_rev_mapping_package'),
    )


class RevendedoresMappingItem(db.Model):
    """Un item remoto dentro de un mapeo, con cuántas veces se repite.

    Reemplaza al límite fijo de 2 items (catalog_item_id/catalog_item_id_2
    de RevendedoresItemMapping, que se mantienen solo como espejo del
    primer y segundo item para no romper mapeos viejos que no se hayan
    vuelto a guardar desde el nuevo editor). La recarga secuencial recorre
    estos items en orden y repite cada uno `quantity` veces.
    """
    __tablename__ = 'revendedores_mapping_items'
    id = db.Column(db.Integer, primary_key=True)
    mapping_id = db.Column(db.Integer, db.ForeignKey('revendedores_item_mappings.id'), nullable=False)
    catalog_item_id = db.Column(db.Integer, db.ForeignKey('revendedores_catalog.id'), nullable=False)
    quantity = db.Column(db.Integer, default=1)
    sort_order = db.Column(db.Integer, default=0)

    catalog_item = db.relationship('RevendedoresCatalogItem')


class CentralOneCatalogItem(db.Model):
    """Producto del catálogo de Central One (segundo proveedor de recargas).

    `product_id` es el UUID que Central One manda como `catalog_item_id` al
    crear un pedido. `family_name` es el juego y `name` el paquete: con eso el
    mapeo muestra "juego → paquetes" igual que con Revendedores."""
    __tablename__ = 'centralone_catalog'
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    sku = db.Column(db.String(120), default='')
    name = db.Column(db.String(255), default='')
    family_id = db.Column(db.String(120), default='')
    family_name = db.Column(db.String(200), default='')
    category = db.Column(db.String(60), default='')
    product_type = db.Column(db.String(60), default='')
    currency = db.Column(db.String(10), default='USD')
    # Precio con 4 decimales: el margen vive ahí, no se redondea a 2.
    price = db.Column(db.Numeric(12, 4))
    in_stock = db.Column(db.Boolean, default=True)
    available_stock = db.Column(db.Integer)  # None = el proveedor no informa cantidad
    requires_target = db.Column(db.Boolean, default=False)
    target_fields = db.Column(db.Text, default='[]')   # JSON: claves que pide el producto
    target_schema = db.Column(db.Text, default='[]')   # JSON: tipo (text/select) y opciones
    price_updated_at = db.Column(db.String(40), default='')
    active = db.Column(db.Boolean, default=True)
    raw_json = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    synced_at = db.Column(db.DateTime, default=datetime.utcnow)


class CentralOneItemMapping(db.Model):
    """Qué recarga(s) de Central One entregan un paquete de la tienda."""
    __tablename__ = 'centralone_item_mappings'
    id = db.Column(db.Integer, primary_key=True)
    store_package_id = db.Column(db.Integer, db.ForeignKey('packages.id'), nullable=False)
    active = db.Column(db.Boolean, default=True)
    auto_enabled = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    package = db.relationship('Package')
    items = db.relationship(
        'CentralOneMappingItem',
        order_by='CentralOneMappingItem.sort_order',
        cascade='all, delete-orphan',
    )

    __table_args__ = (
        db.UniqueConstraint('store_package_id', name='uq_co_mapping_package'),
    )


class CentralOneMappingItem(db.Model):
    """Un producto de Central One dentro de un mapeo, con cuántas veces se
    repite (cada repetición es una recarga aparte, en orden)."""
    __tablename__ = 'centralone_mapping_items'
    id = db.Column(db.Integer, primary_key=True)
    mapping_id = db.Column(db.Integer, db.ForeignKey('centralone_item_mappings.id'), nullable=False)
    catalog_item_id = db.Column(db.Integer, db.ForeignKey('centralone_catalog.id'), nullable=False)
    quantity = db.Column(db.Integer, default=1)
    sort_order = db.Column(db.Integer, default=0)
    # JSON {campo: valor} para campos del producto que el cliente no llena en
    # la tienda (por ejemplo un servidor fijo de una lista).
    target_defaults = db.Column(db.Text, default='')

    catalog_item = db.relationship('CentralOneCatalogItem')


class PackageDeliveryPlan(db.Model):
    """Qué proveedor entrega cada paquete y cuál lo respalda.

    Sin fila = comportamiento de siempre (Revendedores). Con respaldo, si el
    principal confirma que NO entregó nada (sin saldo, sin stock, paquete dado
    de baja) la orden pasa sola al respaldo."""
    __tablename__ = 'package_delivery_plans'
    id = db.Column(db.Integer, primary_key=True)
    store_package_id = db.Column(db.Integer, db.ForeignKey('packages.id'), nullable=False)
    primary_provider = db.Column(db.String(20), nullable=False, default='revendedores')
    backup_provider = db.Column(db.String(20))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('store_package_id', name='uq_delivery_plan_package'),
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


class ApiRateLimitHit(db.Model):
    """Contador de peticiones por ventana de tiempo, respaldado por la BD
    (igual que ProcessLock: un contador en memoria por worker de Gunicorn
    no serviría, cada proceso vería su propio conteo). Se usa para frenar
    scripts que mandan peticiones rápido desde la misma IP, por ejemplo
    contra el endpoint de Adivina el Número."""
    __tablename__ = 'api_rate_limit_hits'
    id = db.Column(db.Integer, primary_key=True)
    bucket_key = db.Column(db.String(150), nullable=False)
    window_start = db.Column(db.DateTime, nullable=False)
    count = db.Column(db.Integer, default=0)

    __table_args__ = (
        db.UniqueConstraint('bucket_key', 'window_start', name='uq_rate_limit_bucket_window'),
    )


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


class PointsRedeemOption(db.Model):
    """Paquete canjeable DIRECTO por puntos (sin pasar por la ruleta).

    A diferencia de PointsPrizeMapping (el premio único de la ruleta, un
    solo paquete por juego), acá puede haber varias opciones por juego,
    cada una con su propio costo en puntos — la lista de precios que arma
    el admin (ej. 110💎 por 150 pts, 341💎 por 420 pts...)."""
    __tablename__ = 'points_redeem_options'
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=False)
    package_id = db.Column(db.Integer, db.ForeignKey('packages.id'), nullable=False)
    points_cost = db.Column(db.Integer, nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    game = db.relationship('Game')
    package = db.relationship('Package')


class PointsRedeemLog(db.Model):
    """Historial de canjes directos (puntos -> paquete). Además de servir
    para auditoría en el admin, es lo que hace valer el límite de 1 canje
    por día por (juego, ID de jugador): se consulta el canje más reciente
    de ese par antes de dejar canjear de nuevo.

    Guarda package_id/points_spent propios (no solo el option_id) para que
    el historial no se rompa si el admin edita o borra la opción después."""
    __tablename__ = 'points_redeem_logs'
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=False)
    player_id = db.Column(db.String(100), nullable=False, index=True)
    option_id = db.Column(db.Integer, db.ForeignKey('points_redeem_options.id'), nullable=True)
    package_id = db.Column(db.Integer, db.ForeignKey('packages.id'), nullable=False)
    points_spent = db.Column(db.Integer, default=0)
    prize_order_id = db.Column(db.Integer, db.ForeignKey('orders.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    game = db.relationship('Game')
    package = db.relationship('Package')
    prize_order = db.relationship('Order')


class SiteNotification(db.Model):
    """Novedad que el admin publica: sale en la campana de la tienda para todos
    los visitantes, la más nueva primero."""
    __tablename__ = 'site_notifications'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), nullable=False)
    body = db.Column(db.Text, nullable=False)
    url = db.Column(db.String(300))
    is_active = db.Column(db.Boolean, default=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class OrderNotification(db.Model):
    """Mensaje que el admin le manda a UNA orden: lo ve en su campana quien hizo
    esa compra en ese navegador ("Sobre tu orden #...")."""
    __tablename__ = 'order_notifications'
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id', ondelete='CASCADE'), nullable=False, index=True)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    order = db.relationship('Order', backref=db.backref(
        'order_notifications', cascade='all, delete-orphan', order_by='OrderNotification.created_at.desc()'))


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
    # Igual que `order_id` pero para el chat de soporte: permite avisar al
    # cliente de que le respondieron aunque no tenga ninguna orden de por
    # medio (quien escribe desde la portada no tiene pedido que seguir).
    chat_id = db.Column(db.Integer, db.ForeignKey('support_chats.id'), nullable=True)
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


# ─── Soporte (chat cliente ↔ admin) ──────────────────────────────────────────

# Alfabeto sin caracteres que se confunden al dictarlos por voz o al leerlos
# en una captura: sin O/0, sin I/1. El código corto de un chat se dicta por
# WhatsApp más de una vez, así que vale la pena la precaución.
_SUPPORT_CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'


def generate_support_token():
    """Credencial del hilo para un invitado. Es lo único que prueba que
    quien pide los mensajes es el mismo navegador que abrió el chat, así
    que va con la entropía de un token de sesión, no de un identificador."""
    return secrets.token_hex(16)


def generate_support_short_code():
    """Código visible del chat (`#A7F3`). No es secreto ni se usa para
    autenticar: existe para que el cliente y el admin puedan referirse al
    mismo hilo en voz alta, porque el nombre no distingue a tres 'José'."""
    return ''.join(secrets.choice(_SUPPORT_CODE_ALPHABET) for _ in range(4))


class SupportChat(db.Model):
    """Conversación de soporte. Para abrirla el cliente solo escribe su
    nombre: cualquier otro dato se captura solo desde la página (número de
    orden si escribe desde el estado de su pedido, contacto recordado del
    último checkout, paquete que estaba mirando). Lo que no se pueda
    deducir lo resuelve el admin etiquetando y vinculando la orden a mano.
    """
    __tablename__ = 'support_chats'
    id = db.Column(db.Integer, primary_key=True)
    public_token = db.Column(db.String(64), unique=True, nullable=False, index=True,
                             default=generate_support_token)
    short_code = db.Column(db.String(8), index=True, default=generate_support_short_code)

    client_name = db.Column(db.String(60), nullable=False)
    # Correo opcional. Sin el, un cliente que cierra la pestana no se entera
    # de que le respondieron hasta que vuelve a entrar por su cuenta.
    client_email = db.Column(db.String(255))

    # Vinculación con el resto del sistema. `order_id` puede llegar solo
    # (contexto) o ponerlo el admin desde el buscador del hilo.
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)

    # Contexto capturado sin preguntarle nada al cliente. Todo opcional:
    # quien escribe desde la portada no aporta ninguno de estos.
    context_order_number = db.Column(db.String(20))
    context_player_id = db.Column(db.String(100))
    context_email = db.Column(db.String(255))
    context_phone = db.Column(db.String(50))
    context_game = db.Column(db.String(120))
    context_package = db.Column(db.String(160))
    context_page = db.Column(db.String(255))

    status = db.Column(db.String(20), default='open', index=True)
    is_blocked = db.Column(db.Boolean, default=False)

    unread_admin = db.Column(db.Integer, default=0)
    unread_client = db.Column(db.Integer, default=0)

    client_ip = db.Column(db.String(45))
    user_agent = db.Column(db.String(255))

    # Nota interna del admin: qué pasó realmente y cómo se resolvió. Vive
    # aquí, en el chat, y NO como un mensaje del hilo a propósito: si fuera
    # un SupportMessage bastaría un fallo en el filtro del serializador
    # para que el cliente leyera lo que el admin anotó sobre él. Siendo un
    # campo aparte, no existe ningún camino por el que pueda salir.
    # Asistente de IA: atiende mientras ai_active; se apaga al pasar el chat
    # a un humano o cuando el equipo responde. ai_failed cuenta las
    # búsquedas sin resultado (al llegar al máximo, pasa a humano).
    # ai_verified_orders: órdenes que el cliente demostró que son suyas
    # (dio número de orden o referencia): solo de esas se le manda el link.
    ai_active = db.Column(db.Boolean, default=True)
    ai_failed = db.Column(db.Integer, default=0)
    ai_handoff_at = db.Column(db.DateTime)
    ai_typing_at = db.Column(db.DateTime)
    ai_verified_orders = db.Column(db.Text)

    admin_note = db.Column(db.Text)
    admin_note_at = db.Column(db.DateTime)
    admin_note_admin_id = db.Column(db.Integer, db.ForeignKey('admin_users.id'), nullable=True)

    last_message_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    closed_at = db.Column(db.DateTime)

    order = db.relationship('Order')
    user = db.relationship('User')
    admin_note_author = db.relationship('AdminUser', foreign_keys=[admin_note_admin_id])
    messages = db.relationship(
        'SupportMessage', backref='chat',
        cascade='all, delete-orphan',
        order_by='SupportMessage.id',
    )
    tag_links = db.relationship(
        'SupportChatTag', backref='chat',
        cascade='all, delete-orphan',
    )

    STATUS_LABELS = {
        'open': ('Abierto', 'status-pending'),
        'waiting_client': ('Esperando cliente', 'status-approved'),
        'closed': ('Cerrado', 'status-completed'),
    }

    @property
    def status_label(self):
        return self.STATUS_LABELS.get(self.status, (self.status, ''))[0]

    @property
    def status_class(self):
        return self.STATUS_LABELS.get(self.status, (self.status, ''))[1]

    @property
    def display_code(self):
        return f'#{self.short_code}' if self.short_code else f'#{self.id}'

    @property
    def is_identified(self):
        """Un chat está identificado cuando se sabe a qué pedido o cuenta
        pertenece — no basta con tener el nombre, que no verifica nada."""
        return bool(self.order_id or self.user_id)

    @property
    def tags(self):
        return [link.tag for link in self.tag_links if link.tag]

    def tags_of_kind(self, kind):
        return [tag for tag in self.tags if tag.kind == kind]


class SupportMessage(db.Model):
    """Un mensaje del hilo. `sender` distingue quién habla; 'system' son
    las notas automáticas del propio sistema (chat abierto, orden
    vinculada, chat cerrado), que se pintan distinto y no cuentan como
    no leídos para nadie."""
    __tablename__ = 'support_messages'
    id = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(db.Integer, db.ForeignKey('support_chats.id'), nullable=False, index=True)
    sender = db.Column(db.String(10), nullable=False)  # 'client' | 'admin' | 'system'
    admin_id = db.Column(db.Integer, db.ForeignKey('admin_users.id'), nullable=True)

    body = db.Column(db.Text)
    attachment = db.Column(db.String(255))
    # Mensajes del asistente de IA: sender='admin' con is_ai=True. Pueden
    # traer un botón (p. ej. el link de la orden).
    is_ai = db.Column(db.Boolean, default=False)
    action_url = db.Column(db.String(255))
    action_label = db.Column(db.String(60))
    # Nota interna para la IA (p. ej. referencia leída del comprobante).
    # Nunca se envía al cliente.
    ai_note = db.Column(db.Text)

    # "Eliminar para todos": el mensaje se queda en la base (por si hace
    # falta auditar qué se borró y quién lo mandó) pero deja de mostrarse
    # tal cual — se sirve como "Mensaje eliminado" a ambos lados del chat.
    is_deleted = db.Column(db.Boolean, default=False)
    deleted_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    read_at = db.Column(db.DateTime)

    admin = db.relationship('AdminUser')


class SupportTag(db.Model):
    """Etiqueta del catálogo. `kind` separa las dos preguntas que el admin
    responde en cada chat: quién es esta persona ('user') y qué le pasa
    ('error'). Se pintan en filas distintas del hilo por eso mismo."""
    __tablename__ = 'support_tags'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), nullable=False)
    slug = db.Column(db.String(60), unique=True, nullable=False, index=True)
    kind = db.Column(db.String(10), nullable=False, default='error')  # 'user' | 'error'
    color = db.Column(db.String(9), default='#6c5ce7')
    is_active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    KIND_LABELS = {
        'user': 'Usuario',
        'error': 'Error',
    }

    @property
    def kind_label(self):
        return self.KIND_LABELS.get(self.kind, self.kind)


class SupportChatTag(db.Model):
    """Etiqueta aplicada a un chat. El par (chat, etiqueta) es único para
    que un doble clic en el admin no la pegue dos veces."""
    __tablename__ = 'support_chat_tags'
    __table_args__ = (
        db.UniqueConstraint('chat_id', 'tag_id', name='uq_support_chat_tag'),
    )
    id = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(db.Integer, db.ForeignKey('support_chats.id'), nullable=False, index=True)
    tag_id = db.Column(db.Integer, db.ForeignKey('support_tags.id'), nullable=False, index=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('admin_users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    tag = db.relationship('SupportTag')


class SupportQuickReply(db.Model):
    """Respuesta prearmada que el admin puede insertar en el chat con un
    clic, para las preguntas que se repiten (horario, cómo pagar, etc.).
    `title` es solo el nombre del botón; `body` es el texto que se manda."""
    __tablename__ = 'support_quick_replies'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(60), nullable=False)
    body = db.Column(db.Text, nullable=False)
    attachment = db.Column(db.String(255))
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ─── Promociones: Recarga Acumulada / Sorteo Diario / Adivina el Número ──────

class PromoAccumulatedLevel(db.Model):
    """Niveles de la barra de Recarga Acumulada, por juego. El admin define
    hasta 3 niveles con el monto acumulado (en $, sumando lo pagado en el
    mes calendario) que hace falta para ganar el premio de ese nivel."""
    __tablename__ = 'promo_accumulated_levels'
    __table_args__ = (
        db.UniqueConstraint('game_id', 'level_number', name='uq_promo_acc_level'),
    )
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=False, index=True)
    level_number = db.Column(db.Integer, nullable=False)
    level_name = db.Column(db.String(60), nullable=False)
    threshold_amount = db.Column(db.Numeric(10, 2), nullable=False)
    package_id = db.Column(db.Integer, db.ForeignKey('packages.id'), nullable=False)
    # Orden del juego en el selector de la promo (no en la tienda general):
    # el mismo valor se guarda en los 3 niveles del juego. Menor va primero,
    # y el primero de la lista es el que queda seleccionado por defecto.
    sort_order = db.Column(db.Integer, default=100, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    game = db.relationship('Game')
    package = db.relationship('Package')


class PromoAccumulatedProgress(db.Model):
    """Progreso de un ID de jugador en la barra de Recarga Acumulada de un
    juego durante un mes calendario (hora Venezuela). `current_level_number`
    apunta al nivel que está llenando; si supera el último nivel definido,
    ya completó todos los premios de ese mes."""
    __tablename__ = 'promo_accumulated_progress'
    __table_args__ = (
        db.UniqueConstraint('game_id', 'player_id', 'month_key', name='uq_promo_acc_progress'),
    )
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=False, index=True)
    player_id = db.Column(db.String(100), nullable=False, index=True)
    month_key = db.Column(db.String(7), nullable=False)  # 'AAAA-MM' hora Venezuela
    current_level_number = db.Column(db.Integer, default=1)
    accumulated_amount = db.Column(db.Numeric(10, 2), default=0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PromoAccumulatedAward(db.Model):
    """Historial de premios entregados por completar un nivel de la barra."""
    __tablename__ = 'promo_accumulated_awards'
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=False, index=True)
    player_id = db.Column(db.String(100), nullable=False, index=True)
    month_key = db.Column(db.String(7), nullable=False)
    level_number = db.Column(db.Integer, nullable=False)
    level_name = db.Column(db.String(60))
    prize_order_id = db.Column(db.Integer, db.ForeignKey('orders.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    game = db.relationship('Game')
    prize_order = db.relationship('Order')


class PromoAccumulatedOrderLog(db.Model):
    """Marca qué órdenes ya sumaron a la barra de Recarga Acumulada, para no
    contarlas dos veces si el flujo de aprobación se re-ejecuta."""
    __tablename__ = 'promo_accumulated_order_log'
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class PromoRaffleConfig(db.Model):
    """Configuración del Sorteo Diario por juego."""
    __tablename__ = 'promo_raffle_configs'
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=False, unique=True)
    is_active = db.Column(db.Boolean, default=False)
    draw_hour = db.Column(db.Integer, default=21)  # hora Venezuela, 0-23
    draw_minute = db.Column(db.Integer, default=0, nullable=False)  # minuto, 0-59
    winners_per_draw = db.Column(db.Integer, default=5)
    package_id = db.Column(db.Integer, db.ForeignKey('packages.id'))
    require_verification = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=100, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    game = db.relationship('Game')
    package = db.relationship('Package')


class PromoRaffleEntry(db.Model):
    """Un registro (ticket) de un ID en el sorteo de un día concreto."""
    __tablename__ = 'promo_raffle_entries'
    __table_args__ = (
        db.UniqueConstraint('game_id', 'player_id', 'day_key', name='uq_promo_raffle_entry'),
        db.UniqueConstraint('game_id', 'day_key', 'ticket_number', name='uq_promo_raffle_ticket'),
    )
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=False, index=True)
    player_id = db.Column(db.String(100), nullable=False, index=True)
    player_nick = db.Column(db.String(150))
    day_key = db.Column(db.String(10), nullable=False, index=True)  # 'AAAA-MM-DD' hora Venezuela
    ticket_number = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class PromoRaffleWinner(db.Model):
    """Ganador de un sorteo diario ya realizado."""
    __tablename__ = 'promo_raffle_winners'
    __table_args__ = (
        db.UniqueConstraint('game_id', 'day_key', 'player_id', name='uq_promo_raffle_winner'),
    )
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=False, index=True)
    day_key = db.Column(db.String(10), nullable=False, index=True)
    player_id = db.Column(db.String(100), nullable=False)
    player_nick = db.Column(db.String(150))
    ticket_number = db.Column(db.Integer)
    prize_order_id = db.Column(db.Integer, db.ForeignKey('orders.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    game = db.relationship('Game')
    prize_order = db.relationship('Order')


class PromoGuessConfig(db.Model):
    """Configuración del juego Adivina el Número por juego."""
    __tablename__ = 'promo_guess_configs'
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=False, unique=True)
    is_active = db.Column(db.Boolean, default=False)
    number_max = db.Column(db.Integer, default=50)
    max_attempts = db.Column(db.Integer, default=2)
    winners_per_day = db.Column(db.Integer, default=3)
    package_id = db.Column(db.Integer, db.ForeignKey('packages.id'))
    require_verification = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=100, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    game = db.relationship('Game')
    package = db.relationship('Package')


class PromoGuessLog(db.Model):
    """Registro de CADA intento de Adivina el Número (hora, ID, número
    probado, si ganó, IP). Sirve para investigar patrones raros: muchos IDs
    desde la misma IP, intentos a medianoche en punto, etc."""
    __tablename__ = 'promo_guess_logs'
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=False, index=True)
    day_key = db.Column(db.String(10), nullable=False, index=True)
    player_id = db.Column(db.String(100), nullable=False, index=True)
    guess = db.Column(db.Integer)
    won = db.Column(db.Boolean, default=False)
    ip = db.Column(db.String(64), index=True)
    user_agent = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class PromoGuessRound(db.Model):
    """Estado del día para un juego: número secreto vigente y cuántos
    ganadores lleva. Se crea solo cuando alguien juega ese día."""
    __tablename__ = 'promo_guess_rounds'
    __table_args__ = (
        db.UniqueConstraint('game_id', 'day_key', name='uq_promo_guess_round'),
    )
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=False, index=True)
    day_key = db.Column(db.String(10), nullable=False)
    secret_number = db.Column(db.Integer, nullable=False)
    winners_count = db.Column(db.Integer, default=0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PromoGuessAttempt(db.Model):
    """Intentos usados por un ID en el día. `attempts_used` sube en cada
    intento (acierte o no) hasta el máximo configurado."""
    __tablename__ = 'promo_guess_attempts'
    __table_args__ = (
        db.UniqueConstraint('game_id', 'day_key', 'player_id', name='uq_promo_guess_attempt'),
    )
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=False, index=True)
    day_key = db.Column(db.String(10), nullable=False)
    player_id = db.Column(db.String(100), nullable=False)
    attempts_used = db.Column(db.Integer, default=0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PromoGuessWinner(db.Model):
    """Ganador de un cupo del día en Adivina el Número."""
    __tablename__ = 'promo_guess_winners'
    __table_args__ = (
        db.UniqueConstraint('game_id', 'day_key', 'player_id', name='uq_promo_guess_winner'),
    )
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=False, index=True)
    day_key = db.Column(db.String(10), nullable=False, index=True)
    player_id = db.Column(db.String(100), nullable=False)
    slot_index = db.Column(db.Integer, nullable=False)
    guessed_number = db.Column(db.Integer)
    player_nick = db.Column(db.String(150))
    prize_order_id = db.Column(db.Integer, db.ForeignKey('orders.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    game = db.relationship('Game')
    prize_order = db.relationship('Order')


class PromoHordeConfig(db.Model):
    """Configuración del juego Hordas de Diamantes (ranking semanal) por juego."""
    __tablename__ = 'promo_horde_configs'
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=False, unique=True)
    is_active = db.Column(db.Boolean, default=False)
    # Premio del 1er lugar; 2do y 3ro son opcionales (vacío = sin premio).
    package_id = db.Column(db.Integer, db.ForeignKey('packages.id'))
    package_id_2 = db.Column(db.Integer, db.ForeignKey('packages.id'))
    package_id_3 = db.Column(db.Integer, db.ForeignKey('packages.id'))
    winners_per_week = db.Column(db.Integer, default=1)
    runs_per_day = db.Column(db.Integer, default=5)
    # Puntos que cuesta cada partida extra al acabarse las gratis (0 = no se venden).
    points_per_extra_run = db.Column(db.Integer, default=0)
    require_verification = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=100, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    game = db.relationship('Game')
    package = db.relationship('Package', foreign_keys=[package_id])
    package_2 = db.relationship('Package', foreign_keys=[package_id_2])
    package_3 = db.relationship('Package', foreign_keys=[package_id_3])

    def place_packages(self):
        """[(puesto, paquete), ...] de los puestos que tienen premio."""
        places = [(1, self.package), (2, self.package_2), (3, self.package_3)]
        return [(place, pkg) for place, pkg in places if pkg is not None]


class PromoHordeRun(db.Model):
    """Una partida. Nace al empezar (con la hora del servidor) y se cierra
    una sola vez al terminar; los diamantes aceptados se topan según el
    tiempo real que duró, medido por el servidor."""
    __tablename__ = 'promo_horde_runs'
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(32), nullable=False, unique=True, index=True)
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=False, index=True)
    player_id = db.Column(db.String(100), nullable=False, index=True)
    player_nick = db.Column(db.String(150))
    week_key = db.Column(db.String(10), nullable=False, index=True)  # lunes 'AAAA-MM-DD' hora Venezuela
    day_key = db.Column(db.String(10), nullable=False, index=True)
    ip = db.Column(db.String(64))
    diamonds = db.Column(db.Integer, default=0, nullable=False)
    claimed_diamonds = db.Column(db.Integer, default=0, nullable=False)
    kills = db.Column(db.Integer, default=0, nullable=False)
    started_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    finished_at = db.Column(db.DateTime)
    # Puntos canjeados para abrir esta partida (0 = partida gratis del día).
    points_spent = db.Column(db.Integer, default=0, nullable=False)
    # Semilla que decide qué enemigos salen (el servidor calcula con ella
    # cuántos diamantes existen de verdad en la partida).
    seed = db.Column(db.Integer)
    wave = db.Column(db.Integer)
    # Motivo si la partida parece trampa (p. ej. 'gano_oleada_final').
    flag = db.Column(db.String(40))
    # Resultado de volver a jugar la grabación en el admin:
    # 'ok' / 'dudoso' / 'no_coincide', con la oleada y diamantes de la repetición.
    replay_check = db.Column(db.String(20))
    replay_wave = db.Column(db.Integer)
    replay_diamonds = db.Column(db.Integer)


class PromoHordeBan(db.Model):
    """ID bloqueado en Hordas de Diamantes de un juego (por trampa): no puede
    jugar, no sale en el ranking y no recibe premio."""
    __tablename__ = 'promo_horde_bans'
    __table_args__ = (db.UniqueConstraint('game_id', 'player_id', name='uq_horde_ban_game_player'),)
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=False, index=True)
    player_id = db.Column(db.String(100), nullable=False, index=True)
    reason = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class PromoHordeReplay(db.Model):
    """Grabación de una partida (semilla + movimientos del dedo + mejoras),
    en una tabla aparte para que el ranking nunca la lea. Al cerrar la
    semana solo se conservan las del top 5."""
    __tablename__ = 'promo_horde_replays'
    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey('promo_horde_runs.id'), nullable=False, unique=True, index=True)
    game_id = db.Column(db.Integer, nullable=False, index=True)
    week_key = db.Column(db.String(10), nullable=False, index=True)
    player_id = db.Column(db.String(100), nullable=False, index=True)
    width = db.Column(db.Integer, nullable=False)
    height = db.Column(db.Integer, nullable=False)
    character_slot = db.Column(db.Integer, default=0)
    # Versión de las reglas con que se jugó (la repetición usa las mismas).
    rules = db.Column(db.Integer, default=1)
    data = db.Column(db.Text, nullable=False)
    size = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class PromoHordeWinner(db.Model):
    """Ganador de una semana de Hordas de Diamantes."""
    __tablename__ = 'promo_horde_winners'
    __table_args__ = (
        db.UniqueConstraint('game_id', 'week_key', 'player_id', name='uq_promo_horde_winner'),
    )
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=False, index=True)
    week_key = db.Column(db.String(10), nullable=False, index=True)
    place = db.Column(db.Integer, nullable=False)
    player_id = db.Column(db.String(100), nullable=False)
    player_nick = db.Column(db.String(150))
    diamonds = db.Column(db.Integer, default=0)
    prize_order_id = db.Column(db.Integer, db.ForeignKey('orders.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # El premio no sale solo: queda 'pending' hasta que el admin revisa las
    # repeticiones y aprueba ('approved', se entrega en ese momento).
    # Los ganadores de antes de este cambio ya estaban entregados: 'approved'.
    status = db.Column(db.String(20), nullable=False, default='pending')
    package_id = db.Column(db.Integer, db.ForeignKey('packages.id'))
    reviewed_at = db.Column(db.DateTime)

    game = db.relationship('Game')
    prize_order = db.relationship('Order')
    package = db.relationship('Package')

# ───────────────────────── Torneos ─────────────────────────
# El dueño publica el link del torneo en su canal de WhatsApp y, a la hora, abre las inscripciones desde el admin. Cada torneo tiene SALAS
# (nombre, ID y clave de la sala del juego, y cuántos jugadores caben); la gente entra por orden de llegada con su ID verificado y la web
# la mete en la primera sala que todavía tenga lugar (la siguiente se "desbloquea" cuando se llena la anterior). Los datos de la sala solo
# los ve quien ya se inscribió. Los premios se definen por puesto (un paquete del juego) y se entregan solos al elegir al ganador.
class Tournament(db.Model):
    __tablename__ = 'tournaments'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(300), default='')       # texto libre que ve el cliente (p. ej. "Sábado 8:00 PM · Free Fire")
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=False, index=True)
    # 'draft' (preparando: no se ve) · 'open' (inscripciones abiertas) · 'closed' (inscripciones cerradas: se ve, pero no se puede entrar)
    # · 'finished' (terminó: ya no se muestra a los clientes)
    status = db.Column(db.String(12), nullable=False, default='draft', index=True)
    max_per_ip = db.Column(db.Integer, nullable=False, default=5)   # inscripciones por conexión (0 = sin límite): frena a quien quiera llenar las salas
    # Fecha y hora (UTC, se captura en hora de Venezuela) en que se desbloquean las inscripciones. Con el torneo publicado ('open') y esta hora en el futuro,
    # los clientes ven una cuenta regresiva y no pueden entrar hasta que llegue; sin hora (None) se abre cuando el dueño lo abre a mano.
    opens_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    opened_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)

    game = db.relationship('Game')
    rooms = db.relationship('TournamentRoom', backref='tournament', cascade='all, delete-orphan', order_by='TournamentRoom.position, TournamentRoom.id')
    prizes = db.relationship('TournamentPrize', backref='tournament', cascade='all, delete-orphan', order_by='TournamentPrize.place')


class TournamentRoom(db.Model):
    __tablename__ = 'tournament_rooms'
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=False, index=True)
    position = db.Column(db.Integer, nullable=False, default=1)       # orden en que se van abriendo
    name = db.Column(db.String(80), nullable=False)                   # nombre de la sala en el juego
    room_code = db.Column(db.String(60), nullable=False, default='')  # ID de la sala en el juego
    room_password = db.Column(db.String(60), nullable=False, default='')  # clave de la sala (solo la ve quien ya se inscribió)
    max_players = db.Column(db.Integer, nullable=False, default=50)
    # Cuando la sala TERMINA (el dueño la termina, o se entregaron todos sus premios) sus jugadores quedan libres para inscribirse en otra sala.
    # Una sala terminada ya no recibe gente y no frena a la siguiente sala.
    finished_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    entries = db.relationship('TournamentEntry', backref='room', cascade='all, delete-orphan', order_by='TournamentEntry.slot_number')


class TournamentEntry(db.Model):
    """Un jugador inscrito en una sala. Cada sala tiene lugares FIJOS del 1 al máximo y `slot_number` es único por sala: aunque muchas personas
    lleguen al mismo tiempo, la base de datos no deja que dos tomen el mismo lugar ni que se pase del máximo (la que pierde la carrera vuelve a
    intentar y cae en la sala siguiente). Al sacar a alguien se le pone un número negativo (único) y su lugar queda libre.

    Regla del dueño: un ID solo puede estar en UNA sala a la vez (en toda la web, del mismo juego). Mientras la sala donde se inscribió no termine, no puede
    entrar a otra; al terminar la sala se llena `released_at` y el ID queda libre. La base de datos lo garantiza con un índice único PARCIAL que solo mira
    las inscripciones activas y sin liberar (`game_id` + `player_id`), así dos toques a la vez tampoco lo duplican."""
    __tablename__ = 'tournament_entries'
    __table_args__ = (
        db.UniqueConstraint('room_id', 'slot_number', name='uq_tournament_entry_slot'),
        db.Index('uq_tournament_entry_live_player', 'game_id', 'player_id', unique=True,
                 sqlite_where=db.text("status = 'active' AND released_at IS NULL"), postgresql_where=db.text("status = 'active' AND released_at IS NULL")),
    )
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=False, index=True)
    room_id = db.Column(db.Integer, db.ForeignKey('tournament_rooms.id'), nullable=False, index=True)
    game_id = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=False, index=True)   # el juego del torneo (para saber si el ID ya está en otra sala)
    player_id = db.Column(db.String(100), nullable=False, index=True)
    player_nick = db.Column(db.String(150))
    slot_number = db.Column(db.Integer, nullable=False)
    ip = db.Column(db.String(64), index=True)
    status = db.Column(db.String(10), nullable=False, default='active')   # 'active' | 'removed' (el admin lo sacó de la lista: no puede volver a entrar)
    released_at = db.Column(db.DateTime)   # la sala terminó: el ID ya puede inscribirse en otra sala (la inscripción queda en el historial)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class TournamentPrize(db.Model):
    """Premio de un puesto (1.º, 2.º...) del torneo: un paquete del juego que se entrega solo. Vale para CADA sala (cada sala tiene sus ganadores)."""
    __tablename__ = 'tournament_prizes'
    __table_args__ = (db.UniqueConstraint('tournament_id', 'place', name='uq_tournament_prize_place'),)
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=False, index=True)
    place = db.Column(db.Integer, nullable=False)
    package_id = db.Column(db.Integer, db.ForeignKey('packages.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    package = db.relationship('Package')


class TournamentWinner(db.Model):
    """Ganador de un puesto en una sala (historial: fecha, ID y nombre). Una vez entregado no se puede cambiar ni repetir."""
    __tablename__ = 'tournament_winners'
    __table_args__ = (
        db.UniqueConstraint('room_id', 'place', name='uq_tournament_winner_place'),
        db.UniqueConstraint('room_id', 'player_id', name='uq_tournament_winner_player'),
    )
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=False, index=True)
    room_id = db.Column(db.Integer, db.ForeignKey('tournament_rooms.id'), nullable=False, index=True)
    place = db.Column(db.Integer, nullable=False)
    player_id = db.Column(db.String(100), nullable=False)
    player_nick = db.Column(db.String(150))
    package_id = db.Column(db.Integer, db.ForeignKey('packages.id'))
    prize_order_id = db.Column(db.Integer, db.ForeignKey('orders.id'))
    status = db.Column(db.String(12), nullable=False, default='pending')   # 'pending' | 'delivering' | 'delivered'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    delivered_at = db.Column(db.DateTime)

    tournament = db.relationship('Tournament')
    room = db.relationship('TournamentRoom')
    package = db.relationship('Package')
    prize_order = db.relationship('Order')
