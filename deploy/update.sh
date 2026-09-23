#!/usr/bin/env bash
# Обновление развёрнутого приложения: подтянуть код и перезапустить ТО, что
# действительно изменилось.
#
#   bash ~/anon/deploy/update.sh
#
# Зачем скрипт: next start отдаёт уже собранное из .next и не компилирует на
# лету, поэтому правки веб-интерфейса без `npm run build` просто не появятся.
# А правки Python, наоборот, сборки не требуют — она лишь тратит время. Скрипт
# смотрит, что приехало с git pull, и делает ровно необходимое.

set -euo pipefail

REPO="${REPO:-$HOME/anon}"
WEB="$REPO/anonymizer/web"

cd "$REPO"

# Развёрнутая копия — не рабочая: править файлы здесь незачем, а git pull
# --ff-only на изменённом дереве просто отказывается работать ("Your local
# changes would be overwritten by merge"). Сообщение git при этом не
# объясняет, что делать, и подталкивает закоммитить прямо на сервере — после
# чего ветки расходятся и --ff-only не проедет уже никогда. Поэтому
# проверяем заранее и говорим прямо.
#
# Самая частая причина здесь — `chmod +x` на скрипте: бит исполнения git
# отслеживает, и это такое же изменение файла, как правка текста. Запускать
# этот скрипт лучше как `bash deploy/update.sh` — тогда бит не нужен вовсе.
dirty=$(git status --porcelain)
if [ -n "$dirty" ]; then
    echo "[update.sh] в рабочем дереве есть локальные изменения:" >&2
    echo "$dirty" >&2
    echo "[update.sh] Коммитить их здесь НЕ НУЖНО — это копия для запуска." >&2
    echo "[update.sh] Отбросить изменения отслеживаемых файлов: git checkout -- ." >&2
    exit 1
fi

before=$(git rev-parse HEAD)
echo "== git pull =="
git pull --ff-only
after=$(git rev-parse HEAD)

if [ "$before" = "$after" ]; then
    echo "Изменений нет — перезапускать нечего."
    exit 0
fi

changed=$(git diff --name-only "$before" "$after")
echo "== изменено файлов: $(echo "$changed" | wc -l) =="

web_changed=$(echo "$changed" | grep -c '^anonymizer/web/' || true)
py_changed=$(echo "$changed"  | grep -c '^anonymizer/.*\.py$' || true)
# И package.json, и package-lock.json: версия зависимости может измениться
# только в lock-файле, и тогда сборка молча пойдёт со старыми пакетами.
pkg_changed=$(echo "$changed" | grep -cE '^anonymizer/web/package(-lock)?\.json$' || true)

if [ "$py_changed" -gt 0 ]; then
    echo "== бэкенд: $py_changed файлов, перезапуск =="
    systemctl --user restart anonymizer
else
    echo "-- бэкенд не менялся, пропускаю"
fi

if [ "$web_changed" -gt 0 ]; then
    cd "$WEB"
    # Зависимости доставляются ВСЕГДА, а не только когда изменился манифест.
    # npm install идемпотентен: если всё уже на месте, он отрабатывает за
    # секунду и ничего не меняет. Зато не остаётся случая, когда сборка падает
    # с "Module not found" из-за пакета, который приехал в package.json, но не
    # в node_modules (ровно так ломался ручной скрипт без npm install).
    if [ "$pkg_changed" -gt 0 ]; then
        echo "== зависимости изменились, npm install =="
    else
        echo "== проверка зависимостей (npm install) =="
    fi
    # --include=dev ОБЯЗАТЕЛЕН. Сервис запускается с NODE_ENV=production, а в
    # этом режиме npm не только не ставит devDependencies, но и ВЫЧИЩАЕТ уже
    # установленные. Между тем сборка без них невозможна: и typescript, и
    # @types/*, и CLI prisma лежат именно там. Без флага первый же `npm
    # install` выносит их из node_modules, и следом падает либо миграция,
    # либо `next build`.
    npm install --include=dev --no-audit --no-fund

    # Миграции — ВСЕГДА и ДО сборки. `migrate deploy` идемпотентен: если
    # применять нечего, он молча выходит. Без этого шага новая таблица просто
    # не появляется на боевой базе, а приложение падает на первом же
    # обращении к ней — и падает не при обновлении, а потом, у пользователя.
    #
    # Вызывается ЛОКАЛЬНЫЙ бинарник, а не `npx prisma`. npx, не найдя пакет в
    # node_modules, молча тянет ИЗ РЕЕСТРА свежайшую версию — а это сейчас
    # 8.0.0-rc.15 при проекте на 6.x. Release candidate чужой мажорной версии
    # на боевой биллинговой базе — не то, что должно происходить само собой
    # посреди скрипта обновления.
    echo "== миграции базы =="
    # CLI prisma НЕ читает .env.local — это формат Next.js, а CLI смотрит
    # только .env. Без этого миграция падает с P1012 "Environment variable
    # not found: DATABASE_URL", хотя приложение те же переменные видит
    # прекрасно. Подгружаем их сами (set -a экспортирует всё, что объявлено
    # в файле).
    if [ -f .env.local ]; then
        set -a
        # shellcheck disable=SC1091
        . ./.env.local
        set +a
    fi
    if [ -z "${DATABASE_URL:-}" ]; then
        echo "[update.sh] DATABASE_URL не задан — миграцию применять некуда." >&2
        echo "[update.sh] Проверьте $WEB/.env.local" >&2
        exit 1
    fi
    if [ ! -x node_modules/.bin/prisma ]; then
        echo "[update.sh] node_modules/.bin/prisma не найден: devDependencies не установлены." >&2
        echo "[update.sh] Выполните: cd $WEB && npm install --include=dev" >&2
        exit 1
    fi
    # SQLite не отдаёт базу под схемную миграцию, пока её держит работающее
    # приложение: schema engine падает с "database is locked". Поэтому UI на
    # время миграции останавливается. Простой — доли секунды: сервис
    # поднимается обратно СРАЗУ после миграции, а долгая сборка идёт уже при
    # работающем сайте, как и раньше.
    #
    # В промежутке между миграцией и сборкой работает СТАРЫЙ код на НОВОЙ
    # схеме. Это безопасно ровно до тех пор, пока миграции только добавляют
    # (таблицу, колонку, индекс): старый код просто не знает о новом и не
    # трогает его. Миграция, которая удаляет или переименовывает уже
    # используемое, так не проедет — её нужно разбивать на два выпуска
    # (сначала перестать использовать, выпустить, потом удалить).
    echo "-- останавливаю UI на время миграции"
    systemctl --user stop anonymizer-web
    if ! node_modules/.bin/prisma migrate deploy; then
        # Сборки ещё не было, .next прежний — достаточно поднять сервис, и
        # пользователь получит ровно ту версию, что работала до обновления.
        echo "[update.sh] миграция не применилась — поднимаю прежнюю версию" >&2
        systemctl --user start anonymizer-web
        exit 1
    fi
    systemctl --user start anonymizer-web

    echo "== сборка веб-интерфейса =="
    # При провале сборки set -e обрывает скрипт ДО перезапуска, так что
    # работающий сервис не трогается и сайт продолжает отвечать старой
    # версией. Но каталог .next при этом может остаться неконсистентным:
    # почините причину и запустите скрипт заново, не перезапуская сервис
    # вручную.
    npm run build
    echo "== перезапуск UI =="
    systemctl --user restart anonymizer-web
else
    echo "-- веб-интерфейс не менялся, сборка не нужна"
fi

echo "== проверка =="
sleep 5
printf 'UI:      '; curl -s -o /dev/null -w '%{http_code}\n' localhost:8010/
printf 'бэкенд:  '; curl -s -o /dev/null -w '%{http_code}\n' localhost:8011/health
systemctl --user is-active anonymizer anonymizer-web
