from aiogram import Router

from . import admin, billing, faq, feedback, gift, legal, library, partner, referral, start, story, support


def setup_routers() -> Router:
    root = Router(name="root")
    root.include_routers(
        start.router,
        # admin / partner / legal — ДО основной логики, чтобы их /команды
        # не перехватились FSM-стейтами или общими хендлерами.
        admin.router,
        partner.router,
        legal.router,
        story.router,
        library.router,
        billing.router,
        referral.router,
        faq.router,
        gift.router,
        # feedback — ДО support, чтобы FSM-state WaitingFeedbackText
        # не перехватился support catch-all хендлером
        feedback.router,
        # support — ПОСЛЕДНИМ: его catch-all message handler не должен
        # перехватывать сообщения мастера сказки, кастомного героя
        # и подарочного послания
        support.router,
    )
    return root
