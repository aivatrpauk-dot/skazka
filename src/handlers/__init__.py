from aiogram import Router

from . import start, story, library, billing, referral, faq, gift, support


def setup_routers() -> Router:
    root = Router(name="root")
    root.include_routers(
        start.router,
        story.router,
        library.router,
        billing.router,
        referral.router,
        faq.router,
        gift.router,
        # support — ПОСЛЕДНИМ: его catch-all message handler не должен
        # перехватывать сообщения мастера сказки, кастомного героя
        # и подарочного послания
        support.router,
    )
    return root
