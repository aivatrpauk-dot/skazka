"""Настройки бота через Telegram Bot API.
Применяются при каждом старте бота. Если хочешь поменять — правишь здесь и рестартишь.

Что НЕЛЬЗЯ настроить программно (только руками в @BotFather):
- аватарка (/setuserpic, нужен PNG 512x512)
- привязка ЮKassa как платёжного провайдера (/mybots → Payments → ЮKassa)
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import BotCommand

from .config import config

logger = logging.getLogger(__name__)


BOT_NAME = "Сказка | Истории на ночь"

BOT_SHORT_DESCRIPTION = (
    "Персональные сказки на ночь для вашего ребёнка. "
    "Имя, любимый герой — и через 10 секунд готовая сказка с озвучкой."
)

BOT_DESCRIPTION = (
    "Здравствуйте! Это «Сказка» — бот, который пишет короткие сказки на ночь "
    "с именем вашего ребёнка, его любимым героем и доброй моралью.\n\n"
    "Бесплатно — 3 сказки. Дальше:\n"
    "✨ Подписка 490 ₽/мес — безлимит + озвучка нежным голосом + обложка-картинка\n"
    "🎁 Подарок 199 ₽ — одна персональная сказка для ребёнка близких\n\n"
    "Нажмите «Запустить», чтобы попробовать."
)

BOT_COMMANDS = [
    BotCommand(command="start", description="Главное меню и создание сказки"),
    BotCommand(command="support", description="Связаться с поддержкой"),
    BotCommand(command="cancel_subscription", description="Отменить подписку"),
    BotCommand(command="refund", description="Возврат в первые 7 дней"),
    BotCommand(command="delete_me", description="Удалить мои данные (152-ФЗ)"),
]


async def setup_bot_profile(bot: Bot) -> None:
    """Шлёт в Telegram Bot API все метаданные бота. Идемпотентно."""
    me = await bot.get_me()
    logger.info("Setting profile for @%s", me.username)

    try:
        await bot.set_my_name(name=BOT_NAME)
    except Exception as e:
        # set_my_name можно дёргать максимум раз в N минут на язык
        logger.warning("set_my_name skipped: %s", e)

    try:
        await bot.set_my_short_description(short_description=BOT_SHORT_DESCRIPTION)
    except Exception as e:
        logger.warning("set_my_short_description skipped: %s", e)

    try:
        await bot.set_my_description(description=BOT_DESCRIPTION)
    except Exception as e:
        logger.warning("set_my_description skipped: %s", e)

    try:
        await bot.set_my_commands(commands=BOT_COMMANDS)
    except Exception as e:
        logger.warning("set_my_commands skipped: %s", e)

    logger.info("Profile setup done for @%s (brand=%s)", me.username, config.bot_brand)
