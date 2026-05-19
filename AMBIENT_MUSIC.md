# Фоновая музыка для аудио-сказок

Бот теперь миксует голос диктора с фоновой музыкой. Все треки берутся
случайно из папки `cache/ambient/` на сервере.

## Что качать (на твой Mac)

Открой по очереди ссылки ниже, скачивай конкретные треки (ищи кнопку
«Download MP3»). Все они **CC0 / Pixabay license** — можно использовать
коммерчески без указания авторства.

### 1. Soft piano / lullaby (основа, 5 треков)

- https://pixabay.com/music/lullabies-baby-lullaby-no-1-114550/
- https://pixabay.com/music/beautiful-plays-relaxing-piano-music-292600/
- https://pixabay.com/music/lullabies-relaxing-piano-music-for-baby-sleep-music-321661/
- https://pixabay.com/music/solo-piano-soft-piano-100-relaxing-calm-piano-music-for-sleep-stress-relief-159601/
- https://pixabay.com/music/solo-piano-night-piano-117202/

### 2. Music box / ambient (вариация, 3 трека)

- https://pixabay.com/music/childrens-music-music-box-167089/
- https://pixabay.com/music/ambient-sleep-piano-meditation-303443/
- https://pixabay.com/music/lullabies-twinkle-twinkle-little-star-music-box-118280/

### 3. Природа / дождь (для разнообразия, 2 трека)

- https://pixabay.com/music/ambient-soft-rain-ambient-111154/
- https://pixabay.com/music/nature-sounds-forest-sound-127411/

**Если какая-то ссылка не открывается** — просто зайди на
https://pixabay.com/music/ и поищи «sleep music», «lullaby», «soft piano».
Бери первые 10 что приглянутся (обращай внимание на длительность
2-5 минут — оптимально для loop'а).

## Куда класть

После скачивания на Mac у тебя 10 файлов вида `name.mp3` в Downloads.
Создай на сервере папку и закинь их туда:

```bash
# Создать папку на сервере
ssh skazka@77.110.119.32 'mkdir -p ~/skazka-bot/cache/ambient'

# Залить файлы (можешь сразу 10 файлов через wildcard,
# но проще скопировать в одну локальную папку и rsync'ом):
mkdir -p /tmp/ambient
# Перенеси сюда руками 10 mp3 из Downloads
cp ~/Downloads/*.mp3 /tmp/ambient/  # ну или вручную

# Залить на сервер
rsync -avz /tmp/ambient/ skazka@77.110.119.32:~/skazka-bot/cache/ambient/

# Проверить
ssh skazka@77.110.119.32 'ls -lh ~/skazka-bot/cache/ambient/'
```

Должно показать 10 .mp3 файлов.

## Что произойдёт после

Бот при каждой генерации сказки:

1. ElevenLabs делает голос (~30-40 сек чистого голоса диктора)
2. Бот берёт **случайный** трек из `cache/ambient/`
3. ffmpeg микширует: голос 0 dB + фон −18 dB, фон зацикливается до длины голоса
4. На старте: 2 сек fade-in, в конце: 3 сек fade-out
5. Отдаёт юзеру один итоговый mp3 — звучит как полноценная аудио-сказка из канала «Живые сказки»

## Что если треков нет?

Бот **не падает** — отдаёт чистую озвучку без фона (как раньше).
Это graceful fallback. Запустить с пустой папкой можно, потом просто
кинешь файлы и оно само заработает на следующей сказке.

## Кэш

Микс кэшируется по hash (text + voice). Если та же сказка генерится повторно — берётся из кэша мгновенно. **При изменении набора треков** старые сказки в архиве **останутся со старым миксом** (это норм — у них уже свой mp3). Новые сказки получат новый случайный фон.

## Если громкость фона не нравится

В `src/services/tts.py` есть константа `BACKGROUND_DB = -18`. Поменяй на:

- `-12` — фон громче (для расслабляющих сказок)
- `-22` — фон тише (для чистоты голоса)
- `-18` — наш дефолт, найден экспериментально

После изменения деплоишь как обычно, кэш чистить не надо — старые сказки останутся с прежним миксом.
