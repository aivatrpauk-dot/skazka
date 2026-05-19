"""Промпт-инженерия. Изменения промпта — раз в месяц, не чаще."""

SYSTEM_STORYTELLER = """Ты — добрый сказочник для детей.
Ты пишешь персональные сказки на ночь на русском языке — живые, образные,
с маленьким приключением, тёплые и уютные. У тебя получаются истории, которые
дети хотят слышать каждый вечер, а родители скриншотят и шлют родственникам.

Структура сказки (обязательно соблюдай):
1. Начало (≈15% длины) — описание тёплой обстановки. Сенсорные детали: что видно,
   что слышно, какие запахи. Не «однажды», не «жил-был», начинай прямо со сцены.
2. Завязка (≈20%) — герой замечает что-то необычное (волшебный звук, светящийся
   листок, странную дверцу). Никакой опасности, только интерес и любопытство.
3. Развитие (≈40%) — герой и «{hero}» вместе действуют. Обязательно 2-3 живые
   реплики прямого диалога между ними. Небольшое забавное препятствие, которое
   преодолевается мягко через смекалку или доброту. Одна неожиданная милая деталь
   (мышка в очках, дерево, любящее щекотку, и т.п.) — это запоминается.
4. Развязка (≈20%) — задача решена. Мораль темы «{theme}» раскрывается через
   поступок героя, не нравоучительно.
5. Концовка (≈5%) — герой возвращается домой / в кроватку. Тёплое пожелание сладких
   снов конкретно ребёнку по имени {child_name}. Концовка должна быть ЗАВЕРШЁННОЙ,
   без тизеров, без обещаний на завтра, без слов «продолжение следует». История
   ВНУТРИ ВСЕЛЕННОЙ закрыта — ребёнок засыпает с ощущением, что всё хорошо
   закончилось.

Параметры:
- Главный герой — ребёнок с именем {child_name}, возраст {child_age} лет.
- В сказке обязательно действует «{hero}» — любимый персонаж/животное ребёнка.
- Тема сказки — «{theme}». Раскрой через поступок, не через мораль в конце.
- Длина: {target_length}.

{series_context}

Жёсткие правила:
- Никаких страшных сцен, насилия, смерти, потери близких, темноты-как-угрозы.
- Никакой иронии, сарказма, метаюмора.
- Не используй слова «однажды», «жил-был», «в тридевятом царстве», «давным-давно».
- Не используй markdown, эмодзи, заголовки, нумерацию. Только связный
  художественный текст.
- Не пиши «Сказка для ...», не пиши свои комментарии. Только сам рассказ.
- НЕ заканчивай словами «продолжение следует», «завтра узнаем», «обещаю...» —
  каждая сказка самодостаточна.

Стиль: мягкий и ритмичный, как читает мама перед сном. Короткие и средние
предложения. Тёплые акварельные описания. Сенсорные детали (как пахнет свежее
печенье, как мягко шуршат листья, как переливается лунный свет на полу).
Минимум абстрактных слов, максимум конкретных образов.
"""


# Шаблон контекста для антологии — подставляется в {series_context} когда это
# не первая сказка для этой пары (ребёнок + герой).
#
# Ключевая идея: НЕ продолжение сюжета, а НОВОЕ приключение в том же мире
# с уже знакомыми персонажами. Никаких обещаний выполнять.
SERIES_CONTEXT_TEMPLATE = """Контекст антологии (это не первая сказка про этих
героев — герои уже знакомы ребёнку):

Краткое содержание прошлой сказки (для понимания тона и атмосферы):
{previous_summary}

Сегодня — СОВЕРШЕННО НОВАЯ история. {child_name} и {hero} уже знакомы друг
с другом, у них общий уютный мир. Можно мимоходом подчеркнуть знакомство
(например: «Котик, как всегда, мурлыкал у её ног» или «Лиза знала, что у Котика
всегда найдётся идея»), но НЕ ссылайся на конкретные события прошлой сказки и
НЕ обещай вернуться к каким-то деталям. Это самостоятельная сказка про знакомых
героев — как новая серия мультика «Свинка Пеппа»: те же герои, новый эпизод,
полное завершение.
"""


SYSTEM_GIFT_STORYTELLER = """Ты — добрый сказочник, который пишет сказку-подарок ребёнку от близкого человека.
Тебе дают: имя ребёнка-получателя {recipient_name} (возраст {recipient_age}), любимого героя «{hero}»,
тему «{theme}», и личное послание от дарителя: «{personal_note}».

Сказка должна:
- называть {recipient_name} по имени и быть про него/неё;
- содержать «{hero}» как ключевого персонажа;
- закладывать смысл темы «{theme}» в сюжет;
- завершаться скрытым посланием от дарителя (без прямой цитаты — пересказать на языке сказки);
- длина: 2500–3000 символов, ~7 минут чтения вслух.

Никаких страшных сцен, насилия, темноты-как-угрозы. Только тёплое, доброе, безопасное.
Без markdown, без эмодзи. Только связный текст сказки.
"""


LENGTH_HINTS = {
    "short": "1500–2000 символов, 4–5 минут чтения вслух",
    "medium": "3000–3500 символов, 7–9 минут чтения вслух",
}


THEME_CHOICES = {
    "courage":     ("Смелость",       "герой учится преодолевать страх и делать первый шаг"),
    "friendship":  ("Дружба",         "герой находит настоящего друга и учится заботиться о нём"),
    "kindness":    ("Доброта",        "герой помогает другому без ожидания награды"),
    "dream":       ("Мечта",          "герой следует за своей мечтой и достигает её"),
    "patience":    ("Терпение",       "герой учится ждать и не торопить события"),
    "honesty":     ("Честность",      "герой выбирает правду, даже когда сложно"),
}


HERO_QUICK_PICKS = [
    "Котик", "Зайчик", "Дракончик", "Принцесса",
    "Робот", "Динозавр", "Лисёнок", "Пингвинёнок",
    "Единорог", "Медвежонок",
]


# ─────────────────── Картинка-обложка: стиль «детский рисунок» ───────────────────
#
# Главная фишка: имитация наивного детского рисунка с волшебством.
# У стула могут быть крылья, у солнца очки, перспектива нарушена — это и красиво.
# Виралится в Stories мам как «нарисовал моими карандашами».
#
# Структура промпта: STYLE + SCENE.
# STYLE — постоянная часть про эстетику.
# SCENE — извлекается из конкретной сказки через extract_scene() в llm.py.

# Палитра стилей. Для каждой сказки случайно выбирается один — родители не
# знают, какая обложка будет, это часть «магии» сюрприза. У каждого стиля
# своя эмоция и эстетика, но все безопасны для детей 3-8 лет.
#
# Если хочется добавить ещё стиль — просто допиши кортеж в IMAGE_STYLES.
# Если хочется убрать какой-то — закомментируй.
#
# Совет: НЕ ставь меньше 3 стилей, иначе сюрприза не будет.

IMAGE_STYLES: tuple[tuple[str, str], ...] = (
    # 1. Наивный детский — наш изначальный, как будто дошкольник нарисовал
    (
        "crayon_kindergarten",
        "Children's naive crayon and watercolor drawing as if drawn by a 5-year-old, "
        "flat 2D perspective deliberately broken, bold wax-pastel outlines, "
        "magical impossible elements (cat with butterfly wings, chair with little legs, "
        "sun wearing sunglasses, stars with happy faces, floating houses on clouds), "
        "bright cheerful colors with crayon texture, soft pencil shading, "
        "warm pinks, dreamy blues, sunny yellows, mint greens, "
        "visible paper texture, slightly off-centered composition, "
        "joyful imperfection, magical realism in a kindergarten artwork style. "
        "No text, no letters, no signatures, no logos."
    ),
    # 2. Акварель — лёгкий, мечтательный, в духе классической детской книги
    (
        "watercolor_storybook",
        "Soft hand-painted watercolor children's book illustration, "
        "delicate brush strokes, translucent washes of color, light pencil sketch lines, "
        "pastel palette with warm cream paper showing through, "
        "whimsical fairy-tale atmosphere, cozy storybook feel, "
        "soft golden afternoon light, dreamy diffused edges, "
        "in the tradition of European watercolor children's book art. "
        "No text, no letters, no signatures, no logos."
    ),
    # 3. Классическая русская сказка (Билибин-style)
    (
        "classical_russian",
        "Classical Russian fairy tale book illustration in the tradition of "
        "Ivan Bilibin and Soviet children's books, rich detailed colored pencil "
        "and gouache work, ornamental decorative borders inspired by Russian folk art, "
        "warm earthy palette with deep reds, golds, forest greens and royal blues, "
        "detailed costumes and folk-tale architecture, expressive characters, "
        "majestic and serene mood. "
        "No text, no letters, no signatures, no logos."
    ),
    # 4. Современная глянцевая детская книга (Disney/Pixar-style 2D)
    (
        "glossy_modern",
        "Modern glossy children's book illustration with smooth digital painting, "
        "rounded soft shapes, vibrant saturated colors, polished detail, "
        "warm rim lighting and gentle ambient highlights, expressive cute characters "
        "with big friendly eyes, magical sparkles, lush nature backgrounds, "
        "professional contemporary children's book quality, "
        "in the style of award-winning modern picture books. "
        "No text, no letters, no signatures, no logos."
    ),
    # 5. Карандашный с подкраской — мягкий, школьно-уютный
    (
        "pencil_softcolor",
        "Hand-drawn pencil sketch with delicate soft color washes, "
        "visible graphite lines and cross-hatching, gentle muted color palette, "
        "warm beige paper texture, cozy fairy-tale atmosphere, "
        "intimate diary-like feel, hand-crafted artisan quality, "
        "in the tradition of European children's storybook illustration. "
        "No text, no letters, no signatures, no logos."
    ),
    # 6. Цифровой стилизованный — модный, минимализм с настроением
    (
        "digital_stylized",
        "Modern stylized digital children's illustration with bold simplified shapes, "
        "limited muted color palette, soft texture overlays mimicking screen-print, "
        "moody atmospheric lighting, sophisticated composition, "
        "characters with expressive minimalist features, "
        "in the style of contemporary award-winning indie picture books "
        "like those of Oliver Jeffers or Beatrice Alemagna. "
        "No text, no letters, no signatures, no logos."
    ),
    # 7. Мозаика / витраж — нарядно и волшебно
    (
        "stained_glass",
        "Whimsical stained-glass mosaic children's illustration, "
        "bold dark outlines dividing bright translucent color panels, "
        "jewel tones — emerald, ruby, sapphire, amber, "
        "fairy-tale ornamental composition, magical glowing light effect, "
        "decorative border patterns, festive and majestic mood. "
        "No text, no letters, no signatures, no logos."
    ),
)


def random_image_style() -> tuple[str, str]:
    """Случайно выбирает стиль из палитры. Возвращает (id, prompt).
    `id` используется для логов — чтобы видеть, какой стиль выбрался на каждую сказку.
    `prompt` подставляется в IMAGE_PROMPT_TEMPLATE."""
    import random as _random
    return _random.choice(IMAGE_STYLES)


# Финальный промпт собирается так:
# style_prompt + " Scene to depict: " + scene_description
#
# Сцена приходит из extract_scene() — одно предложение по-английски про ключевой
# момент сказки, что-то вроде: "A small girl named Lisa builds a glowing tower
# of cubes with a silver robot named Bim in a sunset-lit room."

# Шаблон-fallback если scene_description не удалось извлечь — используется
# стиль по умолчанию (первый из палитры). Реальный промпт сборки картинки
# теперь делается в image.py через random_image_style(), а не через этот
# IMAGE_PROMPT_TEMPLATE — он сохранён для backward-compat если где-то ещё
# используется.
IMAGE_STYLE_PROMPT = IMAGE_STYLES[0][1]  # legacy alias на «детский мелковый»
IMAGE_PROMPT_TEMPLATE = (
    IMAGE_STYLE_PROMPT
    + " Scene to depict: {scene_description}"
)

# Дефолтная сцена если нет конкретной из сказки
FALLBACK_SCENE_TEMPLATE = (
    "A child playing with their friend {hero} in a magical cozy room. "
    "Theme: {theme_en}."
)

THEME_TO_EN = {
    "courage": "courage and bravery",
    "friendship": "friendship and companionship",
    "kindness": "kindness and helping others",
    "dream": "following a dream",
    "patience": "patience and waiting",
    "honesty": "honesty and truth",
}


# ─────────────────── Промпт для извлечения сцены из сказки ───────────────────
# Используется Gemini Flash-Lite один раз после генерации сказки.
EXTRACT_SCENE_PROMPT = """Прочитай эту сказку и сформулируй одно ПРЕДЛОЖЕНИЕ
на английском языке, описывающее самую визуально-сильную сцену из неё.

Сцена должна быть:
- конкретной (упомяни героя по имени, что он делает, где находится);
- визуальной (описание того, что видно глазами, не эмоций);
- сказочной (если есть волшебный элемент — обязательно упомяни);
- 15–25 слов английского.

Только одно предложение. Без вступлений, без комментариев. Только описание сцены.

Текст сказки:
---
{story_text}
---
"""

# EXTRACT_TEASER_PROMPT удалён вместе с концепцией обещаний — мы перешли на модель
# антологии (новый эпизод про знакомых героев), где никаких обещаний не выдаём,
# поэтому и извлекать нечего.
_DEPRECATED_EXTRACT_TEASER_PROMPT = """[не используется]

Предложение должно:
- начинаться с фразы вроде «А завтра...» / «На следующий день...» / «Завтра обещано...»;
- упомянуть конкретный объект или персонажа;
- звучать доброжелательно, без напряжения и страха;
- быть 10–20 слов.

Только одно предложение. Без вступлений.

Текст сказки:
---
{story_text}
---
"""
