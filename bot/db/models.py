"""ORM-модели магазина VallShop.

Структура спроектирована так, чтобы администратор мог сам управлять
каталогом: добавлять товары (Product) и их номиналы (Variant), задавать
наценку/цену и остатки (StockItem), включать/выключать позиции.
"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


# Головной магазин владельца платформы — merchant_id = 0 (не франшиза).
# Все существующие данные принадлежат ему; франшизы получают id > 0.
HEAD_MERCHANT_ID = 0


# ──────────────────────────────────────────────────────────────────────────
# Перечисления статусов
# ──────────────────────────────────────────────────────────────────────────
class StockStatus(str, enum.Enum):
    AVAILABLE = "available"   # свободен, можно выдать
    RESERVED = "reserved"     # зарезервирован под заказ
    SOLD = "sold"             # выдан покупателю


class OrderStatus(str, enum.Enum):
    CREATED = "created"               # создан, оплата ещё не начата
    AWAITING_PAYMENT = "awaiting_payment"  # выставлен счёт, ждём оплату
    PAID = "paid"                     # оплата подтверждена
    FULFILLING = "fulfilling"         # идёт выдача (сток/топап)
    COMPLETED = "completed"           # код выдан покупателю
    FAILED = "failed"                 # ошибка выдачи (нужен возврат/ручная работа)
    NEEDS_ACTION = "needs_action"     # ошибка/нет средств у магазина — ждёт ручной выдачи
    EXPIRED = "expired"               # счёт просрочен, не оплачен
    REFUNDED = "refunded"             # возврат


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    UNDERPAID = "underpaid"
    EXPIRED = "expired"
    FAILED = "failed"


# ──────────────────────────────────────────────────────────────────────────
# Пользователи
# ──────────────────────────────────────────────────────────────────────────
class Merchant(Base):
    """Франшиза: отдельный магазин-бот партнёра на том же движке.

    ``id = 0`` (HEAD_MERCHANT_ID) — головной магазин владельца платформы.
    Партнёрские франшизы получают ``id > 0``. На Фазе 1 реально работает
    только головной; таблица и колонки ``merchant_id`` закладывают фундамент.
    """

    __tablename__ = "merchants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[Optional[str]] = mapped_column(String(255))        # название магазина
    bot_token: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    bot_username: Mapped[Optional[str]] = mapped_column(String(64))
    admin_user_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)  # Telegram id владельца франшизы
    admin_username: Mapped[Optional[str]] = mapped_column(String(64))
    channel: Mapped[Optional[str]] = mapped_column(String(128))      # обязательная подписка
    # "active" | "suspended"
    status: Mapped[str] = mapped_column(String(16), default="active")
    # Наценка мерчанта по умолчанию (%) поверх цены владельца; применяется к
    # товарам без индивидуальной цены (см. MerchantVariantPrice).
    default_markup: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=0, server_default="0")
    # Баланс заработка мерчанта (₽) — доступно к выводу (маржа минус выводы).
    earnings_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, server_default="0")
    # Заработано всего (₽) — накопленная маржа за всё время (выводы не уменьшают).
    earnings_total_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MerchantPayout(Base):
    """Заявка франшизы на вывод заработка. Деньги владелец отправляет вручную.

    Сумма — в рублях (даже для USDT-кошелька владелец конвертирует сам).
    ``status``: pending (ждёт решения) | approved (выполнена) | rejected (отклонена).
    При создании сумма резервируется (списывается с earnings_rub), при отклонении
    возвращается.
    """

    __tablename__ = "merchant_payout"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    merchant_id: Mapped[int] = mapped_column(Integer, index=True)
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    method: Mapped[str] = mapped_column(String(12))        # sbp | usdt
    wallet: Mapped[str] = mapped_column(String(255))       # снимок реквизитов на момент заявки
    status: Mapped[str] = mapped_column(String(12), default="pending")
    reason: Mapped[Optional[str]] = mapped_column(String(255))  # причина отклонения
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class MerchantVariantPrice(Base):
    """Цена мерчанта на конкретный номинал (поверх цены владельца).

    ``mode="markup"`` — наценка % от цены владельца; ``mode="fixed"`` — ручная
    ₽-цена (не ниже цены владельца, см. стоп-лосс в services.mprice).
    Записи нет → применяется наценка по умолчанию мерчанта (Merchant.default_markup).
    """

    __tablename__ = "merchant_variant_price"

    merchant_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    variant_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(12), default="markup")  # markup | fixed
    markup_percent: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2))
    price_rub: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))


class User(Base):
    __tablename__ = "users"

    # Составной ключ (id, merchant_id): один Telegram-пользователь может быть
    # клиентом у нескольких мерчантов, у каждого — свой баланс и записи.
    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # Telegram user id
    merchant_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, default=HEAD_MERCHANT_ID, server_default="0", index=True
    )
    username: Mapped[Optional[str]] = mapped_column(String(64))
    full_name: Mapped[Optional[str]] = mapped_column(String(255))
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    block_reason: Mapped[Optional[str]] = mapped_column(String(255))
    # Достижимость для рассылки (НЕ бан админа): False — пользователь заблокировал
    # бота / аккаунт удалён (определяем по ошибке доставки). Возвращается в True,
    # как только человек снова пишет боту. Нужно для счётчика активных/неактивных.
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1"
    )
    # Показали ли разовое сообщение о канале (мягкий режим «ссылка один раз»).
    channel_notified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    # Баланс пользователя (USD ≈ USDT). Пополняется криптой, тратится на покупки.
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    # default (Python) И server_default: перестройка таблицы (составной ключ) не
    # сохраняет DB-дефолт, поэтому Python-дефолт гарантирует, что дата регистрации
    # проставляется на каждой вставке — иначе «новых за период» всегда 0.
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, server_default=func.now()
    )

    # Реферальная программа.
    referred_by: Mapped[Optional[int]] = mapped_column(Integer, index=True)  # кто пригласил
    ref_balance_usdt: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    ref_balance_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    ref_earned_usdt: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    ref_earned_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)


# ──────────────────────────────────────────────────────────────────────────
# Каталог: товар -> вариации (номиналы)
# ──────────────────────────────────────────────────────────────────────────
class Product(Base):
    """Товар верхнего уровня, например «PUBG Mobile UC (Global)»."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game: Mapped[str] = mapped_column(String(64), default="PUBG")
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # Подкатегории: товар может быть вложен в родительский товар-категорию.
    # parent_id IS NULL — товар верхнего уровня.
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE")
    )
    # Кастом-эмодзи (custom_emoji_id) для иконки кнопки товара.
    icon_emoji_id: Mapped[Optional[str]] = mapped_column(String(32))
    # Своя картинка-баннер категории (Telegram file_id). None — общий баннер.
    banner_file_id: Mapped[Optional[str]] = mapped_column(String(255))
    # Оформление кнопки категории в каталоге.
    button_style: Mapped[Optional[str]] = mapped_column(String(16))   # primary/success/danger/None
    full_width: Mapped[bool] = mapped_column(Boolean, default=False)   # кнопка на всю строку
    # Настраиваемые тексты (наследуются номиналами). None = наследовать/дефолт,
    # "" = скрыть. pending_text — строка «оформляем…», delivered_text — сообщение
    # об успешной выдаче на аккаунт (без кода).
    pending_text: Mapped[Optional[str]] = mapped_column(Text)
    delivered_text: Mapped[Optional[str]] = mapped_column(Text)
    # Только для головного магазина: товар не показывается клиентам франшиз.
    head_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    variants: Mapped[List["Variant"]] = relationship(
        back_populates="product",
        order_by="Variant.sort_order",
        cascade="all, delete-orphan",
    )


class Variant(Base):
    """Конкретный номинал товара (например «60 UC»).

    Цена покупателю считается так (см. services.pricing):
      * если задана ``price_usd`` — это фиксированная цена продажи;
      * иначе цена = ``cost_usd`` * (1 + наценка/100), где наценка —
        ``markup_percent`` вариации, а если она не задана — дефолт из конфига.
    """

    __tablename__ = "variants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(255))  # «60 UC»

    # Привязка к поставщику LioGames.
    liog_product_id: Mapped[int] = mapped_column(Integer)
    liog_variation_id: Mapped[int] = mapped_column(Integer)

    # Деньги (USD ≈ USDT).
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)   # закупка GOLD
    price_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))   # фикс-цена (переопределение)
    markup_percent: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2))  # наценка % (переопределение)
    # Цена в рублях. В режиме "fixed" — ручная неизменная цена; в режиме
    # "smart" — «липкая» цена, пересчитывается от живого закупа по наценке.
    price_rub: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    # Режим цены: "smart" (наценка от живого закупа поставщика, порог 1%),
    # "fixed" (фикс ₽, не меняется), "float"/None (плавает по курсу).
    price_mode: Mapped[Optional[str]] = mapped_column(String(12))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # Описание номинала (показывается покупателю на экране номинала). None/"" — нет.
    description: Mapped[Optional[str]] = mapped_column(Text)
    # Кастом-эмодзи (custom_emoji_id) для иконки кнопки номинала.
    icon_emoji_id: Mapped[Optional[str]] = mapped_column(String(32))
    # Тексты уровня номинала (приоритетнее товара). None = наследовать, "" = скрыть.
    pending_text: Mapped[Optional[str]] = mapped_column(Text)
    delivered_text: Mapped[Optional[str]] = mapped_column(Text)

    # Источник выдачи: "stock" (свой сток), "liogames" (сток+ручная закупка),
    # "fazercard" (buy-on-demand у FazerCard).
    source: Mapped[str] = mapped_column(String(16), default="stock")
    # FazerCard: тип ("gamekey"/"giftcard"/"topup") и идентификаторы оффера.
    fzr_kind: Mapped[Optional[str]] = mapped_column(String(16))
    fzr_a: Mapped[Optional[str]] = mapped_column(String(64))  # game_id / category_id
    fzr_b: Mapped[Optional[str]] = mapped_column(String(64))  # key_id / card_id / offer_id

    # SMM (source="socproof"): услуга панели soc-proof.su. Цена — за 1000,
    # считается в рублях (закуп smm_rate ₽/1000 × наценка). При покупке клиент
    # присылает количество (в пределах smm_min..smm_max) и ссылку для накрутки.
    smm_service: Mapped[Optional[int]] = mapped_column(Integer)   # id услуги у поставщика
    smm_min: Mapped[Optional[int]] = mapped_column(Integer)
    smm_max: Mapped[Optional[int]] = mapped_column(Integer)
    smm_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4))  # закуп ₽ за 1000

    product: Mapped["Product"] = relationship(back_populates="variants")
    stock_items: Mapped[List["StockItem"]] = relationship(
        back_populates="variant", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("product_id", "liog_variation_id", name="uq_variant_liog"),
    )


# ──────────────────────────────────────────────────────────────────────────
# Сток ваучер-кодов
# ──────────────────────────────────────────────────────────────────────────
class StockItem(Base):
    """Заранее закупленный ваучер-код для мгновенной выдачи."""

    __tablename__ = "stock_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    variant_id: Mapped[int] = mapped_column(
        ForeignKey("variants.id", ondelete="CASCADE")
    )
    code: Mapped[str] = mapped_column(String(255))
    status: Mapped[StockStatus] = mapped_column(
        String(16), default=StockStatus.AVAILABLE
    )
    order_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    sold_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    variant: Mapped["Variant"] = relationship(back_populates="stock_items")

    __table_args__ = (
        UniqueConstraint("variant_id", "code", name="uq_stock_variant_code"),
    )


# ──────────────────────────────────────────────────────────────────────────
# Заказы и платежи
# ──────────────────────────────────────────────────────────────────────────
class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant_id: Mapped[int] = mapped_column(
        Integer, default=HEAD_MERCHANT_ID, server_default="0", index=True
    )
    client_ref: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    # users теперь с составным ключом (id, merchant_id) — FK одностолбцовый не
    # ставим, связываем по user_id + merchant_id в запросах.
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id"))

    # Цена за единицу и количество; итог = price_usd * quantity.
    price_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[OrderStatus] = mapped_column(String(24), default=OrderStatus.CREATED)

    # Результат выдачи (при количестве >1 — коды построчно).
    delivery_code: Mapped[Optional[str]] = mapped_column(Text)
    # Если выдавали через топап у поставщика — его order_id.
    liog_order_id: Mapped[Optional[str]] = mapped_column(String(64))
    # Поставщик buy-on-demand ("liogames"/"fazercard") и его order_id.
    supplier: Mapped[Optional[str]] = mapped_column(String(16))
    supplier_order_id: Mapped[Optional[str]] = mapped_column(String(64))
    # Собранные при покупке данные (топап: @username, player id и т.п.) — JSON.
    fields_json: Mapped[Optional[str]] = mapped_column(Text)
    # Причина ошибки выдачи (для раздела «Не выполненные»).
    fail_reason: Mapped[Optional[str]] = mapped_column(String(255))
    # Фактическая себестоимость заказа в USD (для товаров с плавающей закупкой,
    # напр. Steam-пополнение). None — считать по variant.cost_usd × quantity.
    cost_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4))
    # Маржа мерчанта по заказу (₽): (цена клиента − цена владельца) × кол-во.
    # Для головного (merchant_id=0) = 0. Начисляется на баланс заработка при
    # покупке и сторнируется при возврате.
    merchant_margin: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, server_default="0")

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    variant: Mapped["Variant"] = relationship()
    payment: Mapped[Optional["Payment"]] = relationship(
        back_populates="order", uselist=False, cascade="all, delete-orphan"
    )


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))

    provider: Mapped[str] = mapped_column(String(32), default="boltutil")
    # Идентификатор заказа на стороне платёжного провайдера.
    provider_order_id: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    checkout_url: Mapped[Optional[str]] = mapped_column(Text)
    address: Mapped[Optional[str]] = mapped_column(String(128))
    network: Mapped[Optional[str]] = mapped_column(String(16))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    currency: Mapped[str] = mapped_column(String(8), default="USDT")

    status: Mapped[PaymentStatus] = mapped_column(
        String(16), default=PaymentStatus.PENDING
    )
    tx_hash: Mapped[Optional[str]] = mapped_column(String(128))

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    order: Mapped["Order"] = relationship(back_populates="payment")


# ──────────────────────────────────────────────────────────────────────────
# Активация кодов (отдельная ветка: уже оплаченные коды Robux/др.)
# ──────────────────────────────────────────────────────────────────────────
class ActivationCode(Base):
    """Заранее оплаченный код для активации (перенос из robloxbot)."""

    __tablename__ = "activation_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    product: Mapped[str] = mapped_column(String(255))
    instruction: Mapped[Optional[str]] = mapped_column(Text)
    robux_amount: Mapped[Optional[int]] = mapped_column(Integer)
    # "free" (свободен) | "used" (использован)
    status: Mapped[str] = mapped_column(String(16), default="free")
    used_by: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class ActivationRequest(Base):
    """Заявка на проверку активации (ожидает решения админа)."""

    __tablename__ = "activation_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant_id: Mapped[int] = mapped_column(
        Integer, default=HEAD_MERCHANT_ID, server_default="0", index=True
    )
    code: Mapped[str] = mapped_column(String(64), index=True)
    product: Mapped[Optional[str]] = mapped_column(String(255))
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(64))
    nickname: Mapped[Optional[str]] = mapped_column(String(128))
    expected_price: Mapped[Optional[int]] = mapped_column(Integer)
    actual_price: Mapped[Optional[int]] = mapped_column(Integer)
    # "review" | "approved" | "rejected"
    status: Mapped[str] = mapped_column(String(16), default="review")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class Setting(Base):
    """Глобальные настройки магазина (ключ-значение)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(Text)


class ReferralEarning(Base):
    """Журнал реферальных начислений (для прозрачности и сторно при возврате)."""

    __tablename__ = "referral_earnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant_id: Mapped[int] = mapped_column(
        Integer, default=HEAD_MERCHANT_ID, server_default="0", index=True
    )
    referrer_id: Mapped[int] = mapped_column(Integer, index=True)   # кто получил
    referral_id: Mapped[int] = mapped_column(Integer, index=True)   # за кого (приглашённый)
    order_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(32), default="purchase_percent")
    currency: Mapped[str] = mapped_column(String(8), default="USDT")  # USDT | RUB
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    # "credited" | "reversed"
    status: Mapped[str] = mapped_column(String(16), default="credited")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class VpnSubscription(Base):
    """Выданная VPN-подписка (VPNresellers): токен → base64-подписка."""

    __tablename__ = "vpn_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant_id: Mapped[int] = mapped_column(
        Integer, default=HEAD_MERCHANT_ID, server_default="0", index=True
    )
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    order_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    account_id: Mapped[Optional[str]] = mapped_column(String(64))
    username: Mapped[Optional[str]] = mapped_column(String(64))
    # Готовая подписка (base64 из vless://), отдаётся на /sub/<token>.
    config_b64: Mapped[Optional[str]] = mapped_column(Text)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MenuButton(Base):
    """Переопределения оформления кнопок меню (раздел «Оформление»).

    Ключ — стабильный id кнопки из реестра (bot/services/menu.py), в паре с
    ``merchant_id`` (0 = головной, >0 = франшиза). У каждого магазина свой слой
    правок; пустой слой → значения по умолчанию из реестра.
    """

    __tablename__ = "menu_buttons"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[int] = mapped_column(Integer, primary_key=True, default=HEAD_MERCHANT_ID)
    label: Mapped[Optional[str]] = mapped_column(String(255))       # None = дефолт
    emoji_id: Mapped[Optional[str]] = mapped_column(String(32))     # None = дефолт, "" = без эмодзи
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[Optional[int]] = mapped_column(Integer)      # None = дефолт
    # Цвет кнопки: None = дефолт (реестр у головного / без цвета у франшизы),
    # "" = без цвета, "primary"/"success"/"danger" = синий/зелёный/красный.
    style: Mapped[Optional[str]] = mapped_column(String(16))


class MerchantCatalogOverride(Base):
    """Переопределение отображения товара/номинала для конкретной франшизы.

    Каталог общий (товары/номиналы владельца). Франшиза может переопределить в
    своём боте подпись (кнопку), эмодзи и описание — не меняя общий каталог.
    ``item_type`` = "product" | "variant"; пустые поля = значение из каталога.
    """

    __tablename__ = "merchant_catalog"

    merchant_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_type: Mapped[str] = mapped_column(String(12), primary_key=True)  # product|variant
    item_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[Optional[str]] = mapped_column(String(255))      # None = дефолт
    emoji_id: Mapped[Optional[str]] = mapped_column(String(32))    # None = дефолт, "" = без эмодзи
    description: Mapped[Optional[str]] = mapped_column(Text)       # None = дефолт (для product)


class MerchantSetting(Base):
    """Пер-мерчантные настройки оформления (тексты экранов, баннеры, контакты).

    Ключ-значение в разрезе магазина (``merchant_id``). Используется только для
    оформления франшиз; глобальные операционные настройки живут в ``Setting``.
    Пустой набор у новой франшизы → базовый шаблон из реестра/ассетов.
    """

    __tablename__ = "merchant_settings"

    merchant_id: Mapped[int] = mapped_column(Integer, primary_key=True, default=HEAD_MERCHANT_ID)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(Text)


class Review(Base):
    """Отзыв покупателя о выполненном заказе."""

    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant_id: Mapped[int] = mapped_column(
        Integer, default=HEAD_MERCHANT_ID, server_default="0", index=True
    )
    order_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(64))
    variant_id: Mapped[Optional[int]] = mapped_column(Integer)
    product_title: Mapped[Optional[str]] = mapped_column(String(255))
    amount_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    rating: Mapped[int] = mapped_column(Integer, default=5)
    text: Mapped[Optional[str]] = mapped_column(Text)
    # "new" (ждёт модерации) | "published"
    status: Mapped[str] = mapped_column(String(16), default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TopUpStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"
    FAILED = "failed"


class TopUp(Base):
    """Пополнение баланса пользователя через BoltUtil (USDT)."""

    __tablename__ = "topups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant_id: Mapped[int] = mapped_column(
        Integer, default=HEAD_MERCHANT_ID, server_default="0", index=True
    )
    client_ref: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)

    amount_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2))  # сколько зачислить
    status: Mapped[TopUpStatus] = mapped_column(String(16), default=TopUpStatus.PENDING)

    provider: Mapped[str] = mapped_column(String(32), default="boltutil")
    provider_order_id: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    checkout_url: Mapped[Optional[str]] = mapped_column(Text)
    address: Mapped[Optional[str]] = mapped_column(String(128))
    network: Mapped[Optional[str]] = mapped_column(String(16))
    pay_amount: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=0)  # сумма к оплате on-chain
    currency: Mapped[str] = mapped_column(String(8), default="USDT")
    tx_hash: Mapped[Optional[str]] = mapped_column(String(128))

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


# ──────────────────────────────────────────────────────────────────────────
# Рассылки: запланированная/выполненная рассылка и клики по кнопкам поста
# ──────────────────────────────────────────────────────────────────────────
class Broadcast(Base):
    """Одна рассылка магазина: черновик + расписание + результат.

    Нужна как сущность с id, чтобы (а) отложенная отправка переживала перезапуск,
    (б) к кнопкам поста можно было привязать подсчёт кликов (callback bck:id:idx).
    Кнопки хранятся JSON-списком (см. broadcast_post_kb).
    """

    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    merchant_id: Mapped[int] = mapped_column(
        Integer, default=HEAD_MERCHANT_ID, server_default="0", index=True
    )
    created_by: Mapped[int] = mapped_column(Integer)  # tg-id автора
    text: Mapped[Optional[str]] = mapped_column(Text)
    photo: Mapped[Optional[str]] = mapped_column(String(255))  # file_id
    buttons_json: Mapped[Optional[str]] = mapped_column(Text)
    # scheduled|sending|done|canceled|failed
    status: Mapped[str] = mapped_column(String(16), default="scheduled", index=True)
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)  # UTC
    recipients: Mapped[int] = mapped_column(Integer, default=0)
    sent: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class BroadcastClick(Base):
    """Счётчик кликов по кнопке поста: (рассылка, индекс кнопки, пользователь).

    Строк столько же, сколько уникальных кликнувших × кнопок. ``count`` — сколько
    раз этот пользователь тапал. Уникальные = число строк, всего тапов = сумма.
    """

    __tablename__ = "broadcast_clicks"

    broadcast_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    btn_idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ──────────────────────────────────────────────────────────────────────────
# Бонусы: промокоды на пополнение баланса и бонус-рассылки (дропы)
# ──────────────────────────────────────────────────────────────────────────
class PromoCode(Base):
    """Статический промокод: сумма бонуса на баланс, лимит активаций, 1 раз/юзер."""

    __tablename__ = "promo_codes"
    __table_args__ = (UniqueConstraint("merchant_id", "code", name="uq_promo_merchant_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    merchant_id: Mapped[int] = mapped_column(
        Integer, default=HEAD_MERCHANT_ID, server_default="0", index=True
    )
    code: Mapped[str] = mapped_column(String(32), index=True)  # в верхнем регистре
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    max_activations: Mapped[int] = mapped_column(Integer, default=0)  # 0 = без лимита
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PromoActivation(Base):
    """Факт активации промокода пользователем (один раз на пользователя)."""

    __tablename__ = "promo_activations"

    promo_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class BonusDrop(Base):
    """Бонус-рассылка: пост с кнопкой «забрать», лимит активаций, 24ч жизни.

    Кто успел нажать в пределах ``max_claims`` — получает ``amount_rub`` на баланс
    и редактируемое сообщение об успехе; остальные — сообщение об ошибке. Через
    24ч пост и служебные сообщения удаляются (см. bonus_drop_messages).
    """

    __tablename__ = "bonus_drops"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    merchant_id: Mapped[int] = mapped_column(
        Integer, default=HEAD_MERCHANT_ID, server_default="0", index=True
    )
    created_by: Mapped[int] = mapped_column(Integer)
    text: Mapped[Optional[str]] = mapped_column(Text)
    photo: Mapped[Optional[str]] = mapped_column(String(255))
    btn_label: Mapped[str] = mapped_column(String(128), default="🎁 Забрать бонус")
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    max_claims: Mapped[int] = mapped_column(Integer, default=0)
    claimed_count: Mapped[int] = mapped_column(Integer, default=0)
    success_text: Mapped[Optional[str]] = mapped_column(Text)
    success_btn_label: Mapped[Optional[str]] = mapped_column(String(128))
    success_btn_kind: Mapped[Optional[str]] = mapped_column(String(8))     # cb|url
    success_btn_target: Mapped[Optional[str]] = mapped_column(String(255))
    error_text: Mapped[Optional[str]] = mapped_column(Text)
    # sending → active (разослан, приём активаций) → expired (24ч прошли/удалён)
    status: Mapped[str] = mapped_column(String(16), default="sending", index=True)
    recipients: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)


class BonusActivation(Base):
    """Факт получения бонуса из дропа пользователем (один раз на пользователя)."""

    __tablename__ = "bonus_activations"

    drop_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class BonusDropMessage(Base):
    """Отправленное сообщение дропа (пост/успех/ошибка) — для удаления через 24ч."""

    __tablename__ = "bonus_drop_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    drop_id: Mapped[int] = mapped_column(Integer, index=True)
    chat_id: Mapped[int] = mapped_column(Integer)
    message_id: Mapped[int] = mapped_column(Integer)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class WebAccount(Base):
    """Аккаунт публичного сайта (email+пароль, опционально привязан к Telegram).

    Кошелёк (баланс/заказы) хранится в строке ``User`` с ``merchant_id=0``:
    ``wallet_user_id`` указывает на неё. При входе через Telegram и связывании
    аккаунтов кошелёк Telegram становится главным (балансы складываются,
    история объединяется) — см. сервис связывания.
    """

    __tablename__ = "web_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Telegram user id (если вошёл/связал через Telegram), иначе None.
    telegram_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, index=True)
    # Кошелёк аккаунта: User.id при merchant_id=0 (баланс/заказы). None — ещё не создан.
    wallet_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, index=True)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
