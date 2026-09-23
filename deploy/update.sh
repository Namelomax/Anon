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
    npm install --no-audit --no-fund
    # Миграции — ВСЕГДА и ДО сборки. `migrate deploy` идемпотентен: если
    # применять нечего, он молча выходит. Без этого шага новая таблица просто
    # не появляется на боевой базе, а приложение падает на первом же
    # обращении к ней — и падает не при обновлении, а потом, у пользователя.
    echo "== миграции базы =="
    npx prisma migrate deploy
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
