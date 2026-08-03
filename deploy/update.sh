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
pkg_changed=$(echo "$changed" | grep -c '^anonymizer/web/package.json$' || true)

if [ "$py_changed" -gt 0 ]; then
    echo "== бэкенд: $py_changed файлов, перезапуск =="
    systemctl --user restart anonymizer
else
    echo "-- бэкенд не менялся, пропускаю"
fi

if [ "$web_changed" -gt 0 ]; then
    cd "$WEB"
    if [ "$pkg_changed" -gt 0 ]; then
        echo "== package.json изменился, npm install =="
        npm install --no-audit --no-fund
    fi
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
